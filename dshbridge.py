"""把 DSH 当"大脑"接进机器人的桥。

协议是 ACP（Agent Client Protocol），JSON-RPC over stdio，DSH 侧用 `dsh --profile qq`
起服务端。每个 QQ 群对应一个常驻 session，上下文留在 DSH 里，重启机器人不丢（session 持久化）。

设计取舍：
- 只用一个子进程，所有会话共用一条 stdio 连接（ACP 支持多会话）。
- 每群串行：同一 session 同时只允许一个 prompt 在飞，用 per-group 锁。
- 拿不到回复就抛异常，让上层回退到本地模型，别让群友干等。
"""

import asyncio
import base64
import json
import logging
import os
from collections import defaultdict

log = logging.getLogger("dshbridge")


class DshBridge:
    def __init__(self, exe: str, profile: str, cwd: str, api_key: str = ""):
        self.exe = os.path.expanduser(exe)
        self.profile = profile
        self.cwd = cwd
        self.api_key = api_key
        self.proc: asyncio.subprocess.Process | None = None
        self.sessions: dict[str, str] = {}
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.pending: dict[int, asyncio.Future] = {}
        self.chunks: dict[str, list[str]] = defaultdict(list)
        self.req_id = 0
        self.ready = asyncio.Event()
        self._reader: asyncio.Task | None = None

    # ---------- 生命周期 ----------

    async def start(self):
        env = dict(os.environ)
        if self.api_key:
            env["DEEPSEEK_API_KEY"] = self.api_key  # 给 web_search / web_fetch 用
        self.proc = await asyncio.create_subprocess_exec(
            self.exe, "--profile", self.profile,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )
        self._reader = asyncio.create_task(self._read_loop())
        asyncio.create_task(self._drain_stderr())
        await self._request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
        }, timeout=60)
        self.ready.set()
        log.info("DSH 桥就绪：profile=%s cwd=%s", self.profile, self.cwd)

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()

    async def ensure_alive(self):
        if self.proc is None or self.proc.returncode is not None:
            log.warning("DSH 子进程不在，重启")
            self.sessions.clear()
            await self.start()

    # ---------- 收发 ----------

    async def _drain_stderr(self):
        # dsh 会把个别日志写到 stdout（见 README 的坑），stderr 仅记录
        while self.proc and self.proc.stderr:
            line = await self.proc.stderr.readline()
            if not line:
                break
            log.debug("dsh stderr: %s", line.decode("utf-8", "replace").rstrip())

    async def _read_loop(self):
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue  # dsh 偶尔往 stdout 打日志行，忽略
            if "method" in msg:
                if "id" in msg:
                    await self._on_server_request(msg)
                else:
                    self._on_notification(msg)
            elif msg.get("id") in self.pending:
                fut = self.pending.pop(msg["id"])
                if not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(msg["error"].get("message", "acp error")))
                    else:
                        fut.set_result(msg.get("result"))

    async def _on_server_request(self, msg):
        # 机器人侧一律拒绝敏感请求（我们本来也没给工具）
        log.info("DSH 反向请求 %s，拒绝", msg.get("method"))
        await self._write({
            "jsonrpc": "2.0", "id": msg["id"],
            "result": {"outcome": {"outcome": "denied"}},
        })

    def _on_notification(self, msg):
        if msg.get("method") != "session/update":
            return
        sid = msg["params"].get("sessionId")
        upd = msg["params"].get("update", {})
        if upd.get("sessionUpdate") == "agent_message_chunk":
            content = upd.get("content", {})
            if content.get("type") == "text":
                self.chunks[sid].append(content["text"])

    async def _write(self, obj):
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("DSH 未启动")
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self.proc.stdin.drain()

    async def _request(self, method, params, timeout=180):
        self.req_id += 1
        mid = self.req_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[mid] = fut
        await self._write({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(mid, None)

    # ---------- 对外 ----------

    async def prompt(self, group: str, text: str, timeout: float = 180,
                     resume_sid: str | None = None,
                     images: list[tuple[str, bytes]] | None = None) -> str:
        await self.ensure_alive()
        async with self.locks[group]:
            sid = self.sessions.get(group)
            if not sid and resume_sid:
                try:
                    await self._request("session/resume", {
                        "sessionId": resume_sid, "cwd": self.cwd, "mcpServers": [],
                    }, timeout=60)
                    sid = resume_sid
                    self.sessions[group] = sid
                    log.info("恢复 DSH 会话 group=%s sid=%s", group[:8], sid[:8])
                except Exception:
                    log.warning("恢复会话失败，改为新建 group=%s sid=%s",
                                group[:8], resume_sid[:8], exc_info=True)
                    sid = None
            if not sid:
                result = await self._request("session/new", {"cwd": self.cwd, "mcpServers": []}, timeout=90)
                sid = result["sessionId"]
                self.sessions[group] = sid
                log.info("新建 DSH 会话 group=%s sid=%s", group[:8], sid[:8])

            # 图走 ACP 的 inline image block：DSH 有自己的 attachments 管线
            # （校验 → 归一化 → 内容寻址存储 → 进模型请求），图片本身进模型比一句
            # 文字描述靠谱得多。注意 payload 别太大，否则 dsh 侧 JSON 解析会爆栈。
            blocks = [{"type": "text", "text": text}]
            for mime, data in images or []:
                blocks.append({
                    "type": "image",
                    "mimeType": mime,
                    "data": base64.b64encode(data).decode(),
                })

            self.chunks[sid] = []
            try:
                await self._request("session/prompt", {
                    "sessionId": sid,
                    "prompt": blocks,
                }, timeout=timeout)
            except Exception:
                if len(blocks) == 1:
                    raise
                # 图被拒（路由不支持视觉 / 太大 / 格式不认）就退回纯文本，别把整条回复丢了
                log.warning("带图 prompt 失败，退回纯文本 group=%s", group[:8], exc_info=True)
                self.chunks[sid] = []
                await self._request("session/prompt", {
                    "sessionId": sid,
                    "prompt": [{"type": "text", "text": text}],
                }, timeout=timeout)
            return "".join(self.chunks.pop(sid, [])).strip()

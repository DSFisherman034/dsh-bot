"""LLM 层：把群聊上下文拼成提示词、调 OpenAI 兼容接口、清洗输出。

所有调用都是 async-httpx，不阻塞事件循环（旧 simple-bot 是同步 openai 客户端，会卡住整个服务）。
"""

import asyncio
import re
from datetime import datetime
from pathlib import Path

import httpx

TASKS = {
    "addressed": "{who} 刚 @ 了你，最后一条就是。回他，别装死。",
    "ambient": "没人叫你。你可以顺着话题插一句，也可以不说话。",
    "heartbeat": "群里安静了一会儿。你可以自己起个话头、吐槽一句，也可以什么都不说。",
}


class Brain:
    def __init__(self, cfg, persona_path):
        self.base_url = str(cfg["base_url"]).rstrip("/")
        self.api_key = str(cfg["api_key"])
        self.model = str(cfg["model"])
        self.temperature = float(cfg.get("temperature", 0.9))
        self.max_tokens = int(cfg.get("max_tokens", 260))
        self.bot_name = str(cfg.get("name", "DSH"))
        self.persona_path = Path(persona_path)
        self._persona = (0.0, "")
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))

    async def aclose(self):
        await self.client.aclose()

    # ---------- 人设（改了立即生效，不用重启） ----------

    def persona(self) -> str:
        try:
            mtime = self.persona_path.stat().st_mtime
            if mtime != self._persona[0]:
                self._persona = (mtime, self.persona_path.read_text("utf-8"))
        except OSError:
            pass
        return (self._persona[1] or "你是群友，说话短、自然。").replace("{name}", self.bot_name)

    # ---------- 上下文 ----------

    def transcript(self, rows, limit=26) -> str:
        lines = []
        for author, content, ts, is_bot in rows[-limit:]:
            clock = datetime.fromtimestamp(ts).strftime("%H:%M")
            who = self.bot_name if is_bot else (author or "某人")
            text = re.sub(r"\s+", " ", content or "").strip()[:160]
            lines.append(f"[{clock}] {who}: {text}")
        return "\n".join(lines) if lines else "（还没有聊天记录）"

    # ---------- 调用 ----------

    async def complete(self, system: str, user: str, temperature=None, max_tokens=None,
                       enable_thinking=None) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
        }
        if enable_thinking is not None:
            payload["enable_thinking"] = enable_thinking
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last = None
        for _ in range(2):
            try:
                resp = await self.client.post(
                    f"{self.base_url}/chat/completions", headers=headers, json=payload
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"] or ""
            except Exception as exc:  # 网络抖动重试一次
                last = exc
                await asyncio.sleep(0.8)
        raise RuntimeError(f"LLM 调用失败: {last!r}")

    # ---------- 生成一句话 ----------

    async def respond(self, rows, task: str = "ambient", who: str = "") -> str | None:
        system = self.persona()
        rule = TASKS.get(task, TASKS["ambient"]).format(who=who)
        now = datetime.now().strftime("%H:%M")
        mine = [c for a, c, ts, b in rows if b][-3:]
        mine_hint = ""
        if mine:
            said = " / ".join(x[:40] for x in mine)
            mine_hint = f"\n你最近已经说过：{said}\n别重复自己说过的话，也别换个说法再说一遍。\n"
        user = (
            f"现在是 {now}。\n\n"
            f"群里的最近聊天：\n{self.transcript(rows)}\n{mine_hint}\n"
            f"{rule}\n\n"
            "只输出要发到群里的那一句话本身：不要加引号、不要加名字前缀、不要 Markdown、"
            "不要解释你在做什么。决定不说话，就只输出 SKIP。"
        )
        raw = await self.complete(system, user)
        return self.clean(raw)

    def clean(self, raw: str) -> str | None:
        text = (raw or "").strip()
        if not text:
            return None
        first = text.splitlines()[0].strip().strip("。.!！")
        if first.upper().startswith("SKIP") or not first:
            return None

        # 模型偶尔会输出多行，合并成一句
        text = " ".join(line.strip() for line in text.splitlines() if line.strip())
        text = re.sub(rf"^(?:{re.escape(self.bot_name)}|机器人|我)\s*[:：]\s*", "", text)
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = re.sub(r"`([^`]*)`", r"\1", text)
        text = text.strip().strip('"').strip("“”").strip()
        # 被 max_tokens 截断时，退回到最后一个句末标点，别留半句话
        if text and text[-1] not in "。！？!?…~～.）”\"'":
            cut = max((text.rfind(c) for c in "。！？!?…"), default=-1)
            if cut >= 8:
                text = text[: cut + 1]
        return text or None

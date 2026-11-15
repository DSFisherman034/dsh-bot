"""抄一份 webhook 原始包：botsns 模板里丢掉的字段从这里补。

botsns 的 `_GroupMessageTemplate` 只把 quote 的 `msg_idx` / `content` 传出来。
被引用的**图片**消息 `content` 是空串，图在 `msg_elements[0].attachments` 里，
dsh-bot 拿不到——渲染出来就只剩一句「你强开了吗」，模型会以为在问自己
（2026-11-14 08:36 实例：深海渔民引用 Silverbird 的截图问「你强开了吗」，
鱼回「我哪来的号」）。

这里用纯 ASGI 中间件旁路复制请求体（只是多读一份，不改动下游怎么读），
解析后按 `d["id"]` 暂存；`main.quote_prefix` 用 `message.id` 取回原始包，
看被引用的东西到底是什么。取完即删，最多留最近 64 条。
"""

import json
import logging
from collections import OrderedDict

log = logging.getLogger("dshbot.rawhook")

_KEEP = 64
RAWS: "OrderedDict[str, dict]" = OrderedDict()


def take(message_id):
    """取出某条消息的原始包（取完即删）。"""
    if not message_id:
        return None
    return RAWS.pop(message_id, None)


def quoted_attachments(raw, quote_id) -> list[dict]:
    """原始包 → 被引用那条消息的附件列表（图片/文件）。"""
    if not raw or not quote_id:
        return []
    out = []
    for el in raw.get("msg_elements") or []:
        if not isinstance(el, dict) or el.get("msg_idx") != quote_id:
            continue
        for att in el.get("attachments") or []:
            if isinstance(att, dict) and att.get("url"):
                out.append(att)
    return out


def _remember(body: bytes) -> None:
    if not body:
        return
    try:
        payload = json.loads(body)
    except Exception:
        return
    d = payload.get("d") if isinstance(payload, dict) else None
    if not isinstance(d, dict):
        return
    mid = d.get("id")
    if not isinstance(mid, str) or not mid:
        return
    RAWS[mid] = d
    RAWS.move_to_end(mid)
    while len(RAWS) > _KEEP:
        RAWS.popitem(last=False)


class Capture:
    """纯 ASGI 中间件：把 POST / 的 body 抄一份，然后原样喂给下游。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope.get("type") != "http"
                or scope.get("method") != "POST"
                or scope.get("path") != "/"):
            return await self.app(scope, receive, send)

        chunks: list[bytes] = []

        async def tee():
            message = await receive()
            if message.get("type") == "http.request":
                chunks.append(message.get("body") or b"")
            return message

        try:
            return await self.app(scope, tee, send)
        finally:
            try:
                _remember(b"".join(chunks))
            except Exception:          # 抄一份失败绝不能影响 webhook 本身
                log.exception("原始包解析失败")

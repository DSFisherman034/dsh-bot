"""把 QQ 的原始消息体渲染成模型能读的文本。

为什么不用 botsns 的 tools：
- `replace_mention` 有 bug（`names[id]` 用了内置函数当 key，必抛 KeyError）；
- `replace_bilibili_ark` 只要内容里有 `jump_url:` 就当成 bilibili 卡片，别的 ark 会被吞掉。

这里的策略：
- @ 渲染成 `@名字(openid)`，保留 id，模型想 @ 谁时可以直接用；
- 引用渲染成 `[引用：…]` 前缀（botsns 已解析成 `message.quote`，120 字截断、折成一行）；
- 表情/图片渲染成可读占位；
- ark 只有确认是 bilibili 才去解析，失败或非 bilibili 一律留一个 `[卡片]` 占位，绝不吞内容。
"""

import json
import re
import base64

import httpx

MENTION_RE = re.compile(r"<@([A-Za-z0-9]{32})>")
FACE_RE = re.compile(r'<faceType=1,faceId="(\d+)",ext="([^"]*)">')
IMAGE_MARK = '<faceType=6,faceId="0",ext="eyJ0ZXh0IjoiIn0=">'
ARK_RE = re.compile(r"jump_url:\s*(\S+)")
BILI_RE = re.compile(r"(bilibili\.com|b23\.tv)")
BV_RE = re.compile(r"https://www\.bilibili\.com/video/([^?]+)\?")
QUOTE_CLIP = 120


async def render_incoming(message) -> str:
    """把一条入站消息渲染成给模型看的文本。"""
    content = message.content or ""

    names = {m.openid: m.username for m in (getattr(message, "mentions", None) or [])}
    content = MENTION_RE.sub(lambda m: f"@{names.get(m.group(1), '某人')}({m.group(1)})", content)

    content = _faces(content)
    content = await _ark(content)

    # 引用（QQ 的「回复」/quote）：botsns 已经把 msg_elements[0] 解析成
    # message.quote.content，但以前只拿它判断"是不是在叫自己"，没进上下文。
    # 结果群里引用着说一句「，」「？」时，模型只看到标点，回「悟不出来」。
    content = content.strip()
    quote = _render_quote(message, names)
    if quote:
        content = f"{quote} {content}".strip()
    return content


def _render_quote(message, names: dict[str, str]) -> str:
    """把被引用的那条消息压成一行 `[引用：…]`，没有就返回空串。"""
    q = getattr(message, "quote", None)
    if not q:
        return ""
    text = (getattr(q, "content", "") or "").strip()
    if not text:
        return ""
    text = MENTION_RE.sub(lambda m: f"@{names.get(m.group(1), '某人')}", text)
    text = " ".join(text.split())  # 折成一行，别让引用里的换行拆散转录
    if len(text) > QUOTE_CLIP:
        text = text[:QUOTE_CLIP] + "…"
    return f"[引用：{text}]"


def _faces(content: str) -> str:
    def face(m):
        try:
            name = json.loads(base64.b64decode(m.group(2)).decode("utf-8"))["text"]
            return f"[表情:{name}]"
        except Exception:
            return "[表情]"

    content = FACE_RE.sub(face, content)
    return content.replace(IMAGE_MARK, "[图片]")


async def _ark(content: str) -> str:
    m = ARK_RE.search(content)
    if not m:
        return content

    url = m.group(1)
    head = content[: m.start()].strip()  # jump_url 之前的正文，必须保住

    if not BILI_RE.search(url):
        # 其它类型的 ark：不猜、不解析，只留占位
        return " ".join(x for x in (head, "[卡片]") if x)

    try:
        title, up, dur, view = await _bilibili(url)
        card = f"[bilibili卡片] {title} | up:{up} | {dur}秒 | 播放{view}"
    except Exception:
        card = "[bilibili卡片（解析失败）]"
    return " ".join(x for x in (head, card) if x)


async def _bilibili(url: str):
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(headers=headers, timeout=8, follow_redirects=True) as client:
        resp = await client.get(url)
        m = BV_RE.search(str(resp.url)) or BV_RE.search(resp.text)
        if not m:
            raise ValueError("拿不到 bvid")
        bvid = m.group(1)
        detail = (await client.get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")).json()
        data = detail["data"]
        return data["title"], data["owner"]["name"], data["duration"], data["stat"]["view"]

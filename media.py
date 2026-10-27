"""图片：下载、去重、归一化、描述、发送。

为什么要自己写：
- botsns 的 `upload_file()` 是个空函数（pass），所以它发不了图；
- 入站图片需要"看懂"：一路是本地多模态模型描述成文字（给转录与本地模型兜底），
  一路是把图本身作为 ACP image block 交给 DSH（那边真的能看）。

归一化策略参考 DSH 自己的 attachments（`@deepseek-ai/dsh-attachment`）：
- 媒体类型按 **magic bytes** 嗅探，不信 Content-Type 头；
- 进模型前统一成 8-bit sRGB、长边 ≤ `image_max_edge`、字节 ≤ 2MB——
  实测 ACP 单行 JSON 里塞 8.7MB base64 会让 dsh 直接报
  "Maximum call stack size exceeded"；
- 描述按原始 sha256 缓存，同一张图只描述一次。

设计：
- 每张图按 sha256 去重，同一张图只下载、只描述一次，描述存 SQLite 复用。
- 描述截断到 MAX_DESC 字，进上下文时用 `[图:描述|h=hash8]` 的短格式，省 token。
- 图片文件落盘在 data.db 同级的 images/ 目录，文件名就是哈希。
"""

import base64
import hashlib
import io
import shutil
import json
import logging
import math
from array import array
from pathlib import Path

import httpx

log = logging.getLogger("media")

MAX_DESC = 60
# 落盘保留原图，但超过这个大小就不描述了（QQ 侧也很少给这么大的）
MAX_ORIGINAL_BYTES = 20 * 1024 * 1024
# 送给模型/ACP 的上限：ACP 单行 JSON 太大 = dsh 直接崩，见模块注释
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
DEFAULT_EDGE = 1280
DEFAULT_QUALITY = 82

MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sniff(data: bytes) -> str | None:
    """按文件签名判断图片类型（和 DSH read_image 一样，不信扩展名/头）。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _dims(data: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


def _to_srgb(im):
    """带透明通道的图先去白底，避免转 JPEG 後透明区变黑。"""
    from PIL import Image

    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def prepare(path: Path, edge: int = DEFAULT_EDGE,
            max_bytes: int = MAX_PAYLOAD_BYTES) -> tuple[str, bytes] | None:
    """把图变成可以直接塞给模型的一小块：返回 (mime, bytes)。

    已经够小就原样返回；否则等比缩到长边 edge 并转 JPEG。
    """
    try:
        raw = path.read_bytes()
    except Exception:
        log.warning("读图失败：%s", path)
        return None

    mime = sniff(raw) or "image/jpeg"
    size = _dims(raw)
    if len(raw) <= max_bytes and size and max(size) <= edge:
        return mime, raw

    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            im = _to_srgb(im)
            im.thumbnail((edge, edge), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=DEFAULT_QUALITY, optimize=True)
            return "image/jpeg", buf.getvalue()
    except Exception as exc:
        log.warning("图片归一化失败（原样使用）：%r", exc)
        return (mime, raw) if len(raw) <= max_bytes else None


async def download(url: str, images_dir: Path, timeout: float = 15.0) -> tuple[str, Path] | None:
    """下载一张图，返回 (hash, 本地路径)。"""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
    except Exception as exc:
        log.info("图片下载失败：%r", exc)
        return None

    if not data:
        return None
    if len(data) > MAX_ORIGINAL_BYTES:
        log.warning("图片太大（%s 字节），跳过", len(data))
        return None

    h = digest(data)
    mime = sniff(data)
    if mime is None:
        # 认不出来就别按图片处理，让上层留占位
        log.info("附件不是已知图片格式（%s 字节），跳过", len(data))
        return None
    suffix = MIME_EXT[mime]

    images_dir.mkdir(parents=True, exist_ok=True)
    path = images_dir / f"{h[:16]}{suffix}"
    if not path.exists():
        path.write_bytes(data)
    return h, path


async def describe(path: Path, base_url: str, api_key: str, model: str,
                   limit: int = MAX_DESC, edge: int = DEFAULT_EDGE) -> str:
    """用多模态模型描述一张图，返回一句短描述。失败返回空串。

    先 prepare 成小块再上传：原图直传实测 5MB 图能直接写到写超时，
    而且慢。关掉 enable_thinking 后同图从 ~12s 降到 ~1s。
    """
    got = prepare(path, edge=edge)
    if not got:
        log.warning("图片描述跳过：准备失败 %s", path)
        return ""
    mime, data = got
    b64 = base64.b64encode(data).decode()

    url = f"{base_url.rstrip('/')}/chat/completions"
    base_payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": f"用不超过{limit}个字描述这张图。只输出描述本身，不要解释。"},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        "max_tokens": 160,
    }

    # 阿里云/Qwen 兼容端点认 enable_thinking；别的端点不认就退回去重试一次
    for extra in ({"enable_thinking": False}, {}):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={**base_payload, **extra},
                )
                if resp.status_code >= 400:
                    log.warning("图片描述 HTTP %s：%s", resp.status_code, resp.text[:200])
                    raise httpx.HTTPStatusError("bad status", request=resp.request, response=resp)
                msg = resp.json()["choices"][0]["message"]
                text = msg.get("content") or msg.get("reasoning_content") or ""
            return " ".join(text.split())[:limit]
        except Exception as exc:
            log.warning("图片描述失败（%s）：%r", "enable_thinking" if extra else "plain", exc)
    return ""


# ---------------- 图片检索（按描述找图） ----------------

def pack_vec(vec) -> bytes:
    return array("f", vec).tobytes()


def unpack_vec(blob: bytes):
    a = array("f")
    a.frombytes(blob)
    return a


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def local_score(query: str, text: str) -> float:
    """没有 embedding 时的兜底：查询的字符二元组有多大比例出现在描述里。"""
    q = "".join(query.split()).lower()
    t = "".join(text.split()).lower()
    if not q or not t:
        return 0.0
    grams = {q[i:i + 2] for i in range(max(1, len(q) - 1))}
    return sum(1 for g in grams if g in t) / len(grams)


async def embed_texts(texts: list[str], base_url: str, api_key: str, model: str,
                      timeout: float = 15.0) -> list[list[float]] | None:
    """OpenAI 兼容 /embeddings。失败返回 None（调用方退回字面匹配）。"""
    if not texts:
        return []
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "input": texts, "encoding_format": "float"},
            )
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
            return [d["embedding"] for d in data]
    except Exception as exc:
        log.warning("embedding 调用失败：%r", exc)
        return None


def publish(path: Path, public_dir: Path) -> bool:
    """把图复制到 nginx 的静态目录，让它有个公网 URL。"""
    try:
        public_dir.mkdir(parents=True, exist_ok=True)
        dst = public_dir / path.name
        if dst.resolve() == path.resolve():
            return True  # 已经在静态目录里（正常情况：图片就落在 nginx root 下）
        if not dst.exists():
            shutil.copy2(path, dst)
        return True
    except Exception:
        log.exception("复制到静态目录失败")
        return False


async def upload_and_send(client, group: str, path: Path, msg_id: str | None = None,
                          base_url: str = "", public_dir: Path | None = None) -> bool:
    """把图发到群里：QQ 的文件接口只收 URL（file_type=1），所以要有个公网地址。"""
    public = client.group.public
    headers = {"Authorization": f"QQBot {public.access_token}"}
    try:
        if True:
            if public_dir is not None:
                publish(path, public_dir)
            image_url = f"{base_url.rstrip('/')}/{path.name}"
            log.info("发图 URL: %r (base=%r)", image_url, base_url)
            async with httpx.AsyncClient(timeout=60.0) as http:
                up = await http.post(
                    f"{public.qqapi.base_url}/v2/groups/{group}/files",
                    headers=headers,
                    json={"file_type": 1, "url": image_url, "srv_send_msg": False},
                )
                if up.status_code != 200:
                    log.warning("上传失败 %s: %s", up.status_code, up.text[:300])
                    return False
                info = up.json()
                file_info = info.get("file_info")
                if not file_info:
                    log.warning("上传没拿到 file_info：%s", json.dumps(info, ensure_ascii=False)[:200])
                    return False

                # 被动回复优先用当前消息的 id，否则走可用 id 队列
                msg_seq = None
                if msg_id is not None:
                    msg_seq = public.get_seq_of_an_id(msg_id, group)
                else:
                    msg_id, msg_seq = public.get_an_available_id(group_openid=group)

                payload = {"msg_type": 7, "media": {"file_info": file_info}, "content": ""}
                if msg_id is not None and msg_seq is not None:
                    payload["msg_id"] = msg_id
                    payload["msg_seq"] = msg_seq

                send = await http.post(
                    f"{public.qqapi.base_url}/v2/groups/{group}/messages",
                    headers=headers, json=payload,
                )
                body = send.json()
                if body.get("err_code"):
                    log.warning("发图失败：%s", json.dumps(body, ensure_ascii=False)[:200])
                    return False
                return True
    except Exception:
        log.exception("发图出错")
        return False

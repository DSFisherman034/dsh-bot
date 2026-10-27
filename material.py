"""暖场用的"材料"：现抓，不预置池子。

三个来源，随机挑一个：
1. V2EX 热门（中文技术圈，跟群里的话题最贴）
2. Hacker News 首页（英文，能聊"外面在讨论什么"）
3. 本群历史上的一段话（几小时前的，人能接得住）

全部失败就返回空串，心跳照常问，只是没有材料。
"""

import logging
import random
import re
import time
import xml.etree.ElementTree as ET

import httpx

log = logging.getLogger("material")

V2EX = "https://www.v2ex.com/api/topics/hot.json"
HNRSS = "https://hnrss.org/frontpage"
TIMEOUT = 7.0

# 会踩 QQ 内容风控的题材，直接不抓：宁可没材料，也不要发出去被拒
BLOCK_NODES = {"政治", "情感问题", "律师", "疾病", "投资"}
BLOCK_WORDS = (
    "政治", "选举", "战争", "游行", "抗议", "维权", "色情", "自杀", "死亡",
    "election", "war in", "porn", "coup", "protest",
)


def _allowed(text: str) -> bool:
    low = text.lower()
    return not any(w in low for w in BLOCK_WORDS)

# 每个群最近用过的材料，避免连着重复
_last_used: dict[str, list[str]] = {}

# 抓取结果缓存：V2EX/HN 的榜单十分钟内不变，没必要每个群都重新拉
_cache: dict[str, tuple[float, list[str]]] = {}
CACHE_TTL = 600


def _cached(key: str):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    return None


def _store(key: str, value: list[str]):
    _cache[key] = (time.time(), value)
    return value


def _pick(group: str, candidates: list[str], keep: int = 12) -> str | None:
    used = _last_used.setdefault(group, [])
    fresh = [c for c in candidates if c not in used]
    if not fresh:
        fresh = candidates
    pick = random.choice(fresh) if fresh else None
    if pick:
        used.append(pick)
        del used[:-keep]
    return pick


async def _v2ex(client) -> list[str]:
    if (hit := _cached("v2ex")) is not None:
        return hit
    data = (await client.get(V2EX)).json()
    out = []
    for t in data[:20]:
        title = (t.get("title") or "").strip()
        node = ((t.get("node") or {}).get("title") or "").strip()
        if not title or node in BLOCK_NODES or not _allowed(title):
            continue
        out.append(f"V2EX 热门（{node or '讨论'}）：{title}")
    return _store("v2ex", out)


async def _hn(client) -> list[str]:
    if (hit := _cached("hn")) is not None:
        return hit
    text = (await client.get(HNRSS)).text
    root = ET.fromstring(text)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if title and _allowed(title):
            out.append(f"Hacker News 首页：{title}")
        if len(out) >= 12:
            break
    return _store("hn", out)


def _history(group: str, memory) -> list[str]:
    now = time.time()
    rows = memory.recent_messages(group, minutes=48 * 60, limit=120)
    old = [(a, c) for (a, c, ts, is_bot) in rows
           if not is_bot and now - ts > 3 * 3600 and len(c or "") > 8]
    out = []
    for author, content in old[-40:]:
        snippet = re.sub(r"\s+", " ", content).strip()[:60]
        out.append(f"几小时前群里 {author} 说过：{snippet}")
    return out


async def collect(group: str, memory) -> str:
    """返回一段材料文本；拿不到就返回空串。按顺序尝试，一个失败就换下一个。"""
    order = ["v2ex", "hn", "history"]
    random.shuffle(order)
    if random.random() < 0.5:      # 稍微偏向中文技术圈
        order.sort(key=lambda x: x != "v2ex")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True,
                                     headers={"User-Agent": "dsh-bot/1.0"}) as client:
            for source in order:
                candidates: list[str] = []
                try:
                    if source == "v2ex":
                        candidates = await _v2ex(client)
                    elif source == "hn":
                        candidates = await _hn(client)
                    else:
                        candidates = _history(group, memory)
                except Exception as exc:
                    log.info("%s 抓取失败：%r", source, exc)
                    continue
                pick = _pick(group, candidates)
                if pick:
                    return pick
            return ""
    except Exception:
        log.exception("材料收集失败")
        return ""

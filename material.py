"""暖场用的"由头"：只从群里长出来的东西里找，不再抓外部新闻。

四个来源，按相关性加权：
1. revisit  群史回访——1-7 天前没下文的问句/待办/承诺，问一句后续
2. history  挖坟——几小时前某人的一句有意思的话
3. image    图库配文——挑一张最近没发过的图，配一句当下的吐槽
4. local    本机状态——负载/内存/磁盘/开机时长（群里有服务器的梗）

拿不到合格由头就返回空串：宁可不说，也不要播报新闻。
"""

import logging
import os
import random
import re
import shutil
import time

log = logging.getLogger("material")

# 会踩 QQ 内容风控或挖到不愉快旧事的题材，直接不碰
BLOCK_WORDS = (
    "政治", "选举", "战争", "游行", "抗议", "维权", "色情", "自杀", "死亡",
    "傻逼", "滚", "骂", "吵架", "分手", "赌", "难过", "烦死",
)

# "没下文"的信号：问句、承诺、待办
OPEN_ENDED = (
    "?", "？", "吗", "回头", "明天", "下次", "改天", "试试", "要不要",
    "打算", "准备", "等我", "还没", "什么时候",
)

# 每个群最近用过的由头，避免连着重复
_last_used: dict[str, list[str]] = {}


def _allowed(text: str) -> bool:
    low = (text or "").lower()
    return not any(w in low for w in BLOCK_WORDS)


def _pick(group: str, candidates: list[str], keep: int = 10) -> str | None:
    used = _last_used.setdefault(group, [])
    fresh = [c for c in candidates if c not in used]
    if not fresh:
        fresh = candidates
    pick = random.choice(fresh) if fresh else None
    if pick:
        used.append(pick)
        del used[:-keep]
    return pick


# ---------------- 四个来源 ----------------

def _revisit(group: str, memory) -> list[str]:
    """1-7 天前没下文的话：问句、待办、承诺。"""
    now = time.time()
    try:
        rows = memory.recent_messages(group, minutes=7 * 24 * 60, limit=200)
    except Exception:
        return []
    cand = []
    for author, content, ts, is_bot in rows:
        if is_bot:
            continue
        age = now - ts
        if age < 24 * 3600 or age > 7 * 24 * 3600:
            continue
        text = re.sub(r"\s+", " ", content or "").strip()
        if len(text) < 8 or len(text) > 120:
            continue
        if not _allowed(text):
            continue
        if not any(k in text for k in OPEN_ENDED):
            continue
        cand.append((author or "某人", text, ts))
    if not cand:
        return []
    out = []
    for author, text, ts in cand[-20:]:
        days = int((now - ts) // 86400)
        when = "昨天" if days <= 1 else f"{days} 天前"
        out.append(f"{when}群里 {author} 说过：{text[:60]}——后来没下文了，可以问一句后续")
    return out


def _history(group: str, memory) -> list[str]:
    """几小时前的一句有意思的话，用来接梗。"""
    now = time.time()
    try:
        rows = memory.recent_messages(group, minutes=48 * 60, limit=120)
    except Exception:
        return []
    out = []
    for author, content, ts, is_bot in rows:
        if is_bot:
            continue
        age = now - ts
        if age < 3 * 3600 or age > 24 * 3600:
            continue
        text = re.sub(r"\s+", " ", content or "").strip()
        if len(text) < 10 or not _allowed(text):
            continue
        out.append(f"几小时前群里 {author} 说过：{text[:60]}")
    return out[-40:]


def _image(group: str, memory) -> list[str]:
    """图库配文：挑一张图，配一句和当下有关的吐槽。"""
    try:
        rows = memory.image_library(limit=80)
    except Exception:
        return []
    out = []
    for _h, desc, scope in rows:
        desc = (desc or "").strip()
        if not desc:
            continue
        out.append(
            f"图库里有这张图：{desc[:50]}（适用范围：{(scope or '通用')[:30]}）。"
            f"想发就用 [[img:{desc[:24]}]]，配一句当下的话"
        )
    return out


def _local() -> list[str]:
    """本机状态：负载、内存、磁盘、开机时长。"""
    try:
        load = os.getloadavg()[0]
        mem_total = mem_avail = 0.0
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = float(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_avail = float(line.split()[1])
        disk = shutil.disk_usage("/")
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        mem_pct = (1 - mem_avail / mem_total) * 100 if mem_total else 0
        disk_pct = disk.used / disk.total * 100
        days = uptime / 86400
        return [
            f"本机：负载 {load:.1f}，内存 {mem_pct:.0f}%，磁盘 {disk_pct:.0f}%，"
            f"已经连续跑了 {days:.1f} 天"
        ]
    except Exception:
        return []


# 权重：群里长出来的优先，本机状态只是偶尔
SOURCES = (
    ("revisit", _revisit, 0.50),
    ("history", _history, 0.25),
    ("image", _image, 0.15),
    ("local", _local, 0.10),
)


def _weighted_order():
    items = [(name, fn, w) for name, fn, w in SOURCES]
    out = []
    while items:
        total = sum(w for _n, _f, w in items)
        r = random.uniform(0, total)
        acc = 0.0
        for i, (name, fn, w) in enumerate(items):
            acc += w
            if r <= acc:
                out.append((name, fn))
                items.pop(i)
                break
    return out


async def collect(group: str, memory) -> str:
    """返回一条由头文本；没有合格材料返回空串。"""
    for name, fn in _weighted_order():
        try:
            candidates = fn(group, memory)
        except Exception as exc:
            log.info("%s 取材料失败：%r", name, exc)
            continue
        if not candidates:
            continue
        pick = _pick(group, candidates)
        if pick:
            log.info("暖场材料[%s]：%s", name, pick[:60])
            return pick
    return ""

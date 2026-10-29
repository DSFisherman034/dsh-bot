"""主动说话的"事件"：真有事才开口，代替准点暖场。

只报"确实发生了、本机能查到"的事，不抓新闻、不编天气——群友不会每两小时准点说话，
但看到什么会随口提一句。这是 heartbeat 的一个补充来源，不是替代：
有事件时优先用事件当由头，没有就退回 material 的四源。

每条事件带一个 key，同一件事（同一天/同一小时）只说一次，存在 state 里。
"""

import logging
import os
import shutil
from datetime import datetime

log = logging.getLogger("events")


def _disk_event():
    """磁盘快满：真事，群里有服务器梗，可以吐槽。"""
    try:
        u = shutil.disk_usage("/")
        pct = u.used / u.total * 100
        if pct >= 88:
            return {
                "kind": "disk",
                "key": f"disk:{datetime.now():%Y-%m-%d}",
                "text": f"本机磁盘用了 {pct:.0f}%，快满了",
            }
    except Exception:
        pass
    return None


def _load_event():
    """负载飙高：每小时最多说一次。"""
    try:
        load = os.getloadavg()[0]
        cpus = os.cpu_count() or 1
        if load > cpus * 2:
            return {
                "kind": "load",
                "key": f"load:{datetime.now():%Y-%m-%d-%H}",
                "text": f"本机负载 {load:.1f}，{cpus} 核跑得有点喘",
            }
    except Exception:
        pass
    return None


def _mem_event():
    """内存吃紧。"""
    try:
        total = avail = 0.0
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = float(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail = float(line.split()[1])
        if total:
            pct = (1 - avail / total) * 100
            if pct >= 92:
                return {
                    "kind": "mem",
                    "key": f"mem:{datetime.now():%Y-%m-%d-%H}",
                    "text": f"本机内存用了 {pct:.0f}%",
                }
    except Exception:
        pass
    return None


def _time_event():
    """时间节点：最弱的一类，只在群里很安静时才轮到它。"""
    now = datetime.now()
    if now.weekday() == 4 and 18 <= now.hour < 24:
        return {"kind": "time", "key": f"fri:{now:%Y-%m-%d}", "text": "今天是周五晚上"}
    if now.weekday() == 0 and 8 <= now.hour < 12:
        return {"kind": "time", "key": f"mon:{now:%Y-%m-%d}", "text": "今天是周一"}
    if now.day >= 28 and 10 <= now.hour < 22:
        return {"kind": "time", "key": f"monthend:{now:%Y-%m}", "text": "这个月快过完了"}
    return None


# 顺序即优先级：真异常在前，时间节点垫底
SOURCES = (_disk_event, _load_event, _mem_event, _time_event)


def collect(memory):
    """返回一条还没说过的事件 dict；没有就 None。"""
    for fn in SOURCES:
        try:
            ev = fn()
        except Exception as exc:
            log.info("事件检查失败 %s: %r", fn.__name__, exc)
            continue
        if not ev:
            continue
        if memory.get_state(f"event:{ev['key']}"):
            continue
        return ev
    return None


def mark(memory, ev):
    """标记这条事件已经说过，别重复。"""
    if ev and ev.get("key"):
        memory.set_state(f"event:{ev['key']}", "1")

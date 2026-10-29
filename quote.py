"""引用目标：提示词里的消息编号块 + 模型 `[[引:#k]]` 标记解析。

单独成模块是为了能脱离 main.py 单测：main.py 一 import 就会打开生产 data.db、
连 QQ 客户端，不适合在测试里拉起来。
"""

import re
from datetime import datetime

# 模型可能写成 [[引:#3]] / [[引用:3]] / [[引：3]]
RE_QUOTE_REF = re.compile(r"\[\[\s*引(?:用)?\s*[:：]\s*#?(\d{1,2})\s*\]\]")


def refs_block(refs, content_chars: int = 40) -> str:
    """外部消息列表 → 提示词里的编号块；不足两条就不给（没得选）。"""
    if len(refs) < 2:
        return ""
    lines = []
    for i, (_mid, author, content, ts) in enumerate(refs, 1):
        clock = datetime.fromtimestamp(ts).strftime("%H:%M")
        text = " ".join((content or "").split())[:content_chars]
        lines.append(f"#{i} [{clock}] {author or '某人'}: {text}")
    newest = len(refs)
    return (
        f"群里最近几条（引用编号 #1 最早、#{newest} 最新）：\n"
        + "\n".join(lines)
        + "\n每条回复最前面都写 [[引:#编号]]，指明你接的是哪条（编号换成上面的数字，"
        f"接最新那条就写 [[引:#{newest}]]，系统不会引）。\n\n"
    )


def take_marker(text: str, refs) -> tuple[str, str | None, int | None]:
    """解析回复里的 `[[引:#k]]`。

    返回 (去掉标记的文本, 要引的 msg_id 或 None, 标记里的编号或 None)。
    引最新那条等于没引（引用块只是噪音），msg_id 给 None，但编号照样返回，
    方便日志区分「没写标记」和「写了但指向最新」。
    """
    if not text:
        return text, None, None
    m = RE_QUOTE_REF.search(text)
    if not m:
        return text, None, None
    clean = RE_QUOTE_REF.sub("", text).strip()
    try:
        k = int(m.group(1))
    except (TypeError, ValueError):
        return clean, None, None
    if not refs or k < 1 or k > len(refs) or k == len(refs):
        return clean, None, k
    return clean, refs[k - 1][0], k

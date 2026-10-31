"""回复正文的清洗：模型偶尔把"再想想、改一版"的自我检查写进正文。

2026-10-31 13:30 实例（场 4 step 2）：模型已经写好两条气泡，又在正文里接着写
「等等，说好一条。改一下…最终：…」，`split_bubbles` 把剩下的整段并成第三条，
直接发进群里；下一轮它又把自己的思考当群聊读回去（「草，背鳍没盖住」）。

这里不猜语义，只认「自我修改」的强信号：命中两个以上才算溢出，避免误伤正常聊天。
命中后只保留最后一个 `[[引:#k]]` 之后的候选答案（模型改写时最终版总在最后）。
单独成模块是为了能脱离 main.py 单测（main.py 一 import 就连 botsns、开生产库）。
"""

import re

# 只收"改稿"语境才出现的词，单个词不够（比如"改一下"正常也会说），要两个以上。
_META_SIGNALS = [
    re.compile(r"等等[，,]\s*说好"),
    re.compile(r"改一下"),
    re.compile(r"修改成"),
    re.compile(r"这个是?最[终后]答案"),
    re.compile(r"不输出思考"),
    re.compile(r"最终输出"),
    re.compile(r"最终[：:]"),
    re.compile(r"不加句号"),
    re.compile(r"约\s*\d+\s*字"),
]

# 和 quote.RE_QUOTE_REF 等价，这里自带一份，避免模块耦合
_QUOTE_MARK_RE = re.compile(r"\[\[\s*引(?:用)?\s*[:：]\s*#?\d{1,2}\s*\]\]")


def meta_hits(text: str) -> list[str]:
    """命中的自我修改信号（按模式去重），用于日志和测试。"""
    return [p.pattern for p in _META_SIGNALS if p.search(text or "")]


def strip_meta_leak(text: str) -> tuple[str, bool]:
    """返回 (清干净的文字, 是否命中)。

    - 没命中：原样返回。
    - 命中且找得到 `[[引:#k]]`：从最后一个标记开始，截到结束（或下一条自我修改行）。
    - 命中但没标记（暖场/私信）：砍掉第一条自我修改行之后的全部。
    """
    if not text:
        return text, False
    if len(meta_hits(text)) < 2:
        return text, False

    last = None
    for m in _QUOTE_MARK_RE.finditer(text):
        last = m
    if last:
        tail = text[last.start():]
        kept = []
        for i, line in enumerate(tail.splitlines()):
            if i and any(p.search(line) for p in _META_SIGNALS):
                break
            kept.append(line)
        out = "\n".join(kept).strip()
        if out:
            return out, True

    kept = []
    for line in text.splitlines():
        if any(p.search(line) for p in _META_SIGNALS):
            break
        kept.append(line)
    out = "\n".join(kept).strip()
    return (out or text), True

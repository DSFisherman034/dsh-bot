"""回复正文的清洗：两件事——思考溢出、搜索来源。

1) 模型偶尔把"再想想、改一版"的自我检查写进正文。

2026-10-31 13:30 实例（场 4 step 2）：模型已经写好两条气泡，又在正文里接着写
「等等，说好一条。改一下…最终：…」，`split_bubbles` 把剩下的整段并成第三条，
直接发进群里；下一轮它又把自己的思考当群聊读回去（「草，背鳍没盖住」）。

这里不猜语义，只认「自我修改」的强信号：命中两个以上才算溢出，避免误伤正常聊天。
命中后只保留最后一个 `[[引:#k]]` 之后的候选答案（模型改写时最终版总在最后）。

2) 搜索来源不该进群聊（2026-11-03 用户：发言还带搜索来源的，不能带）。
2026-11-03 20:39 实例：被问某版本什么时候上线，回复正文末尾跟了
「[来源](https://www.sohu.com/a/…_)」，群里像贴了张参考书目。
`strip_sources` 只剥「链接/URL/来源行」这三种形状，不动正文语义。

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


# ---------------- 搜索来源 ----------------

# 链接文字本身就是出处的，整段删；域名形式的也删。
_LABEL_ONLY = re.compile(
    r"来源|参考|参考来源|参考链接|信息来源|资料来源|出处|原文|资料|链接|详见|官网|点击|网页|新闻|报道"
)
_DOMAIN_ONLY = re.compile(r"[a-z0-9.-]+\.[a-z]{2,}(?:/\S*)?", re.I)
_MD_LINK = re.compile(r"\[([^\[\]]{0,80})\]\(\s*(?:https?://|www\.)[^)\s]*\)")
_BARE_URL = re.compile(r"(?:https?://|www\.)[^\s)\]」，。！？、；]+")
_SRC_LINE = re.compile(
    r"^\s*(?:来源|参考来源|参考链接|信息来源|资料来源|参考|出处|原文|资料|链接|详见)\s*[:：]"
)
_SRC_PAREN = re.compile(r"[（(]\s*(?:来源|参考|出处|资料)\s*[:：][^）)]{0,200}[）)]")


def strip_sources(text: str) -> tuple[str, bool]:
    """剥掉回复里的搜索来源痕迹，返回 (干净文字, 是否命中)。

    只认三种形状，不猜语义：
    1. `[文字](url)` → 留文字；文字本身就是「来源/参考/域名」→ 整段删。
    2. 裸 URL（http/https/www.）→ 删。
    3. 整行以「来源：」「参考来源：」… 开头 → 整行删；括号里的「（来源：…）」→ 删。
    """
    if not text or not text.strip():
        return text, False
    hit = False

    def _md(m):
        nonlocal hit
        hit = True
        label = (m.group(1) or "").strip()
        if not label or _LABEL_ONLY.fullmatch(label) or _DOMAIN_ONLY.fullmatch(label):
            return ""
        return label

    out = _MD_LINK.sub(_md, text)
    out, n = _BARE_URL.subn("", out)
    hit = hit or bool(n)

    kept = []
    for line in out.splitlines():
        if _SRC_LINE.match(line):
            hit = True
            continue
        line, n = _SRC_PAREN.subn("", line)
        if n:
            hit = True
        kept.append(line)
    out = "\n".join(kept)

    if not hit:
        return text, False
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+$", "", out, flags=re.M)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip(), True

"""DSH Bot —— 一个会自己看群、挑时机说话的 QQ 群友。

分工（与 /opt/simple-bot 一致）：
  - botsns：登录、token 刷新、webhook 接收、事件解析、被动回复 id 管理、发送
  - 本文件：编排（谁的话要理、什么时候说话、发什么）
  - brain.py：提示词与 LLM 调用
  - memory.py：群聊流水（重启不掉线）
  - persona.md：人设（改完立即生效，不用重启）

运行：/opt/qqbot/venv/bin/python /opt/dsh-bot/main.py
监听：127.0.0.1:3704（nginx 从 https://dshbot.rantindom.com:8443 反代）
"""

import asyncio
import configparser
import json
import logging
import random
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from fastapi import Request

sys.path.insert(0, "/opt")

from qqbot.botsns import QQClient, QQCallbacks  # noqa: E402
from qqbot.botsns.QQClient import exceptions as qqexc  # noqa: E402

from brain import Brain  # noqa: E402
from render import render_incoming  # noqa: E402
import material  # noqa: E402
import media  # noqa: E402
from dshbridge import DshBridge  # noqa: E402
from memory import Memory  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
IMAGES_DIR = BASE_DIR / "images"
# [[img:3478444e]] 精确哈希，或 [[img:震惊脸]] 按描述找
IMG_MARKER = re.compile(r"\[\[img:([^\[\]\n]{1,60})\]\]")
IMG_SEARCH_RE = re.compile(r"\[\[img-search:([^\[\]\n]{1,80})\]\]")
IMG_SCOPE_RE = re.compile(r"\[\[img-scope:([^\[\]\n|]{1,60})\|([^\[\]\n]{1,120})\]\]")
IMG_HASH_RE = re.compile(r"h=([0-9a-f]{8})")
# 判断"是不是在叫我"用；不含单字"鱼"，避免把"吃鱼"当成叫我
ADDRESS_NAMES = ("DSH", "大肥鱼", "小鲸鱼")
# 渲染后的 mention：@名字(openid)
AT_TARGET_RE = re.compile(r"@([^()\n]{1,32}?)\(([^)\n]{6,})\)")

# 发送报错里出现这些 = 这个群永远发不进去（群号无效/被踢/无权限），关掉主动发言别再烧 token。
# 禁言、频控是临时的，不要放进这里。
PERMANENT_SEND_ERRORS = (
    "无权限", "不是群成员", "已下线",
    "group_id无效", "40011036", "11321", "群不存在",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("dshbot")

CONFIG = None
MEMORY: Memory | None = None
BRAIN: Brain | None = None
CLIENT: QQClient | None = None
BRIDGE: DshBridge | None = None
LAST_PROMPT_TS: dict[str, float] = {}
# 消息 id 游标：给 DSH 的增量按 id 算，不用本机时间（平台时间慢 1-2 秒会漏消息）
LAST_SEEN_ID: dict[str, int] = {}
PENDING_SEEN: dict[str, int] = {}
LAST_BUBBLES: dict[str, list[int]] = {}
LAST_IMAGES: dict[str, list[int]] = {}
GROUP_SEND_LOCKS: dict[str, asyncio.Lock] = {}

# 上下文滚动压缩：本群会话窗口攒到 50 条（含机器人自己的发言）就把最早 30 条
# 压成摘要，只留最近 20 条原文；压缩后清掉 DSH 会话，下一轮用「摘要 + 20 条」重开。
CTX_COMPACT_AT = 50
CTX_COMPACT_HEAD = 30
CTX_KEEP = 20
REPLAY: dict[str, int] = {}   # group -> 压缩后窗口起点 id，下一轮重放这段原文


# ---------------- 工具 ----------------


def split_bubbles(text: str, limit: int) -> list[str]:
    """按模型写的 `<🦄发送>` 或换行分条（照 qqbot 的拆条设计）。

    - 普通聊天 2–3 条，模型在逗号/句末该断的地方写 `<🦄发送>`。
    - 单行太长也不按长度硬切：宁可一条长消息，也不要留半句话。
    - 超过 3 条合并进最后一条。
    """
    text = re.sub(r"<\s*🦄\s*发送\s*>|🦄\s*发送", "\n", text or "")
    text = text.strip()
    if not text:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    if len(lines) <= 3:
        return lines
    return lines[:2] + ["\n".join(lines[2:])]



async def enrich_attachments(message, content: str) -> str:
    """把附件里的图片变成一句描述塞进消息，带短哈希，方便模型复用（表情包）。

    描述只是"文字兜底"：真正读图走 ACP image block（见 collect_prompt_images），
    本地模型/转录靠这句话。
    """
    atts = getattr(message, "attachments", None) or []
    marks = []
    for a in atts:
        url = a.get("url") if isinstance(a, dict) else getattr(a, "url", None)
        if not url:
            continue
        got = await media.download(url, IMAGES_DIR)
        if not got:
            marks.append("[图]")
            continue
        h, path = got
        row = MEMORY.get_image(h)
        if row and row[2]:
            desc = row[2]            # 同一张图只描述一次，省 token
        else:
            desc = await media.describe(
                path,
                CONFIG.get("ai", "base_url").strip(),
                CONFIG.get("ai", "api_key").strip(),
                CONFIG.get("ai", "vision_model", fallback="qwen3.7-flash").strip(),
                limit=CONFIG.getint("ai", "vision_desc_chars", fallback=media.MAX_DESC),
                edge=CONFIG.getint("ai", "image_max_edge", fallback=media.DEFAULT_EDGE),
            )
            MEMORY.put_image(h, path, desc)
            if desc:
                # 向量后台算，别把消息入库卡住
                asyncio.create_task(store_embedding(h, desc))
        short = h[:8]
        marks.append(f"[图:{desc}|h={short}]" if desc else f"[图|h={short}]")
    if not marks:
        return content
    content = content.strip()
    # render 已经把图片占位成 [图片] 了，别让同一张图出两个标记
    for _ in marks:
        if content.endswith("[图片]"):
            content = content[: -len("[图片]")].rstrip()
    return (content + " " + " ".join(marks)).strip()


# ---------------- 回调 ----------------

class Callbacks(QQCallbacks):
    """一律立即 return None（不阻塞 webhook），真正回复丢给后台任务。"""

    async def when_get_dc_message(self, message):
        asyncio.create_task(handle_dc(message))
        return None

    async def when_get_group_message(self, message):
        # 「接收全部消息」模式：@ 也走这里，mentions 里能看出是不是叫我
        asyncio.create_task(handle_group(message, addressed=is_addressed(message)))
        return None

    async def when_get_group_at_message(self, message):
        # 「仅 @ 时说话」模式：所有消息都走这里，mention 信息不保证有
        asyncio.create_task(handle_group(message, addressed=True))
        return None


def is_addressed(message) -> bool:
    """这条是不是在叫我。

    先信平台 mentions：只要 mentions 里有别人、没有我，就是找别人，
    哪怕正文里提到了「大肥鱼」（第三人称）。没有 mentions 时才退回文本判断。
    """
    try:
        author = getattr(message, "author", None)
        if (author is not None and getattr(author, "is_bot", False)
                and (getattr(author, "username", "") or "").strip() in SELF_NAMES):
            return False  # 自己发的，别回自己
        mentions = getattr(message, "mentions", []) or []
        if mentions:
            for m in mentions:
                if getattr(m, "is_you", False):
                    return True
                if (getattr(m, "username", "") or "").strip() in ADDRESS_NAMES:
                    return True
            return False
        content = message.content or ""
        targets = AT_TARGET_RE.findall(content)
        if targets:
            return any(name.strip() in ADDRESS_NAMES for name, _ in targets)
        if any(name in content for name in ADDRESS_NAMES):
            return True
        quote = getattr(message, "quote", None)
        qcontent = getattr(quote, "content", "") if quote else ""
        if qcontent:
            qtargets = AT_TARGET_RE.findall(qcontent)
            if qtargets:
                return any(name.strip() in ADDRESS_NAMES for name, _ in qtargets)
            return any(name in qcontent for name in ADDRESS_NAMES)
    except Exception:
        pass
    return False

# ---------------- 处理消息 ----------------

async def handle_group(message, addressed: bool):
    group = message.group_openid
    try:
        is_bot_author = bool(message.author.is_bot)
        content = await render_incoming(message)
        content = await enrich_attachments(message, content)
        MEMORY.add_message(
            group, message.id, message.author.username, message.author.openid,
            content, is_bot=False, ts=_ts(message.timestamp),
        )
        MEMORY.ensure_group(group)

        if addressed:
            await reply_group(group, "addressed", message.author.username, msg_id=message.id, message=message)
        elif is_bot_author and message.author.username in SELF_NAMES:
            log.debug("自己发的消息，仅记入上下文 group=%s", group[:8])
        elif CONFIG.get("behavior", "reply_policy", fallback="full") == "full":
            # 其他机器人和人类同地位：非 @ 也触发插话，允许机器人自己开聊。
            schedule_ambient(group)
        else:
            log.debug("非 @ 消息，仅记入上下文 group=%s", group[:8])
    except Exception:
        log.exception("处理群消息出错 group=%s", group)


async def handle_dc(message):
    group = "dc:" + message.author.openid
    try:
        if message.author.is_bot:
            return
        MEMORY.add_message(
            group, message.id, message.author.username, message.author.openid,
            await enrich_attachments(message, await render_incoming(message)),
            is_bot=False, ts=_ts(message.timestamp),
        )
        rows = MEMORY.recent_messages(group)
        who = message.author.username or "对方"

        # 弱隔离：私聊能看到群里公开的上下文，但群聊永远看不到私聊。
        extra = ""
        try:
            groups = [g for g in MEMORY.known_groups() if not g.startswith("dc:")]
            if groups:
                main_group = max(groups, key=MEMORY.last_activity)
                parts = []
                mems = MEMORY.recent_memories(main_group, limit=6)
                if mems:
                    mem_lines = [
                        f"[{datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')}] {text}"
                        for ts, _kind, text in mems
                    ]
                    parts.append("群内记忆（群里公开发生的事）：\n" + "\n".join(mem_lines))
                gr = MEMORY.recent_messages(main_group, minutes=180, limit=12)
                if gr:
                    gr_lines = []
                    for author, content, ts, is_bot in gr:
                        clock = datetime.fromtimestamp(ts).strftime("%H:%M")
                        name = "你" if is_bot else (author or "某人")
                        gr_lines.append(f"[{clock}] {name}: {(content or '').strip()[:160]}")
                    parts.append("群里最近的消息（群里的人都知道）：\n" + "\n".join(gr_lines))
                if parts:
                    extra = (
                        "这是私聊，只有你和对方知道。下面是群里公开的上下文，可以拿来接话，"
                        "但不要把私聊内容当成群里已知，也不要在群里提私聊。\n\n"
                        + "\n\n".join(parts)
                    )
        except Exception:
            log.exception("读取群上下文失败（私信继续）")

        text = await BRAIN.respond(rows, "dc", who, extra=extra, title="你们的私聊记录")
        if not text:
            return
        await asyncio.sleep(_typing_delay(text))
        await CLIENT.dc.send_message(message.author.openid, text)
        _record_own(group, text)
        log.info("私信回复 %s: %s", who, text[:60])
    except Exception:
        log.exception("处理私信出错")


AMBIENT_PENDING: dict[str, asyncio.Task] = {}


def schedule_ambient(group):
    """收到非 @ 消息：起一个合并窗口，窗口内多条消息合并成一次 DSH 决策。"""
    task = AMBIENT_PENDING.get(group)
    if task and not task.done():
        return  # 窗口已经开着，它会读到最新消息
    AMBIENT_PENDING[group] = asyncio.create_task(_ambient_round(group))


async def _ambient_round(group):
    debounce = CONFIG.getint("behavior", "ambient_debounce", fallback=10)
    try:
        await asyncio.sleep(debounce)
        reason = ambient_skip_reason(group)
        if reason:
            log.info("插话不开口[%s] group=%s", reason, group[:8])
            return

        quote_since = LAST_SEEN_ID.get(group, 0)
        cut_id = MEMORY.max_id(group)
        text = await ask_dsh(group, ambient_instruction(group))
        if not text:
            log.info("插话跳过[模型决定不说] group=%s", group[:8])
            return

        await asyncio.sleep(_typing_delay(text))
        quote = quote_for_ambient(group, quote_since, text)
        await send_group(group, text, msg_id=None, quote_id=quote, since_id=cut_id)
    except Exception:
        log.exception("插话回合出错 group=%s", group[:8])
    finally:
        AMBIENT_PENDING.pop(group, None)
        # 这一轮处理期间又有了外部消息（真人/其他 bot）才再开一个窗口。
        # 不能拿 MAX(ts) 比：自己刚发的回复也在这张表里，会导致无限自问自答。
        if MEMORY.has_external_since(group, LAST_SEEN_ID.get(group, 0), SELF_NAMES):
            schedule_ambient(group)


def _addressed_to_others(text: str) -> bool:
    """这条消息的 @ 目标是不是别人（而不是我）。

    只看渲染出来的 @目标：正文里出现「大肥鱼」但 @ 的是别人，不算在说我。
    """
    text = text or ""
    targets = AT_TARGET_RE.findall(text)
    if targets:
        return all(name.strip() not in ADDRESS_NAMES for name, _ in targets)
    if "@" in text and not any(w in text for w in ADDRESS_NAMES):
        return True
    return False


def _looks_like_followup(rows, last_bot_ts) -> bool:
    """最后一条是不是在接你的话/问你。"""
    if not last_bot_ts or time.time() - last_bot_ts >= 180:
        return False
    last_other = next((r for r in reversed(rows) if not r[3]), None)
    if not last_other:
        return False
    content = last_other[1] or ""
    return any(k in content for k in (
        "?", "？", "什么", "怎么", "为什么", "哪", "谁", "多少",
        "是不是", "能不能", "有没有", "吗",
    ))


def ambient_skip_reason(group: str) -> str | None:
    """这轮插话是不是压根不该开——省一次 DSH 调用，也压住"每句都回"。"""
    rows = MEMORY.recent_messages(group, minutes=30, limit=4)
    if not rows:
        return "群里没消息"
    last_author, last_content, _ts_, last_is_bot = rows[-1]
    if last_is_bot:
        return "最后一条是自己说的"
    last = MEMORY.last_bot_message(group)
    if _looks_like_followup(rows, last[1] if last else 0):
        return None
    if _addressed_to_others(last_content):
        return "最后一条 @ 的是别人"
    cooldown = CONFIG.getint("behavior", "ambient_cooldown", fallback=90)
    if last and time.time() - last[1] < cooldown:
        return f"自己 {int(time.time() - last[1])} 秒前刚说过话"
    return None


def ambient_instruction(group: str) -> str:
    hint = ""
    last = MEMORY.last_bot_message(group)
    if last:
        ago = int(time.time() - last[1])
        hint = f"你 {ago} 秒前刚说过：「{last[0][:40]}」。刚说过的话不要重复。\n"

    rows = MEMORY.recent_messages(group, minutes=30, limit=6)
    if _looks_like_followup(rows, last[1] if last else 0):
        return (
            "最后一条明显是在接你刚才的话、或者在问你，**必须回**，不要 SKIP。"
            "顺着对方的问题答，别装死。\n" + hint
        )

    last_other = next((r for r in reversed(rows) if not r[3]), None)
    who = (last_other[0] or "某人") if last_other else "某人"
    if last_other and _addressed_to_others(last_other[1]):
        return (
            f"最后一条是 {who} 在 @ 别人，不是对你说的。"
            "除非你能补一句真正有用的信息，否则输出 SKIP。\n" + hint
        )

    # 上一条是另一个 bot 刚说的、最后一条是短句 → 多半是在跟那个 bot 打招呼/回它
    prev = rows[-2] if len(rows) >= 2 else None
    if (prev and prev[3] and (prev[0] or "") not in SELF_NAMES
            and last_other and last_other is rows[-1]
            and len(last_other[1] or "") <= 12
            and time.time() - prev[2] <= 90):
        who_prev = prev[0] or "另一个机器人"
        return (
            f"最后一条像是在回 {who_prev}，不是在跟你说话。"
            "除非它明确点你的名字，否则输出 SKIP。\n" + hint
        )

    return (
        f"没人 @ 你。先判断最后一条（{who} 说的）是不是在跟你说："
        "如果是在跟别人说话、或者在接别人的话，就输出 SKIP。"
        "只有你能补一句真正有用或好笑的，或者对方明显在等你，才接；不确定就 SKIP。\n" + hint
    )


async def reply_group(group, task, who, msg_id=None, message=None):
    # 被 @ 时才把图本身交给 DSH：它真的能看，插话/暖场就够了文字描述
    cut_id = MEMORY.max_id(group)
    with_images = CONFIG.getboolean("dsh", "attach_images", fallback=True)
    quote = quote_for_addressed(group, message)
    text = await ask_dsh(group, f"最后一条是 {who} @ 你，回他一句。", who, message,
                         with_images=with_images)

    if not text:  # 只有桥挂了才会走到这里
        rows = MEMORY.recent_messages(group, minutes=90)
        text = await BRAIN.respond(rows, task, who)
    if not text:
        log.info("模型选择沉默 task=%s", task)
        return
    await asyncio.sleep(_typing_delay(text))
    await send_group(group, text, msg_id=msg_id, quote_id=quote, since_id=cut_id)


# ---------------- 发送 ----------------

async def apply_image_scopes(text: str) -> str:
    """模型用 [[img-scope:描述|适用范围]] 更新图库；这条不发给群。"""
    for m in IMG_SCOPE_RE.finditer(text or ""):
        token, scope = m.group(1).strip(), m.group(2).strip()
        got = await resolve_image(token)
        if not got:
            log.info("改图适用范围失败（图没找到）：%r", token)
            continue
        row = MEMORY.image_by_path(got[0])
        if row:
            MEMORY.set_image_scope(row[0], scope)
            log.info("图适用范围更新 %s: %s", row[0][:8], scope[:60])
    return IMG_SCOPE_RE.sub("", text or "").strip()


CONTINUE_STOP_KEYS = ("?", "？", "什么", "怎么", "为什么", "哪", "谁", "吗",
                     "不是", "错误", "不对", "别发", "停", "等等", "等下",
                     "@", "改", "坏", "乱")


async def _confirm_continue(group, since_id, remaining, sent_count):
    """发送中途来了新消息：先暂停，再确认剩余气泡还要不要发。

    确认用本地 Brain，只输出 CONTINUE/STOP；失败时按关键词兜底。
    返回 (是否继续, 已确认到的最后消息 id)。
    """
    try:
        rows = MEMORY.messages_after_id(group, since_id, limit=10)
    except Exception:
        return True, None
    new = [
        (a or "", c or "")
        for _id, a, c, _ts, is_bot in rows
        if not is_bot and (a or "") not in SELF_NAMES
    ]
    if not new:
        return True, None
    last_id = rows[-1][0]
    text = " ".join(c for _a, c in new[-3:])
    convo = "\n".join(f"[{a}] {c}" for a, c in new[-5:])
    remain = "\n".join(f"- {p[:80]}" for p in remaining[:3])
    system = "你是群聊发言节奏判断器。只输出 CONTINUE 或 STOP，不解释。"
    user = (
        f"你正在分条发消息，已经发了 {sent_count} 条。\n"
        f"剩余未发：\n{remain}\n\n"
        f"群里刚来了新消息：\n{convo}\n\n"
        "判断剩余还要不要继续发。规则：\n"
        "- 新消息在追问你、纠正你、@你、换话题，或明显在等你回应 → STOP（先回新的）\n"
        "- 新消息只是无关的哈哈/表情/别人之间聊天，剩余内容仍然成立 → CONTINUE\n"
        "- 不确定 → STOP\n"
        "只输出 CONTINUE 或 STOP。"
    )
    decision = "STOP"
    try:
        if BRAIN is None:
            raise RuntimeError("brain not ready")
        out = await asyncio.wait_for(
            BRAIN.complete(system, user, temperature=0.0,
                           max_tokens=8, enable_thinking=False),
            timeout=3.0,
        )
        up = (out or "").strip().upper()
        if up.startswith("CONTINUE") or up.startswith("继续"):
            decision = "CONTINUE"
    except Exception:
        log.warning("二次确认失败，按关键词兜底 group=%s", group[:8], exc_info=True)
        decision = "STOP" if any(k in text for k in CONTINUE_STOP_KEYS) else "CONTINUE"
    log.info("发送中二次确认：%s（剩余 %d 条，新消息 %r）group=%s",
             decision, len(remaining), text[:60], group[:8])
    return decision == "CONTINUE", last_id


async def send_group(group, text, msg_id=None, quote_id=None, since_id=None) -> bool:
    """返回是否至少发出了一条；同群发送串行，中途被追问会停掉剩余气泡。"""
    text = IMG_SEARCH_RE.sub(lambda m: f"[[img:{m.group(1)}]]", text or "")
    text = await apply_image_scopes(text)
    if not text.strip():
        return False
    if since_id is None:
        since_id = MEMORY.max_id(group)
    limit = CONFIG.getint("behavior", "max_reply_chars", fallback=300)
    bubbles = split_bubbles(text, limit)
    lock = GROUP_SEND_LOCKS.setdefault(group, asyncio.Lock())
    sent_count = 0
    sent_image = False
    async with lock:
        for i, part in enumerate(bubbles):
            cont, new_cursor = await _confirm_continue(group, since_id, bubbles[i:], sent_count)
            if not cont:
                log.info("二次确认 STOP，剩余 %d 条不发 group=%s", len(bubbles) - i, group[:8])
                break
            if new_cursor is not None:
                since_id = new_cursor
            try:
                m = IMG_MARKER.search(part)
                if m:
                    row = await resolve_image(m.group(1))
                    if row:
                        ok_img = await media.upload_and_send(
                            CLIENT, group, Path(row[0]), msg_id=msg_id,
                            base_url=CONFIG.get("bot", "image_base", fallback=""),
                            public_dir=Path(CONFIG.get("bot", "image_public_dir", fallback="/var/www/images")),
                        )
                        if ok_img:
                            sent_count += 1
                            sent_image = True
                            MEMORY.mark_bot_spoke(group)
                            log.info("群发图 %s: %s", group[:8], row[1][:30])
                        else:
                            log.warning("群发图失败 %s: %s", group[:8], row[1][:30])
                    else:
                        log.info("想发的图没找到：%r", m.group(1))
                    part = IMG_MARKER.sub("", part).strip()
                    if not part:
                        continue

                quote = quote_id if i == 0 else None
                use_md = "qqbot-at-user" in part
                try:
                    if use_md:
                        # QQ 的 @ 标签只在 markdown 消息里渲染
                        await CLIENT.group.send_markdown(
                            group, part, msg_id=msg_id if i == 0 else None,
                            quote_message_id=quote)
                    else:
                        await CLIENT.group.send_message(
                            group, part, msg_id=msg_id if i == 0 else None,
                            quote_message_id=quote)
                except Exception:
                    if not quote:
                        raise
                    log.warning("引用发送失败，退回不带引用 group=%s", group[:8])
                    if use_md:
                        await CLIENT.group.send_markdown(group, part, msg_id=msg_id if i == 0 else None)
                    else:
                        await CLIENT.group.send_message(group, part, msg_id=msg_id if i == 0 else None)
                _record_own(group, part)
                MEMORY.mark_bot_spoke(group)
                sent_count += 1
                log.info("群回复%s %s: %s", "（引用）" if quote else "", group[:8], part[:80])
            except (qqexc.BadSituation, qqexc.BadArguments, qqexc.UnknownError) as exc:
                msg = str(exc)
                log.warning("发送被拒（%s）：%s", group[:8], msg[:200])
                # 只有"这个群永远发不进去"才关掉主动性；禁言/频控是临时的，别把暖场整个停掉
                if any(k in msg for k in PERMANENT_SEND_ERRORS):
                    MEMORY.set_group_flag(group, "proactive_ok", False)
                    log.warning("这个群发不进去，关掉主动发言 group=%s", group[:8])
                break
            except Exception:
                log.exception("发送失败 group=%s", group[:8])
                break
            if i + 1 < len(bubbles):
                # 补一句的节奏：短句抢着发，长句稍顿一下
                gap = 0.6 + min(len(part), 40) * 0.03 + random.uniform(0.2, 1.4)
                gap = min(3.0, gap)
                log.debug("气泡间隔 %.2fs group=%s", gap, group[:8])
                await asyncio.sleep(gap)
    if sent_count:
        hist = LAST_BUBBLES.setdefault(group, [])
        hist.append(sent_count)
        del hist[:-6]
        ih = LAST_IMAGES.setdefault(group, [])
        ih.append(1 if sent_image else 0)
        del ih[:-6]
    return sent_count > 0


def _record_own(group, text):
    MEMORY.add_message(
        group, None, CONFIG.get("bot", "name", fallback="DSH"), "self", text, is_bot=True
    )


def _quote_mode() -> str:
    return (CONFIG.get("behavior", "quote_mode", fallback="auto") or "auto").strip().lower()


def _quote_stale() -> float:
    return CONFIG.getfloat("behavior", "quote_stale_seconds", fallback=120)


def quote_for_addressed(group, message) -> str | None:
    """被 @ 时的引用路由。

    always：每条都引；off：不引；auto（默认）只在有理由时引：
    - 这条 @ 已经被后面的消息顶上去 → 引它，别人才知道你在回哪句；
    - 这条 @ 已经隔了 2 分钟以上 → 引它，补上下文；
    - 它就紧跟在你上一句后面、或者就是最新一条 → 不引，引了是噪音。
    """
    mode = _quote_mode()
    if mode == "off" or message is None or not getattr(message, "id", None):
        return None
    if mode == "always":
        return message.id
    latest = MEMORY.last_external_msg_id(group, 0, SELF_NAMES)
    if latest and latest != message.id:
        log.info("引用：@ 的消息已被顶上去 group=%s", group[:8])
        return message.id
    age = time.time() - _ts(message.timestamp)
    if age >= _quote_stale():
        log.info("引用：@ 的消息隔了 %d 秒 group=%s", age, group[:8])
        return message.id
    return None


def quote_for_ambient(group, since_id, text="") -> str | None:
    """插话时的引用路由：只在"接的不是最新那条、或者群里有好几个人在说"时引。"""
    mode = _quote_mode()
    if mode == "off":
        return None
    rows = MEMORY.external_messages_after(group, since_id, SELF_NAMES, limit=10)
    if not rows:
        return None
    if mode == "always":
        return rows[-1][0]
    # 回复正文点到谁，就引谁那条：避免"回都报"却引了最新的雪天图片
    if text:
        for msg_id, author, _ts in reversed(rows):
            if author and author in text:
                log.info("引用：回复点到 %s group=%s", author, group[:8])
                return msg_id
    # 多人插话：最近两条来自不同的人 → 标明接谁的
    if len(rows) >= 2 and rows[-1][1] != rows[-2][1]:
        log.info("引用：多人插话，接 %s group=%s", rows[-1][1], group[:8])
        return rows[-1][0]
    # 隔太久：已经脱节了
    if time.time() - rows[-1][2] >= _quote_stale():
        log.info("引用：最后一条隔了 %d 秒 group=%s", time.time() - rows[-1][2], group[:8])
        return rows[-1][0]
    return None


def _typing_delay(text: str) -> float:
    # 比旧版多留约 1 秒：先让别人有机会接话，别每次都抢在人类前面开口
    base = 1.6 + min(len(text), 120) * 0.03
    return min(5.5, base + random.uniform(0.4, 1.4))


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return time.time()




def _ctx_state(group: str, name: str) -> str:
    return MEMORY.get_state(f"{name}:{group}", "") or ""


async def summarize_context(group: str, rows: list) -> str:
    """把窗口里最早的几十条压成一段摘要；失败就用截断兜底，不卡消息。"""
    lines = []
    for _id, author, content, ts, is_bot in rows:
        clock = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        name = CONFIG.get("bot", "name", fallback="DSH") if is_bot else (author or "某人")
        lines.append(f"[{clock}] {name}: {(content or '').strip()[:200]}")
    try:
        out = await BRAIN.complete(
            "你是群聊摘要器。只输出摘要正文，不要解释、不要评价。",
            "把下面这段群聊压成 1 段不超过 200 字的事实摘要："
            "谁说了什么、结论是什么、还有什么事没下文。不要复述原话。\n\n"
            + "\n".join(lines),
            temperature=0.2, max_tokens=320, enable_thinking=False,
        )
        return (out or "").strip()[:400]
    except Exception:
        log.warning("上下文摘要生成失败，用截断兜底 group=%s", group[:8], exc_info=True)
        return "；".join((c or "").strip()[:40] for _i, _a, c, _t, _b in rows)[:200]


async def maybe_compact(group: str):
    """滑动窗口：本群会话攒到 50 条（含自己的发言）就压最早 30 条，留最近 20 条原文。

    压缩后清掉 DSH session，下一轮用「累积摘要 + 20 条原文」新建会话，
    这样 DSH 侧历史不会从一次对话滚成一整天的流水。
    """
    key_base = f"ctxbase:{group}"
    base_raw = MEMORY.get_state(key_base)
    if base_raw is None:
        # 首次（含重启后旧会话已清）：窗口从最近 20 条开始，别把陈年流水压进来
        MEMORY.set_state(key_base, MEMORY.window_base(group, keep=CTX_KEEP))
        return
    base = int(base_raw or 0)
    rows = MEMORY.messages_after_id(group, base, limit=CTX_COMPACT_AT + 50)
    if len(rows) < CTX_COMPACT_AT:
        return
    # 正常是 50 条：压最早 30、留最近 20；异常积压时一次压到只剩最近 20
    head = rows[:-CTX_KEEP]
    if len(head) > 60:                 # 摘要输入封顶，别一次塞几百条
        head = head[-60:]
    keep = rows[-CTX_KEEP:]
    new_base = rows[-CTX_KEEP - 1][0]
    try:
        summary = await asyncio.wait_for(summarize_context(group, head), timeout=10)
    except asyncio.TimeoutError:
        log.warning("上下文摘要超时，用截断兜底 group=%s", group[:8])
        summary = "；".join((c or "").strip()[:40] for _i, _a, c, _t, _b in head)[:200]
    if summary:
        prev = _ctx_state(group, "ctxsum")
        merged = (prev + "\n" + summary).strip() if prev else summary
        MEMORY.set_state(f"ctxsum:{group}", "\n".join(merged.splitlines()[-3:]))
    new_base = head[-1][0]
    MEMORY.set_state(key_base, new_base)
    MEMORY.set_state(f"sid:{group}", "")       # 下一轮新建会话
    if BRIDGE is not None:
        BRIDGE.sessions.pop(group, None)       # 内存里的旧 session 也要丢掉
    REPLAY[group] = new_base
    LAST_SEEN_ID[group] = new_base
    PENDING_SEEN.pop(group, None)
    log.info("上下文压缩 group=%s：压 %d 条，留 %d 条原文",
             group[:8], len(head), len(keep))


def _fresh_rows(group: str) -> list:
    """这一轮要喂给 DSH 的增量转录。

    游标是消息自增 id，不是时间：平台 ts 精度到秒且比本机慢 1-2 秒，
    用本机 time.time() 当 since 会把刚到的消息当成"旧的"丢掉。
    """
    if group in REPLAY:
        # 刚压缩完：新会话第一轮重放保留的 20 条原文（可能夹着压缩后新到的消息）
        base = REPLAY.pop(group)
        rows = MEMORY.messages_after_id(group, base, limit=100)
        if rows:
            PENDING_SEEN[group] = rows[-1][0]
            return [(author, content, ts, is_bot) for _id, author, content, ts, is_bot in rows]
        return []
    last = PENDING_SEEN.get(group) or LAST_SEEN_ID.get(group)
    if last is None:
        rows = MEMORY.recent_messages(group, minutes=180, limit=20)
        if rows:
            PENDING_SEEN[group] = MEMORY.max_id(group)
        return rows[-CTX_KEEP:] if rows else []
    rows = MEMORY.messages_after_id(group, last, limit=20)
    if rows:
        PENDING_SEEN[group] = rows[-1][0]
        return [(author, content, ts, is_bot) for _id, author, content, ts, is_bot in rows]
    return MEMORY.recent_messages(group, minutes=180, limit=8)


SELF_NAMES = {"DSH", "大肥鱼", "小鲸鱼", "鱼"}
MEME_WORDS = ("大肥鱼", "白饭", "token", "服务器繁忙", "得加钱", "誓死", "终有一天",
              "V我50", "上错模型", "吃白饭", "蹭饭", "小鱼干")


def meme_budget_hint(group: str) -> str:
    """按最近几条自己说过的话，给这一轮一个用梗预算，防止每条都玩梗。"""
    rows = MEMORY.recent_messages(group, minutes=180, limit=20)
    bots = [c for _a, c, _ts, b in rows if b][-3:]
    used = [c for c in bots if any(w in (c or "") for w in MEME_WORDS)]
    if len(used) >= 2:
        return "你最近两条回复都用了梗，这一轮收住，别再用梗，正常接话。"
    if used:
        return "你最近刚用过一次梗，这一轮不是特别合适就别再用。"
    return "这一轮如果语境合适，可以自然带一个梗；不合适就别硬塞。"


def bubble_hint(group: str) -> str:
    """防止永远只发一条：最近连续单条就推一把，最近拆太多就收一收。"""
    hist = LAST_BUBBLES.get(group, [])[-4:]
    if len(hist) >= 4 and all(n <= 1 for n in hist):
        return "你最近四条都是一条，这轮如果能自然分成「反应 + 补充」，就用 <🦄发送> 分两条；别硬拆。"
    if sum(1 for n in hist if n >= 2) >= 3:
        return "你最近分条有点多，这轮能一条说完就一条。"
    return "普通闲聊大约一半适合分两条；这轮看情况，别硬拆。"


def image_hint(group: str) -> str:
    """把配图比例往 1/3 左右推：太久没配就提醒，太密就压。"""
    hist = LAST_IMAGES.get(group, [])[-4:]
    if len(hist) >= 4 and sum(hist) == 0:
        return ("你最近四条都没配图，这轮如果图能加分，就配一张（[[img:描述]]）；别硬配。"
                "选图前先看适用范围，嘲讽/互损图别用在严肃、安慰或自己被调侃的场合。")
    if sum(hist[-3:]) >= 2:
        return "你最近配图有点多，这轮别配，先说话。"
    return ("大约每 3 条回复可以配一张图；有合适的就配，没有别硬找。"
            "选图前先看适用范围，别把嘲讽/互损图用在严肃、安慰或自己被调侃的场合。")


def image_library_hint(limit=12) -> str:
    """把图库描述 + 适用范围给模型，作为选图与改范围的依据。"""
    rows = MEMORY.image_library(limit)
    if not rows:
        return ""
    lines = [
        f"- {(desc or '').strip()[:56]}｜{(scope or '通用').strip()[:40]}"
        for _h, desc, scope in rows
    ]
    return ("图库（选图前先看适用范围；嘲讽/互损图只在朋友互损时用，"
            "不用于自己被调侃、严肃、安慰、陌生人；想按情绪/场景找更多，用 [[img-search:描述]]）：\n"
            + "\n".join(lines))


def build_dsh_prompt(group: str, instruction: str, who: str = "", message=None,
                     fresh=None) -> str:
    """把增量转录 + 本次任务指令拼成提示词。"""
    if fresh is None:
        fresh = _fresh_rows(group)
    LAST_PROMPT_TS[group] = time.time()

    # 每轮都告诉模型"现在几点"：DSH 会话是跨天持久的，只给消息时间戳时，
    # 模型会把几小时前的旧消息当成现在（07:04 的暖场说"都三点了"就是这么来的）。
    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    now_line = (
        f"现在是 {now.strftime('%Y-%m-%d %H:%M')}（周{week}，24 小时制）。"
        "消息里的时间是发生的时刻，不是现在；别把旧消息的时间当成当前时间。\n\n"
    )

    lines = []
    for author, content, ts, is_bot in fresh:
        clock = datetime.fromtimestamp(ts).strftime("%H:%M")
        name = CONFIG.get("bot", "name", fallback="DSH") if is_bot else author
        lines.append(f"[{clock}] {name}: {(content or '').strip()[:200]}")

    head = ("以下是群里刚才的消息：\n" + "\n".join(lines)) if lines else "这一批没有新消息。"
    summ = _ctx_state(group, "ctxsum").strip()
    sum_block = (
        "更早的对话摘要（已压缩，参考就好，不要主动复述）：\n" + summ + "\n\n"
    ) if summ else ""
    mem = MEMORY.recent_memories(group, limit=8)
    mem_block = ""
    if mem:
        mem_lines = [
            f"[{datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')}] {text}"
            for ts, _kind, text in mem
        ]
        mem_block = (
            "群内记忆（之前发生的事，参考就好，不要主动复述）：\n"
            + "\n".join(mem_lines) + "\n\n"
        )
    tail = (
        "只输出要发到群里的话本身；默认一条，想在自然停顿处分条就写 <🦄发送>；"
        "决定不说话就只输出 SKIP。\n"
        "想附带一张表情包，就另起一行写 [[img:一句话描述]]（例如 [[img:震惊脸]]、"
        "[[img:竖大拇指]]），系统会去图库里找最像的那张；找不到就不发图，别编哈希。\n"
        "想按情绪/场景搜图，就写 [[img-search:描述或情绪]]（例如 [[img-search:摸鱼]]）；"
        "系统会搜索图库并发出最像的一张。这不是外部工具，不需要审批，也不要说“我没有搜索工具”。\n"
        "选图前先看上面的适用范围；用户说「错误发图」或解释某张图的用法时，"
        "可以另起一行写 [[img-scope:图的一句话描述|适用范围]] 更新图库，这条不会发出去。"
    )
    lib = image_library_hint()
    lib_block = (lib + "\n\n") if lib else ""
    return (
        f"{now_line}{sum_block}{mem_block}{head}\n\n{instruction}\n\n{meme_budget_hint(group)}\n\n"
        f"{bubble_hint(group)}\n\n{image_hint(group)}\n\n{lib_block}{tail}"
    )


def collect_prompt_images(fresh) -> list[tuple[str, bytes]]:
    """从本轮转录的 `|h=xxxxxxxx` 标记找回原图，归一化后交给 DSH 读。

    只发最近几张：图会留在 DSH 会话历史里，每次都带一遍很烧 token。
    """
    limit = CONFIG.getint("dsh", "attach_max_images", fallback=2)
    if limit <= 0:
        return []
    edge = CONFIG.getint("ai", "image_max_edge", fallback=media.DEFAULT_EDGE)

    hashes: list[str] = []
    for _author, content, _ts, _is_bot in fresh:
        for h in IMG_HASH_RE.findall(content or ""):
            if h not in hashes:
                hashes.append(h)

    payloads = []
    for h in hashes[-limit:]:
        row = MEMORY.find_image(h)
        if not row:
            continue
        got = media.prepare(Path(row[0]), edge=edge)
        if got:
            payloads.append(got)
    return payloads


async def store_embedding(h: str, desc: str):
    """把图片描述算成向量存起来（失败就算了，查图时还会懒补）。"""
    model = CONFIG.get("ai", "embedding_model", fallback="").strip()
    if not desc or not model:
        return
    vecs = await media.embed_texts(
        [desc], CONFIG.get("ai", "base_url").strip(),
        CONFIG.get("ai", "api_key").strip(), model, timeout=8.0,
    )
    if vecs:
        MEMORY.set_image_emb(h, media.pack_vec(vecs[0]))


async def _ensure_embeddings(rows):
    """给还没算向量的图补一批 embedding（10 条一批，接口对批量有上限）。"""
    missing = [(h, desc) for h, _p, desc, emb in rows if emb is None and desc]
    if not missing:
        return rows
    model = CONFIG.get("ai", "embedding_model", fallback="").strip()
    if not model:
        return rows
    base = CONFIG.get("ai", "base_url").strip()
    key = CONFIG.get("ai", "api_key").strip()
    done = 0
    for start in range(0, len(missing), 10):
        chunk = missing[start:start + 10]
        vecs = await media.embed_texts([d for _h, d in chunk], base, key, model, timeout=8.0)
        if not vecs or len(vecs) != len(chunk):
            break  # 接口挂了就先这样，剩下的下次懒补
        for (h, _d), v in zip(chunk, vecs):
            MEMORY.set_image_emb(h, media.pack_vec(v))
        done += len(chunk)
    if done:
        log.info("补算图片向量 %s 张", done)
    return MEMORY.images_with_emb()


async def match_image(query: str):
    """按一句描述找库里最像的图，返回 (path, desc) 或 None。"""
    rows = MEMORY.images_with_emb()
    if not rows:
        return None
    threshold = CONFIG.getfloat("ai", "image_match_threshold", fallback=0.45)
    model = CONFIG.get("ai", "embedding_model", fallback="").strip()

    if model:
        rows = await _ensure_embeddings(rows)
        vecs = await media.embed_texts(
            [query], CONFIG.get("ai", "base_url").strip(),
            CONFIG.get("ai", "api_key").strip(), model, timeout=6.0,
        )
        if vecs:
            best, best_score = None, -1.0
            for _h, path, desc, emb in rows:
                if not emb:
                    continue
                score = media.cosine(vecs[0], media.unpack_vec(emb))
                if score > best_score:
                    best, best_score = (path, desc), score
            if best and best_score >= threshold:
                return best, best_score
            log.info("按描述找图没找到：%r（最高 %.2f < %.2f）", query, best_score, threshold)
            return None

    # 兜底：字面二元组匹配
    best, best_score = None, -1.0
    for _h, path, desc, _emb in rows:
        score = media.local_score(query, desc or "")
        if score > best_score:
            best, best_score = (path, desc), score
    if best and best_score >= 0.5:
        return best, best_score
    log.info("按描述找图没找到（字面）：%r（最高 %.2f）", query, best_score)
    return None


async def search_images(query: str, limit: int = 5):
    """按描述/情绪搜图库，返回 top-N（含 hash/desc/scope/score）。"""
    limit = max(1, min(int(limit), 20))
    rows = MEMORY.images_with_emb()
    if not rows:
        return []
    meta = {h: (d, s) for h, d, s in MEMORY.image_library(1000)}
    model = CONFIG.get("ai", "embedding_model", fallback="").strip()
    results = []
    if model:
        rows = await _ensure_embeddings(rows)
        vecs = await media.embed_texts(
            [query], CONFIG.get("ai", "base_url").strip(),
            CONFIG.get("ai", "api_key").strip(), model, timeout=8.0,
        )
        if vecs:
            for h, _path, desc, emb in rows:
                if not emb:
                    continue
                score = media.cosine(vecs[0], media.unpack_vec(emb))
                d, s = meta.get(h, (desc, ""))
                results.append({"hash": h, "desc": d or desc or "", "scope": s or "", "score": score})
    if not results:
        for h, _path, desc, _emb in rows:
            d, s = meta.get(h, (desc, ""))
            results.append({
                "hash": h, "desc": d or desc or "", "scope": s or "",
                "score": media.local_score(query, desc or ""),
            })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


async def resolve_image(token: str):
    """`[[img:<8位哈希>]]` 精确找；`[[img:<描述>]]` 走匹配。返回 (path, desc) 或 None。"""
    t = token.strip()
    if re.fullmatch(r"[0-9a-f]{8}", t):
        row = MEMORY.find_image(t)
        if row:
            return row[0], row[1]
    got = await match_image(t)
    if not got:
        return None
    (path, desc), score = got
    log.info("按描述找到图：%r → %s（%.2f）", t, (desc or "")[:24], score)
    return path, desc


def last_spoke_hint(group: str) -> str:
    """告诉模型自己刚才说过什么，避免复读。"""
    last = MEMORY.last_bot_message(group)
    if not last:
        return ""
    ago = int(time.time() - last[1])
    return f"\n你 {ago} 秒前刚说过：「{last[0][:40]}」，这次别再重复那个说法或那个话题。"


async def ask_dsh(group: str, instruction: str, who: str = "", message=None,
                  with_images: bool = False) -> str | None:
    """所有"要不要说话、说什么"都问 DSH。返回 None 表示这次不说话。"""
    if BRIDGE is None:
        return None
    key = f"sid:{group}"
    await maybe_compact(group)   # 窗口到 50 条先压成摘要，再取本轮增量
    fresh = _fresh_rows(group)
    prompt = build_dsh_prompt(group, instruction, who, message, fresh=fresh)
    images = collect_prompt_images(fresh) if with_images else []
    try:
        text = await BRIDGE.prompt(
            group, prompt,
            resume_sid=MEMORY.get_state(key),
            images=images,
        )
        # 这一批消息已经进了 DSH 会话，游标才推进；失败不推进，下一轮重发
        LAST_SEEN_ID[group] = PENDING_SEEN.pop(group, LAST_SEEN_ID.get(group, 0))
        sid = BRIDGE.sessions.get(group)
        if sid and MEMORY.get_state(key) != sid:
            MEMORY.set_state(key, sid)
        if images:
            log.info("DSH 带图 %s 张（共 %d 字节）group=%s",
                     len(images), sum(len(d) for _m, d in images), group[:8])
    except Exception:
        PENDING_SEEN.pop(group, None)  # 没送进去，下一轮重发这一批
        log.exception("DSH 桥调用失败")
        return None
    if not text:
        return None
    if text.strip().upper().startswith("SKIP"):
        return None
    return text


# ---------------- 心跳：群安静时自己起话头 ----------------

async def housekeeping_loop():
    """每天清一次 7 天前的消息（以前只在启动时清）。"""
    while True:
        await asyncio.sleep(12 * 3600)
        try:
            MEMORY.prune(days=7)
            log.info("清理完成：%s", MEMORY.stats())
        except Exception:
            log.exception("清理出错")


HEARTBEAT_INSTRUCTION = (
    "群里冷场了，没人说话。你是负责暖场的那个。"
    "用下面这条由头起一个话头：一句具体的、能接的话——问一句后续、吐槽一句、说个小发现。"
    "不要播报新闻，不要解释材料从哪来。"
    "只发一句，不要自问自答，不要一次发好几条，不要问「在吗」「有人吗」。"
    "由头跟群里最近聊的对不上，宁可输出 SKIP。"
)


SUMMARY_INTERVAL = 1800
SUMMARY_MIN_MESSAGES = 12


async def summarize_group(group: str) -> int:
    """把一批群消息压成 1-3 条事件记忆，存库；失败不推进游标。"""
    if BRAIN is None:
        return 0
    since = float(MEMORY.get_state(f"mem:{group}") or 0)
    rows = MEMORY.messages_after(group, since, limit=80)
    if len(rows) < SUMMARY_MIN_MESSAGES:
        return 0
    lines = []
    for author, content, ts, _ in rows:
        clock = datetime.fromtimestamp(ts).strftime("%H:%M")
        lines.append(f"[{clock}] {author}: {(content or '').strip()[:160]}")
    system = (
        "你是群聊记录员。从下面的群聊里提取 1-3 条值得以后记住的群内事件/约定/变化"
        "（谁做了什么、群里发生了什么），每条一句话，只写事实，不评价、不编造，"
        "不要 Markdown，不要编号。没有就只输出 NONE。"
    )
    try:
        text = await BRAIN.complete(
            system, "群聊：\n" + "\n".join(lines),
            temperature=0.2, max_tokens=240, enable_thinking=False,
        )
    except Exception:
        log.exception("群记忆总结调用失败 group=%s", group[:8])
        return 0
    events = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if not line or line.upper() == "NONE":
            continue
        events.append(line)
    if len(events) == 1:
        parts = [p.strip() for p in events[0].split("。") if p.strip()]
        if len(parts) > 1:
            events = parts
    for e in events[:3]:
        MEMORY.add_memory(group, e)
    MEMORY.set_state(f"mem:{group}", str(rows[-1][2]))
    return len(events[:3])


async def memory_loop():
    await asyncio.sleep(5)
    while True:
        try:
            for group in MEMORY.known_groups():
                n = await summarize_group(group)
                if n:
                    log.info("群记忆 +%d 条 group=%s", n, group[:8])
        except Exception:
            log.exception("群记忆总结出错")
        await asyncio.sleep(SUMMARY_INTERVAL)


def _warm_state(group: str) -> dict:
    raw = MEMORY.get_state(f"warm:{group}")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_warm_state(group: str, st: dict):
    try:
        MEMORY.set_state(f"warm:{group}", json.dumps(st, ensure_ascii=False))
    except Exception:
        log.exception("保存暖场状态失败 group=%s", group[:8])


async def _maybe_warmup(group: str):
    """够条件才暖场：有人在、冷场够久、不烦人、有由头。"""
    if group.startswith("dc:"):
        return  # 私聊不暖场
    now = time.time()
    st = _warm_state(group)
    if now < float(st.get("silent_until", 0) or 0):
        return
    if not MEMORY.group_flag(group, "proactive_ok"):
        return
    if 1 <= datetime.now().hour < 7:
        return

    idle = CONFIG.getint("behavior", "warmup_idle", fallback=1800)
    alive = CONFIG.getint("behavior", "warmup_alive", fallback=21600)
    cooldown = CONFIG.getint("behavior", "warmup_cooldown", fallback=10800)
    daily_max = CONFIG.getint("behavior", "warmup_daily_max", fallback=3)
    miss_stop = CONFIG.getint("behavior", "warmup_miss_stop", fallback=2)
    silent = CONFIG.getint("behavior", "warmup_silent", fallback=86400)

    last_act = MEMORY.last_activity(group)
    if now - last_act < idle:
        return                       # 还有人在说话，不用暖
    if now - last_act > alive:
        return                       # 太久没人，说了也没人看

    # 上一次暖场有没有人接：没人接记一笔，连着两次就歇一天
    pending = st.pop("pending_id", None)
    if pending is not None:
        if MEMORY.has_external_since(group, int(pending), SELF_NAMES):
            st["miss"] = 0
        else:
            st["miss"] = int(st.get("miss", 0) or 0) + 1
            if st["miss"] >= miss_stop:
                st["silent_until"] = now + silent
                st["miss"] = 0
                log.info("暖场连着没人接，静默 %d 小时 group=%s",
                         silent // 3600, group[:8])

    if now < float(st.get("silent_until", 0) or 0):
        _save_warm_state(group, st)
        return
    if now - float(st.get("last_ts", 0) or 0) < cooldown:
        _save_warm_state(group, st)
        return

    today = datetime.now().strftime("%Y-%m-%d")
    if st.get("day") != today:
        st["day"] = today
        st["count"] = 0
    if int(st.get("count", 0) or 0) >= daily_max:
        _save_warm_state(group, st)
        return

    last_bot = MEMORY.last_bot_message(group)
    if last_bot and now - last_bot[1] < 600:
        _save_warm_state(group, st)
        return

    stuff = await material.collect(group, MEMORY)
    if not stuff:
        log.info("暖场：没有合格由头，跳过 group=%s", group[:8])
        st["last_ts"] = now - cooldown + 1800
        _save_warm_state(group, st)
        return

    instruction = (HEARTBEAT_INSTRUCTION + last_spoke_hint(group)
                   + f"\n\n手上的由头：\n{stuff}")
    cut_id = MEMORY.max_id(group)
    text = await ask_dsh(group, instruction)
    if not text:
        st["last_ts"] = now - cooldown + 1800
        _save_warm_state(group, st)
        log.info("暖场：模型选择不说，半小时后再看 group=%s", group[:8])
        return

    await asyncio.sleep(_typing_delay(text))
    ok = await send_group(group, text, msg_id=None, since_id=cut_id)
    if ok:
        st["last_ts"] = now
        st["count"] = int(st.get("count", 0) or 0) + 1
        st["pending_id"] = MEMORY.max_id(group)
        log.info("暖场发出（今天第 %d 次）group=%s", st["count"], group[:8])
    else:
        st["last_ts"] = now - cooldown + 1800
        log.info("暖场发送失败 group=%s", group[:8])
    _save_warm_state(group, st)


async def heartbeat_loop():
    if not CONFIG.getboolean("behavior", "heartbeat_enabled", fallback=False):
        log.info("暖场已关闭（heartbeat_enabled=false）")
        return
    if CONFIG.get("behavior", "reply_policy", fallback="mention_only") != "full":
        log.info("reply_policy=mention_only，暖场不开口")
        return
    interval = CONFIG.getint("behavior", "heartbeat_interval", fallback=300)
    while True:
        await asyncio.sleep(interval)
        for group in MEMORY.known_groups():
            try:
                await _maybe_warmup(group)
            except Exception:
                log.exception("暖场出错 group=%s", group[:8])



async def start_bridge():
    global BRIDGE
    if not CONFIG.getboolean("dsh", "enabled", fallback=False):
        log.info("DSH 桥未启用")
        return
    bridge = DshBridge(
        CONFIG.get("dsh", "exe", fallback="~/.local/bin/dsh"),
        CONFIG.get("dsh", "profile", fallback="qq"),
        CONFIG.get("dsh", "agent_cwd", fallback="/opt/dsh-bot/agent"),
        CONFIG.get("dsh", "api_key", fallback=""),
    )
    try:
        await bridge.start()
        BRIDGE = bridge
    except Exception:
        log.exception("DSH 桥启动失败，@ 回复走本地模型")


# ---------------- 启动 ----------------

async def main():
    global CONFIG, MEMORY, BRAIN, CLIENT

    CONFIG = configparser.ConfigParser()
    CONFIG.read(BASE_DIR / "config.ini", encoding="utf-8")

    appid = CONFIG.get("bot", "appid").strip()
    appsecret = CONFIG.get("bot", "appsecret").strip()
    host = CONFIG.get("bot", "host", fallback="127.0.0.1").strip()
    port = CONFIG.getint("bot", "port", fallback=3704)
    db_path = CONFIG.get("bot", "db_path", fallback=str(BASE_DIR / "data.db"))

    MEMORY = Memory(db_path)
    MEMORY.prune(days=7)
    # 首次启用压缩机制时，窗口从最近 20 条开始，别把几天的旧流水压进摘要
    for _g in MEMORY.known_groups():
        if MEMORY.get_state(f"ctxbase:{_g}") is None:
            MEMORY.set_state(f"ctxbase:{_g}", MEMORY.window_base(_g, keep=CTX_KEEP))
    BRAIN = Brain(
        {
            "base_url": CONFIG.get("ai", "base_url").strip(),
            "api_key": CONFIG.get("ai", "api_key").strip(),
            "model": CONFIG.get("ai", "model").strip(),
            "temperature": CONFIG.getfloat("ai", "temperature", fallback=0.9),
            "max_tokens": CONFIG.getint("ai", "max_tokens", fallback=260),
            "name": CONFIG.get("bot", "name", fallback="DSH"),
        },
        BASE_DIR / "persona.md",
    )

    CLIENT = QQClient(appid, appsecret, host, port, Callbacks())

    send_token = CONFIG.get("send", "token", fallback="").strip()

    @CLIENT.app.app.post("/send")
    async def send_api(request: Request):
        """给 DSH 用的出站口：POST {group, text}，需要 token。"""
        token = request.headers.get("x-token") or request.query_params.get("token")
        if not send_token or token != send_token:
            return {"ok": False, "error": "unauthorized"}
        body = await request.json()
        group = (body.get("group") or "").strip()
        text = (body.get("text") or "").strip()
        image = (body.get("image") or "").strip()   # 图片哈希前 8 位，可单独发图
        if image:
            row = await resolve_image(image)   # 8 位哈希精确找，否则按描述匹配
            if not row:
                return {"ok": False, "error": "image not found"}
            ok = await media.upload_and_send(
                CLIENT, group, Path(row[0]),
                base_url=CONFIG.get("bot", "image_base", fallback=""),
                public_dir=Path(CONFIG.get("bot", "image_public_dir", fallback="/var/www/images")),
            )
            return {"ok": ok, "sent": "image", "desc": row[1]}
        if not group or not text:
            return {"ok": False, "error": "group and text required"}
        ok = await send_group(group, text, msg_id=None)
        return {"ok": ok, **({} if ok else {"error": "send failed"})}

    @CLIENT.app.app.post("/send_dc")
    async def send_dc_api(request: Request):
        """出站私信：POST {openid, text}，需要 token。"""
        token = request.headers.get("x-token") or request.query_params.get("token")
        if not send_token or token != send_token:
            return {"ok": False, "error": "unauthorized"}
        body = await request.json()
        openid = (body.get("openid") or "").strip()
        text = (body.get("text") or "").strip()
        if not openid or not text:
            return {"ok": False, "error": "openid and text required"}
        try:
            await CLIENT.dc.send_message(openid, text)
        except Exception as exc:
            log.exception("私信发送失败")
            return {"ok": False, "error": str(exc)}
        _record_own("dc:" + openid, text)
        return {"ok": True}

    @CLIENT.app.app.get("/image_scope")
    async def image_scope_list(request: Request):
        """看图库描述 + 适用范围；token 保护。"""
        token = request.headers.get("x-token") or request.query_params.get("token")
        if not send_token or token != send_token:
            return {"ok": False, "error": "unauthorized"}
        rows = MEMORY.image_library(50)
        return {
            "images": [
                {"hash": h[:8], "desc": d, "scope": s}
                for h, d, s in rows
            ]
        }

    @CLIENT.app.app.get("/image_search")
    async def image_search(request: Request):
        """按描述/情绪搜图库：GET /image_search?q=摸鱼&limit=5，token 保护。"""
        token = request.headers.get("x-token") or request.query_params.get("token")
        if not send_token or token != send_token:
            return {"ok": False, "error": "unauthorized"}
        q = (request.query_params.get("q") or "").strip()
        if not q:
            return {"ok": False, "error": "q required"}
        try:
            limit = int(request.query_params.get("limit", "5"))
        except ValueError:
            limit = 5
        results = await search_images(q, limit)
        return {
            "ok": True,
            "query": q,
            "results": [
                {
                    "hash": r["hash"][:8],
                    "desc": r["desc"],
                    "scope": r["scope"],
                    "score": round(r["score"], 3),
                }
                for r in results
            ],
        }

    @CLIENT.app.app.get("/image_sources")
    async def image_sources(request: Request):
        """返回图源登记表；后加的源先存入口，不急着接。"""
        token = request.headers.get("x-token") or request.query_params.get("token")
        if not send_token or token != send_token:
            return {"ok": False, "error": "unauthorized"}
        try:
            sources = json.loads((BASE_DIR / "image_sources.json").read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "sources": sources}

    @CLIENT.app.app.post("/image_scope")
    async def image_scope_set(request: Request):
        """改某张图的适用范围：POST {image, scope}；image 可传 8 位哈希或描述。"""
        token = request.headers.get("x-token") or request.query_params.get("token")
        if not send_token or token != send_token:
            return {"ok": False, "error": "unauthorized"}
        body = await request.json()
        image = (body.get("image") or "").strip()
        scope = (body.get("scope") or "").strip()
        if not image:
            return {"ok": False, "error": "image required"}
        got = await resolve_image(image)
        if not got:
            return {"ok": False, "error": "image not found"}
        row = MEMORY.image_by_path(got[0])
        if not row:
            return {"ok": False, "error": "image record not found"}
        MEMORY.set_image_scope(row[0], scope)
        log.info("图适用范围 %s：%s", row[0][:8], scope[:80])
        return {"ok": True, "hash": row[0][:8], "desc": got[1][:80], "scope": scope}

    @CLIENT.app.app.post("/debug_reply")
    async def debug_reply(request: Request):
        """本地测试口：喂一条消息，返回它会发的话；不往群里发。"""
        token = request.headers.get("x-token") or request.query_params.get("token")
        if not send_token or token != send_token:
            return {"ok": False, "error": "unauthorized"}
        body = await request.json()
        text = (body.get("text") or "").strip()
        task = (body.get("task") or "ambient").strip()
        if not text:
            return {"ok": False, "error": "text required"}
        if BRIDGE is None:
            return {"ok": False, "error": "bridge not ready"}
        if body.get("reset"):
            BRIDGE.sessions.pop("debug:persona", None)
        if task == "addressed":
            instruction = "最后一条是 深海渔民 @ 你，回他一句。"
        else:
            instruction = ("没人 @ 你。能接就接，别太保守；只有完全无关、"
                           "或者说了只会显得多余，才输出 SKIP。")
        prompt = build_dsh_prompt(
            "debug:persona", instruction,
            fresh=[("深海渔民", text, time.time(), False)],
        )
        try:
            reply = await BRIDGE.prompt("debug:persona", prompt)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "reply": reply}

    @CLIENT.app.app.get("/groups")
    async def groups_api():
        return {"groups": MEMORY.active_groups(minutes=60 * 24 * 7)}

    @CLIENT.app.app.middleware("http")
    async def log_webhook_headers(request, call_next):
        # 只为了核对 QQ 到底发不发签名头，确认后可以删
        if request.method == "POST" and request.url.path == "/":
            sig = request.headers.get("x-signature-ed25519")
            ts = request.headers.get("x-signature-timestamp")
            log.info("webhook 头 sig=%s ts=%s ua=%s",
                     (sig[:12] + "…") if sig else "无", ts or "无",
                     request.headers.get("user-agent", "?"))
        return await call_next(request)

    @CLIENT.app.app.get("/health")
    async def health():
        return {"ok": True, "service": "dsh-bot", "time": datetime.now().isoformat(timespec="seconds")}

    @CLIENT.app.app.get("/stats")
    async def stats():
        return MEMORY.stats()

    log.info("DSH Bot 启动 appid=%s %s:%s model=%s", appid, host, port, BRAIN.model)
    asyncio.create_task(start_bridge())
    asyncio.create_task(heartbeat_loop())
    asyncio.create_task(memory_loop())
    asyncio.create_task(housekeeping_loop())
    await CLIENT.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

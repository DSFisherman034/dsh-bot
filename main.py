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
IMG_SCOPE_RE = re.compile(r"\[\[img-scope:([^\[\]\n|]{1,60})\|([^\[\]\n]{1,120})\]\]")
IMG_HASH_RE = re.compile(r"h=([0-9a-f]{8})")

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
LAST_BUBBLES: dict[str, list[int]] = {}
LAST_IMAGES: dict[str, list[int]] = {}


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
    try:
        if any(getattr(m, "is_you", False) for m in getattr(message, "mentions", []) or []):
            return True
        name = CONFIG.get("bot", "name", fallback="DSH").strip()
        if name and name in (message.content or ""):
            return True
        quote = getattr(message, "quote", None)
        if quote and quote.content and name and name in quote.content:
            return True
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
        elif is_bot_author:
            log.debug("机器人消息，仅记入上下文 group=%s", group[:8])
        elif CONFIG.get("behavior", "reply_policy", fallback="full") == "full":
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

        text = await ask_dsh(group, ambient_instruction(group))
        if not text:
            log.info("插话跳过[模型决定不说] group=%s", group[:8])
            return

        await asyncio.sleep(_typing_delay(text))
        await send_group(group, text, msg_id=None)
    except Exception:
        log.exception("插话回合出错 group=%s", group[:8])
    finally:
        AMBIENT_PENDING.pop(group, None)
        # 这一轮处理期间又来了新消息 → 再开一个窗口
        if MEMORY.last_activity(group) > LAST_PROMPT_TS.get(group, 0):
            schedule_ambient(group)


def ambient_instruction(group: str) -> str:
    hint = ""
    last = MEMORY.last_bot_message(group)
    if last:
        ago = int(time.time() - last[1])
        hint = f"你 {ago} 秒前刚说过：「{last[0][:40]}」。刚说过的话不要重复。\n"

    rows = MEMORY.recent_messages(group, minutes=30, limit=6)
    last_other = next((r for r in reversed(rows) if not r[3]), None)
    bot_names = {"Rantindom机器人", "神", "都报"}
    followup = False
    if last and last_other and time.time() - last[1] < 180:
        author, content = last_other[0], (last_other[1] or "")
        if author not in bot_names:
            followup = any(k in content for k in (
                "?", "？", "什么", "怎么", "为什么", "哪", "谁", "多少",
                "是不是", "能不能", "有没有", "吗",
            ))
    if followup:
        return (
            "最后一条明显是在接你刚才的话、或者在问你，**必须回**，不要 SKIP。"
            "顺着对方的问题答，别装死。\n" + hint
        )
    return (
        "没人 @ 你。能接就接，别太保守：话题跟你有关、你能接一句有意思的、或者对方在等你接，就说；"
        "只有完全无关、或者说了只会显得多余，才输出 SKIP。"
        + ("\n" + hint if hint else "")
    )


async def reply_group(group, task, who, msg_id=None, message=None):
    # 被 @ 时才把图本身交给 DSH：它真的能看，插话/暖场就够了文字描述
    with_images = CONFIG.getboolean("dsh", "attach_images", fallback=True)
    text = await ask_dsh(group, f"最后一条是 {who} @ 你，回他一句。", who, message,
                         with_images=with_images)

    if not text:  # 只有桥挂了才会走到这里
        rows = MEMORY.recent_messages(group, minutes=90)
        text = await BRAIN.respond(rows, task, who)
    if not text:
        log.info("模型选择沉默 task=%s", task)
        return
    await asyncio.sleep(_typing_delay(text))
    await send_group(group, text, msg_id=msg_id)


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


async def send_group(group, text, msg_id=None) -> bool:
    """返回是否至少发出了一条。"""
    text = await apply_image_scopes(text)
    if not text.strip():
        return False
    limit = CONFIG.getint("behavior", "max_reply_chars", fallback=300)
    bubbles = split_bubbles(text, limit)
    sent = False
    for i, part in enumerate(bubbles):
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
                        sent = True
                    MEMORY.mark_bot_spoke(group)
                    log.info("群发图 %s: %s", group[:8], row[1][:30])
                else:
                    log.info("想发的图没找到：%r", m.group(1))
                part = IMG_MARKER.sub("", part).strip()
                if not part:
                    continue

            if "qqbot-at-user" in part:
                # QQ 的 @ 标签只在 markdown 消息里渲染
                await CLIENT.group.send_markdown(group, part, msg_id=msg_id if i == 0 else None)
            else:
                await CLIENT.group.send_message(group, part, msg_id=msg_id if i == 0 else None)
            _record_own(group, part)
            MEMORY.mark_bot_spoke(group)
            sent = True
            log.info("群回复 %s: %s", group[:8], part[:80])
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
            await asyncio.sleep(min(3.0, gap))
    if sent:
        hist = LAST_BUBBLES.setdefault(group, [])
        hist.append(len(bubbles))
        del hist[:-6]
        ih = LAST_IMAGES.setdefault(group, [])
        ih.append(1 if "[[img:" in (text or "") else 0)
        del ih[:-6]
    return sent


def _record_own(group, text):
    MEMORY.add_message(
        group, None, CONFIG.get("bot", "name", fallback="DSH"), "self", text, is_bot=True
    )


def _typing_delay(text: str) -> float:
    base = 0.9 + min(len(text), 120) * 0.022
    return min(4.0, base + random.uniform(0.0, 1.1))


def _ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return time.time()




def _fresh_rows(group: str) -> list:
    """这一轮要喂给 DSH 的增量转录（上次 prompt 之后的消息）。"""
    since = LAST_PROMPT_TS.get(group, 0)
    rows = MEMORY.recent_messages(group, minutes=180, limit=20)
    return [r for r in rows if r[2] > since] or rows[-8:]


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
            "不用于自己被调侃、严肃、安慰、陌生人）：\n" + "\n".join(lines))


def build_dsh_prompt(group: str, instruction: str, who: str = "", message=None,
                     fresh=None) -> str:
    """把增量转录 + 本次任务指令拼成提示词。"""
    if fresh is None:
        fresh = _fresh_rows(group)
    LAST_PROMPT_TS[group] = time.time()

    lines = []
    for author, content, ts, is_bot in fresh:
        clock = datetime.fromtimestamp(ts).strftime("%H:%M")
        name = CONFIG.get("bot", "name", fallback="DSH") if is_bot else author
        lines.append(f"[{clock}] {name}: {(content or '').strip()[:200]}")

    head = "以下是群里刚才的消息（时间 24 小时制）：\n" + "\n".join(lines) if lines else "群里还没有聊天记录。"
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
        "选图前先看上面的适用范围；用户说「错误发图」或解释某张图的用法时，"
        "可以另起一行写 [[img-scope:图的一句话描述|适用范围]] 更新图库，这条不会发出去。"
    )
    lib = image_library_hint()
    lib_block = (lib + "\n\n") if lib else ""
    return (
        f"{mem_block}{head}\n\n{instruction}\n\n{meme_budget_hint(group)}\n\n"
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
    fresh = _fresh_rows(group)
    prompt = build_dsh_prompt(group, instruction, who, message, fresh=fresh)
    images = collect_prompt_images(fresh) if with_images else []
    try:
        text = await BRIDGE.prompt(
            group, prompt,
            resume_sid=MEMORY.get_state(key),
            images=images,
        )
        sid = BRIDGE.sessions.get(group)
        if sid and MEMORY.get_state(key) != sid:
            MEMORY.set_state(key, sid)
        if images:
            log.info("DSH 带图 %s 张（共 %d 字节）group=%s",
                     len(images), sum(len(d) for _m, d in images), group[:8])
    except Exception:
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


HEARTBEAT_NEXT: dict[str, float] = {}

HEARTBEAT_INSTRUCTION = (
    "群里冷场了，没人说话。你是负责暖场的那个。"
    "**不要接着上面聊过的旧话题**，直接起一个新的：一句具体的观点、一个小发现，或者一个能接的问题。"
    "只发一句，不要自问自答，不要一次发好几条，不要问「在吗」「有人吗」。"
    "语气像随口一句，不要像开场白。不想说就输出 SKIP。"
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


async def heartbeat_loop():
    interval = CONFIG.getint("behavior", "heartbeat_interval", fallback=240)
    idle = CONFIG.getint("behavior", "heartbeat_idle", fallback=900)
    cooldown = CONFIG.getint("behavior", "heartbeat_cooldown", fallback=1500)
    if not CONFIG.getboolean("behavior", "heartbeat_enabled", fallback=False):
        log.info("心跳/暖场已关闭（heartbeat_enabled=false），等重新设计话题机制")
        return
    if CONFIG.get("behavior", "reply_policy", fallback="mention_only") != "full":
        log.info("reply_policy=mention_only，心跳不主动开口")
        return

    while True:
        await asyncio.sleep(interval)
        try:
            for group in MEMORY.known_groups():
                if time.time() < HEARTBEAT_NEXT.get(group, 0):
                    continue
                if not MEMORY.group_flag(group, "proactive_ok"):
                    continue
                last_bot = MEMORY.last_bot_message(group)
                if last_bot and time.time() - last_bot[1] < cooldown:
                    continue
                if time.time() - MEMORY.last_activity(group) < idle:
                    continue
                stuff = await material.collect(group, MEMORY)
                instruction = HEARTBEAT_INSTRUCTION + last_spoke_hint(group)
                if stuff:
                    instruction += f"\n\n手上有个现成的材料，你可以用它起话头，也可以不聊它：\n{stuff}"
                    log.info("暖场材料 group=%s: %s", group[:8], stuff[:60])
                text = await ask_dsh(group, instruction)
                if not text:
                    # 冷场但模型不想说：半小时后再问，别每 2 分钟烧一次
                    HEARTBEAT_NEXT[group] = time.time() + 1800
                    log.info("心跳：模型选择不说，30 分钟后再试 group=%s", group[:8])
                    continue
                await asyncio.sleep(_typing_delay(text))
                if not await send_group(group, text, msg_id=None):  # 主动消息
                    # 发不进去就别每 2 分钟白烧一轮 agent
                    HEARTBEAT_NEXT[group] = time.time() + 1800
                    log.info("心跳发送失败，30 分钟后再试 group=%s", group[:8])
        except Exception:
            log.exception("心跳循环出错")



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

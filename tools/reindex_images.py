#!/usr/bin/env python3
"""给图库里的表情包补「适用场合」，并按 描述+适用 重算 embedding（v0.68）。

为什么要这个：
    检索用的向量原来只算 `desc`（一句画面描述）。短句区分度不够——
    「他说要请我吃顿好的」对「捧着蓝碗干饭」只有 0.42（阈值 0.45，直接漏）。
    把「什么时候该发这张」也写进去，语义才连得上。

用法（读写 /opt/dsh-bot/data.db）：
    /opt/qqbot/venv/bin/python dsh-bot/tools/reindex_images.py --dry-run
    /opt/qqbot/venv/bin/python dsh-bot/tools/reindex_images.py

--dry-run 只打印将要写入的文本和条数，不改 use_when、不算向量
（库里缺 use_when 列的话会先补一个空列）。
"""

import argparse
import asyncio
import configparser
import sys
from pathlib import Path

CONFIG_PATH = Path("/opt/dsh-bot/config.ini")

# 图库里「大肥鱼表情包」的适用场合。按 desc 前缀匹配（哈希在不同机器上一样，但这里可读）。
# 写法：用群里真正会说的话，别写抽象标签——embedding 匹配的是字面语义。
USE_WHEN = [
    ("得意闭眼拳头", "被夸了、事情办成了、赢了、得意、满意、好耶、炫耀、开心到握拳"),
    ("捧着蓝碗干饭", "饭点到了、吃饭、干饭、饿了、馋了、请客、请我吃顿好的、下馆子、蹭饭、报菜名、想吃好的、看别人吃"),
    ("气鼓鼓叉腰", "生气了、被惹毛、哼、不服、抗议、闹脾气、气鼓鼓、你再说一遍"),
    ("疑惑举手", "没听懂、疑惑、问号、什么意思、看不懂、懵了、你在说什么、离谱、真的假的、不信、怀疑"),
    ("吓到大叫", "吓一跳、震惊、啊、卧槽、突然、意外、被吓到、离谱到叫出来"),
    ("晕了，螺旋眼", "崩溃、脑子转不动、受不了、心累、晕、无语到晕、信息量太大、绕晕了"),
    ("双手比心卖萌", "比心、喜欢、可爱、谢谢、撒娇、卖萌、爱你、开心"),
    ("害羞捂脸", "害羞、脸红、不好意思、被夸、腼腆、别说了、捂脸"),
    ("爱心眼花痴", "花痴、心动、好可爱、星星眼、喜欢、犯花痴、看呆了"),
    ("抱小枕头睡觉", "困了、困得睁不开眼、睁不开眼、打哈欠、睡觉、晚安、熬夜、打盹、想睡、撑不住、几点了还不睡"),
    ("早上好挥手", "早上好、早安、你好、欢迎、打招呼、新的一天、起床"),
    ("摸鱼躺平", "摸鱼、躺平、不想干活、下班、偷懒、摆烂、上班、打工、想放假"),
    ("无语死鱼脸", "无语、无奈、叹气、不想说话、沉默、离谱、服了、说不出话、懒得理"),
    ("哭着双手合十求饶", "求饶、对不起、我错了、委屈、哭、饶了我、别骂了、求放过"),
    ("侧目嫌弃挥手", "嫌弃、鄙视、不想理你、不理你了、懒得理、哼、吐槽、走开、傻瓜、看不上"),
    ("加班崩溃", "加班、工作累、熬夜干活、咖啡、撑不住、deadline、肝、打工人"),
    ("惊吓跳起", "看到离谱的、震惊、哇、吓到、辣眼睛、不敢看、瞳孔地震"),
    ("托腮思考", "思考、卡住、想不明白、听不懂、装死、等他解释、慢半拍、让我想想、加载中"),
    ("眨眼竖大拇指", "好的、收到、没问题、点赞、赞、同意、可以、OK、棒、谢谢、多谢、感谢"),
    ("双手交叉摇头", "不行、不要、拒绝、摇头、不可以、免谈、想得美"),
    ("躲在鲸鱼尾巴后面", "偷看、害羞、躲起来、暗中观察、不敢说话、围观、冒个泡"),
    ("被夸飘了", "被夸、飘了、得意、开心、夸奖、谢谢、夸我、虚荣"),
    ("猫爪手势卖萌", "卖萌、可爱、猫爪、嘿嘿、装乖、撒娇、喵"),
    ("捧着小钱包", "要钱、穷、求打赏、给点零食、工资、红包、打钱、没钱了"),
    # 不是大肥鱼，但当初标过 scope（熟人互损用）
    ("一个黄色圆形表情，头顶蓝黄角状装饰", "嘲笑、损人、看笑话、阴阳怪气、朋友互损；别对自己人、严肃场合用"),
]


def load_config():
    cfg = configparser.ConfigParser()
    if not cfg.read(CONFIG_PATH, encoding="utf-8"):
        sys.exit(f"读不到 {CONFIG_PATH}")
    return cfg


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from memory import Memory          # noqa: E402
    import media                       # noqa: E402

    mem = Memory(cfg.get("bot", "db_path", fallback="/opt/dsh-bot/data.db"))
    model = cfg.get("ai", "embedding_model", fallback="").strip()
    base = cfg.get("ai", "base_url").strip()
    key = cfg.get("ai", "api_key").strip()
    if not model:
        sys.exit("config.ini 里没有 [ai] embedding_model")

    # 1) 写 use_when
    applied = 0
    for prefix, use in USE_WHEN:
        row = mem.conn.execute(
            "SELECT hash, desc FROM images WHERE desc LIKE ? LIMIT 1", (prefix + "%",)
        ).fetchone()
        if not row:
            print(f"!! 没找到描述以「{prefix}」开头的图，跳过")
            continue
        applied += 1
        print(f"{row[0][:8]}  {row[1][:18]}…  →  {use[:40]}…")
        if not args.dry_run:
            mem.set_image_use(row[0], use)
    print(f"适用场合：{applied}/{len(USE_WHEN)} 张")

    # 2) 重算 embedding
    rows = mem.images_with_emb()
    print(f"要重算向量的图：{len(rows)} 张")
    if args.dry_run:
        for h, _p, desc, _emb, use in rows:
            print(f"  {h[:8]}  {media_embed_text(desc, use)[:60]}…")
        return

    done = 0
    for start in range(0, len(rows), 10):
        chunk = rows[start:start + 10]
        texts = [media_embed_text(desc, use) for _h, _p, desc, _emb, use in chunk]
        vecs = await media.embed_texts(texts, base, key, model, timeout=20.0)
        if not vecs or len(vecs) != len(chunk):
            print(f"!! embedding 失败（第 {start} 批），中止")
            break
        for (h, _p, _d, _e, _u), v in zip(chunk, vecs):
            mem.set_image_emb(h, media.pack_vec(v))
        done += len(chunk)
        print(f"  已算 {done}/{len(rows)}")
    print(f"完成：{done} 张向量已更新")


def media_embed_text(desc, use_when):
    """和 main.image_embed_text 保持一致（工具独立可跑，不 import main）。"""
    d = (desc or "").strip()
    u = (use_when or "").strip()
    return f"{d}；适用：{u}" if u else d


if __name__ == "__main__":
    asyncio.run(main())

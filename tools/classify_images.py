#!/opt/qqbot/venv/bin/python
"""给图库里的老图补分类：表情包 / 截图 / 广告 / 照片 / 其他。

2026-11-03 起新图入库前会先分类；这个脚本只用来处理升级前已经进库的图。
只有 kind 为 NULL 的图会被处理（升级前的图都还没有 kind），--force 可重判全部。

用法：
  /opt/qqbot/venv/bin/python tools/classify_images.py --dry-run          # 只列表，不调模型
  /opt/qqbot/venv/bin/python tools/classify_images.py                    # 分类 kind=NULL 的图
  /opt/qqbot/venv/bin/python tools/classify_images.py --since 2026-11-01 # 只处理这天之后的
  /opt/qqbot/venv/bin/python tools/classify_images.py --all --force      # 全部重判
"""

import argparse
import asyncio
import configparser
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import media  # noqa: E402
from memory import Memory  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("classify")


async def one(h, path, cfg, sem):
    async with sem:
        if not path or not Path(path).exists():
            return h, "其他", ""
        kind, desc = await media.analyze(
            Path(path),
            cfg.get("ai", "base_url").strip(),
            cfg.get("ai", "api_key").strip(),
            cfg.get("ai", "vision_model", fallback="qwen3.7-flash").strip(),
            limit=cfg.getint("ai", "vision_desc_chars", fallback=media.MAX_DESC),
            edge=cfg.getint("ai", "image_max_edge", fallback=media.DEFAULT_EDGE),
        )
        return h, kind, desc


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data.db"))
    ap.add_argument("--since", default="", help="只处理 ts >= 这天的图，格式 YYYY-MM-DD")
    ap.add_argument("--all", action="store_true", help="包括已有 kind 的图（配合 --force）")
    ap.add_argument("--force", action="store_true", help="重判已有 kind 的图")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = configparser.ConfigParser()
    cfg.read(ROOT / "config.ini", encoding="utf-8")
    mem = Memory(args.db)

    since_ts = 0.0
    if args.since:
        since_ts = datetime.strptime(args.since, "%Y-%m-%d").timestamp()

    rows = mem.conn.execute(
        "SELECT hash, path, kind, ts FROM images ORDER BY ts"
    ).fetchall()
    todo = []
    for h, path, kind, ts in rows:
        if (since_ts and (ts or 0) < since_ts) or (args.limit and len(todo) >= args.limit):
            continue
        if kind and not args.force:
            continue
        todo.append((h, path))

    if args.dry_run:
        for h, path in todo:
            print(h[:8], path)
        print(f"待分类 {len(todo)} 张（dry-run，未调模型）")
        return

    sem = asyncio.Semaphore(max(1, args.concurrency))
    started = time.time()
    counts = {}
    for i, (h, path) in enumerate(todo, 1):
        hh, kind, desc = await one(h, path, cfg, sem)
        mem.set_image_kind(hh, kind)
        counts[kind] = counts.get(kind, 0) + 1
        log.info("[%d/%d] %s %s %s", i, len(todo), hh[:8], kind, (desc or "")[:40])
    log.info("完成 %d 张，用时 %.0fs，分布：%s", len(todo), time.time() - started, counts)
    log.info("图库现有表情包：%d 张", len(mem.image_library(100000)))


if __name__ == "__main__":
    asyncio.run(main())

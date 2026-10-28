#!/opt/qqbot/venv/bin/python
"""把大肥鱼表情包导入 dsh-bot 图库。

源：
- nekowhale（322 张，CC BY-NC 4.0，作者：可以叫我云大人 / kiriyuki.com）
- dsh-meme 内置大肥鱼包（24 张，MIT 代码；图包许可见包内）

前置：把 nekowhale.json、nekowhale-manifest.json、dsh-meme 包解压到 import/。
运行：danger-full-access（要写 /opt/dsh-bot/images 和 data.db）
"""

import asyncio
import configparser
import hashlib
import json
import sys
import time
import urllib.parse
from pathlib import Path

import httpx

sys.path.insert(0, "/opt/dsh-bot")
from memory import Memory  # noqa: E402
import media  # noqa: E402

BASE = Path("/home/dsfisherman/dsh/dsh-bot")
IMP = BASE / "import"
DEST = Path("/opt/dsh-bot/images")
DB = "/opt/dsh-bot/data.db"
CONFIG = Path("/opt/dsh-bot/config.ini")
UA = "dsh-bot-sticker-import/1.0 (+https://github.com/DSFisherman034/dsh-bot)"


async def fetch(client, url):
    for attempt in range(4):
        try:
            r = await client.get(url)
            if r.status_code == 200 and r.content:
                return r.content
            print("HTTP", r.status_code, url[:120], flush=True)
        except Exception as exc:
            print("ERR", repr(exc)[:160], url[:120], flush=True)
        await asyncio.sleep(1.0 + attempt)
    return None


async def import_nekowhale(client, mem, embeds):
    meta = json.loads((IMP / "nekowhale.json").read_text(encoding="utf-8"))
    manifest = json.loads((IMP / "nekowhale-manifest.json").read_text(encoding="utf-8"))
    by_id = {s["id"]: s for s in meta["stickers"]}
    packs = {"大肥鱼", "大肥鱼和GPT娘"}
    items = [x for x in manifest["items"] if x.get("pack") in packs]
    print(f"nekowhale: {len(items)} items, meta {len(by_id)}", flush=True)
    sem = asyncio.Semaphore(8)
    done = {"ok": 0, "skip": 0, "fail": 0}

    async def one(item):
        async with sem:
            m = by_id.get(item["id"], {})
            pack, filename = item["pack"], item["file"]
            url = (
                "https://kiriyuki.com/media/gif/"
                + urllib.parse.quote(pack) + "/"
                + urllib.parse.quote(filename)
                + "?h=" + item.get("hash", "")
            )
            data = await fetch(client, url)
            if not data:
                done["fail"] += 1
                print("FAIL", item["id"], flush=True)
                return
            if hashlib.sha1(data).hexdigest()[:12] != item.get("hash"):
                print("HASH-MISMATCH", item["id"], flush=True)
            sha = hashlib.sha256(data).hexdigest()
            if mem.get_image(sha):
                done["skip"] += 1
                return
            ext = Path(filename).suffix.lower() or ".gif"
            path = DEST / f"{sha[:16]}{ext}"
            path.write_bytes(data)
            desc = (m.get("desc") or m.get("en") or m.get("text") or item["id"]).strip()
            scope = (
                f"大肥鱼表情包｜情绪={m.get('emotion','')}｜"
                f"标签={','.join((m.get('tags') or [])[:6])}｜"
                f"用法={m.get('use','')}｜方向={m.get('dir') or '单人'}｜"
                "来源=nekowhale（CC BY-NC 4.0，作者：可以叫我云大人 / kiriyuki.com）"
            )
            mem.put_image(sha, path, desc)
            mem.set_image_scope(sha, scope)
            embeds.append((
                sha,
                " ".join([
                    desc, "情绪：" + str(m.get("emotion", "")),
                    "标签：" + " ".join(m.get("tags") or []),
                    "用法：" + str(m.get("use", "")),
                    "梗：" + str(m.get("meme", "")),
                    "方向：" + str(m.get("dir", "")),
                ]),
            ))
            done["ok"] += 1
            if done["ok"] % 25 == 0:
                print("nekowhale imported", done, flush=True)

    await asyncio.gather(*(one(x) for x in items))
    print("nekowhale done", done, flush=True)


def import_dsh_meme(mem, embeds):
    import sqlite3
    root = IMP / "dsh-meme-pkg" / "package" / "memes" / "dafeiyu-001"
    db = sqlite3.connect(root / "index.db")
    rows = db.execute("select path, tag, file_name, file_hash, caption, keywords from memes").fetchall()
    db.close()
    ok = skip = fail = 0
    for rel, tag, file_name, _fh, caption, keywords in rows:
        src = root / rel
        if not src.exists():
            fail += 1
            print("MISSING", rel, flush=True)
            continue
        data = src.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if mem.get_image(sha):
            skip += 1
            continue
        ext = src.suffix.lower() or ".jpg"
        dest = DEST / f"{sha[:16]}{ext}"
        dest.write_bytes(data)
        desc = (caption or file_name).strip()
        scope = (
            f"大肥鱼表情包｜分类={tag}｜关键词={keywords}｜"
            "来源=dsh-meme（内置大肥鱼包，代码 MIT；图包许可见包内）"
        )
        mem.put_image(sha, dest, desc)
        mem.set_image_scope(sha, scope)
        embeds.append((sha, f"{desc} 分类：{tag} 关键词：{keywords}"))
        ok += 1
    print("dsh-meme done", {"ok": ok, "skip": skip, "fail": fail}, flush=True)


async def embed_all(embeds, mem):
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG, encoding="utf-8")
    model = cfg.get("ai", "embedding_model", fallback="").strip()
    if not model:
        print("no embedding model; skip embeddings", flush=True)
        return
    base = cfg.get("ai", "base_url").strip()
    key = cfg.get("ai", "api_key").strip()
    print("embedding", len(embeds), "items", flush=True)
    done = 0
    for i in range(0, len(embeds), 10):
        chunk = embeds[i:i + 10]
        vecs = await media.embed_texts([t for _h, t in chunk], base, key, model, timeout=15.0)
        if not vecs or len(vecs) != len(chunk):
            print("embedding batch failed at", i, flush=True)
            continue
        for (h, _t), v in zip(chunk, vecs):
            mem.set_image_emb(h, media.pack_vec(v))
        done += len(chunk)
        if done % 50 == 0:
            print("embedded", done, "/", len(embeds), flush=True)
    print("embedding done", done, "/", len(embeds), flush=True)


async def main():
    if not (IMP / "nekowhale.json").exists():
        sys.exit("missing import/nekowhale.json")
    if not (IMP / "nekowhale-manifest.json").exists():
        sys.exit("missing import/nekowhale-manifest.json")
    DEST.mkdir(parents=True, exist_ok=True)
    mem = Memory(DB)
    embeds = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=20.0),
        follow_redirects=True,
        headers={"User-Agent": UA},
    ) as client:
        await import_nekowhale(client, mem, embeds)
    import_dsh_meme(mem, embeds)
    await embed_all(embeds, mem)
    print("total new", len(embeds), "images", mem.stats(), flush=True)
    mem.conn.close()


if __name__ == "__main__":
    asyncio.run(main())

"""轻量 SQLite 记忆：群消息流水 + 每个群的运行状态。

设计原则：
- 只有一个连接，全部访问都在事件循环线程里（异步 HTTP 不切线程），不加锁。
- 消息只留 7 天，避免无限增长；上下文窗口由查询时的 minutes/limit 控制。
"""

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_openid TEXT NOT NULL,
    msg_id TEXT,
    author TEXT,
    author_openid TEXT,
    content TEXT,
    ts REAL NOT NULL,
    is_bot INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_group_ts ON messages(group_openid, ts);
CREATE TABLE IF NOT EXISTS groups (
    group_openid TEXT PRIMARY KEY,
    last_bot_ts REAL DEFAULT 0,
    proactive_ok INTEGER DEFAULT 1,
    ambient_ok INTEGER DEFAULT 1,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS images (
    hash TEXT PRIMARY KEY,
    path TEXT,
    desc TEXT,
    scope TEXT DEFAULT '',
    ts REAL
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_openid TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'event',
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_group_ts ON memories(group_openid, ts);
"""


class Memory:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """老库补列：图片向量（描述 embedding）+ 适用范围 scope。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(images)")}
        if "emb" not in cols:
            self.conn.execute("ALTER TABLE images ADD COLUMN emb BLOB")
        if "scope" not in cols:
            self.conn.execute("ALTER TABLE images ADD COLUMN scope TEXT DEFAULT ''")
        self.conn.commit()

    # ---------- 消息 ----------

    def add_message(self, group, msg_id, author, author_openid, content, is_bot=False, ts=None):
        self.conn.execute(
            "INSERT INTO messages(group_openid,msg_id,author,author_openid,content,ts,is_bot)"
            " VALUES (?,?,?,?,?,?,?)",
            (group, msg_id, author, author_openid, (content or "")[:2000], ts or time.time(), 1 if is_bot else 0),
        )
        self.conn.commit()

    def recent_messages(self, group, minutes=90, limit=40):
        """返回 [(author, content, ts, is_bot), ...]，按时间正序。"""
        rows = self.conn.execute(
            "SELECT author, content, ts, is_bot FROM messages"
            " WHERE group_openid=? AND ts >= ? ORDER BY ts DESC LIMIT ?",
            (group, time.time() - minutes * 60, limit),
        ).fetchall()
        return list(reversed(rows))

    def last_activity(self, group):
        row = self.conn.execute(
            "SELECT MAX(ts) FROM messages WHERE group_openid=?", (group,)
        ).fetchone()
        return row[0] or 0

    def last_bot_message(self, group):
        row = self.conn.execute(
            "SELECT content, ts FROM messages WHERE group_openid=? AND is_bot=1"
            " ORDER BY ts DESC LIMIT 1",
            (group,),
        ).fetchone()
        return row  # (content, ts) or None

    def prune(self, days=7):
        self.conn.execute("DELETE FROM messages WHERE ts < ?", (time.time() - days * 86400,))
        self.conn.execute("DELETE FROM memories WHERE ts < ?", (time.time() - 60 * 86400,))
        self.conn.commit()

    def messages_after(self, group, ts, limit=80):
        """某时间点之后的非 bot 消息，按时间正序；给群记忆总结用。"""
        rows = self.conn.execute(
            "SELECT author, content, ts, is_bot FROM messages"
            " WHERE group_openid=? AND ts>? AND is_bot=0 ORDER BY ts DESC LIMIT ?",
            (group, ts, limit),
        ).fetchall()
        return list(reversed(rows))

    # ---------- 群事件记忆 ----------

    def add_memory(self, group, text, kind="event", ts=None):
        text = (text or "").strip()
        if not text:
            return
        self.conn.execute(
            "INSERT INTO memories(group_openid,ts,kind,text) VALUES (?,?,?,?)",
            (group, ts or time.time(), kind, text[:500]),
        )
        self.conn.commit()

    def recent_memories(self, group, limit=8):
        """[(ts, kind, text), ...]，按时间正序，最近的 limit 条。"""
        rows = self.conn.execute(
            "SELECT ts, kind, text FROM memories WHERE group_openid=?"
            " ORDER BY ts DESC LIMIT ?",
            (group, limit),
        ).fetchall()
        return list(reversed(rows))

    # ---------- 群状态 ----------

    def ensure_group(self, group):
        self.conn.execute(
            "INSERT OR IGNORE INTO groups(group_openid) VALUES (?)", (group,)
        )
        self.conn.commit()

    def group_flag(self, group, name):
        self.ensure_group(group)
        row = self.conn.execute(
            f"SELECT {name} FROM groups WHERE group_openid=?", (group,)
        ).fetchone()
        return bool(row[0]) if row else True

    def set_group_flag(self, group, name, value):
        self.ensure_group(group)
        self.conn.execute(
            f"UPDATE groups SET {name}=? WHERE group_openid=?", (1 if value else 0, group)
        )
        self.conn.commit()

    def mark_bot_spoke(self, group, ts=None):
        self.ensure_group(group)
        self.conn.execute(
            "UPDATE groups SET last_bot_ts=? WHERE group_openid=?", (ts or time.time(), group)
        )
        self.conn.commit()

    def known_groups(self):
        """所有见过的群，不论最近有没有人说话。"""
        rows = self.conn.execute("SELECT group_openid FROM groups").fetchall()
        return [r[0] for r in rows]

    def active_groups(self, minutes=45):
        rows = self.conn.execute(
            "SELECT group_openid, MAX(ts) AS t FROM messages WHERE is_bot=0"
            " GROUP BY group_openid HAVING t >= ?",
            (time.time() - minutes * 60,),
        ).fetchall()
        return [r[0] for r in rows]

    # ---------- 图片 ----------

    def get_image(self, h):
        row = self.conn.execute("SELECT hash, path, desc FROM images WHERE hash=?", (h,)).fetchone()
        return row

    def put_image(self, h, path, desc):
        self.conn.execute(
            "INSERT INTO images(hash,path,desc,ts) VALUES (?,?,?,?)"
            " ON CONFLICT(hash) DO UPDATE SET desc=excluded.desc, path=excluded.path",
            (h, str(path), desc or "", time.time()),
        )
        self.conn.commit()

    def find_image(self, prefix):
        row = self.conn.execute(
            "SELECT path, desc FROM images WHERE hash LIKE ? LIMIT 1", (prefix + "%",)
        ).fetchone()
        return row

    def set_image_emb(self, h, blob):
        self.conn.execute("UPDATE images SET emb=? WHERE hash=?", (blob, h))
        self.conn.commit()

    def images_with_emb(self):
        """[(hash, path, desc, emb_bytes_or_None), ...]，按时间正序。"""
        rows = self.conn.execute(
            "SELECT hash, path, desc, emb FROM images ORDER BY ts"
        ).fetchall()
        return rows

    def image_by_path(self, path):
        row = self.conn.execute(
            "SELECT hash, scope FROM images WHERE path=? LIMIT 1", (str(path),)
        ).fetchone()
        return row

    def set_image_scope(self, h, scope):
        self.conn.execute(
            "UPDATE images SET scope=? WHERE hash=?", ((scope or "")[:500], h)
        )
        self.conn.commit()

    def image_library(self, limit=20):
        """[(hash, desc, scope), ...]，有适用范围的排前面，方便模型参考。"""
        rows = self.conn.execute(
            "SELECT hash, desc, scope FROM images"
            " ORDER BY CASE WHEN scope IS NULL OR scope='' THEN 1 ELSE 0 END, ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return rows

    # ---------- 键值状态 ----------

    def get_state(self, key, default=None):
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key, value):
        self.conn.execute(
            "INSERT INTO state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def stats(self):
        return {
            "messages": self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "groups": self.conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0],
            "memories": self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
            "db": self.path,
        }

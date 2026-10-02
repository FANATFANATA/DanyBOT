import contextlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "memory.db"

MAX_KEY = 200
MAX_VALUE = 20000
MAX_TAGS = 500
MAX_QUERY = 200
MAX_LIMIT = 200
MAX_ROWS_PER_CHAT = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(chat_id, key)
);
CREATE INDEX IF NOT EXISTS idx_memories_chat ON memories(chat_id);
CREATE INDEX IF NOT EXISTS idx_memories_key ON memories(key);
"""


_initialized: set[str] = set()


def _has_table(conn, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _connect():
    if not DATA_DIR.is_dir():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    key = str(DB_PATH)
    if key not in _initialized or not _has_table(conn, "memories"):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        except sqlite3.Error:
            conn.close()
            raise
        _initialized.add(key)
    with contextlib.suppress(sqlite3.Error):
        conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _prune(conn, chat_id: int) -> None:
    conn.execute(
        "DELETE FROM memories WHERE chat_id=? AND id NOT IN ("
        "SELECT id FROM memories WHERE chat_id=? ORDER BY updated_at DESC, id DESC LIMIT ?"
        ")",
        (int(chat_id), int(chat_id), MAX_ROWS_PER_CHAT),
    )


def _now():
    return time.time()


def _clean_tags(raw):
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        parts = [str(x).strip().replace(",", " ") for x in raw]
    else:
        parts = [x.strip() for x in str(raw).replace(";", ",").split(",")]
    kept = []
    used = 0
    for part in parts:
        if not part or part in kept:
            continue
        extra = len(part) + (1 if kept else 0)
        if extra > MAX_TAGS - used:
            continue
        kept.append(part)
        used += extra
    return ",".join(kept)


def _tags_list(raw):
    return [t for t in (raw or "").split(",") if t]


def _row_to_dict(row):
    return {
        "id": row["id"],
        "key": row["key"],
        "value": row["value"],
        "tags": _tags_list(row["tags"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def remember(chat_id: int, key: str, value: str, tags: Any = None) -> dict:
    key = (key or "").strip()[:MAX_KEY]
    value = str(value or "")[:MAX_VALUE]
    if not key:
        return {"ok": False, "error": "Пустой ключ."}
    if not value:
        return {"ok": False, "error": "Пустое значение."}
    tags_clean = _clean_tags(tags)
    now = _now()
    with closing(_connect()) as conn:
        cur = conn.execute(
            "INSERT INTO memories (chat_id, key, value, tags, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, key) DO UPDATE SET "
            "value=excluded.value, tags=excluded.tags, updated_at=excluded.updated_at "
            "RETURNING id, created_at",
            (int(chat_id), key, value, tags_clean, now, now),
        )
        row = cur.fetchone()
        _prune(conn, int(chat_id))
        conn.commit()
    action = "created" if row["created_at"] == now else "updated"
    return {"ok": True, "action": action, "id": row["id"], "key": key}


def recall(chat_id: int, key: str = "", query: str = "", limit: int = 10) -> dict:
    limit = max(1, min(int(limit or 10), MAX_LIMIT))
    key = (key or "").strip()[:MAX_KEY]
    query = (query or "").strip()[:MAX_QUERY]
    with closing(_connect()) as conn:
        if key:
            cur = conn.execute(
                "SELECT * FROM memories WHERE chat_id=? AND key=?",
                (int(chat_id), key),
            )
            row = cur.fetchone()
            if not row:
                return {"ok": True, "count": 0, "items": []}
            return {"ok": True, "count": 1, "items": [_row_to_dict(row)]}
        if query:
            like = f"%{query}%"
            cur = conn.execute(
                "SELECT * FROM memories WHERE chat_id=? AND (key LIKE ? OR value LIKE ? OR tags LIKE ?) "
                "ORDER BY updated_at DESC LIMIT ?",
                (int(chat_id), like, like, like, limit),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM memories WHERE chat_id=? ORDER BY updated_at DESC LIMIT ?",
                (int(chat_id), limit),
            )
        rows = cur.fetchall()
    return {"ok": True, "count": len(rows), "items": [_row_to_dict(r) for r in rows]}


def forget(chat_id: int, key: str = "", mem_id: int = 0) -> dict:
    key = (key or "").strip()[:MAX_KEY]
    if not key and not mem_id:
        return {"ok": False, "error": "Нужен key или id."}
    with closing(_connect()) as conn:
        if key:
            cur = conn.execute(
                "DELETE FROM memories WHERE chat_id=? AND key=?",
                (int(chat_id), key),
            )
        else:
            cur = conn.execute(
                "DELETE FROM memories WHERE chat_id=? AND id=?",
                (int(chat_id), int(mem_id)),
            )
        conn.commit()
        deleted = cur.rowcount
    return {"ok": True, "deleted": deleted}


def list_memories(chat_id: int, limit: int = 50) -> dict:
    limit = max(1, min(int(limit or 50), MAX_LIMIT))
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT * FROM memories WHERE chat_id=? ORDER BY updated_at DESC LIMIT ?",
            (int(chat_id), limit),
        )
        rows = cur.fetchall()
    return {"ok": True, "count": len(rows), "items": [_row_to_dict(r) for r in rows]}


def stats(chat_id: int) -> dict:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE chat_id=?", (int(chat_id),)
        )
        n = cur.fetchone()["n"]
    return {"ok": True, "chat_id": int(chat_id), "count": n, "db": DB_PATH.name}


def dumps(data):
    return json.dumps(data, ensure_ascii=False)

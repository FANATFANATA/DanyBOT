import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "memory.db"

MAX_KEY = 200
MAX_VALUE = 20000
MAX_TAGS = 500
MAX_QUERY = 200
MAX_LIMIT = 200

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


def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _now():
    return time.time()


def _clean_tags(raw):
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        parts = [str(x).strip() for x in raw]
    else:
        parts = [x.strip() for x in str(raw).replace(";", ",").split(",")]
    seen = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return ",".join(seen)[:MAX_TAGS]


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
    with _connect() as conn:
        cur = conn.execute("SELECT id, created_at FROM memories WHERE chat_id=? AND key=?", (int(chat_id), key))
        row = cur.fetchone()
        if row:
            conn.execute(
                "UPDATE memories SET value=?, tags=?, updated_at=? WHERE id=?",
                (value, tags_clean, now, row["id"]),
            )
            action = "updated"
            mem_id = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO memories (chat_id, key, value, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (int(chat_id), key, value, tags_clean, now, now),
            )
            action = "created"
            mem_id = cur.lastrowid
        conn.commit()
    return {"ok": True, "action": action, "id": mem_id, "key": key}


def recall(chat_id: int, key: str = "", query: str = "", limit: int = 10) -> dict:
    limit = max(1, min(int(limit or 10), MAX_LIMIT))
    key = (key or "").strip()[:MAX_KEY]
    query = (query or "").strip()[:MAX_QUERY]
    with _connect() as conn:
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
    with _connect() as conn:
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
    with _connect() as conn:
        cur = conn.execute(
            "SELECT * FROM memories WHERE chat_id=? ORDER BY updated_at DESC LIMIT ?",
            (int(chat_id), limit),
        )
        rows = cur.fetchall()
    return {"ok": True, "count": len(rows), "items": [_row_to_dict(r) for r in rows]}


def stats(chat_id: int) -> dict:
    with _connect() as conn:
        cur = conn.execute("SELECT COUNT(*) AS n FROM memories WHERE chat_id=?", (int(chat_id),))
        n = cur.fetchone()["n"]
    return {"ok": True, "chat_id": int(chat_id), "count": n, "db": str(DB_PATH)}


def dumps(data):
    return json.dumps(data, ensure_ascii=False)
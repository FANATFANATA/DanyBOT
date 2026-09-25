import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "skills.db"

MAX_NAME = 100
MAX_DESC = 1000
MAX_BODY = 50000
MAX_TAGS = 500
MAX_QUERY = 200
MAX_LIMIT = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    uses INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name);
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


def _row_to_dict(row, with_body=True):
    data = {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "tags": _tags_list(row["tags"]),
        "uses": row["uses"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if with_body:
        data["body"] = row["body"]
    return data


def save_skill(name: str, description: str, body: str, tags: Any = None) -> dict:
    name = (name or "").strip()[:MAX_NAME]
    description = str(description or "").strip()[:MAX_DESC]
    body = str(body or "")[:MAX_BODY]
    if not name:
        return {"ok": False, "error": "Пустое имя."}
    tags_clean = _clean_tags(tags)
    now = _now()
    with _connect() as conn:
        cur = conn.execute("SELECT id FROM skills WHERE name=?", (name,))
        row = cur.fetchone()
        if row:
            conn.execute(
                "UPDATE skills SET description=?, body=?, tags=?, updated_at=? WHERE id=?",
                (description, body, tags_clean, now, row["id"]),
            )
            action = "updated"
            skill_id = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO skills (name, description, body, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name, description, body, tags_clean, now, now),
            )
            action = "created"
            skill_id = cur.lastrowid
        conn.commit()
    return {"ok": True, "action": action, "id": skill_id, "name": name}


def load_skill(name: str, touch: bool = True) -> dict:
    name = (name or "").strip()[:MAX_NAME]
    if not name:
        return {"ok": False, "error": "Пустое имя."}
    with _connect() as conn:
        cur = conn.execute("SELECT * FROM skills WHERE name=?", (name,))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": f"Скилл не найден: {name}"}
        if touch:
            conn.execute("UPDATE skills SET uses=uses+1 WHERE id=?", (row["id"],))
            conn.commit()
        return {"ok": True, "skill": _row_to_dict(row)}


def list_skills(tag: str = "", query: str = "", limit: int = 50) -> dict:
    limit = max(1, min(int(limit or 50), MAX_LIMIT))
    tag = (tag or "").strip()[:MAX_QUERY]
    query = (query or "").strip()[:MAX_QUERY]
    clauses = []
    params: list[Any] = []
    if tag:
        clauses.append("tags LIKE ?")
        params.append(f"%{tag}%")
    if query:
        clauses.append("(name LIKE ? OR description LIKE ? OR body LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like, like])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _connect() as conn:
        cur = conn.execute(
            f"SELECT * FROM skills {where} ORDER BY uses DESC, updated_at DESC LIMIT ?",
            tuple(params),
        )
        rows = cur.fetchall()
    return {"ok": True, "count": len(rows), "items": [_row_to_dict(r, with_body=False) for r in rows]}


def delete_skill(name: str) -> dict:
    name = (name or "").strip()[:MAX_NAME]
    if not name:
        return {"ok": False, "error": "Пустое имя."}
    with _connect() as conn:
        cur = conn.execute("DELETE FROM skills WHERE name=?", (name,))
        conn.commit()
        deleted = cur.rowcount
    return {"ok": True, "deleted": deleted}


def stats() -> dict:
    with _connect() as conn:
        cur = conn.execute("SELECT COUNT(*) AS n FROM skills")
        n = cur.fetchone()["n"]
    return {"ok": True, "count": n, "db": str(DB_PATH)}


def dumps(data):
    return json.dumps(data, ensure_ascii=False)
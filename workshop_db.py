"""Workshop SQLite layer — stdlib only. DB at ~/.workshop/workshop.db"""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

WORKSHOP_DIR = Path.home() / ".workshop"
DB_PATH = WORKSHOP_DIR / "workshop.db"
NOTIF_LOG = WORKSHOP_DIR / "notifications.log"

_local = threading.local()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_conn() -> sqlite3.Connection:
    """One connection per thread (safe with HTTP server threads + ticker)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        WORKSHOP_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS app_data (
            app TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (app, key)
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            read INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS schedules (
            id TEXT PRIMARY KEY,
            app TEXT NOT NULL,
            every TEXT NOT NULL,
            at_time TEXT,
            action TEXT NOT NULL DEFAULT 'notify',
            message TEXT,
            payload TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_ran TEXT
        );
    """)
    conn.commit()


def _row_dict(row) -> dict:
    return dict(row) if row else None


# ── app_data ───────────────────────────────────────────────────────────────────

def data_get(app: str, key: str):
    row = get_conn().execute(
        "SELECT value FROM app_data WHERE app=? AND key=?", (app, key)
    ).fetchone()
    if not row:
        return None
    return json.loads(row["value"])


def data_put(app: str, key: str, value) -> dict:
    ts = _now()
    blob = json.dumps(value)
    get_conn().execute(
        """INSERT INTO app_data (app, key, value, updated_at) VALUES (?,?,?,?)
           ON CONFLICT(app, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (app, key, blob, ts),
    )
    get_conn().commit()
    return {"app": app, "key": key, "updated_at": ts}


def data_list(app: str) -> list:
    rows = get_conn().execute(
        "SELECT key, updated_at FROM app_data WHERE app=? ORDER BY key", (app,)
    ).fetchall()
    return [{"key": r["key"], "updated_at": r["updated_at"]} for r in rows]


def data_delete(app: str, key: str) -> bool:
    cur = get_conn().execute(
        "DELETE FROM app_data WHERE app=? AND key=?", (app, key)
    )
    get_conn().commit()
    return cur.rowcount > 0


# ── notifications ──────────────────────────────────────────────────────────────

def notif_insert(app: str, title: str, message: str) -> dict:
    ts = _now()
    cur = get_conn().execute(
        "INSERT INTO notifications (app, title, message, created_at, read) VALUES (?,?,?,?,0)",
        (app, title, message, ts),
    )
    get_conn().commit()
    nid = cur.lastrowid
    line = f"{ts} [{app}] {title}: {message}\n"
    try:
        NOTIF_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(NOTIF_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    return {"id": nid, "app": app, "title": title, "message": message, "created_at": ts}


def notif_list(unread_only: bool = False) -> list:
    q = "SELECT * FROM notifications"
    if unread_only:
        q += " WHERE read=0"
    q += " ORDER BY id DESC LIMIT 100"
    return [_row_dict(r) for r in get_conn().execute(q).fetchall()]


def notif_mark_read(nid: int) -> bool:
    cur = get_conn().execute(
        "UPDATE notifications SET read=1 WHERE id=?", (nid,)
    )
    get_conn().commit()
    return cur.rowcount > 0


# ── schedules ──────────────────────────────────────────────────────────────────

def schedule_list(app: str = None) -> list:
    if app:
        rows = get_conn().execute(
            "SELECT * FROM schedules WHERE app=? ORDER BY id", (app,)
        ).fetchall()
    else:
        rows = get_conn().execute("SELECT * FROM schedules ORDER BY app, id").fetchall()
    out = []
    for r in rows:
        d = _row_dict(r)
        if d.get("payload"):
            try:
                d["payload"] = json.loads(d["payload"])
            except json.JSONDecodeError:
                pass
        out.append(d)
    return out


def schedule_upsert(body: dict) -> dict:
    sid = (body.get("id") or "").strip()
    if not sid:
        raise ValueError("id required")
    app = (body.get("app") or "").strip()
    if not app:
        raise ValueError("app required")
    every = (body.get("every") or "day").strip()
    payload = body.get("payload")
    payload_s = json.dumps(payload) if payload is not None else None
    get_conn().execute(
        """INSERT INTO schedules (id, app, every, at_time, action, message, payload, enabled, last_ran)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             app=excluded.app, every=excluded.every, at_time=excluded.at_time,
             action=excluded.action, message=excluded.message, payload=excluded.payload,
             enabled=excluded.enabled""",
        (
            sid, app, every, body.get("at_time"), body.get("action") or "notify",
            body.get("message"), payload_s,
            1 if body.get("enabled", True) else 0,
            body.get("last_ran"),
        ),
    )
    get_conn().commit()
    return schedule_list(app=app)


def schedule_delete(sid: str) -> bool:
    cur = get_conn().execute("DELETE FROM schedules WHERE id=?", (sid,))
    get_conn().commit()
    return cur.rowcount > 0


def schedule_enabled() -> list:
    rows = get_conn().execute(
        "SELECT * FROM schedules WHERE enabled=1"
    ).fetchall()
    out = []
    for r in rows:
        d = _row_dict(r)
        if d.get("payload"):
            try:
                d["payload"] = json.loads(d["payload"])
            except json.JSONDecodeError:
                d["payload"] = {}
        else:
            d["payload"] = {}
        out.append(d)
    return out


def schedule_mark_ran(sid: str, when: str = None):
    get_conn().execute(
        "UPDATE schedules SET last_ran=? WHERE id=?",
        (when or _now(), sid),
    )
    get_conn().commit()

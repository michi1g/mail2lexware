import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    msg_key TEXT UNIQUE NOT NULL,
    message_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sender TEXT,
    sender_email TEXT,
    subject TEXT,
    mail_date TEXT,
    score INTEGER DEFAULT 0,
    status TEXT NOT NULL,          -- pending | uploaded | ignored | failed | duplicate
    lexware_id TEXT,
    error TEXT,
    attempts INTEGER DEFAULT 0,
    labeled INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE TABLE IF NOT EXISTS messages (message_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    pattern TEXT UNIQUE NOT NULL,  -- E-Mail-Adresse oder Domain
    action TEXT NOT NULL           -- always | never
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn():
    c = sqlite3.connect(config.DB_FILE, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init() -> None:
    config.ensure_dirs()
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)


# --- Dokumente -------------------------------------------------------------

def add_document(**f) -> int:
    f.setdefault("created_at", now())
    f["updated_at"] = f["created_at"]
    cols = ", ".join(f)
    marks = ", ".join("?" for _ in f)
    with conn() as c:
        return c.execute(f"INSERT INTO documents ({cols}) VALUES ({marks})", list(f.values())).lastrowid


def get_document(doc_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()


def list_documents(status: str | None = None, limit: int = 500):
    with conn() as c:
        if status:
            return c.execute(
                "SELECT * FROM documents WHERE status=? ORDER BY mail_date DESC, id DESC LIMIT ?", (status, limit)
            ).fetchall()
        return c.execute("SELECT * FROM documents ORDER BY mail_date DESC, id DESC LIMIT ?", (limit,)).fetchall()


def set_status(doc_id: int, status: str, lexware_id: str | None = None, error: str | None = None,
               count_attempt: bool = False) -> None:
    with conn() as c:
        c.execute(
            "UPDATE documents SET status=?, lexware_id=COALESCE(?, lexware_id), error=?, "
            "attempts=attempts+?, updated_at=? WHERE id=?",
            (status, lexware_id, error, 1 if count_attempt else 0, now(), doc_id),
        )


def uploaded_sha_exists(sha256: str) -> bool:
    with conn() as c:
        return c.execute(
            "SELECT 1 FROM documents WHERE sha256=? AND status='uploaded'", (sha256,)
        ).fetchone() is not None


def retryable_failed(max_attempts: int = 3):
    with conn() as c:
        return c.execute(
            "SELECT * FROM documents WHERE status='failed' AND attempts<?", (max_attempts,)
        ).fetchall()


def pending_from_sender(sender_email: str):
    with conn() as c:
        return c.execute(
            "SELECT * FROM documents WHERE status='pending' AND sender_email=?", (sender_email,)
        ).fetchall()


def counts() -> dict:
    with conn() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM documents GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


# --- Mails / Labels --------------------------------------------------------

def message_known(message_id: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM messages WHERE message_id=?", (message_id,)).fetchone() is not None


def add_message(message_id: str) -> None:
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO messages (message_id) VALUES (?)", (message_id,))


def messages_to_label() -> list[str]:
    """Mails mit hochgeladenen Belegen, bei denen kein Dokument mehr offen oder fehlgeschlagen ist."""
    with conn() as c:
        rows = c.execute(
            """SELECT message_id FROM documents GROUP BY message_id
               HAVING SUM(status IN ('uploaded','duplicate') AND labeled=0) > 0
                  AND SUM(status IN ('pending','failed')) = 0"""
        ).fetchall()
    return [r["message_id"] for r in rows]


def mark_labeled(message_id: str) -> None:
    with conn() as c:
        c.execute("UPDATE documents SET labeled=1 WHERE message_id=?", (message_id,))


# --- Regeln ----------------------------------------------------------------

def list_rules():
    with conn() as c:
        return c.execute("SELECT * FROM rules ORDER BY pattern").fetchall()


def add_rule(pattern: str, action: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO rules (pattern, action) VALUES (?, ?) "
            "ON CONFLICT(pattern) DO UPDATE SET action=excluded.action",
            (pattern.strip().lower(), action),
        )


def delete_rule(rule_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM rules WHERE id=?", (rule_id,))


def match_rule(sender_email: str) -> str | None:
    """Exakte Adresse hat Vorrang vor einer Domain-Regel."""
    email = (sender_email or "").lower()
    domain = email.rsplit("@", 1)[-1]
    best = None
    for r in list_rules():
        p = r["pattern"]
        if p == email:
            return r["action"]
        if p.lstrip("@") == domain and "@" not in p.lstrip("@"):
            best = r["action"]
    return best


# --- Key/Value -------------------------------------------------------------

def kv_set(key: str, value) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def kv_get(key: str, default=None):
    with conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default

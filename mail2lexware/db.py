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
    field TEXT NOT NULL DEFAULT 'sender',  -- sender | filename | subject
    pattern TEXT NOT NULL,         -- sender: E-Mail-Adresse oder Domain, sonst Textbaustein (Teilstring)
    action TEXT NOT NULL,          -- always | never
    UNIQUE (field, pattern)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,            -- imap | microsoft
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    host TEXT,
    port INTEGER,
    security TEXT,                 -- ssl | starttls
    username TEXT,
    folder TEXT NOT NULL DEFAULT 'INBOX',
    keywords TEXT NOT NULL DEFAULT '',
    secret TEXT NOT NULL,          -- verschlüsseltes JSON (Passwort bzw. OAuth-Tokens)
    enabled INTEGER NOT NULL DEFAULT 1,
    last_status TEXT,
    last_checked TEXT,
    created_at TEXT NOT NULL
);
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
        cols = {r["name"] for r in c.execute("PRAGMA table_info(documents)")}
        if "account_id" not in cols:  # Migration älterer Datenbanken: bisherige Belege stammen aus Gmail
            c.execute("ALTER TABLE documents ADD COLUMN account_id TEXT NOT NULL DEFAULT 'google'")
        if "source" not in cols:
            c.execute("ALTER TABLE documents ADD COLUMN source TEXT NOT NULL DEFAULT 'Gmail'")
        rule_cols = {r["name"] for r in c.execute("PRAGMA table_info(rules)")}
        if "field" not in rule_cols:  # Migration: bisherige Regeln gelten für den Absender
            c.executescript(
                "ALTER TABLE rules RENAME TO rules_old;"
                "CREATE TABLE rules (id INTEGER PRIMARY KEY, field TEXT NOT NULL DEFAULT 'sender', "
                "pattern TEXT NOT NULL, action TEXT NOT NULL, UNIQUE (field, pattern));"
                "INSERT INTO rules (id, field, pattern, action) SELECT id, 'sender', pattern, action FROM rules_old;"
                "DROP TABLE rules_old;"
            )


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
                  AND SUM(status IN ('pending','failed')) = 0
                  AND MAX(account_id = 'google') = 1"""
        ).fetchall()
    return [r["message_id"] for r in rows]


def mark_labeled(message_id: str) -> None:
    with conn() as c:
        c.execute("UPDATE documents SET labeled=1 WHERE message_id=?", (message_id,))


# --- Postfächer (IMAP, Microsoft) ---------------------------------------------

def add_account(account_id: str, kind: str, name: str, email: str, secret: str, host: str | None = None,
                port: int | None = None, security: str | None = None, username: str | None = None,
                folder: str = "INBOX", keywords: str = "") -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO accounts (id, kind, name, email, host, port, security, username, folder, keywords, secret, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, kind, name, email, host, port, security, username, folder, keywords, secret, now()),
        )


def list_accounts(enabled_only: bool = False):
    with conn() as c:
        sql = "SELECT * FROM accounts" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY created_at, id"
        return c.execute(sql).fetchall()


def get_account(account_id: str):
    with conn() as c:
        return c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()


def delete_account(account_id: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM accounts WHERE id=?", (account_id,))


def update_account_secret(account_id: str, secret: str) -> None:
    with conn() as c:
        c.execute("UPDATE accounts SET secret=? WHERE id=?", (secret, account_id))


def set_account_status(account_id: str, status: str) -> None:
    with conn() as c:
        c.execute("UPDATE accounts SET last_status=?, last_checked=? WHERE id=?", (status, now(), account_id))


# --- Regeln ----------------------------------------------------------------

def list_rules():
    with conn() as c:
        return c.execute("SELECT * FROM rules ORDER BY pattern").fetchall()


RULE_FIELDS = ("sender", "filename", "subject")


def add_rule(pattern: str, action: str, field: str = "sender") -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO rules (field, pattern, action) VALUES (?, ?, ?) "
            "ON CONFLICT(field, pattern) DO UPDATE SET action=excluded.action",
            (field, pattern.strip().lower(), action),
        )


def delete_rule(rule_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM rules WHERE id=?", (rule_id,))


def match_rule(sender_email: str, filename: str = "", subject: str = "") -> str | None:
    """Regel für einen Anhang: always | never | None.

    Beim Absender hat die exakte Adresse Vorrang vor einer Domain. Regeln für Dateiname und Betreff
    (Teilstring, ohne Groß-/Kleinschreibung) ergänzen das, und "never" gewinnt immer.
    """
    email = (sender_email or "").lower()
    domain = email.rsplit("@", 1)[-1]
    texts = {"filename": (filename or "").lower(), "subject": (subject or "").lower()}
    exact = by_domain = None
    content = set()
    for r in list_rules():
        p = r["pattern"]
        if r["field"] == "sender":
            if p == email:
                exact = r["action"]
            elif p.lstrip("@") == domain and "@" not in p.lstrip("@"):
                by_domain = r["action"]
        elif p in texts.get(r["field"], ""):
            content.add(r["action"])
    sender_action = exact or by_domain
    if "never" in content or sender_action == "never":
        return "never"
    if "always" in content:
        return "always"
    return sender_action


# --- Key/Value -------------------------------------------------------------

def kv_set(key: str, value) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def kv_get(key: str, default=None):
    with conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default

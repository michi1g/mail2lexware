import base64
import email
import email.policy
import imaplib
import mimetypes
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterator

from .gmail import Attachment, Mail

TIMEOUT = 30
MAX_MAIL_SIZE = 30 * 1024 * 1024  # größere Mails werden nicht geladen
_MONATE = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_AUTH_FEHLER = ("Anmeldung fehlgeschlagen. Prüfe Benutzername und Passwort. "
                "Viele Anbieter verlangen ein eigenes App-Passwort.")


class ImapError(RuntimeError):
    pass


@dataclass
class ImapConfig:
    host: str
    port: int
    security: str  # "ssl" (implizites TLS) oder "starttls"
    username: str
    folder: str = "INBOX"


# --- Verbindung --------------------------------------------------------------

def _check_config(cfg: ImapConfig) -> None:
    if not cfg.host or not cfg.host.strip():
        raise ImapError("Ungültige Konfiguration: Server (Host) fehlt.")
    if not isinstance(cfg.port, int) or not 0 < cfg.port < 65536:
        raise ImapError("Ungültige Konfiguration: Port muss eine Zahl zwischen 1 und 65535 sein.")
    if cfg.security not in ("ssl", "starttls"):
        raise ImapError('Ungültige Konfiguration: Verschlüsselung muss "ssl" oder "starttls" sein. '
                        "Unverschlüsselte Verbindungen werden nicht unterstützt.")
    if not cfg.username:
        raise ImapError("Ungültige Konfiguration: Benutzername fehlt.")


def _translate(exc: Exception, cfg: ImapConfig) -> ImapError:
    if isinstance(exc, ssl.SSLError):
        return ImapError(f"Sichere Verbindung (TLS) zu {cfg.host}:{cfg.port} fehlgeschlagen: {exc}. "
                         "Prüfe Server, Port und Verschlüsselung.")
    if isinstance(exc, imaplib.IMAP4.error):
        return ImapError(f"Fehler vom IMAP-Server: {exc}")
    return ImapError(f"Verbindung zu {cfg.host}:{cfg.port} nicht möglich: {exc}")


def _logout(conn: imaplib.IMAP4) -> None:
    try:
        conn.logout()
    except Exception:
        try:
            conn.shutdown()
        except Exception:
            pass


def open_connection(cfg: ImapConfig, password: str | None = None,
                    access_token: str | None = None) -> imaplib.IMAP4:
    """Verbindet verschlüsselt und meldet per LOGIN (Passwort) oder XOAUTH2 (Token) an."""
    _check_config(cfg)
    if not password and not access_token:
        raise ImapError("Ungültige Konfiguration: Passwort oder Zugriffstoken fehlt.")
    ctx = ssl.create_default_context()
    conn = None
    try:
        try:
            if cfg.security == "ssl":
                conn = imaplib.IMAP4_SSL(cfg.host, cfg.port, ssl_context=ctx, timeout=TIMEOUT)
            else:
                conn = imaplib.IMAP4(cfg.host, cfg.port, timeout=TIMEOUT)
                conn.starttls(ssl_context=ctx)
        except (ssl.SSLError, imaplib.IMAP4.error, OSError) as e:
            raise _translate(e, cfg) from e
        try:
            if access_token:
                auth = f"user={cfg.username}\x01auth=Bearer {access_token}\x01\x01".encode()
                antworten = iter([auth])
                # Nach einer Fehlerantwort des Servers leer antworten, damit er mit NO abschließt
                conn.authenticate("XOAUTH2", lambda _: next(antworten, b""))
            else:
                conn.login(cfg.username, password)
        except imaplib.IMAP4.error as e:
            raise ImapError(_AUTH_FEHLER) from e
        except (ssl.SSLError, OSError) as e:
            raise _translate(e, cfg) from e
        return conn
    except BaseException:
        if conn is not None:
            _logout(conn)
        raise


# --- Ordner ------------------------------------------------------------------

def _utf7(name: str) -> str:
    """Ordnername nach IMAP-modifiziertem UTF-7 (für Umlaute etc.)."""
    out, buf = [], ""

    def flush():
        nonlocal buf
        if buf:
            b64 = base64.b64encode(buf.encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append(f"&{b64}-")
            buf = ""

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf += ch
    flush()
    return "".join(out)


def _quote_folder(name: str) -> str:
    return '"' + _utf7(name).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _open_folder(conn: imaplib.IMAP4, cfg: ImapConfig) -> tuple[str, int]:
    """Öffnet den Ordner schreibgeschützt (EXAMINE). Gibt (UIDVALIDITY, Mailzahl) zurück."""
    typ, data = conn.select(_quote_folder(cfg.folder), readonly=True)
    if typ != "OK":
        raise ImapError(f'Ordner "{cfg.folder}" wurde nicht gefunden oder kann nicht geöffnet werden.')
    count = int(data[0]) if data and data[0] else 0
    _, val = conn.response("UIDVALIDITY")
    if not val or not val[0]:
        raise ImapError("Der Server liefert keine UIDVALIDITY für den Ordner.")
    return val[0].decode(), count


def test_connection(cfg: ImapConfig, password: str | None = None, access_token: str | None = None) -> int:
    """Prüft Zugangsdaten und Ordner. Gibt die Anzahl Mails im Ordner zurück."""
    conn = open_connection(cfg, password, access_token)
    try:
        return _open_folder(conn, cfg)[1]
    except ImapError:
        raise
    except (imaplib.IMAP4.error, ssl.SSLError, OSError) as e:
        raise _translate(e, cfg) from e
    finally:
        _logout(conn)


# --- Mails -------------------------------------------------------------------

def _imap_date(d: date) -> str:
    return f"{d.day:02d}-{_MONATE[d.month - 1]}-{d.year}"  # englische Monatsnamen, nicht locale-abhängig


def _fetch(conn: imaplib.IMAP4, uid: str, item: str) -> list:
    typ, data = conn.uid("FETCH", uid, item)
    if typ != "OK" or not data:
        raise ImapError(f"Mail {uid} konnte nicht vom Server geladen werden.")
    return data


def _mail_size(conn: imaplib.IMAP4, uid: str) -> int:
    for item in _fetch(conn, uid, "(RFC822.SIZE)"):
        raw = item[0] if isinstance(item, tuple) else item
        if isinstance(raw, bytes) and (m := re.search(rb"RFC822\.SIZE (\d+)", raw)):
            return int(m.group(1))
    return 0


def _mail_bytes(conn: imaplib.IMAP4, uid: str, what: str = "(BODY.PEEK[])") -> bytes:
    for item in _fetch(conn, uid, what):  # PEEK: \Seen bleibt unverändert
        if isinstance(item, tuple):
            return item[1]
    raise ImapError(f"Mail {uid} konnte nicht vom Server geladen werden.")


def _parse_date(msg) -> str:
    try:
        dt = parsedate_to_datetime(str(msg["date"]))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        dt = datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _build_mail(raw: bytes, message_id: str, extensions: set[str], min_size: int,
                keywords: list[str]) -> Mail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = parseaddr(str(msg.get("from") or ""))
    subject = str(msg.get("subject") or "")
    mail = Mail(message_id=message_id, subject=subject, sender=name or addr,
                sender_email=addr.lower(), date=_parse_date(msg))
    kws = [k.casefold() for k in keywords if k and k.strip()]
    for nr, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        if Path(filename).suffix.lower() not in extensions:
            continue
        data = part.get_payload(decode=True)
        if data is None or len(data) < min_size:
            continue
        if kws and not any(k in subject.casefold() or k in filename.casefold() for k in kws):
            continue
        mime = part.get_content_type()
        if mime == "application/octet-stream":
            mime = mimetypes.guess_type(filename)[0] or mime
        mail.attachments.append(Attachment(msg_key=f"{message_id}:{nr}", filename=filename,
                                           mime_type=mime, data=data))
    return mail


def iter_mails(cfg: ImapConfig, since: date, until: date | None, extensions: set[str], min_size: int,
               keywords: list[str], known: Callable[[str], bool], account_id: str,
               password: str | None = None, access_token: str | None = None) -> Iterator[Mail]:
    """Liefert jede noch unbekannte Mail (auch ohne passende Anhänge). Das Postfach wird nie verändert."""
    conn = open_connection(cfg, password, access_token)
    try:
        try:
            uidvalidity, _ = _open_folder(conn, cfg)
            criteria = ["SINCE", _imap_date(since)]
            if until is not None:
                criteria += ["BEFORE", _imap_date(until + timedelta(days=1))]  # until zählt inklusive
            typ, data = conn.uid("SEARCH", *criteria)
            if typ != "OK":
                raise ImapError("Die Suche im Postfach ist fehlgeschlagen.")
            uids = sorted(data[0].split(), key=int) if data and data[0] else []
            for raw_uid in uids:
                uid = raw_uid.decode()
                message_id = f"imap:{account_id}:{uidvalidity}:{uid}"
                if known(message_id):
                    continue
                if _mail_size(conn, uid) > MAX_MAIL_SIZE:
                    # zu groß: nur die Kopfzeilen laden, Mail ohne Anhänge liefern
                    yield _build_mail(_mail_bytes(conn, uid, "(BODY.PEEK[HEADER])"), message_id,
                                      set(), 0, [])
                    continue
                yield _build_mail(_mail_bytes(conn, uid), message_id, extensions, min_size, keywords)
        except ImapError:
            raise
        except (imaplib.IMAP4.error, ssl.SSLError, OSError) as e:
            raise _translate(e, cfg) from e
    finally:
        _logout(conn)

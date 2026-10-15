"""Kernlogik: Gmail durchsuchen, bewerten, nach Lexware hochladen, Mails labeln."""
import hashlib
import logging
import threading
from datetime import timedelta
from pathlib import Path

import requests

from . import config, db, detect, gmail, imap_source, microsoft, secrets_store
from .lexware import LexwareClient, LexwareError

log = logging.getLogger("mail2lexware")
_lock = threading.Lock()  # nur ein Durchlauf bzw. Upload gleichzeitig (Rate-Limit, SQLite)


def is_running() -> bool:
    return _lock.locked()


def file_path(doc) -> Path:
    return config.FILES_DIR / (doc["sha256"] + Path(doc["filename"]).suffix.lower())


def _client(settings: dict) -> LexwareClient | None:
    key = config.api_key(settings)
    return LexwareClient(key, settings["base_url"]) if key else None


def _upload(doc, client: LexwareClient) -> bool:
    if db.uploaded_sha_exists(doc["sha256"]):
        db.set_status(doc["id"], "duplicate")
        return True
    try:
        lexware_id = client.upload_voucher_file(doc["filename"], file_path(doc).read_bytes(), doc["mime_type"])
    except (LexwareError, requests.RequestException, OSError) as e:
        log.warning("Upload fehlgeschlagen: %s: %s", doc["filename"], e)
        db.set_status(doc["id"], "failed", error=str(e), count_attempt=True)
        return False
    db.set_status(doc["id"], "uploaded", lexware_id=lexware_id)
    log.info("hochgeladen: %s -> %s", doc["filename"], lexware_id)
    return True


def _ingest(mail, settings: dict, account_id: str, source: str) -> tuple[int, list[int]]:
    """Legt die Anhänge einer Mail in der DB an. Gibt (Anzahl neu, IDs für direkten Upload) zurück."""
    new = 0
    upload_ids: list[int] = []
    for att in mail.attachments:
        sha = hashlib.sha256(att.data).hexdigest()
        ext = Path(att.filename).suffix.lower()
        path = config.FILES_DIR / (sha + ext)
        if not path.exists():
            path.write_bytes(att.data)
        rule = db.match_rule(mail.sender_email, att.filename, mail.subject)
        points = detect.score(att.filename, detect.extract_text(att.data, ext), ext)

        auto = rule == "always" or (rule is None and settings["mode"] == "auto"
                                    and points >= int(settings["auto_threshold"]))
        if rule == "never":
            status = "ignored"
        elif db.uploaded_sha_exists(sha):
            status = "duplicate"
        else:
            status = "pending"
        doc_id = db.add_document(
            msg_key=att.msg_key, message_id=mail.message_id, sha256=sha, filename=att.filename,
            mime_type=att.mime_type, size=len(att.data), sender=mail.sender, sender_email=mail.sender_email,
            subject=mail.subject, mail_date=mail.date, score=points, status=status,
            account_id=account_id, source=source,
        )
        new += 1
        if status == "pending" and auto:
            upload_ids.append(doc_id)
    db.add_message(mail.message_id)
    return new, upload_ids


def _scan(settings: dict, gmail_service) -> tuple[int, list[int]]:
    """Gmail durchsuchen. Gibt (Anzahl neu, IDs für direkten Upload) zurück."""
    since, until = config.period_range(settings)
    query = f"{settings['query']} after:{since:%Y/%m/%d}"
    if until:  # Gmail-"before" schließt den Tag selbst aus
        query += f" before:{until + timedelta(days=1):%Y/%m/%d}"
    if settings["label"]:
        query += f' -label:"{settings["label"]}"'
    extensions = {e.lower() for e in settings["extensions"]}

    new = 0
    upload_ids: list[int] = []
    for mail in gmail.iter_mails(gmail_service, query, extensions, int(settings["min_size_bytes"]), db.message_known):
        n, ids = _ingest(mail, settings, "google", "Gmail")
        new += n
        upload_ids += ids
    return new, upload_ids


def imap_params(account) -> tuple[imap_source.ImapConfig, dict]:
    """ImapConfig und Anmeldeargumente (password bzw. access_token) eines gespeicherten Postfachs.

    Bei Microsoft wird das Token bei Bedarf erneuert und verschlüsselt zurückgeschrieben.
    """
    secret = secrets_store.decrypt_json(account["secret"])
    cfg = imap_source.ImapConfig(
        host=account["host"], port=int(account["port"]), security=account["security"],
        username=account["username"], folder=account["folder"] or "INBOX",
    )
    if account["kind"] == "microsoft":
        token, tokens, changed = microsoft.ensure_access_token(secret)
        if changed:
            db.update_account_secret(account["id"], secrets_store.encrypt_json(tokens))
        return cfg, {"access_token": token}
    return cfg, {"password": secret["password"]}


def _scan_account(settings: dict, account) -> tuple[int, list[int]]:
    """IMAP-Postfach durchsuchen (nur lesend). Gibt (Anzahl neu, IDs für direkten Upload) zurück."""
    cfg, auth = imap_params(account)
    since, until = config.period_range(settings)
    extensions = {e.lower() for e in settings["extensions"]}
    keywords = [k.strip() for k in (account["keywords"] or "").split(",") if k.strip()]

    new = 0
    upload_ids: list[int] = []
    for mail in imap_source.iter_mails(cfg, since, until, extensions, int(settings["min_size_bytes"]), keywords,
                                       db.message_known, account["id"], **auth):
        n, ids = _ingest(mail, settings, account["id"], account["name"])
        new += n
        upload_ids += ids
    return new, upload_ids


def _label_mails(settings: dict, gmail_service) -> int:
    name = settings.get("label", "")
    todo = db.messages_to_label()
    if not name or not todo:
        return 0
    label_id = gmail.ensure_label(gmail_service, name)
    for message_id in todo:
        gmail.add_label(gmail_service, message_id, label_id)
        db.mark_labeled(message_id)
    return len(todo)


def run_cycle() -> dict:
    """Ein kompletter Durchlauf: alle Postfächer scannen, Freigegebenes hochladen, Gmail-Mails labeln."""
    if not _lock.acquire(blocking=False):
        return {"error": "Es läuft bereits ein Durchlauf."}
    result = {"time": db.now(), "new": 0, "uploaded": 0, "failed": 0, "labeled": 0, "error": None}
    errors: list[str] = []
    try:
        settings = config.load_settings()
        client = _client(settings)
        ids: list[int] = []
        gmail_service = None
        accounts = db.list_accounts(enabled_only=True)

        if config.TOKEN_FILE.exists():
            try:
                gmail_service = gmail.connect()
                new, found = _scan(settings, gmail_service)
                result["new"] += new
                ids += found
            except gmail.NotConnected as e:
                errors.append(str(e))
            except Exception as e:  # Netzwerk, widerrufener Token usw. sollen die anderen Postfächer nicht stoppen
                log.exception("Gmail-Durchlauf fehlgeschlagen")
                errors.append(f"Gmail: {type(e).__name__}: {e}")
        elif not accounts:
            errors.append("Kein Postfach verbunden.")

        for account in accounts:
            try:
                new, found = _scan_account(settings, account)
                result["new"] += new
                ids += found
                db.set_account_status(account["id"], "ok")
            except (imap_source.ImapError, microsoft.MicrosoftAuthError, secrets_store.SecretError) as e:
                db.set_account_status(account["id"], str(e))
                errors.append(f"{account['name']}: {e}")
            except Exception as e:
                log.exception("Durchlauf für %s fehlgeschlagen", account["name"])
                db.set_account_status(account["id"], f"{type(e).__name__}: {e}")
                errors.append(f"{account['name']}: {type(e).__name__}: {e}")

        ids += [d["id"] for d in db.retryable_failed()]
        if ids and not client:
            errors.append("Kein Lexware-API-Key hinterlegt. Uploads wurden übersprungen.")
        elif client:
            for doc_id in dict.fromkeys(ids):
                ok = _upload(db.get_document(doc_id), client)
                result["uploaded" if ok else "failed"] += 1
        if gmail_service:
            result["labeled"] = _label_mails(settings, gmail_service)
    except Exception as e:
        log.exception("Durchlauf fehlgeschlagen")
        errors.append(f"{type(e).__name__}: {e}")
    finally:
        _lock.release()
    result["error"] = " | ".join(errors) or None
    db.kv_set("last_run", result)
    return result


def apply_rules() -> dict:
    """Wendet die Regeln auf die offenen Belege an: "nie" ignoriert, "immer" lädt hoch."""
    result = {"ignored": 0, "uploaded": 0, "failed": 0, "error": None}
    upload: list[int] = []
    for d in db.list_documents("pending", limit=100_000):
        rule = db.match_rule(d["sender_email"], d["filename"], d["subject"])
        if rule == "never":
            db.set_status(d["id"], "ignored")
            result["ignored"] += 1
        elif rule == "always":
            upload.append(d["id"])
    if upload:
        r = upload_ids(upload)
        result.update(uploaded=r["uploaded"], failed=r["failed"], error=r["error"])
    return result


def upload_ids(ids: list[int]) -> dict:
    """Lädt manuell ausgewählte Dokumente hoch (Status pending oder failed)."""
    result = {"uploaded": 0, "failed": 0, "error": None}
    settings = config.load_settings()
    client = _client(settings)
    if not client:
        result["error"] = "Kein Lexware-API-Key hinterlegt."
        return result
    with _lock:
        for doc_id in ids:
            doc = db.get_document(doc_id)
            if doc and doc["status"] in ("pending", "failed"):
                result["uploaded" if _upload(doc, client) else "failed"] += 1
        try:
            if config.TOKEN_FILE.exists():
                _label_mails(settings, gmail.connect())
        except Exception as e:  # Label ist nachrangig; der nächste Durchlauf holt es nach
            log.warning("Labeln fehlgeschlagen: %s", e)
    return result

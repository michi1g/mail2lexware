"""Kernlogik: Gmail durchsuchen, bewerten, nach Lexware hochladen, Mails labeln."""
import hashlib
import logging
import threading
from datetime import timedelta
from pathlib import Path

import requests

from . import config, db, detect, gmail
from .lexware import LexwareClient, LexwareError

log = logging.getLogger("workspace2lexware")
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


def _scan(settings: dict, gmail_service) -> tuple[int, list[int]]:
    """Legt neue Anhänge in der DB an. Gibt (Anzahl neu, IDs für direkten Upload) zurück."""
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
        rule = db.match_rule(mail.sender_email)
        for att in mail.attachments:
            sha = hashlib.sha256(att.data).hexdigest()
            ext = Path(att.filename).suffix.lower()
            path = config.FILES_DIR / (sha + ext)
            if not path.exists():
                path.write_bytes(att.data)
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
            )
            new += 1
            if status == "pending" and auto:
                upload_ids.append(doc_id)
        db.add_message(mail.message_id)
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
    """Ein kompletter Durchlauf: scannen, automatisch Freigegebenes hochladen, Mails labeln."""
    if not _lock.acquire(blocking=False):
        return {"error": "Es läuft bereits ein Durchlauf."}
    result = {"time": db.now(), "new": 0, "uploaded": 0, "failed": 0, "labeled": 0, "error": None}
    try:
        settings = config.load_settings()
        client = _client(settings)
        gmail_service = gmail.connect()
        result["new"], ids = _scan(settings, gmail_service)
        ids += [d["id"] for d in db.retryable_failed()]
        if ids and not client:
            result["error"] = "Kein Lexware-API-Key hinterlegt. Uploads wurden übersprungen."
        elif client:
            for doc_id in ids:
                ok = _upload(db.get_document(doc_id), client)
                result["uploaded" if ok else "failed"] += 1
        result["labeled"] = _label_mails(settings, gmail_service)
    except gmail.NotConnected as e:
        result["error"] = str(e)
    except Exception as e:  # Netzwerk, widerrufener Token usw. sollen den Scheduler nicht beenden
        log.exception("Durchlauf fehlgeschlagen")
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        _lock.release()
    db.kv_set("last_run", result)
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
            _label_mails(settings, gmail.connect())
        except Exception as e:  # Label ist nachrangig; der nächste Durchlauf holt es nach
            log.warning("Labeln fehlgeschlagen: %s", e)
    return result

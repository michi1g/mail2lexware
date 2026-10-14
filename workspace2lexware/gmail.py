import base64
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Callable, Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from . import config

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # lesen + Labels setzen
# Der Login läuft über eine nicht erreichbare localhost-Seite; die URL kopiert der Nutzer in die GUI.
REDIRECT_URI = "http://localhost:8080/"

_pending_flow: Flow | None = None


@dataclass
class Attachment:
    msg_key: str
    filename: str
    mime_type: str
    data: bytes


@dataclass
class Mail:
    message_id: str
    subject: str
    sender: str
    sender_email: str
    date: str
    attachments: list[Attachment] = field(default_factory=list)


class NotConnected(RuntimeError):
    pass


# --- Login -----------------------------------------------------------------

def _load_credentials() -> Credentials | None:
    if not config.TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(config.TOKEN_FILE, SCOPES)
    if not creds.has_scopes(SCOPES):
        return None
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            config.TOKEN_FILE.write_text(creds.to_json())
        else:
            return None
    return creds


def connect():
    creds = _load_credentials()
    if not creds:
        raise NotConnected("Gmail ist nicht verbunden")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def profile_email() -> str:
    return connect().users().getProfile(userId="me").execute()["emailAddress"]


def start_login() -> str:
    """Gibt die Google-Login-URL zurück. Der Flow bleibt im Speicher, bis finish_login() aufgerufen wird."""
    global _pending_flow
    if not config.CREDENTIALS_FILE.exists():
        raise NotConnected("credentials.json fehlt")
    _pending_flow = Flow.from_client_secrets_file(str(config.CREDENTIALS_FILE), SCOPES, redirect_uri=REDIRECT_URI)
    url, _ = _pending_flow.authorization_url(access_type="offline", prompt="consent")
    return url


def finish_login(pasted: str) -> None:
    """Nimmt die kopierte localhost-URL (oder nur den code) entgegen und speichert den Token."""
    if _pending_flow is None:
        raise NotConnected("Login wurde nicht gestartet (oder der Container wurde neu gestartet)")
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"  # Redirect-URL ist http://localhost
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    pasted = pasted.strip()
    if pasted.startswith("http"):
        _pending_flow.fetch_token(authorization_response=pasted)
    else:
        _pending_flow.fetch_token(code=pasted)
    config.TOKEN_FILE.write_text(_pending_flow.credentials.to_json())


def disconnect() -> None:
    config.TOKEN_FILE.unlink(missing_ok=True)


# --- Labels ----------------------------------------------------------------

def ensure_label(service, name: str) -> str:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"] == name:
            return label["id"]
    return service.users().labels().create(userId="me", body={"name": name}).execute()["id"]


def add_label(service, message_id: str, label_id: str) -> None:
    service.users().messages().modify(userId="me", id=message_id, body={"addLabelIds": [label_id]}).execute()


# --- Mails -----------------------------------------------------------------

def _walk_parts(payload: dict) -> Iterator[dict]:
    yield payload
    for part in payload.get("parts", []):
        yield from _walk_parts(part)


def iter_mails(service, query: str, extensions: set[str], min_size: int,
               known: Callable[[str], bool]) -> Iterator[Mail]:
    """Liefert pro noch unbekannter Mail die passenden Anhänge (Daten bereits geladen)."""
    page_token = None
    while True:
        resp = service.users().messages().list(userId="me", q=query, pageToken=page_token, maxResults=100).execute()
        for ref in resp.get("messages", []):
            if known(ref["id"]):
                continue
            msg = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
            name, addr = parseaddr(headers.get("from", ""))
            mail = Mail(
                message_id=msg["id"],
                subject=headers.get("subject", ""),
                sender=name or addr,
                sender_email=addr.lower(),
                date=datetime.fromtimestamp(int(msg["internalDate"]) / 1000, timezone.utc).isoformat(timespec="seconds"),
            )
            for part in _walk_parts(msg["payload"]):
                filename = part.get("filename") or ""
                att_id = part.get("body", {}).get("attachmentId")
                if not filename or not att_id:
                    continue
                if Path(filename).suffix.lower() not in extensions:
                    continue
                if part["body"].get("size", 0) < min_size:
                    continue
                raw = service.users().messages().attachments().get(
                    userId="me", messageId=msg["id"], id=att_id
                ).execute()
                mail.attachments.append(Attachment(
                    msg_key=f"{msg['id']}:{part.get('partId', att_id[:16])}",
                    filename=filename,
                    mime_type=part.get("mimeType", "application/octet-stream"),
                    data=base64.urlsafe_b64decode(raw["data"]),
                ))
            yield mail
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

"""Microsoft-365-/Outlook.com-Anbindung: OAuth 2.0 (Code-Flow mit PKCE) und XOAUTH2 für IMAP.

Das Modul speichert nichts; es arbeitet nur mit Dicts, die der Aufrufer ablegt.
"""
import base64
import hashlib
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

import requests

SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
IMAP_HOST = "outlook.office365.com"
IMAP_PORT = 993
REDIRECT_URI = "http://localhost"

_AUTHORITY = "https://login.microsoftonline.com"
_TIMEOUT = 20
_REFRESH_MARGIN = 120  # Sekunden


class MicrosoftAuthError(RuntimeError):
    """Fehler bei der Microsoft-Anmeldung (Meldung ist für Endnutzer gedacht)."""


def _validate(client_id: str, tenant: str | None) -> tuple[str, str]:
    client_id = (client_id or "").strip()
    tenant = (tenant or "").strip() or "common"
    if not client_id:
        raise MicrosoftAuthError("Die Client-ID (Anwendungs-ID) fehlt.")
    if not re.fullmatch(r"[A-Za-z0-9-]+", client_id):
        raise MicrosoftAuthError("Die Client-ID enthält ungültige Zeichen.")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", tenant):
        raise MicrosoftAuthError("Der Mandant (Tenant) enthält ungültige Zeichen.")
    return client_id, tenant


def new_login(client_id: str, tenant: str, email: str) -> tuple[str, dict]:
    """Gibt (login_url, pending) zurück; pending ist JSON-serialisierbar und wird für finish_login gebraucht."""
    client_id, tenant = _validate(client_id, tenant)
    verifier = secrets.token_urlsafe(64)  # 86 Zeichen
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "response_mode": "query",
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    if email:
        params["login_hint"] = email
    url = f"{_AUTHORITY}/{tenant}/oauth2/v2.0/authorize?{urlencode(params)}"
    pending = {
        "code_verifier": verifier,
        "state": state,
        "client_id": client_id,
        "tenant": tenant,
        "email": email,
        "redirect_uri": REDIRECT_URI,
    }
    return url, pending


def _token_request(tenant: str, data: dict) -> dict:
    """POST an den Token-Endpunkt; übersetzt Fehler in MicrosoftAuthError (ohne Tokens in Meldungen)."""
    try:
        resp = requests.post(f"{_AUTHORITY}/{tenant}/oauth2/v2.0/token", data=data, timeout=_TIMEOUT)
    except requests.RequestException as e:
        raise MicrosoftAuthError(f"Microsoft ist nicht erreichbar ({type(e).__name__}). Bitte Internetverbindung prüfen.") from None
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise MicrosoftAuthError(f"Unerwartete Antwort von Microsoft (HTTP {resp.status_code}).")
    if resp.status_code >= 400 or "error" in body:
        err = body.get("error", "unbekannt")
        desc = str(body.get("error_description", "")).split("\r\n")[0].split("\n")[0]
        if err == "invalid_grant":
            raise MicrosoftAuthError(
                "Die Anmeldung ist abgelaufen oder wurde widerrufen. Bitte die Anmeldung erneut durchführen."
                + (f" ({desc})" if desc else ""))
        raise MicrosoftAuthError(f"Microsoft hat die Anmeldung abgelehnt: {err}" + (f" - {desc}" if desc else ""))
    if not body.get("access_token"):
        raise MicrosoftAuthError("Microsoft hat kein Zugriffstoken geliefert.")
    return body


def _expires_at(body: dict, now: float) -> int:
    try:
        return int(now + int(body.get("expires_in", 3600)))
    except (TypeError, ValueError):
        return int(now + 3600)


def finish_login(pending: dict, pasted: str, now: float | None = None) -> dict:
    """Nimmt die kopierte localhost-Adresse (oder nur den code) und tauscht ihn gegen Tokens."""
    pasted = (pasted or "").strip()
    if not pasted:
        raise MicrosoftAuthError("Bitte die Adresse aus der Browser-Adressleiste einfügen.")
    if pasted.startswith("http"):
        query = parse_qs(urlparse(pasted).query)
        if "error" in query:
            desc = query.get("error_description", [""])[0]
            raise MicrosoftAuthError(f"Microsoft meldet einen Fehler: {query['error'][0]}" + (f" - {desc}" if desc else ""))
        if query.get("state", [None])[0] != pending.get("state"):
            raise MicrosoftAuthError("Der state-Wert passt nicht zur gestarteten Anmeldung. Bitte den Login neu starten.")
        code = query.get("code", [""])[0]
        if not code:
            raise MicrosoftAuthError("In der Adresse wurde kein code gefunden.")
    else:
        code = pasted
    body = _token_request(pending["tenant"], {
        "grant_type": "authorization_code",
        "client_id": pending["client_id"],
        "code": code,
        "redirect_uri": pending.get("redirect_uri", REDIRECT_URI),
        "code_verifier": pending["code_verifier"],
        "scope": SCOPE,
    })
    if not body.get("refresh_token"):
        raise MicrosoftAuthError("Microsoft hat kein Refresh-Token geliefert. Bitte prüfen, ob der Scope offline_access erlaubt ist.")
    return {
        "client_id": pending["client_id"],
        "tenant": pending["tenant"],
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": _expires_at(body, time.time() if now is None else now),
    }


def ensure_access_token(tokens: dict, now: float | None = None) -> tuple[str, dict, bool]:
    """Gibt (access_token, tokens, changed) zurück und erneuert das Token, wenn es bald abläuft."""
    now = time.time() if now is None else now
    tokens = dict(tokens)
    if tokens.get("access_token") and tokens.get("expires_at", 0) - now >= _REFRESH_MARGIN:
        return tokens["access_token"], tokens, False
    body = _token_request(tokens.get("tenant") or "common", {
        "grant_type": "refresh_token",
        "client_id": tokens["client_id"],
        "refresh_token": tokens["refresh_token"],
        "scope": SCOPE,
    })
    tokens["access_token"] = body["access_token"]
    tokens["refresh_token"] = body.get("refresh_token") or tokens["refresh_token"]
    tokens["expires_at"] = _expires_at(body, now)
    return tokens["access_token"], tokens, True


def xoauth2_string(email: str, access_token: str) -> str:
    """XOAUTH2-Initialantwort (roh, nicht base64)."""
    return f"user={email}\x01auth=Bearer {access_token}\x01\x01"

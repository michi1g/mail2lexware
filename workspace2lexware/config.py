import json
import os
import secrets
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
FILES_DIR = DATA_DIR / "files"
SETTINGS_FILE = DATA_DIR / "settings.json"
CREDENTIALS_FILE = DATA_DIR / "credentials.json"
TOKEN_FILE = DATA_DIR / "token.json"
SECRET_FILE = DATA_DIR / "secret_key"
DB_FILE = DATA_DIR / "state.db"

DEFAULTS = {
    "query": "has:attachment (rechnung OR invoice OR rechnungsnummer OR beleg) -in:sent -in:spam -in:trash",
    "lookback_days": 30,
    "extensions": [".pdf", ".xml"],
    "min_size_bytes": 3000,
    "label": "Lexware-übertragen",
    "interval_minutes": 60,  # 0 = kein automatischer Abruf
    "mode": "review",  # review: alles zur Prüfung; auto: Treffer ab auto_threshold direkt hochladen
    "auto_threshold": 3,
    "lexware_api_key": "",
    "base_url": "https://api.lexware.io/v1",
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    return settings


def save_settings(settings: dict) -> None:
    ensure_dirs()
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        SETTINGS_FILE.chmod(0o600)  # enthält den API-Key
    except OSError:
        pass


def api_key(settings: dict) -> str:
    return os.environ.get("LEXWARE_API_KEY") or settings.get("lexware_api_key", "")


def secret_key() -> bytes:
    ensure_dirs()
    if not SECRET_FILE.exists():
        SECRET_FILE.write_bytes(secrets.token_bytes(32))
        try:
            SECRET_FILE.chmod(0o600)
        except OSError:
            pass
    return SECRET_FILE.read_bytes()

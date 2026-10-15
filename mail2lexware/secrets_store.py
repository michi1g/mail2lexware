"""Verschlüsselte Ablage von Zugangsdaten (Fernet, AES-128-CBC mit HMAC).

Der Schlüssel kommt aus der Umgebungsvariable W2L_ENCRYPTION_KEY (dann liegt er nicht neben den Daten)
oder wird beim ersten Start in /data/encryption.key erzeugt. Ohne Umgebungsvariable schützt die
Verschlüsselung vor versehentlich weitergegebenen Einzeldateien (Backup, Screenshot, Support), nicht vor
jemandem, der das ganze Datenverzeichnis kopiert.
"""
import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import config

PREFIX = "enc:v1:"
ENV_NAME = "W2L_ENCRYPTION_KEY"
KEY_FILE = config.DATA_DIR / "encryption.key"


class SecretError(RuntimeError):
    pass


def _env_key() -> bytes | None:
    env = os.environ.get(ENV_NAME, "").strip()
    if not env:
        return None
    try:  # gültiger Fernet-Schlüssel (44 Zeichen Base64) direkt verwenden
        Fernet(env.encode())
        return env.encode()
    except ValueError:  # sonst beliebige Zeichenfolge in einen Schlüssel umrechnen
        return base64.urlsafe_b64encode(hashlib.sha256(env.encode()).digest())


def _file_key(create: bool) -> bytes | None:
    if not KEY_FILE.exists():
        if not create:
            return None
        config.ensure_dirs()
        KEY_FILE.write_bytes(Fernet.generate_key())
        _restrict(KEY_FILE)
    return KEY_FILE.read_bytes().strip()


def _key() -> bytes:
    """Schlüssel zum Verschlüsseln: Umgebungsvariable, sonst die Schlüsseldatei."""
    return _env_key() or _file_key(create=True)


def _decrypt_keys() -> list[bytes]:
    """Kandidaten zum Entschlüsseln. Wird die Umgebungsvariable erst nach dem ersten Start gesetzt, bleiben
    ältere Daten über die vorhandene Schlüsseldatei lesbar."""
    keys = [k for k in (_env_key(), _file_key(create=False)) if k]
    return keys or [_key()]


def _restrict(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(text: str) -> str:
    return PREFIX + Fernet(_key()).encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    """Entschlüsselt; Klartext aus Altbeständen (ohne Präfix) wird unverändert zurückgegeben."""
    if not is_encrypted(value):
        return value
    token = value[len(PREFIX):].encode("ascii")
    for key in _decrypt_keys():
        try:
            return Fernet(key).decrypt(token).decode("utf-8")
        except InvalidToken:
            continue
    raise SecretError(
        "Gespeicherte Zugangsdaten lassen sich nicht entschlüsseln. Der Schlüssel (W2L_ENCRYPTION_KEY bzw. "
        "encryption.key im Datenordner) passt nicht zu den Daten."
    )


def encrypt_json(obj) -> str:
    return encrypt(json.dumps(obj, ensure_ascii=False))


def decrypt_json(value: str):
    return json.loads(decrypt(value))


def write_file(path: Path, text: str) -> None:
    path.write_text(encrypt(text), encoding="ascii")
    _restrict(path)


def read_file(path: Path) -> str:
    return decrypt(path.read_text(encoding="utf-8"))


def encrypt_file_in_place(path: Path) -> bool:
    """Verschlüsselt eine noch unverschlüsselte Datei. True, wenn etwas geändert wurde."""
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    if is_encrypted(content):
        return False
    write_file(path, content)
    return True

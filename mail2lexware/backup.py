"""Datensicherung: ZIP mit Datenbank, Einstellungen, Zugangsdaten und (optional) Belegdateien.

Zugangsdaten liegen im Backup so verschlüsselt wie im Datenordner. Damit eine Sicherung auf einer neuen
Installation lesbar ist, kommt die Schlüsseldatei encryption.key mit ins ZIP. Wer den Schlüssel per
W2L_ENCRYPTION_KEY setzt, muss ihn beim Wiederherstellen ebenfalls setzen.
"""
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from . import config, db, secrets_store, service

MANIFEST = "mail2lexware-backup.json"
FORMAT = 1
DB_NAME = "state.db"
KEY_NAME = "encryption.key"
FILE_RE = re.compile(r"^files/[0-9a-f]{64}\.[a-z0-9]{1,5}$")
KEEP_SAFETY_BACKUPS = 5


class BackupError(RuntimeError):
    pass


def _config_files() -> dict[str, Path]:
    """Dateien, die eine Wiederherstellung exakt auf den Stand der Sicherung bringt (fehlt eine, wird sie entfernt)."""
    return {"settings.json": config.SETTINGS_FILE, "credentials.json": config.CREDENTIALS_FILE,
            "token.json": config.TOKEN_FILE}


def safety_dir() -> Path:
    return config.DATA_DIR / "backups"


def _restrict(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _snapshot_db(target: Path) -> None:
    """Konsistente Kopie der laufenden Datenbank (auch mit WAL) über die SQLite-Backup-API."""
    src = sqlite3.connect(config.DB_FILE, timeout=30)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def filename() -> str:
    return f"mail2lexware-backup-{datetime.now():%Y-%m-%d-%H%M}.zip"


def create(target: BinaryIO | Path, include_files: bool = True) -> dict:
    """Schreibt ein Backup-ZIP nach target und gibt das Manifest zurück."""
    config.ensure_dirs()
    db.init()
    tmp = tempfile.NamedTemporaryFile(dir=config.DATA_DIR, suffix=".db", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    try:
        _snapshot_db(tmp_path)
        c = sqlite3.connect(tmp_path)
        try:
            n_docs = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        finally:
            c.close()
        files = sorted(p for p in config.FILES_DIR.iterdir() if p.is_file()) if include_files else []
        manifest = {
            "app": "mail2lexware",
            "format": FORMAT,
            "created_at": db.now(),
            "documents": n_docs,
            "files": len(files),
            "includes_files": include_files,
            "includes_key": secrets_store.KEY_FILE.exists(),
            "env_key": bool(os.environ.get(secrets_store.ENV_NAME, "").strip()),
        }
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr(MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
            z.write(tmp_path, DB_NAME)
            for name, path in _config_files().items():
                if path.exists():
                    z.write(path, name)
            if manifest["includes_key"]:
                z.write(secrets_store.KEY_FILE, KEY_NAME)
            for p in files:
                # PDFs und Bilder sind schon komprimiert; nochmal packen kostet nur Zeit
                kind = zipfile.ZIP_DEFLATED if p.suffix.lower() == ".xml" else zipfile.ZIP_STORED
                z.write(p, f"files/{p.name}", compress_type=kind)
        return manifest
    finally:
        tmp_path.unlink(missing_ok=True)


def _read_manifest(z: zipfile.ZipFile) -> dict:
    try:
        manifest = json.loads(z.read(MANIFEST))
    except KeyError:
        raise BackupError("Das ist kein Backup von mail2lexware (Manifest fehlt).") from None
    except ValueError:
        raise BackupError("Das Manifest im Backup ist beschädigt.") from None
    if manifest.get("app") != "mail2lexware":
        raise BackupError("Das ist kein Backup von mail2lexware.")
    if int(manifest.get("format", 0)) > FORMAT:
        raise BackupError("Das Backup stammt aus einer neueren Version. Aktualisiere zuerst den Container.")
    if DB_NAME not in z.namelist():
        raise BackupError("Im Backup fehlt die Datenbank.")
    return manifest


def _check_db(path: Path) -> None:
    try:
        c = sqlite3.connect(path)
        try:
            ok = c.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            c.close()
    except sqlite3.DatabaseError:
        raise BackupError("Die Datenbank im Backup ist beschädigt.") from None
    if ok != "ok" or "documents" not in tables:
        raise BackupError("Die Datenbank im Backup ist beschädigt oder unvollständig.")


def _prune_safety_backups() -> None:
    old = sorted(safety_dir().glob("vor-wiederherstellung-*.zip"))
    for p in old[:-KEEP_SAFETY_BACKUPS]:
        p.unlink(missing_ok=True)


def _decrypt_problems() -> str | None:
    """Prüft nach dem Einspielen, ob sich die Zugangsdaten mit dem jetzigen Schlüssel entschlüsseln lassen."""
    try:
        config.load_settings()
        for path in (config.TOKEN_FILE, config.CREDENTIALS_FILE):
            if path.exists():
                secrets_store.read_file(path)
        for account in db.list_accounts():
            secrets_store.decrypt(account["secret"])
    except secrets_store.SecretError:
        hint = (" Setze W2L_ENCRYPTION_KEY auf den Wert der alten Installation und starte den Container neu."
                if not secrets_store.KEY_FILE.exists() or os.environ.get(secrets_store.ENV_NAME) else "")
        return ("Belege, Verlauf und Regeln sind wiederhergestellt, die Zugangsdaten lassen sich aber nicht "
                "entschlüsseln." + hint)
    return None


def restore(source: BinaryIO | Path) -> dict:
    """Spielt ein Backup ein. Vorher wird der aktuelle Stand (ohne Belegdateien) unter backups/ gesichert."""
    config.ensure_dirs()
    try:
        z = zipfile.ZipFile(source)
    except zipfile.BadZipFile:
        raise BackupError("Die Datei ist kein gültiges ZIP-Archiv.") from None

    if not service._lock.acquire(blocking=False):
        z.close()
        raise BackupError("Es läuft gerade ein Durchlauf oder Upload. Warte, bis er fertig ist.")
    tmp_path = None
    try:
        manifest = _read_manifest(z)
        names = set(z.namelist())
        file_names = sorted(n for n in names if FILE_RE.match(n))

        tmp = tempfile.NamedTemporaryFile(dir=config.DATA_DIR, suffix=".db", delete=False)
        with tmp:
            with z.open(DB_NAME) as src:
                while chunk := src.read(1024 * 1024):
                    tmp.write(chunk)
        tmp_path = Path(tmp.name)
        _check_db(tmp_path)

        # Sicherheitskopie des jetzigen Stands, falls das falsche Backup erwischt wurde
        safety_dir().mkdir(parents=True, exist_ok=True)
        safety = safety_dir() / f"vor-wiederherstellung-{datetime.now():%Y-%m-%d-%H%M%S}.zip"
        create(safety, include_files=False)
        _restrict(safety)
        _prune_safety_backups()

        # Datenbank über die Backup-API einspielen: ersetzt den Inhalt sauber, auch im WAL-Modus
        src = sqlite3.connect(tmp_path)
        dst = sqlite3.connect(config.DB_FILE, timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        db.init()  # Migrationen für Backups aus älteren Versionen

        if KEY_NAME in names:
            secrets_store.KEY_FILE.write_bytes(z.read(KEY_NAME))
            _restrict(secrets_store.KEY_FILE)
        for name, path in _config_files().items():
            if name in names:
                path.write_bytes(z.read(name))
                _restrict(path)
            else:
                path.unlink(missing_ok=True)

        restored_files = 0
        for name in file_names:
            target = config.FILES_DIR / name.split("/", 1)[1]
            if target.exists():  # Dateiname ist der SHA-256: gleicher Name, gleicher Inhalt
                continue
            with z.open(name) as src_file, open(target, "wb") as out:
                while chunk := src_file.read(1024 * 1024):
                    out.write(chunk)
            restored_files += 1

        return {
            "created_at": manifest.get("created_at"),
            "documents": sum(db.counts().values()),
            "files": len(file_names),
            "new_files": restored_files,
            "accounts": len(db.list_accounts()),
            "rules": len(db.list_rules()),
            "safety_backup": safety.name,
            "warning": _decrypt_problems(),
        }
    finally:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)
        z.close()
        service._lock.release()

import io
import json
import re
import zipfile

import pytest

from mail2lexware import backup, config, db, secrets_store, service
from mail2lexware.web import create_app

SHA = "a" * 64


@pytest.fixture(scope="module")
def client():
    app = create_app(start_scheduler=False)
    app.config["TESTING"] = True
    return app.test_client()


def csrf_of(client):
    html = client.get("/settings").get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _add_doc(msg_key="m1:1"):
    return db.add_document(msg_key=msg_key, message_id="backup-test", sha256=SHA, filename="r.pdf", mime_type="application/pdf",
                           size=10, sender_email="x@firma.de", subject="Rechnung", status="pending")


def _clear_docs():
    with db.conn() as c:
        c.execute("DELETE FROM documents")
        c.execute("DELETE FROM rules")


@pytest.fixture(autouse=True)
def _aufraeumen():
    """Die Test-Datenbank ist modulübergreifend; nichts aus diesen Tests darf andere beeinflussen."""
    yield
    _clear_docs()
    s = config.load_settings()
    s["lexware_api_key"] = ""
    config.save_settings(s)


def test_roundtrip_restores_db_settings_and_files():
    _clear_docs()
    _add_doc()
    db.add_rule("firma.de", "always")
    s = config.load_settings()
    s["lexware_api_key"] = "geheim-123"
    config.save_settings(s)
    (config.FILES_DIR / f"{SHA}.pdf").write_bytes(b"%PDF-1.4 test")

    buf = io.BytesIO()
    manifest = backup.create(buf)
    assert manifest["documents"] == 1 and manifest["includes_key"]
    names = zipfile.ZipFile(io.BytesIO(buf.getvalue())).namelist()
    assert {"state.db", "settings.json", "encryption.key", f"files/{SHA}.pdf"} <= set(names)

    # Stand verändern, dann zurückspielen
    _clear_docs()
    (config.FILES_DIR / f"{SHA}.pdf").unlink()
    s["lexware_api_key"] = "anderer"
    config.save_settings(s)

    r = backup.restore(io.BytesIO(buf.getvalue()))
    assert r["documents"] == 1 and r["rules"] == 1 and r["new_files"] == 1 and r["warning"] is None
    assert config.load_settings()["lexware_api_key"] == "geheim-123"
    assert (config.FILES_DIR / f"{SHA}.pdf").read_bytes() == b"%PDF-1.4 test"
    assert (backup.safety_dir() / r["safety_backup"]).exists()


def test_backup_without_files():
    (config.FILES_DIR / f"{SHA}.pdf").write_bytes(b"x")
    buf = io.BytesIO()
    backup.create(buf, include_files=False)
    assert not any(n.startswith("files/") for n in zipfile.ZipFile(buf).namelist())


def test_restore_rejects_foreign_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("irgendwas.txt", "hallo")
    with pytest.raises(backup.BackupError, match="Manifest"):
        backup.restore(buf)
    with pytest.raises(backup.BackupError, match="ZIP"):
        backup.restore(io.BytesIO(b"kein zip"))


def test_restore_ignores_unsafe_paths_and_bad_db():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(backup.MANIFEST, json.dumps({"app": "mail2lexware", "format": 1}))
        z.writestr("state.db", b"das ist keine datenbank")
        z.writestr("../evil.txt", "x")
    with pytest.raises(backup.BackupError, match="beschädigt"):
        backup.restore(buf)
    assert not (config.DATA_DIR.parent / "evil.txt").exists()


def test_restore_refused_while_running():
    buf = io.BytesIO()
    backup.create(buf, include_files=False)
    with service._lock:
        with pytest.raises(backup.BackupError, match="Durchlauf"):
            backup.restore(io.BytesIO(buf.getvalue()))


def test_restore_warns_when_key_does_not_match():
    _clear_docs()
    s = config.load_settings()
    s["lexware_api_key"] = "geheim"
    config.save_settings(s)
    buf = io.BytesIO()
    backup.create(buf, include_files=False)
    # Backup ohne Schlüsseldatei, dann eigenen Schlüssel neu erzeugen: Zugangsdaten nicht mehr lesbar
    stripped = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as src, zipfile.ZipFile(stripped, "w") as dst:
        for n in src.namelist():
            if n != "encryption.key":
                dst.writestr(n, src.read(n))
    old_key = secrets_store.KEY_FILE.read_bytes()
    try:
        secrets_store.KEY_FILE.unlink()
        secrets_store.encrypt("x")  # erzeugt neuen Schlüssel
        r = backup.restore(stripped)
        assert r["warning"] and "entschlüsseln" in r["warning"]
    finally:
        secrets_store.KEY_FILE.write_bytes(old_key)


def test_web_download_and_restore(client):
    _clear_docs()
    _add_doc("m9:1")
    token = csrf_of(client)
    resp = client.post("/backup/download", data={"csrf": token, "include_files": "1"})
    assert resp.status_code == 200 and resp.mimetype == "application/zip"
    data = resp.get_data()
    resp.close()
    assert "state.db" in zipfile.ZipFile(io.BytesIO(data)).namelist()

    _clear_docs()
    resp = client.post("/backup/restore", data={"csrf": token, "file": (io.BytesIO(data), "b.zip")},
                       content_type="multipart/form-data", follow_redirects=True)
    assert "Backup eingespielt" in resp.get_data(as_text=True)
    assert db.counts().get("pending") == 1


def test_web_restore_requires_csrf(client):
    resp = client.post("/backup/restore", data={"file": (io.BytesIO(b"x"), "b.zip")},
                       content_type="multipart/form-data")
    assert resp.status_code == 400

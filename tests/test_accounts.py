import json
import re
import sqlite3

import pytest

from workspace2lexware import config, db, gmail, imap_source, microsoft, secrets_store, service
from workspace2lexware.web import create_app


@pytest.fixture(scope="module")
def client():
    app = create_app(start_scheduler=False)
    app.config["TESTING"] = True
    return app.test_client()


def csrf(client, path="/connections"):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(path).get_data(as_text=True)).group(1)


# --- Verschlüsselung ---------------------------------------------------------

def test_encrypt_roundtrip_and_legacy_plaintext():
    token = secrets_store.encrypt("dummy-passwort")
    assert token.startswith("enc:v1:") and "dummy-passwort" not in token
    assert secrets_store.decrypt(token) == "dummy-passwort"
    assert secrets_store.decrypt("klartext-aus-altbestand") == "klartext-aus-altbestand"
    assert secrets_store.decrypt_json(secrets_store.encrypt_json({"a": "ä"})) == {"a": "ä"}


def test_env_key_set_later_keeps_old_data_readable(monkeypatch):
    old = secrets_store.encrypt("dummy-alt")  # mit der Schlüsseldatei verschlüsselt
    monkeypatch.setenv(secrets_store.ENV_NAME, "spaeter-gesetzter-schluessel")
    assert secrets_store.decrypt(old) == "dummy-alt"
    neu = secrets_store.encrypt("dummy-neu")  # neue Daten nutzen die Umgebungsvariable
    assert secrets_store.decrypt(neu) == "dummy-neu"
    monkeypatch.delenv(secrets_store.ENV_NAME)
    with pytest.raises(secrets_store.SecretError):
        secrets_store.decrypt(neu)  # ohne die Variable ist der neue Schlüssel nicht mehr da


def test_wrong_key_gives_clear_error(monkeypatch):
    monkeypatch.setenv(secrets_store.ENV_NAME, "schluessel-a")
    token = secrets_store.encrypt("dummy")
    monkeypatch.setenv(secrets_store.ENV_NAME, "schluessel-b")
    with pytest.raises(secrets_store.SecretError, match="entschlüsseln"):
        secrets_store.decrypt(token)


def test_lexware_key_is_encrypted_at_rest():
    settings = config.load_settings()
    settings["lexware_api_key"] = "dummy-lexware-key"
    config.save_settings(settings)
    on_disk = config.SETTINGS_FILE.read_text(encoding="utf-8")
    assert "dummy-lexware-key" not in on_disk and "enc:v1:" in on_disk
    assert config.load_settings()["lexware_api_key"] == "dummy-lexware-key"


def test_legacy_plaintext_files_are_migrated():
    raw = json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8"))
    raw["lexware_api_key"] = "dummy-altbestand"
    config.SETTINGS_FILE.write_text(json.dumps(raw), encoding="utf-8")
    config.TOKEN_FILE.write_text('{"refresh_token": "dummy-refresh"}', encoding="utf-8")
    config.CREDENTIALS_FILE.write_text('{"installed": {"client_secret": "dummy-secret"}}', encoding="utf-8")

    config.migrate_secrets()

    for path, needle in [(config.SETTINGS_FILE, "dummy-altbestand"), (config.TOKEN_FILE, "dummy-refresh"),
                         (config.CREDENTIALS_FILE, "dummy-secret")]:
        assert needle not in path.read_text(encoding="utf-8"), path.name
    assert config.load_settings()["lexware_api_key"] == "dummy-altbestand"
    assert json.loads(secrets_store.read_file(config.TOKEN_FILE))["refresh_token"] == "dummy-refresh"
    config.migrate_secrets()  # zweiter Lauf ändert nichts mehr
    assert json.loads(secrets_store.read_file(config.TOKEN_FILE))["refresh_token"] == "dummy-refresh"


# --- Datenbank ---------------------------------------------------------------

def test_old_database_is_migrated(monkeypatch, tmp_path):
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as c:
        c.executescript("""CREATE TABLE documents (id INTEGER PRIMARY KEY, msg_key TEXT UNIQUE NOT NULL,
            message_id TEXT NOT NULL, sha256 TEXT NOT NULL, filename TEXT NOT NULL, mime_type TEXT NOT NULL,
            size INTEGER NOT NULL, sender TEXT, sender_email TEXT, subject TEXT, mail_date TEXT, score INTEGER DEFAULT 0,
            status TEXT NOT NULL, lexware_id TEXT, error TEXT, attempts INTEGER DEFAULT 0, labeled INTEGER DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO documents (msg_key, message_id, sha256, filename, mime_type, size, status, created_at, updated_at)
            VALUES ('m:1', 'm', 'x', 'a.pdf', 'application/pdf', 1, 'uploaded', 't', 't');""")
    monkeypatch.setattr(config, "DB_FILE", old)
    db.init()
    row = db.list_documents()[0]
    assert (row["account_id"], row["source"]) == ("google", "Gmail")
    assert db.messages_to_label() == ["m"]


# --- Postfächer über die Oberfläche -----------------------------------------

def test_add_imap_account_stores_password_encrypted(client, monkeypatch):
    calls = {}
    monkeypatch.setattr(imap_source, "test_connection",
                        lambda cfg, password=None, access_token=None: calls.update(cfg=cfg, pw=password) or 12)
    r = client.post("/accounts/add", data={"csrf": csrf(client), "provider": "gmx", "email": "max@gmx.de",
                                          "password": "dummy-app-passwort", "keywords": "rechnung"})
    assert r.status_code == 302
    assert (calls["cfg"].host, calls["cfg"].port, calls["cfg"].security) == ("imap.gmx.net", 993, "ssl")
    assert calls["cfg"].username == "max@gmx.de" and calls["pw"] == "dummy-app-passwort"
    row = db.list_accounts()[-1]
    assert row["secret"].startswith("enc:v1:") and "dummy-app-passwort" not in row["secret"]
    assert secrets_store.decrypt_json(row["secret"]) == {"password": "dummy-app-passwort"}
    page = client.get("/connections").get_data(as_text=True)
    assert "GMX" in page and "max@gmx.de" in page and "dummy-app-passwort" not in page


def test_add_account_with_wrong_login_is_not_saved(client, monkeypatch):
    def fail(cfg, password=None, access_token=None):
        raise imap_source.ImapError("Anmeldung fehlgeschlagen. Prüfe Benutzername und Passwort.")
    monkeypatch.setattr(imap_source, "test_connection", fail)
    before = len(db.list_accounts())
    r = client.post("/accounts/add", data={"csrf": csrf(client), "provider": "custom", "email": "a@b.de",
                                          "password": "falsch", "host": "imap.beispiel.de", "port": "993"})
    assert r.status_code == 200 and "Anmeldung fehlgeschlagen" in r.get_data(as_text=True)
    assert len(db.list_accounts()) == before


def test_custom_provider_uses_entered_server(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(imap_source, "test_connection", lambda cfg, password=None, access_token=None: seen.update(cfg=cfg) or 0)
    client.post("/accounts/add", data={"csrf": csrf(client), "provider": "custom", "email": "a@b.de", "password": "dummy",
                                      "host": "mail.eigener-server.de", "port": "143", "security": "starttls",
                                      "username": "benutzer1"})
    cfg = seen["cfg"]
    assert (cfg.host, cfg.port, cfg.security, cfg.username) == ("mail.eigener-server.de", 143, "starttls", "benutzer1")


def test_microsoft_flow_and_account_actions(client, monkeypatch):
    r = client.post("/accounts/microsoft/start", data={"csrf": csrf(client), "email": "chef@firma.de",
                                                      "client_id": "dummy-client-id", "tenant": "dummy-tenant.de"})
    assert r.status_code == 302
    page = client.get("/accounts/microsoft").get_data(as_text=True)
    assert "login.microsoftonline.com/dummy-tenant.de/oauth2/v2.0/authorize" in page
    tokens = {"client_id": "dummy-client-id", "tenant": "dummy-tenant.de", "access_token": "dummy-access",
              "refresh_token": "dummy-refresh", "expires_at": 4102444800}
    monkeypatch.setattr(microsoft, "finish_login", lambda pending, pasted: tokens)
    monkeypatch.setattr(imap_source, "test_connection", lambda cfg, password=None, access_token=None: 3)
    r = client.post("/accounts/microsoft/finish", data={"csrf": csrf(client, "/accounts/microsoft"), "pasted": "http://localhost/?code=x"})
    assert r.status_code == 302
    row = [a for a in db.list_accounts() if a["kind"] == "microsoft"][-1]
    assert (row["host"], row["username"]) == (microsoft.IMAP_HOST, "chef@firma.de")
    assert "dummy-refresh" not in row["secret"] and secrets_store.decrypt_json(row["secret"])["refresh_token"] == "dummy-refresh"

    ok = client.post(f"/accounts/{row['id']}/test", data={"csrf": csrf(client)}, follow_redirects=True)
    assert "Verbindung OK" in ok.get_data(as_text=True)
    assert db.get_account(row["id"])["last_status"] == "ok"
    client.post(f"/accounts/{row['id']}/delete", data={"csrf": csrf(client)})
    assert db.get_account(row["id"]) is None


def test_microsoft_start_validates_input(client):
    r = client.post("/accounts/microsoft/start", data={"csrf": csrf(client), "email": "chef@firma.de", "client_id": ""},
                    follow_redirects=True)
    assert "Client-ID" in r.get_data(as_text=True)


def test_new_account_pages_render(client):
    for path in ["/accounts/new", "/accounts/new?provider=tonline", "/accounts/microsoft"]:
        assert client.get(path).status_code == 200, path


# --- Durchlauf mit IMAP-Postfach ---------------------------------------------

def test_cycle_scans_imap_account_without_touching_gmail(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TOKEN_FILE", tmp_path / "kein-token.json")
    pdf = b"%PDF-1.4 Rechnungsnummer 7 Gesamtbetrag IBAN MwSt " + b"y" * 4000
    mail = gmail.Mail("imap:acc1:77:5", "Rechnung Juli", "Lieferant", "rechnung@lieferant-imap.de",
                      "2026-07-01T08:00:00+00:00",
                      [gmail.Attachment("imap:acc1:77:5:2", "rechnung-juli.pdf", "application/pdf", pdf)])
    seen = {}

    def fake_iter(cfg, since, until, extensions, min_size, keywords, known, account_id, password=None, access_token=None):
        seen.update(host=cfg.host, password=password, keywords=keywords, account_id=account_id)
        yield mail

    monkeypatch.setattr(imap_source, "iter_mails", fake_iter)
    monkeypatch.setattr(gmail, "connect", lambda: pytest.fail("Gmail darf nicht angefasst werden"))
    monkeypatch.setattr(service.LexwareClient, "upload_voucher_file", lambda self, *a: "lex-imap-1")
    db.add_rule("lieferant-imap.de", "always")
    settings = config.load_settings()
    settings.update(lexware_api_key="dummy-key", label="Lexware-übertragen")
    config.save_settings(settings)
    db.add_account("acc1", "imap", "Test-Postfach", "rechnung@lieferant-imap.de",
                   secrets_store.encrypt_json({"password": "dummy-pw"}), host="imap.test.de", port=993,
                   security="ssl", username="rechnung@lieferant-imap.de", keywords="rechnung, beleg")

    result = service.run_cycle()

    assert result["error"] is None and (result["new"], result["uploaded"], result["labeled"]) == (1, 1, 0)
    assert seen == {"host": "imap.test.de", "password": "dummy-pw", "keywords": ["rechnung", "beleg"], "account_id": "acc1"}
    doc = [d for d in db.list_documents("uploaded") if d["message_id"] == "imap:acc1:77:5"][0]
    assert (doc["account_id"], doc["source"], doc["lexware_id"]) == ("acc1", "Test-Postfach", "lex-imap-1")
    assert db.get_account("acc1")["last_status"] == "ok"
    assert "imap:acc1:77:5" not in db.messages_to_label()  # IMAP-Mails bekommen kein Gmail-Label


def test_cycle_continues_when_one_account_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TOKEN_FILE", tmp_path / "kein-token.json")

    def broken(*a, **k):
        raise imap_source.ImapError("Verbindung zu imap.test.de:993 nicht möglich: timed out")
        yield  # pragma: no cover

    monkeypatch.setattr(imap_source, "iter_mails", broken)
    db.add_account("acc2", "imap", "Kaputtes Postfach", "x@y.de", secrets_store.encrypt_json({"password": "dummy"}),
                   host="imap.test.de", port=993, security="ssl", username="x@y.de")
    result = service.run_cycle()
    assert "Kaputtes Postfach: Verbindung zu imap.test.de:993 nicht möglich" in result["error"]
    assert "nicht möglich" in db.get_account("acc2")["last_status"]

import re

import pytest

from workspace2lexware import db, detect
from workspace2lexware.web import create_app


@pytest.fixture(scope="module")
def client():
    app = create_app(start_scheduler=False)
    app.config["TESTING"] = True
    return app.test_client()


def csrf_of(client, path="/settings"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_score_invoice_text():
    text = "Rechnung\nRechnungsnummer 2024-17\nGesamtbetrag 119,00 EUR inkl. MwSt\nIBAN DE02 1203"
    assert detect.score("scan.pdf", text, ".pdf") >= 3


def test_score_non_invoice():
    assert detect.score("agb.pdf", "Allgemeine Geschäftsbedingungen und Widerrufsbelehrung", ".pdf") == 0


def test_score_xrechnung():
    assert detect.score("x.xml", "<rsm:CrossIndustryInvoice>", ".xml") == 10


def test_rules_exact_beats_domain():
    db.init()
    db.add_rule("firma.de", "always")
    db.add_rule("spam@firma.de", "never")
    assert db.match_rule("rechnung@firma.de") == "always"
    assert db.match_rule("spam@firma.de") == "never"
    assert db.match_rule("x@andere.de") is None


def test_label_only_when_all_docs_resolved():
    def doc(key, status):
        db.add_document(msg_key=key, message_id="m1", sha256=key, filename="a.pdf", mime_type="application/pdf",
                        size=1, status=status)
    doc("k1", "uploaded")
    doc("k2", "pending")
    assert "m1" not in db.messages_to_label()
    pending = db.list_documents("pending")[0]
    db.set_status(pending["id"], "ignored")
    assert "m1" in db.messages_to_label()
    db.mark_labeled("m1")
    assert "m1" not in db.messages_to_label()


def test_lexware_test_connection_403_fallback(monkeypatch):
    import pytest
    from workspace2lexware.lexware import LexwareClient, LexwareError

    class R:
        def __init__(self, status, body=None): self.status_code, self._b, self.ok, self.text = status, body or {}, status < 400, ""
        def json(self): return self._b

    def client(profile, countries):
        c = LexwareClient("k")
        c.session.get = lambda url, timeout: profile if url.endswith("/profile") else countries
        return c

    assert client(R(200, {"companyName": "Muster GmbH"}), R(200)).test_connection() == "Muster GmbH"
    assert client(R(403), R(200)).test_connection() == ""
    with pytest.raises(LexwareError, match="Tarif XL"):
        client(R(403), R(403)).test_connection()
    with pytest.raises(LexwareError, match="ungültig"):
        client(R(401), R(401)).test_connection()


def test_period_range():
    from datetime import date
    from workspace2lexware import config
    today = date(2026, 10, 15)
    base = dict(config.DEFAULTS)
    assert config.period_range(base, today) == (date(2026, 9, 15), None)
    assert config.period_range({**base, "period": "this_year"}, today) == (date(2026, 1, 1), None)
    assert config.period_range({**base, "period": "last_year"}, today) == (date(2025, 1, 1), date(2025, 12, 31))
    custom = {**base, "period": "custom", "date_from": "2026-03-01", "date_to": "2026-03-31"}
    assert config.period_range(custom, today) == (date(2026, 3, 1), date(2026, 3, 31))
    swapped = {**custom, "date_from": "2026-03-31", "date_to": "2026-03-01"}
    assert config.period_range(swapped, today) == (date(2026, 3, 1), date(2026, 3, 31))
    assert config.period_range({**base, "period": "custom"}, today) == (date(2026, 9, 15), None)


def test_pages_render(client):
    for path in ["/review", "/history", "/rules", "/settings", "/connections", "/health"]:
        assert client.get(path).status_code == 200, path


def test_post_requires_csrf(client):
    assert client.post("/rules/add", data={"pattern": "a.de", "action": "never"}).status_code == 400


def test_add_rule_and_save_settings(client):
    token = csrf_of(client)
    client.post("/rules/add", data={"csrf": token, "pattern": "Test.de", "action": "never"})
    assert "test.de" in client.get("/rules").get_data(as_text=True)
    r = client.post("/settings", data={"csrf": token, "query": "has:attachment", "lookback_days": "7",
                                       "extensions": "pdf, .png, .exe", "mode": "auto", "auto_threshold": "4",
                                       "interval_minutes": "30", "label": "", "min_size_bytes": "100",
                                       "lexware_api_key": "geheim"})
    assert r.status_code == 302
    page = client.get("/settings").get_data(as_text=True)
    assert 'value="7"' in page and ".exe" not in page
    assert "geheim" not in page  # Key wird nie zurückgegeben


def test_cycle_scan_upload_and_label(monkeypatch):
    from workspace2lexware import config, gmail, service

    pdf = b"%PDF-1.4 Rechnungsnummer 1 Gesamtbetrag IBAN MwSt " + b"x" * 4000
    mail = gmail.Mail("mail-42", "Ihre Rechnung", "Firma", "buchhaltung@lieferant.de", "2026-10-01T10:00:00+00:00",
                      [gmail.Attachment("mail-42:1", "rechnung.pdf", "application/pdf", pdf)])
    labeled = []
    monkeypatch.setattr(gmail, "connect", lambda: object())
    monkeypatch.setattr(gmail, "iter_mails", lambda *a, **k: iter([mail]))
    monkeypatch.setattr(gmail, "ensure_label", lambda s, n: "L1")
    monkeypatch.setattr(gmail, "add_label", lambda s, m, l: labeled.append(m))
    monkeypatch.setattr(service.LexwareClient, "upload_voucher_file", lambda self, *a: "lex-1")
    db.add_rule("lieferant.de", "always")
    s = config.load_settings()
    s.update(lexware_api_key="k", label="Lexware-übertragen")
    config.save_settings(s)

    result = service.run_cycle()
    assert result["error"] is None
    assert (result["new"], result["uploaded"], result["labeled"]) == (1, 1, 1)
    assert labeled == ["mail-42"]
    assert [d["lexware_id"] for d in db.list_documents("uploaded") if d["message_id"] == "mail-42"] == ["lex-1"]

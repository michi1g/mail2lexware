import base64
import imaplib
import shlex
import socketserver
import threading
from datetime import date
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone

import pytest

from mail2lexware import imap_source
from mail2lexware.imap_source import ImapConfig, ImapError

PDF = b"%PDF-1.4 dummy " + b"x" * 200
MONATE = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
_REAL_IMAP4 = imaplib.IMAP4


def parse_date(s: str) -> date:
    d, m, y = s.split("-")
    return date(int(y), MONATE[m], int(d))


def build_mail(subject="Rechnung", attachments=(("rechnung.pdf", PDF),), sender="Max Muster <Max@Example.DE>",
               when=datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)) -> bytes:
    msg = EmailMessage()
    if subject is not None:
        msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "ich@example.de"
    msg["Date"] = format_datetime(when)
    msg.set_content("Hallo")
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg.as_bytes()


class Handler(socketserver.StreamRequestHandler):
    def send(self, text: str):
        self.wfile.write(text.encode() + b"\r\n")

    def handle(self):
        srv = self.server
        self.send("* OK IMAP4rev1 bereit")
        selected = None
        while line := self.rfile.readline():
            parts = shlex.split(line.decode().strip(), posix=True)
            tag, cmd, args = parts[0], parts[1].upper(), parts[2:]
            if cmd == "UID":
                cmd = "UID " + args[0].upper()
                args = args[1:]
            srv.commands.append((cmd, args))
            if cmd == "CAPABILITY":
                self.send("* CAPABILITY IMAP4rev1 AUTH=XOAUTH2")
                self.send(f"{tag} OK fertig")
            elif cmd == "LOGIN":
                if srv.users.get(args[0]) == args[1]:
                    self.send(f"{tag} OK angemeldet")
                else:
                    self.send(f"{tag} NO [AUTHENTICATIONFAILED] ungueltig")
            elif cmd == "AUTHENTICATE":
                self.send("+ ")
                blob = base64.b64decode(self.rfile.readline().strip()).decode()
                srv.auth_strings.append(blob)
                user, tok = blob.split("\x01")[0][5:], blob.split("\x01")[1][len("auth=Bearer "):]
                if srv.tokens.get(user) == tok:
                    self.send(f"{tag} OK angemeldet")
                else:
                    self.send(f"{tag} NO ungueltiges Token")
            elif cmd in ("SELECT", "EXAMINE"):
                if args[0] not in srv.folders:
                    self.send(f"{tag} NO Ordner nicht vorhanden")
                    continue
                selected = args[0]
                self.send(f"* {len(srv.mails)} EXISTS")
                self.send("* OK [UIDVALIDITY 777] ok")
                self.send(f"{tag} OK [READ-ONLY] fertig")
            elif cmd == "UID SEARCH":
                since = before = None
                for i, a in enumerate(args):
                    if a == "SINCE":
                        since = parse_date(args[i + 1])
                    if a == "BEFORE":
                        before = parse_date(args[i + 1])
                hits = [str(uid) for uid, (d, _) in sorted(srv.mails.items())
                        if (since is None or d >= since) and (before is None or d < before)]
                self.send("* SEARCH " + " ".join(hits))
                self.send(f"{tag} OK fertig")
            elif cmd == "UID FETCH":
                uid = int(args[0])
                raw = srv.mails[uid][1]
                what = args[1].upper()
                if "RFC822.SIZE" in what:
                    size = srv.size_override.get(uid, len(raw))
                    self.send(f"* {uid} FETCH (UID {uid} RFC822.SIZE {size})")
                else:
                    if "HEADER" in what:
                        raw = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n" if b"\r\n\r\n" in raw else raw
                        srv.header_fetches.append(uid)
                    else:
                        srv.body_fetches.append(uid)
                    self.wfile.write(f"* {uid} FETCH (UID {uid} BODY[] {{{len(raw)}}}\r\n".encode() + raw + b")\r\n")
                self.send(f"{tag} OK fertig")
            elif cmd == "NOOP":
                self.send(f"{tag} OK")
            elif cmd == "LOGOUT":
                self.send("* BYE tschuess")
                self.send(f"{tag} OK")
                return
            else:
                self.send(f"{tag} BAD unbekannt")


class FakeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.commands = []
        self.auth_strings = []
        self.users = {"ich@example.de": "dummy-passwort"}
        self.tokens = {"ich@example.de": "dummy-token"}
        self.folders = {"INBOX", "Rechnungen 2026"}
        self.mails = {}  # uid -> (Datum, Rohbytes)
        self.size_override = {}
        self.body_fetches = []
        self.header_fetches = []

    def names(self):
        return [c for c, _ in self.commands]


@pytest.fixture
def server(monkeypatch):
    srv = FakeServer()
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def factory(host, port, ssl_context=None, timeout=None):
        return _REAL_IMAP4("127.0.0.1", srv.server_address[1], timeout=timeout)

    monkeypatch.setattr(imap_source.imaplib, "IMAP4_SSL", factory)
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def cfg():
    return ImapConfig("imap.example.de", 993, "ssl", "ich@example.de")


def run(cfg, since=date(2026, 1, 1), until=None, ext={".pdf"}, min_size=0, keywords=(), known=lambda _: False):
    return list(imap_source.iter_mails(cfg, since, until, ext, min_size, list(keywords), known, "acc1",
                                       password="dummy-passwort"))


def test_login_ok_und_falsches_passwort(server, cfg):
    conn = imap_source.open_connection(cfg, password="dummy-passwort")
    conn.logout()
    with pytest.raises(ImapError, match="Anmeldung fehlgeschlagen.*App-Passwort"):
        imap_source.open_connection(cfg, password="falsch")


def test_xoauth2(server, cfg):
    imap_source.open_connection(cfg, access_token="dummy-token").logout()
    assert server.auth_strings == ["user=ich@example.de\x01auth=Bearer dummy-token\x01\x01"]
    with pytest.raises(ImapError, match="Anmeldung fehlgeschlagen"):
        imap_source.open_connection(cfg, access_token="falsch")


def test_test_connection_zaehlt_mails(server, cfg):
    server.mails = {1: (date(2026, 3, 1), build_mail()), 2: (date(2026, 3, 2), build_mail())}
    assert imap_source.test_connection(cfg, password="dummy-passwort") == 2
    assert "LOGOUT" in server.names()


def test_ordner_nicht_gefunden(server, cfg):
    cfg.folder = "Gibt es nicht"
    with pytest.raises(ImapError, match="Gibt es nicht"):
        imap_source.test_connection(cfg, password="dummy-passwort")


def test_ordner_mit_leerzeichen(server, cfg):
    cfg.folder = "Rechnungen 2026"
    assert imap_source.test_connection(cfg, password="dummy-passwort") == 0


def test_nur_examine_nie_schreibend(server, cfg):
    server.mails = {1: (date(2026, 3, 1), build_mail())}
    run(cfg)
    names = server.names()
    assert "EXAMINE" in names
    assert not {"SELECT", "STORE", "UID STORE", "COPY", "UID COPY", "EXPUNGE", "UID EXPUNGE", "CLOSE"} & set(names)
    assert all(a[1].upper() != "BODY[]" for c, a in server.commands if c == "UID FETCH")  # nur PEEK


def test_zeitraum_inklusive_enddatum(server, cfg):
    server.mails = {
        1: (date(2026, 3, 9), build_mail(subject="a")),
        2: (date(2026, 3, 10), build_mail(subject="b")),
        3: (date(2026, 3, 11), build_mail(subject="c")),
    }
    mails = run(cfg, since=date(2026, 3, 10), until=date(2026, 3, 10))
    assert [m.subject for m in mails] == ["b"]
    search = [a for c, a in server.commands if c == "UID SEARCH"][0]
    assert search == ["SINCE", "10-Mar-2026", "BEFORE", "11-Mar-2026"]


def test_known_ueberspringt_ohne_download(server, cfg):
    server.mails = {1: (date(2026, 3, 1), build_mail()), 2: (date(2026, 3, 2), build_mail())}
    mails = run(cfg, known=lambda mid: mid.endswith(":1"))
    assert [m.message_id for m in mails] == ["imap:acc1:777:2"]
    assert server.body_fetches == [2]
    assert not any(c == "UID FETCH" and a[0] == "1" for c, a in server.commands)


def test_message_id_format(server, cfg):
    server.mails = {42: (date(2026, 3, 1), build_mail())}
    m = run(cfg)[0]
    assert m.message_id == "imap:acc1:777:42"
    assert m.attachments[0].msg_key.startswith("imap:acc1:777:42:")


def test_mailfelder(server, cfg):
    server.mails = {1: (date(2026, 3, 1), build_mail())}
    m = run(cfg)[0]
    assert (m.subject, m.sender, m.sender_email) == ("Rechnung", "Max Muster", "max@example.de")
    assert m.date == "2026-03-10T12:00:00+00:00"
    att = m.attachments[0]
    assert (att.filename, att.mime_type, att.data) == ("rechnung.pdf", "application/pdf", PDF)


def test_jede_unbekannte_mail_auch_ohne_anhang(server, cfg):
    server.mails = {1: (date(2026, 3, 1), build_mail(attachments=())), 2: (date(2026, 3, 2), build_mail())}
    mails = run(cfg)
    assert [len(m.attachments) for m in mails] == [0, 1]


def test_stichwortfilter(server, cfg):
    server.mails = {
        1: (date(2026, 3, 1), build_mail(subject="Hallo", attachments=(("foo.pdf", PDF),))),
        2: (date(2026, 3, 2), build_mail(subject="Ihre RECHNUNG", attachments=(("foo.pdf", PDF),))),
        3: (date(2026, 3, 3), build_mail(subject="Hallo", attachments=(("Beleg-Mai.pdf", PDF),))),
    }
    mails = run(cfg, keywords=["rechnung", "beleg"])
    assert [len(m.attachments) for m in mails] == [0, 1, 1]
    assert len(run(cfg, keywords=[])[0].attachments) == 1


def test_umlaut_dateiname_rfc2231(server, cfg):
    raw = build_mail(attachments=(("Rechnung_Müller_März.pdf", PDF),))
    assert b"filename*" in raw  # wirklich RFC2231-kodiert
    server.mails = {1: (date(2026, 3, 1), raw)}
    assert run(cfg)[0].attachments[0].filename == "Rechnung_Müller_März.pdf"


def test_mindestgroesse(server, cfg):
    server.mails = {1: (date(2026, 3, 1), build_mail(attachments=(("klein.pdf", b"x" * 10), ("gross.pdf", PDF))))}
    atts = run(cfg, min_size=100)[0].attachments
    assert [a.filename for a in atts] == ["gross.pdf"]


def test_endungsfilter(server, cfg):
    server.mails = {1: (date(2026, 3, 1), build_mail(attachments=(("a.PDF", PDF), ("b.txt", PDF), ("c.png", PDF))))}
    assert [a.filename for a in run(cfg)[0].attachments] == ["a.PDF"]
    assert [a.filename for a in run(cfg, ext={".pdf", ".txt"})[0].attachments] == ["a.PDF", "b.txt"]


def test_octet_stream_mime_aus_endung(server, cfg):
    msg = EmailMessage()
    msg["Subject"] = "x"
    msg["From"] = "a@b.de"
    msg.set_content("t")
    msg.add_attachment(PDF, maintype="application", subtype="octet-stream", filename="r.pdf")
    server.mails = {1: (date(2026, 3, 1), msg.as_bytes())}
    assert run(cfg)[0].attachments[0].mime_type == "application/pdf"


def test_groessenlimit(server, cfg):
    server.mails = {1: (date(2026, 3, 1), build_mail(subject="riesig")), 2: (date(2026, 3, 2), build_mail())}
    server.size_override = {1: 31 * 1024 * 1024}
    mails = run(cfg)
    assert [(m.subject, len(m.attachments)) for m in mails] == [("riesig", 0), ("Rechnung", 1)]
    assert server.body_fetches == [2]


def test_unverschluesselt_abgelehnt(server, cfg):
    cfg.security = "none"
    with pytest.raises(ImapError, match="Verschlüsselung"):
        imap_source.open_connection(cfg, password="dummy-passwort")
    with pytest.raises(ImapError):
        run(cfg)
    assert server.commands == []


def test_verbindung_nicht_moeglich(monkeypatch, cfg):
    def boom(*a, **k):
        raise ConnectionRefusedError("abgelehnt")
    monkeypatch.setattr(imap_source.imaplib, "IMAP4_SSL", boom)
    with pytest.raises(ImapError, match=r"imap\.example\.de:993.*abgelehnt"):
        imap_source.open_connection(cfg, password="dummy-passwort")


def test_verbindung_wird_bei_abbruch_geschlossen(server, cfg):
    server.mails = {1: (date(2026, 3, 1), build_mail()), 2: (date(2026, 3, 2), build_mail())}
    gen = imap_source.iter_mails(cfg, date(2026, 1, 1), None, {".pdf"}, 0, [], lambda _: False, "acc1",
                                 password="dummy-passwort")
    next(gen)
    gen.close()
    for _ in range(100):
        if "LOGOUT" in server.names():
            break
        threading.Event().wait(0.02)
    assert "LOGOUT" in server.names()

"""Voreinstellungen für IMAP-Anbieter (Gmail und Microsoft laufen über OAuth und stehen hier nicht)."""


def _p(key: str, label: str, host: str, hint: str = "") -> dict:
    return {"key": key, "label": label, "host": host, "port": 993, "security": "ssl", "hint": hint}


PROVIDERS: list[dict] = [
    _p("gmx", "GMX", "imap.gmx.net",
       'Im GMX-Postfach unter Einstellungen > POP3/IMAP-Abruf "Zugriff per POP3 und IMAP" aktivieren.'),
    _p("webde", "WEB.DE", "imap.web.de",
       'Im WEB.DE-Postfach unter Einstellungen > POP3/IMAP-Abruf "Zugriff per POP3 und IMAP" aktivieren.'),
    _p("tonline", "T-Online", "secureimap.t-online.de",
       'Im Kundencenter ein eigenes "E-Mail-Passwort" für externe Programme vergeben.'),
    _p("ionos", "IONOS", "imap.ionos.de"),
    _p("strato", "STRATO", "imap.strato.de"),
    _p("posteo", "Posteo", "posteo.de"),
    _p("mailboxorg", "mailbox.org", "imap.mailbox.org"),
    _p("icloud", "iCloud Mail", "imap.mail.me.com",
       "Unter appleid.apple.com ein App-spezifisches Passwort erzeugen."),
    _p("yahoo", "Yahoo Mail", "imap.mail.yahoo.com",
       "In den Kontosicherheits-Einstellungen ein App-Passwort erzeugen."),
    _p("custom", "Eigener Eintrag", "",
       "Host, Port und Verschlüsselung beim Anbieter nachlesen."),
]


def get_provider(key: str) -> dict | None:
    return next((p for p in PROVIDERS if p["key"] == key), None)

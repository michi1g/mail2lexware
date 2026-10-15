import time

import requests

MAX_BYTES = 5 * 1024 * 1024  # Limit der Lexware-Office-API für Datei-Uploads


class LexwareError(RuntimeError):
    pass


class LexwareClient:
    def __init__(self, api_key: str, base_url: str = "https://api.lexware.io/v1"):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "Accept": "application/json"})

    def test_connection(self) -> str:
        """Prüft den API-Key und gibt den Firmennamen zurück (leer, wenn das Profil nicht lesbar ist).

        Erst /profile, bei 403 ein weiterer lesender Endpunkt: Manche Keys dürfen das Profil nicht lesen,
        obwohl sie gültig sind. Ob der Upload erlaubt ist, zeigt sich erst beim ersten Beleg.
        """
        resp = self.session.get(f"{self.base_url}/profile", timeout=20)
        if resp.ok:
            return resp.json().get("companyName", "")
        if resp.status_code == 401:
            raise LexwareError("Der API-Key ist ungültig (HTTP 401). Erzeuge in Lexware einen neuen Key.")
        if resp.status_code == 403:
            fallback = self.session.get(f"{self.base_url}/countries", timeout=20)
            if fallback.ok:
                return ""
            if fallback.status_code == 401:
                raise LexwareError("Der API-Key ist ungültig (HTTP 401). Erzeuge in Lexware einen neuen Key.")
            raise LexwareError(
                "Lexware lehnt den Zugriff ab (HTTP 403). Die Public API gibt es nur im Tarif XL, und der Key "
                "braucht ausreichende Rechte. Prüfe beides in Lexware unter Einstellungen > Public API."
            )
        raise LexwareError(f"HTTP {resp.status_code} {resp.text[:200]}")

    def upload_voucher_file(self, filename: str, data: bytes, mime_type: str) -> str:
        """Lädt eine Datei in den Belege-Eingang ("Zu prüfen") und gibt die Lexware-File-ID zurück."""
        if len(data) > MAX_BYTES:
            raise LexwareError(f"{len(data)} Bytes überschreiten das 5-MB-Limit")
        for attempt in range(5):
            resp = self.session.post(
                f"{self.base_url}/files",
                files={"file": (filename, data, mime_type)},
                data={"type": "voucher"},
                timeout=60,
            )
            if resp.status_code == 429:  # Rate-Limit: 2 Requests/Sekunde
                time.sleep(1 + attempt)
                continue
            if resp.status_code in (401, 403):
                raise LexwareError(
                    f"Lexware verweigert den Upload (HTTP {resp.status_code}). Prüfe Tarif XL und die Rechte des API-Keys. "
                    f"Antwort: {resp.text[:200]}"
                )
            if not resp.ok:
                raise LexwareError(f"HTTP {resp.status_code} {resp.text[:300]}")
            time.sleep(0.6)  # Rate-Limit respektieren
            return resp.json()["id"]
        raise LexwareError("Rate-Limit nach mehreren Versuchen weiterhin überschritten")

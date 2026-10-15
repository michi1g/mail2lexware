# workspace2lexware

Überträgt Rechnungs-Anhänge (PDF, XRechnung/ZUGFeRD-XML, optional Bilder) aus Gmail / Google Workspace per API in den Belege-Eingang („Zu prüfen“) von Lexware Office. Bedienung über eine Web-GUI, gedacht für den Betrieb als Docker-Container (z. B. auf Unraid) im Heimnetz.

## Funktionen

- **Prüfen:** Gefundene Anhänge mit PDF-Vorschau, Auswahl per Häkchen oder Tastatur (Pfeiltasten, `x`), dann hochladen oder ignorieren.
- **Oberfläche:** Hell und dunkel nach Systemeinstellung, auch mobil bedienbar. Schriften und Icons (Geist, Phosphor) liegen im Image, es werden keine externen Ressourcen geladen.
- **Erkennung:** Der PDF-Text wird auf Rechnungsmerkmale geprüft (Rechnungsnummer, IBAN, MwSt …) und mit einem Score bewertet. XRechnung/ZUGFeRD-XML zählt immer als Rechnung.
- **Zeitraum:** letzte N Tage, dieses Jahr, letztes Jahr oder ein eigener Zeitraum von-bis (Einstellungen).
- **Modus:** „Alles prüfen“ oder „eindeutige Rechnungen automatisch hochladen, Rest prüfen“.
- **Absender-Regeln:** pro Adresse oder Domain „immer hochladen“ oder „nie“.
- **Verlauf** mit Status, Fehlermeldungen und „Erneut versuchen“.
- **Gmail-Label** nach erfolgreichem Upload; die Mail wird bei späteren Läufen übersprungen.
- **Duplikatschutz** über SHA-256 der Datei, **Zeitplan** im Container (Standard: stündlich).

## Voraussetzungen

- Lexware Office mit Zugang zur **Public API** (laut Hersteller nur im Tarif XL). API-Key unter *Einstellungen → Public API* erzeugen. Getestet mit diesen Rechten: Allgemeine Profilinformationen abrufen, Belegbilder hochladen, Belege abrufen, Belege herunterladen, Belege speichern, Belegliste filtern und abrufen, Zahlungsinformationen von Belegen abrufen. Zwingend nötig ist nur „Belegbilder hochladen“. Mit dem Profil-Recht liefert der Verbindungstest zusätzlich den Firmennamen.
- Google-Cloud-Projekt mit aktivierter **Gmail API** und einem OAuth-Client vom Typ **Desktop-App**. Bei Workspace die OAuth-Zustimmung als **„Intern“** konfigurieren: Dann läuft der Refresh-Token nicht nach 7 Tagen ab. Benötigter Scope: `gmail.modify` (Lesen und Labels setzen, kein Senden oder Löschen).

## Betrieb auf Unraid

1. Das Image wird von GitHub Actions nach `ghcr.io/michi1g/workspace2lexware` gebaut. Das Repository ist öffentlich, das Paket ist aber standardmäßig privat. Entweder das Paket unter GitHub → Packages → Package settings auf „Public“ stellen (es enthält keine Zugangsdaten) oder auf dem Unraid-Terminal einmalig anmelden (Personal Access Token mit `read:packages`): `docker login ghcr.io -u michi1g`.
2. Container anlegen: entweder `unraid/workspace2lexware.xml` nach `/boot/config/plugins/dockerMan/templates-user/` kopieren und unter *Docker → Add Container* das Template wählen, oder die `docker-compose.yml` mit dem Compose-Plugin verwenden.
3. `GUI_PASSWORD` setzen und die GUI unter `http://<unraid-ip>:8080` öffnen.
4. **Verbindungen:** `credentials.json` hochladen, „Login-Link erzeugen“, bei Google anmelden. Google leitet danach auf eine `localhost`-Seite weiter, die nicht lädt. Die komplette Adresse aus der Adresszeile in die GUI einfügen.
5. **Einstellungen:** Lexware-API-Key eintragen, Suchanfrage und Modus anpassen.

Die Rechte von `/data` korrigiert der Container beim Start selbst (Standard `PUID=99`, `PGID=100` = nobody:users wie bei Unraid, per Umgebungsvariable änderbar).

Alle veränderlichen Daten (Einstellungen, Token, Datenbank, Beleg-Dateien) liegen im Volume `/data`. Das Image lässt sich ohne Datenverlust aktualisieren.

## Sicherheit

- **Keine Zugangsdaten im Repository.** Das Repository ist öffentlich. API-Keys, `credentials.json`, `token.json` und Passwörter gehören nur in das Volume `/data` bzw. in Umgebungsvariablen. Die `.gitignore` schließt typische Dateien aus, und die CI prüft jeden Push mit gitleaks.
- Die GUI ist nur für das Heimnetz gedacht. Nicht per Portfreigabe ins Internet stellen. Für Zugriff von außen ein VPN verwenden.
- Mit `GUI_PASSWORD` ist ein Login aktiv (ohne Passwort ist die GUI offen). Alle Aktionen sind gegen CSRF geschützt.
- Der Lexware-API-Key liegt in `/data/settings.json` (Dateirechte 600) oder in der Umgebungsvariable `LEXWARE_API_KEY`, die Vorrang hat.

## Lokale Entwicklung

```powershell
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest
$env:DATA_DIR = "data"; python -m workspace2lexware   # http://localhost:8080
```

## Hinweise

- Die Suche ist stichwortbasiert (Gmail-Query). Der Score hilft beim Aussortieren, ersetzt aber nicht die Prüfung. Gescannte PDFs ohne Text erhalten nur über den Dateinamen Punkte, daher im Modus „automatisch“ meist zur manuellen Prüfung.
- Rechnungen, die nur im Mailtext stehen oder hinter Download-Links liegen, werden nicht erfasst.
- Die API-Details (`POST /v1/files` mit `type=voucher`, `GET /v1/profile`) stammen aus der Lexware-Dokumentation, wurden aber noch nicht gegen einen echten Account getestet.

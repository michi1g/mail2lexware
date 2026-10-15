# workspace2lexware

Überträgt Rechnungs-Anhänge (PDF, XRechnung/ZUGFeRD-XML, optional Bilder) aus Gmail / Google Workspace, IMAP-Postfächern und Microsoft 365 per API in den Belege-Eingang („Zu prüfen“) von Lexware Office.

Die Bedienung läuft über eine Web-GUI. Das Tool ist als Docker-Container gedacht, der in einem vertrauenswürdigen Netz läuft (Heimserver, NAS, Unraid, Synology, Raspberry Pi oder jeder andere Docker-Host).

## Funktionen

- **Prüfen:** Gefundene Anhänge mit PDF-Vorschau, Auswahl per Häkchen oder Tastatur (Pfeiltasten, `x`), dann hochladen oder ignorieren.
- **Erkennung:** Der PDF-Text wird auf Rechnungsmerkmale geprüft (Rechnungsnummer, IBAN, MwSt …) und mit einem Score bewertet. XRechnung/ZUGFeRD-XML zählt immer als Rechnung.
- **Modus:** „Alles prüfen“ oder „eindeutige Rechnungen automatisch hochladen, Rest prüfen“.
- **Absender-Regeln:** pro Adresse oder Domain „immer hochladen“ oder „nie“.
- **Zeitraum:** letzte N Tage, dieses Jahr, letztes Jahr oder ein eigener Zeitraum von-bis.
- **Postfächer:** Gmail / Google Workspace (OAuth), IMAP mit Voreinstellungen (GMX, WEB.DE, T-Online, IONOS, STRATO, Posteo, mailbox.org, iCloud, Yahoo) oder „Eigener Eintrag“, Microsoft 365 und Outlook (OAuth). Mehrere Postfächer gleichzeitig sind möglich.
- **Nur lesend:** IMAP-Ordner werden schreibgeschützt geöffnet. Nichts wird markiert, verschoben oder gelöscht. Bei Gmail setzt das Tool optional ein Label.
- **Verlauf** mit Status, Fehlermeldungen und „Erneut versuchen“.
- **Duplikatschutz** über SHA-256 der Datei, **Zeitplan** im Container (Standard: stündlich).
- **Verschlüsselte Ablage** aller Zugangsdaten (siehe unten).
- **Oberfläche:** hell und dunkel nach Systemeinstellung, mobil bedienbar. Schriften und Icons liegen im Image, es werden keine externen Ressourcen geladen.

## Voraussetzungen

- **Docker** (oder ein Anbieter-Template wie bei Unraid).
- **Lexware Office** mit Zugang zur Public API (laut Hersteller nur im Tarif XL). API-Key unter *Einstellungen → Public API* erzeugen. Der Key braucht mindestens die Rechte „Allgemeine Profilinformationen abrufen“ und „Belegbilder hochladen“. Ohne das Profil-Recht schlägt der Verbindungstest fehl.
- **Mindestens ein Postfach:**
  - *Gmail / Google Workspace:* Google-Cloud-Projekt mit aktivierter **Gmail API** und ein OAuth-Client vom Typ **Desktop-App**. Bei Workspace die OAuth-Zustimmung als **„Intern“** konfigurieren, dann läuft der Refresh-Token nicht nach 7 Tagen ab. Benötigter Scope: `gmail.modify` (Lesen und Labels setzen, kein Senden oder Löschen).
  - *IMAP:* E-Mail-Adresse und Passwort, bei den meisten Anbietern ein eigenes App-Passwort.
  - *Microsoft 365 / Outlook:* eine selbst registrierte App im Microsoft Entra Admin Center (Schritte weiter unten).

## Installation

Das Image liegt in der GitHub Container Registry: `ghcr.io/michi1g/workspace2lexware:latest`.

### Docker Compose

```yaml
services:
  workspace2lexware:
    image: ghcr.io/michi1g/workspace2lexware:latest
    container_name: workspace2lexware
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      TZ: Europe/Berlin
      GUI_PASSWORD: ""            # Passwort für die Web-GUI setzen
      # W2L_ENCRYPTION_KEY: ""    # empfohlen, siehe "Verschlüsselung"
      # LEXWARE_API_KEY: ""       # optional, alternativ in der GUI
    volumes:
      - ./data:/data
```

Die mitgelieferte `docker-compose.yml` entspricht diesem Beispiel. Danach `docker compose up -d` und die GUI unter `http://<host>:8080` öffnen.

### Unraid

Die Datei `unraid/workspace2lexware.xml` nach `/boot/config/plugins/dockerMan/templates-user/` kopieren und unter *Docker → Add Container* das Template wählen. Als Datenordner ist `/mnt/user/appdata/workspace2lexware` voreingestellt.

### Daten und Updates

Alle veränderlichen Daten (Einstellungen, Zugangsdaten, Datenbank, Beleg-Dateien) liegen im Volume `/data`. Das Image lässt sich ohne Datenverlust aktualisieren. Die Rechte von `/data` korrigiert der Container beim Start selbst (Standard `PUID=99`, `PGID=100`, per Umgebungsvariable änderbar).

### Umgebungsvariablen

| Variable | Bedeutung |
| --- | --- |
| `GUI_PASSWORD` | Passwort der Web-GUI. Ohne Wert ist die GUI offen. |
| `W2L_ENCRYPTION_KEY` | Schlüssel zum Verschlüsseln der Zugangsdaten (empfohlen). |
| `LEXWARE_API_KEY` | Lexware-API-Key, hat Vorrang vor dem Wert aus den Einstellungen. |
| `TZ` | Zeitzone für Anzeige und Zeitplan, Standard `Europe/Berlin`. |
| `PORT` | Port innerhalb des Containers, Standard `8080`. |
| `PUID`, `PGID` | Benutzer und Gruppe für `/data`, Standard `99` und `100`. |

## Erste Einrichtung

1. **Einstellungen:** Lexware-API-Key eintragen, Zeitraum, Modus und Suche anpassen.
2. **Verbindungen:** Mindestens ein Postfach verbinden (siehe unten) und den Lexware-Zugang testen.
3. **Prüfen:** „Jetzt prüfen“ starten, Belege ansehen und hochladen. Sie erscheinen in Lexware im Belege-Eingang unter „Zu prüfen“.

### Gmail / Google Workspace

Unter *Verbindungen* gibt es eine Schritt-für-Schritt-Anleitung mit Direktlinks zur Google Cloud Console: Projekt anlegen, Gmail API aktivieren, OAuth-Zustimmungsbildschirm einrichten, OAuth-Client „Desktop-App“ erzeugen und `credentials.json` hochladen. Danach „Login-Link erzeugen“ und bei Google anmelden. Google leitet anschließend auf eine `localhost`-Seite weiter, die nicht lädt. Die komplette Adresse aus der Adresszeile in die GUI einfügen.

### IMAP-Postfächer

Unter *Verbindungen → Postfach hinzufügen* den Anbieter wählen, E-Mail-Adresse und Passwort eintragen. Server und Port sind bei den Voreinstellungen schon gesetzt, bei „Eigener Eintrag“ werden Server, Port und Verschlüsselung (SSL/TLS oder STARTTLS) selbst angegeben. Unverschlüsselte Verbindungen werden nicht unterstützt. Bei vielen Anbietern ist ein eigenes App-Passwort nötig, GMX und WEB.DE verlangen zusätzlich die Freigabe von IMAP im Postfach. Hinweise zu jedem Anbieter zeigt die Oberfläche. Beim Speichern wird die Anmeldung getestet.

### Microsoft 365 und Outlook

Microsoft hat IMAP mit Passwort abgeschaltet. Die Anmeldung läuft deshalb über OAuth mit einer eigenen App-Registrierung (kostenlos):

1. Im [Microsoft Entra Admin Center](https://entra.microsoft.com/) unter *App-Registrierungen* eine neue Registrierung anlegen.
2. Umleitungs-URI: Plattform „Mobil- und Desktopanwendungen“, Wert `http://localhost`. Unter *Authentifizierung* „Öffentliche Clientflows zulassen“ aktivieren.
3. Unter *API-Berechtigungen* die delegierte Berechtigung `IMAP.AccessAsUser.All` aus „Office 365 Exchange Online“ hinzufügen.
4. Anwendungs-ID (Client) und bei Firmenkonten die Verzeichnis-ID (Mandant) kopieren.
5. In der GUI unter *Verbindungen → Postfach hinzufügen → Microsoft 365 und Outlook* eintragen und anmelden. Wie bei Google wird die Adresse der nicht ladenden `localhost`-Seite zurück in die GUI kopiert.

In Firmen-Tenants muss IMAP für das Postfach aktiv sein, und ein Administrator muss die Berechtigung eventuell genehmigen.

### Suche und Markierung

- Zeitraum, Dateiendungen und Mindestgröße gelten für alle Postfächer.
- Die Gmail-Suchanfrage in den Einstellungen gilt nur für Gmail. Andere Postfächer filtern über Stichwörter in Betreff und Dateiname (pro Postfach einstellbar, leer bedeutet alle Anhänge der gewählten Endungen).
- Das Label nach dem Upload gibt es nur bei Gmail. Bei den anderen Postfächern merkt sich das Tool erledigte Mails in seiner Datenbank.

## Verschlüsselung der Zugangsdaten

Lexware-API-Key, IMAP-Passwörter, Microsoft-Tokens, Gmail-Token und `credentials.json` werden mit Fernet (AES) verschlüsselt gespeichert. Klartext-Dateien aus älteren Versionen verschlüsselt das Tool beim Start selbst.

- **Schlüssel:** Standardmäßig erzeugt das Tool beim ersten Start `/data/encryption.key`. Das schützt vor einzeln weitergegebenen Dateien (Backup, Screenshot, Support), nicht davor, dass jemand den ganzen Datenordner kopiert.
- **Empfohlen:** Die Umgebungsvariable `W2L_ENCRYPTION_KEY` auf eine lange Zufallszeichenfolge setzen (zum Beispiel `openssl rand -base64 32`), bevor Zugangsdaten eingetragen werden. Dann liegt der Schlüssel nicht neben den Daten.
- **Später nachrüsten:** Wird die Variable erst nach dem ersten Start gesetzt, bleiben ältere Daten über `encryption.key` lesbar, neue Daten nutzen die Variable. Die Schlüsseldatei erst löschen, wenn alle Zugangsdaten einmal neu gespeichert wurden.
- **Wichtig:** Geht der Schlüssel verloren oder wird die Variable geändert, lassen sich damit verschlüsselte Zugangsdaten nicht mehr lesen. Dann müssen Postfächer und Lexware-Key neu eingetragen werden.

## Sicherheit

- Die GUI ist für den Betrieb in einem vertrauenswürdigen Netz gedacht. Nicht per Portfreigabe ins Internet stellen. Für Zugriff von außen ein VPN oder einen abgesicherten Reverse Proxy verwenden.
- Mit `GUI_PASSWORD` ist ein Login aktiv. Alle Aktionen sind gegen CSRF geschützt.
- Der Container braucht nur ausgehende Verbindungen zu den Postfächern, Google, Microsoft und Lexware.
- Keine Zugangsdaten im Repository: API-Keys, Token, `credentials.json` und Passwörter gehören nur in das Volume `/data` bzw. in Umgebungsvariablen. Die `.gitignore` schließt typische Dateien aus, und die CI prüft jeden Push mit gitleaks.

## Lokale Entwicklung

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest
DATA_DIR=data python -m workspace2lexware            # http://localhost:8080
```

## Grenzen

- Die Erkennung ist stichwort- und textbasiert. Der Score hilft beim Aussortieren, ersetzt aber nicht die Prüfung. Gescannte PDFs ohne Text erhalten nur über den Dateinamen Punkte und landen im Modus „automatisch“ meist in der manuellen Prüfung.
- Rechnungen, die nur im Mailtext stehen oder hinter Download-Links liegen, werden nicht erfasst.
- Der Upload nutzt `POST /v1/files` mit `type=voucher`, der Verbindungstest `GET /v1/profile` der Lexware-Office-API. Das Tool legt Belege nur im Eingang ab, es bucht nichts.

## Lizenz

MIT, siehe [LICENSE](LICENSE). Lizenzen der enthaltenen Schriften und Icons stehen in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

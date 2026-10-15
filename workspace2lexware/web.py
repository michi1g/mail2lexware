import hmac
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for)
from markupsafe import Markup

from . import config, db, gmail, scheduler, service
from .lexware import LexwareClient, LexwareError

OPEN_ENDPOINTS = {"health", "login", "static"}
ALLOWED_EXTENSIONS = [".pdf", ".xml", ".jpg", ".jpeg", ".png"]
MIME = {".pdf": "application/pdf", ".xml": "text/plain", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png"}
STATUS_LABELS = {"pending": "Zu prüfen", "uploaded": "Hochgeladen", "ignored": "Ignoriert",
                 "failed": "Fehlgeschlagen", "duplicate": "Duplikat"}


ICON_DIR = Path(__file__).parent / "static" / "icons"


@lru_cache(maxsize=None)
def _icon_svg(name: str, weight: str) -> str:
    suffix = "" if weight == "regular" else f"-{weight}"
    return (ICON_DIR / f"{name}{suffix}.svg").read_text(encoding="utf-8")


def icon(name: str, weight: str = "regular", cls: str = "") -> Markup:
    """Phosphor-Icon inline einbinden (erbt die Textfarbe über currentColor)."""
    svg = _icon_svg(name, weight).replace("<svg ", f'<svg class="icon {cls}" aria-hidden="true" focusable="false" ', 1)
    return Markup(svg)


def _ago(value: str | None) -> str:
    if not value:
        return ""
    seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds()
    if seconds < 60:
        return "gerade eben"
    if seconds < 3600:
        return f"vor {int(seconds // 60)} Min."
    if seconds < 86400:
        return f"vor {int(seconds // 3600)} Std."
    days = int(seconds // 86400)
    return "gestern" if days == 1 else f"vor {days} Tagen"


def _int(value, default: int, minimum: int = 0) -> int:
    try:
        return max(int(value), minimum)
    except (TypeError, ValueError):
        return default


def create_app(start_scheduler: bool = True) -> Flask:
    config.ensure_dirs()
    db.init()
    app = Flask(__name__)
    app.secret_key = config.secret_key()
    app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                      MAX_CONTENT_LENGTH=1024 * 1024)
    password = os.environ.get("GUI_PASSWORD", "")

    # --- Schutz: Login + CSRF ---------------------------------------------
    @app.before_request
    def guard():
        if request.endpoint in OPEN_ENDPOINTS and request.endpoint != "login":
            return None
        if password and not session.get("auth") and request.endpoint != "login":
            return redirect(url_for("login"))
        if request.method == "POST":
            sent = request.form.get("csrf", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "Die Sitzung ist abgelaufen. Lade die Seite neu und versuche es erneut.")
        return None

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return {"csrf": session["csrf"], "auth_enabled": bool(password), "running": service.is_running(),
                "status_labels": STATUS_LABELS, "icon": icon,
                "gmail_ready": config.TOKEN_FILE.exists(),
                "lexware_ready": bool(config.api_key(config.load_settings())),
                "pending_count": db.counts().get("pending", 0) + db.counts().get("failed", 0)}

    @app.template_filter("dt")
    def fmt_dt(value):
        if not value:
            return ""
        return datetime.fromisoformat(value).astimezone().strftime("%d.%m.%Y %H:%M")

    @app.template_filter("ago")
    def fmt_ago(value):
        return _ago(value)

    @app.template_filter("fext")
    def file_ext(filename):
        return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    @app.template_filter("kb")
    def fmt_kb(value):
        return f"{value / 1024:.0f} KB"

    # --- Allgemein --------------------------------------------------------
    @app.get("/health")
    def health():
        return "ok"

    @app.get("/status")
    def status():
        return jsonify(running=service.is_running())

    @app.errorhandler(400)
    @app.errorhandler(404)
    def error_page(e):
        return render_template("error.html", code=e.code, message=e.description), e.code

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not password:
            return redirect(url_for("review"))
        if request.method == "POST":
            sent = request.form.get("csrf", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "Die Sitzung ist abgelaufen. Lade die Seite neu und versuche es erneut.")
            if hmac.compare_digest(request.form.get("password", ""), password):
                session["auth"] = True
                return redirect(url_for("review"))
            time.sleep(1)  # bremst Durchprobieren
            flash("Falsches Passwort.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        return redirect(url_for("review"))

    # --- Prüfen -----------------------------------------------------------
    @app.get("/review")
    def review():
        return render_template(
            "review.html",
            pending=db.list_documents("pending"),
            failed=db.list_documents("failed"),
            last_run=db.kv_get("last_run"),
            counts=db.counts(),
        )

    @app.post("/review/action")
    def review_action():
        ids = request.form.getlist("ids", type=int)
        action = request.form.get("action", "")
        docs = [d for d in (db.get_document(i) for i in ids) if d]
        if not docs:
            flash("Wähle mindestens einen Beleg aus.", "error")
            return redirect(request.referrer or url_for("review"))

        if action in ("always", "never"):
            # Regel pro Absender anlegen und auf alle offenen Belege dieses Absenders anwenden
            targets = []
            for sender in {d["sender_email"] for d in docs if d["sender_email"]}:
                db.add_rule(sender, action)
                targets += db.pending_from_sender(sender)
            ids = [d["id"] for d in targets] or ids
            action = "upload" if action == "always" else "ignore"

        if action == "upload":
            r = service.upload_ids(ids)
            if r["error"]:
                flash(r["error"], "error")
            else:
                flash(f"{r['uploaded']} hochgeladen" + (f", {r['failed']} fehlgeschlagen" if r["failed"] else "") + ".", "error" if r["failed"] else "ok")
        elif action == "ignore":
            for i in ids:
                db.set_status(i, "ignored")
            flash(f"{len(ids)} ignoriert. Im Verlauf kannst du sie zurück zur Prüfung legen.", "ok")
        elif action == "requeue":
            for i in ids:
                db.set_status(i, "pending")
            flash(f"{len(ids)} zurück zur Prüfung gelegt.", "ok")
        return redirect(request.referrer or url_for("review"))

    @app.post("/run")
    def run():
        if service.is_running():
            flash("Es läuft bereits ein Durchlauf.", "error")
        else:
            threading.Thread(target=service.run_cycle, daemon=True).start()
            flash("Durchlauf gestartet.", "ok")
        return redirect(url_for("review"))

    @app.get("/file/<int:doc_id>")
    def file(doc_id):
        doc = db.get_document(doc_id)
        path = doc and service.file_path(doc)
        if not path or not path.exists():
            abort(404)
        ext = path.suffix.lower()
        return send_file(path, mimetype=MIME.get(ext, "application/octet-stream"), download_name=doc["filename"])

    # --- Verlauf ----------------------------------------------------------
    @app.get("/history")
    def history():
        status = request.args.get("status") or None
        if status not in STATUS_LABELS:
            status = None
        return render_template("history.html", docs=db.list_documents(status), current=status,
                               counts=db.counts())

    # --- Regeln -----------------------------------------------------------
    @app.get("/rules")
    def rules():
        return render_template("rules.html", rules=db.list_rules())

    @app.post("/rules/add")
    def rules_add():
        pattern = request.form.get("pattern", "").strip().lower()
        action = request.form.get("action")
        if pattern and action in ("always", "never"):
            db.add_rule(pattern, action)
        else:
            flash("Bitte Adresse/Domain und Aktion angeben.", "error")
        return redirect(url_for("rules"))

    @app.post("/rules/delete/<int:rule_id>")
    def rules_delete(rule_id):
        db.delete_rule(rule_id)
        return redirect(url_for("rules"))

    # --- Einstellungen ----------------------------------------------------
    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        s = config.load_settings()
        if request.method == "POST":
            f = request.form
            exts = [("." + e.strip().lower().lstrip(".")) for e in f.get("extensions", "").split(",") if e.strip()]
            s.update(
                query=f.get("query", "").strip() or s["query"],
                period=f.get("period") if f.get("period") in config.PERIODS else "days",
                lookback_days=_int(f.get("lookback_days"), s["lookback_days"], 1),
                date_from=f.get("date_from", "").strip(),
                date_to=f.get("date_to", "").strip(),
                extensions=[e for e in exts if e in ALLOWED_EXTENSIONS] or s["extensions"],
                min_size_bytes=_int(f.get("min_size_bytes"), s["min_size_bytes"]),
                label=f.get("label", "").strip(),
                interval_minutes=_int(f.get("interval_minutes"), s["interval_minutes"]),
                mode="auto" if f.get("mode") == "auto" else "review",
                auto_threshold=_int(f.get("auto_threshold"), s["auto_threshold"], 1),
            )
            if f.get("lexware_api_key", "").strip():  # leer = unverändert lassen
                s["lexware_api_key"] = f["lexware_api_key"].strip()
            config.save_settings(s)
            flash("Einstellungen gespeichert.", "ok")
            return redirect(url_for("settings"))
        since, until = config.period_range(s)
        period_text = (f"Durchsucht werden Mails ab {since:%d.%m.%Y}" + (f" bis {until:%d.%m.%Y}." if until else " bis heute."))
        return render_template("settings.html", s=s, period_text=period_text,
                               key_from_env=bool(os.environ.get("LEXWARE_API_KEY")))

    # --- Verbindungen -----------------------------------------------------
    @app.get("/connections")
    def connections():
        gmail_email = gmail_error = None
        if config.TOKEN_FILE.exists():
            try:
                gmail_email = gmail.profile_email()
            except Exception as e:
                gmail_error = str(e)
        return render_template(
            "connections.html",
            has_credentials=config.CREDENTIALS_FILE.exists(),
            gmail_email=gmail_email,
            gmail_error=gmail_error,
            has_key=bool(config.api_key(config.load_settings())),
            auth_url=session.pop("auth_url", None),
        )

    @app.post("/connections/credentials")
    def upload_credentials():
        up = request.files.get("file")
        if not up or not up.filename.endswith(".json"):
            flash("Bitte die credentials.json auswählen.", "error")
        else:
            content = up.read()
            if b'"installed"' not in content:
                flash("Das ist kein OAuth-Client vom Typ „Desktop-App“.", "error")
            else:
                config.CREDENTIALS_FILE.write_bytes(content)
                flash("credentials.json gespeichert.", "ok")
        return redirect(url_for("connections"))

    @app.post("/connections/gmail/start")
    def gmail_start():
        try:
            session["auth_url"] = gmail.start_login()
        except gmail.NotConnected as e:
            flash(str(e), "error")
        return redirect(url_for("connections"))

    @app.post("/connections/gmail/finish")
    def gmail_finish():
        try:
            gmail.finish_login(request.form.get("pasted", ""))
            flash("Gmail verbunden.", "ok")
        except Exception as e:
            flash(f"Login fehlgeschlagen: {e}", "error")
        return redirect(url_for("connections"))

    @app.post("/connections/gmail/disconnect")
    def gmail_disconnect():
        gmail.disconnect()
        flash("Gmail-Verbindung getrennt.", "ok")
        return redirect(url_for("connections"))

    @app.post("/connections/lexware/test")
    def lexware_test():
        s = config.load_settings()
        key = config.api_key(s)
        if not key:
            flash("Kein API-Key hinterlegt (Einstellungen).", "error")
        else:
            try:
                name = LexwareClient(key, s["base_url"]).test_connection()
                flash(f"Lexware-Verbindung OK{': ' + name if name else '. Der Key wird akzeptiert, das Profil ist aber nicht lesbar.'}", "ok")
            except (LexwareError, Exception) as e:
                flash(f"Lexware-Verbindung fehlgeschlagen: {e}", "error")
        return redirect(url_for("connections"))

    if start_scheduler:
        scheduler.start()
    return app

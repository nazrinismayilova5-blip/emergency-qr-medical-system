from flask import Flask, g, render_template, abort, request, redirect, url_for, flash, send_from_directory, session
import sqlite3
from pathlib import Path
import qrcode
from itsdangerous import URLSafeSerializer
import os
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

DB_PATH = Path("data/emergency.db")
DB_PATH.parent.mkdir(exist_ok=True)
QR_DIR = Path("qrcodes")
QR_DIR.mkdir(exist_ok=True)

SECRET = os.getenv("EMERGENCY_SECRET") or "my_secrets_;)"
s = URLSafeSerializer(SECRET, salt="emergency-tags")

app = Flask(__name__)
app.secret_key = SECRET
ADMIN_USERNAME = os.getenv("EMERGENCY_ADMIN_USER") or "admin"
ADMIN_PASSWORD_HASH = os.getenv("EMERGENCY_ADMIN_PW_HASH") or generate_password_hash("password")

PER_PAGE = 10
SESSION_TIMEOUT = int(os.getenv("SESSION_TIMEOUT", "1800"))

def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()

def ensure_db():
    db = get_db()
    cur = db.cursor()
    cur.execute("PRAGMA table_info(people)")
    cols = [r[1] for r in cur.fetchall()]
    if "hidden" not in cols:
        cur.execute("ALTER TABLE people ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;")
        db.commit()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            action TEXT NOT NULL,
            target TEXT,
            ip TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.commit()

def log_action(username, action, target=None):
    db = get_db()
    ip = request.remote_addr if request else None
    db.execute(
        "INSERT INTO activity_log (username, action, target, ip) VALUES (?, ?, ?, ?)",
        (username, action, target, ip)
    )
    db.commit()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated

@app.before_request
def require_login_and_timeout():
    endpoint = request.endpoint
    path = request.path
    if endpoint is None:
        return None
    if endpoint in ("login", "logout", "_whoami", "static"):
        return None
    if path.startswith("/static") or path.startswith("/favicon.ico"):
        return None
    user = session.get("user")
    last_active = session.get("last_active")
    if user and last_active:
        try:
            last = datetime.fromisoformat(last_active)
            if (datetime.utcnow() - last).total_seconds() > SESSION_TIMEOUT:
                session.pop("user", None)
                session.pop("last_active", None)
                flash("Logged out due to inactivity.", "danger")
                try:
                    log_action(user, "auto_logout_timeout", None)
                except Exception:
                    pass
                return redirect(url_for("login"))
        except Exception:
            session.pop("last_active", None)
    if user:
        session["last_active"] = datetime.utcnow().isoformat()
        return None
    return redirect(url_for("login", next=request.path))

@app.route("/_whoami")
def _whoami():
    return {"endpoint": request.endpoint, "path": request.path, "user": session.get("user")}

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        next_url = request.form.get("next") or request.args.get("next") or url_for("index")
        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session["user"] = username
            session["last_active"] = datetime.utcnow().isoformat()
            flash("Logged in", "success")
            try:
                log_action(username, "login", None)
            except Exception:
                pass
            return redirect(next_url)
        flash("Invalid credentials", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    user = session.pop("user", None)
    session.pop("last_active", None)
    if user:
        try:
            log_action(user, "logout", None)
        except Exception:
            pass
    flash("Logged out", "success")
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    try:
        page = int(request.args.get("page", 1))
        if page < 1:
            page = 1
    except:
        page = 1
    show_hidden = request.args.get("show_hidden", "0") == "1"
    offset = (page - 1) * PER_PAGE
    db = get_db()

    if show_hidden:
        total = db.execute("SELECT COUNT(1) as cnt FROM people WHERE hidden = 1").fetchone()["cnt"]
        rows = db.execute(
            "SELECT * FROM people WHERE hidden = 1 ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (PER_PAGE, offset)
        ).fetchall()
    else:
        total = db.execute("SELECT COUNT(1) as cnt FROM people WHERE hidden = 0").fetchone()["cnt"]
        rows = db.execute(
            "SELECT * FROM people WHERE hidden = 0 ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (PER_PAGE, offset)
        ).fetchall()

    total_pages = (total + PER_PAGE - 1) // PER_PAGE
    return render_template("admin_list.html", people=rows, page=page, total_pages=total_pages, show_hidden=show_hidden)

@app.route("/person/<token>")
@login_required
def person(token):
    db = get_db()
    row = db.execute("SELECT * FROM people WHERE token = ?", (token,)).fetchone()
    if not row or row["hidden"]:
        abort(404)
    qr_filename = f"{token}.png"
    qr_path = QR_DIR / qr_filename
    qr_image = None
    if qr_path.exists():
        qr_image = url_for("static_qr", filename=qr_filename)
    try:
        log_action(session.get("user"), "view", token)
    except Exception:
        pass
    return render_template("person.html", p=row, qr_image=qr_image)

@app.route("/add", methods=["GET", "POST"])
@login_required
def add_person():
    if request.method == "POST":
        name = request.form["name"]
        blood = request.form.get("blood", "")
        allergies = request.form.get("allergies", "")
        notes = request.form.get("notes", "")
        contact_name = request.form.get("contact_name", "")
        contact_phone = request.form.get("contact_phone", "")
        db = get_db()
        token = s.dumps({"name": name, "ts": datetime.utcnow().timestamp()})
        db.execute(
            "INSERT INTO people (token, name, blood_type, allergies, notes, emergency_contact_name, emergency_contact_phone) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (token, name, blood, allergies, notes, contact_name, contact_phone),
        )
        db.commit()
        base_url = os.getenv("PUBLIC_BASE_URL") or request.host_url.rstrip('/')
        qr_url = f"{base_url}/person/{token}"
        out_path = QR_DIR / f"{token}.png"
        qrcode.make(qr_url).save(out_path)
        try:
            log_action(session.get("user"), "add", token)
        except Exception:
            pass
        return render_template("add_person.html", qr_image=f"/static_qr/{token}.png", qr_url=qr_url, name=name)
    return render_template("add_person.html")

@app.route("/edit/<token>", methods=["GET", "POST"])
@login_required
def edit_person(token):
    db = get_db()
    row = db.execute("SELECT * FROM people WHERE token = ?", (token,)).fetchone()
    if not row:
        abort(404)
    if request.method == "POST":
        name = request.form["name"]
        blood = request.form.get("blood", "")
        allergies = request.form.get("allergies", "")
        notes = request.form.get("notes", "")
        contact_name = request.form.get("contact_name", "")
        contact_phone = request.form.get("contact_phone", "")
        db.execute(
            "UPDATE people SET name = ?, blood_type = ?, allergies = ?, notes = ?, emergency_contact_name = ?, emergency_contact_phone = ? WHERE token = ?",
            (name, blood, allergies, notes, contact_name, contact_phone, token)
        )
        db.commit()
        try:
            log_action(session.get("user"), "edit", token)
        except Exception:
            pass
        flash("Patient updated successfully!", "success")
        return redirect(url_for("index"))
    return render_template("edit_person.html", p=row)

@app.route("/static_qr/<filename>")
@login_required
def static_qr(filename):
    return send_from_directory(QR_DIR, filename)

@app.route("/delete/<token>", methods=["POST"])
@login_required
def hide_person(token):
    page = request.args.get("page", 1)
    db = get_db()
    db.execute("UPDATE people SET hidden = 1 WHERE token = ?", (token,))
    db.commit()
    try:
        log_action(session.get("user"), "hide", token)
    except Exception:
        pass
    flash("Patient hidden (not deleted).", "success")
    return redirect(url_for("index", page=page))

@app.route("/unhide/<token>", methods=["POST"])
@login_required
def unhide_person(token):
    page = request.args.get("page", 1)
    db = get_db()
    db.execute("UPDATE people SET hidden = 0 WHERE token = ?", (token,))
    db.commit()
    try:
        log_action(session.get("user"), "unhide", token)
    except Exception:
        pass
    flash("Patient restored (unhidden).", "success")
    return redirect(url_for("index", page=page))

@app.route("/toggle_status/<token>", methods=["POST"])
@login_required
def toggle_status(token):
    page = request.args.get("page", 1)
    db = get_db()
    row = db.execute("SELECT in_hospital FROM people WHERE token = ?", (token,)).fetchone()
    if not row:
        abort(404)
    new_status = 0 if row["in_hospital"] else 1
    db.execute("UPDATE people SET in_hospital = ? WHERE token = ?", (new_status, token))
    db.commit()
    try:
        log_action(session.get("user"), "toggle_status", token)
    except Exception:
        pass
    flash("Patient status updated.", "success")
    return redirect(url_for("index", page=page))

@app.route("/scan")
@login_required
def scan():
    return render_template("scan.html")

@app.route("/activity")
@login_required
def activity():
    try:
        page = int(request.args.get("page", 1))
        if page < 1:
            page = 1
    except:
        page = 1
    offset = (page - 1) * PER_PAGE
    db = get_db()
    total = db.execute("SELECT COUNT(1) as cnt FROM activity_log").fetchone()["cnt"]
    rows = db.execute(
        "SELECT id, username, action, target, ip, created_at FROM activity_log ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (PER_PAGE, offset)
    ).fetchall()
    total_pages = (total + PER_PAGE - 1) // PER_PAGE
    return render_template("activity.html", logs=rows, page=page, total_pages=total_pages)

if __name__ == "__main__":
    with app.app_context():
        ensure_db()
    app.run(debug=True, host="0.0.0.0", port=5000)


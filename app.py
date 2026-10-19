import os, re, sqlite3, secrets, math
from contextlib import contextmanager
from datetime import timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

import bleach
import markdown
import requests
from dotenv import load_dotenv
from flask import (Flask, render_template, request, redirect, url_for, session,
                   abort, flash, send_from_directory)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

load_dotenv()

ROOT = Path(__file__).resolve().parent
# Sur Render : DATA_DIR=/var/data (disque persistant). En local : dossier du projet.
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT))
DB_PATH = DATA_DIR / "scp_site.db"
UPLOAD_DIR = DATA_DIR / "uploads"
LEGACY_UPLOAD_DIR = ROOT / "static" / "uploads"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
SECTIONS = ["Dossiers SCP", "Rapports d'incident", "Journaux de recherche", "Histoires", "Annonces"]
PER_PAGE = 12
ON_RENDER = bool(os.getenv("RENDER"))

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # HTTPS derrière le proxy Render

secret = os.getenv("FLASK_SECRET_KEY")
if not secret:
    if ON_RENDER:
        raise RuntimeError("FLASK_SECRET_KEY doit être défini sur Render.")
    secret = secrets.token_hex(32)  # dev local uniquement
app.secret_key = secret
app.config.update(
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=ON_RENDER,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=7),
)

# ---------- Base de données ----------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            section TEXT NOT NULL, content TEXT NOT NULL, image TEXT,
            published INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_posts_pub ON posts(published, created_at DESC)")
        if con.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0:
            samples = [
                ("SCP-173 — Protocole de confinement", "Dossiers SCP",
                 "## Résumé\n**Objet :** Sculpture. **Classe :** Euclide.\n\nLe personnel doit maintenir une surveillance constante. Toute procédure doit être suivie selon le protocole de sécurité.\n\n> DOCUMENT FICTIF — MAQUETTE SCP.", 1),
                ("Incident 914 — Rapport d'analyse", "Rapports d'incident",
                 "## Rapport interne\nLes résultats de l'expérience doivent rester confidentiels jusqu'à validation du directeur de recherche.", 1),
                ("Journal 07 — Les portes du secteur C", "Journaux de recherche",
                 "## Extrait de journal\nLes capteurs ont détecté une anomalie non répertoriée. Les équipes sont priées de consulter le protocole de confinement.", 1),
            ]
            con.executemany("INSERT INTO posts(title,section,content,published) VALUES(?,?,?,?)", samples)

init_db()

# ---------- Auth ----------

def allowed_ids():
    return {x.strip() for x in os.getenv("ADMIN_DISCORD_IDS", "").split(",") if x.strip()}

def current_user():
    return session.get("discord_user")

def is_admin():
    user = current_user()
    ids = allowed_ids()
    return bool(user and ids and str(user.get("id")) in ids)

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not is_admin():
            flash("Accès réservé aux éditeurs Discord autorisés.", "error")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped

def redirect_uri():
    return os.getenv("DISCORD_REDIRECT_URI") or url_for("auth_callback", _external=True)

# ---------- CSRF ----------

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]

@app.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Jeton de sécurité invalide. Recharge la page et réessaie.")

# ---------- Rendu ----------

ALLOWED_TAGS = ["p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "b", "i", "del",
                "blockquote", "code", "pre", "ul", "ol", "li", "a", "img", "table", "thead",
                "tbody", "tr", "th", "td", "sup", "sub", "span"]
ALLOWED_ATTRS = {"a": ["href", "title"], "img": ["src", "alt", "title"], "span": ["class"]}

@app.template_filter("markdown")
def render_markdown(value):
    html = markdown.markdown(value or "", extensions=["extra", "sane_lists", "nl2br"])
    # Le HTML brut saisi dans un article est nettoyé (anti-XSS).
    return bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS,
                        protocols=["http", "https", "mailto"], strip=True)

@app.template_filter("media_url")
def media_url(path):
    """Compatible avec les anciens chemins /static/uploads/xxx."""
    return url_for("media", filename=Path(path).name) if path else ""

@app.template_filter("reading_time")
def reading_time(text):
    words = len(re.findall(r"\w+", text or ""))
    return max(1, round(words / 200))

@app.context_processor
def inject_globals():
    return {"sections": SECTIONS, "is_admin": is_admin(), "discord_user": current_user(),
            "csrf_token": csrf_token}

@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.path.startswith("/admin") or request.path in ("/login", "/auth/callback"):
        resp.headers["Cache-Control"] = "no-store"
    return resp

# ---------- Images ----------

def valid_image(stream):
    """Vérifie la signature réelle du fichier, pas seulement l'extension."""
    head = stream.read(16)
    stream.seek(0)
    return (head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff")
            or head[:6] in (b"GIF87a", b"GIF89a") or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"))

def allowed_file(name):
    return "." in name and name.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def delete_image(path):
    if not path:
        return
    f = UPLOAD_DIR / Path(path).name
    try:
        if f.is_file():
            f.unlink()
    except OSError:
        app.logger.warning("Impossible de supprimer %s", f)

@app.route("/media/<path:filename>")
def media(filename):
    if (UPLOAD_DIR / filename).is_file():
        return send_from_directory(UPLOAD_DIR, filename)
    return send_from_directory(LEGACY_UPLOAD_DIR, filename)

# ---------- Pages publiques ----------

@app.route("/healthz")
def healthz():
    return "ok", 200

@app.route("/")
def home():
    with db() as con:
        posts = con.execute("SELECT * FROM posts WHERE published=1 ORDER BY created_at DESC LIMIT 6").fetchall()
    return render_template("home.html", posts=posts)

def like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def list_posts(section, q):
    page = max(1, request.args.get("page", 1, type=int))
    where, params = "published=1", []
    if section in SECTIONS:
        where += " AND section=?"; params.append(section)
    if q:
        where += " AND (title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')"
        like = f"%{like_escape(q)}%"; params += [like, like]
    with db() as con:
        total = con.execute(f"SELECT COUNT(*) FROM posts WHERE {where}", params).fetchone()[0]
        pages = max(1, math.ceil(total / PER_PAGE))
        page = min(page, pages)
        posts = con.execute(f"SELECT * FROM posts WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                            params + [PER_PAGE, (page - 1) * PER_PAGE]).fetchall()
    return posts, page, pages, total

@app.route("/archives")
def archives():
    section = request.args.get("section", "")
    q = request.args.get("q", "").strip()[:100]
    posts, page, pages, total = list_posts(section, q)
    return render_template("archives.html", posts=posts, selected_section=section, q=q,
                           page=page, pages=pages, total=total, endpoint="archives", route_args={})

@app.route("/section/<path:section>")
def section_page(section):
    if section not in SECTIONS:
        abort(404)
    posts, page, pages, total = list_posts(section, "")
    return render_template("archives.html", posts=posts, selected_section=section, q="",
                           page=page, pages=pages, total=total, endpoint="section_page",
                           route_args={"section": section})

@app.route("/article/<int:post_id>")
def article(post_id):
    # Les éditeurs peuvent prévisualiser les brouillons.
    sql = "SELECT * FROM posts WHERE id=?" + ("" if is_admin() else " AND published=1")
    with db() as con:
        post = con.execute(sql, (post_id,)).fetchone()
    if not post:
        abort(404)
    return render_template("article.html", post=post)

# ---------- Connexion Discord ----------

@app.route("/login")
def login():
    client_id = os.getenv("DISCORD_CLIENT_ID", "")
    if not client_id:
        return render_template("login.html", setup_missing=True)
    if is_admin():
        return redirect(url_for("dashboard"))
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    params = {"client_id": client_id, "redirect_uri": redirect_uri(), "response_type": "code",
              "scope": "identify", "state": state}
    return redirect("https://discord.com/oauth2/authorize?" + urlencode(params))

@app.route("/auth/callback")
def auth_callback():
    expected = session.pop("oauth_state", None)
    if not expected or request.args.get("state") != expected:
        abort(400, "État OAuth invalide.")
    if request.args.get("error"):
        flash("Connexion Discord annulée.", "error")
        return redirect(url_for("home"))
    code = request.args.get("code")
    if not code:
        return redirect(url_for("login"))
    try:
        tr = requests.post("https://discord.com/api/oauth2/token", data={
            "client_id": os.getenv("DISCORD_CLIENT_ID"), "client_secret": os.getenv("DISCORD_CLIENT_SECRET"),
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()
        }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
        tr.raise_for_status()
        token = tr.json()["access_token"]
        ur = requests.get("https://discord.com/api/users/@me",
                          headers={"Authorization": f"Bearer {token}"}, timeout=15)
        ur.raise_for_status()
        user = ur.json()
    except Exception:
        app.logger.exception("Discord OAuth failed")
        flash("La connexion Discord a échoué. Vérifie la configuration du portail développeur.", "error")
        return redirect(url_for("login"))
    ids = allowed_ids()
    if not ids or str(user.get("id")) not in ids:
        session.clear()
        return render_template("forbidden.html"), 403
    session.clear()  # nouvelle session après connexion
    session["discord_user"] = {"id": str(user["id"]),
                               "username": user.get("global_name") or user.get("username", "Éditeur")}
    session.permanent = True
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

# ---------- Administration ----------

@app.route("/admin")
@login_required
def dashboard():
    with db() as con:
        posts = con.execute("SELECT * FROM posts ORDER BY updated_at DESC").fetchall()
    return render_template("admin.html", posts=posts)

@app.route("/admin/new", methods=["GET", "POST"])
@app.route("/admin/edit/<int:post_id>", methods=["GET", "POST"])
@login_required
def edit_post(post_id=None):
    post = None
    if post_id is not None:
        with db() as con:
            post = con.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if not post:
            abort(404)
    if request.method == "POST":
        title = request.form.get("title", "").strip()[:180]
        section = request.form.get("section", "")
        content = request.form.get("content", "").strip()
        if not title or not content or section not in SECTIONS:
            flash("Titre, section et contenu sont obligatoires.", "error")
            return render_template("editor.html", post=post, values=request.form)
        image_path = post["image"] if post else None
        old_image = image_path
        upload = request.files.get("image")
        if upload and upload.filename:
            if not allowed_file(upload.filename) or not valid_image(upload.stream):
                flash("Image invalide. Utilise un vrai fichier PNG, JPG, WEBP ou GIF.", "error")
                return render_template("editor.html", post=post, values=request.form)
            filename = secure_filename(f"{secrets.token_hex(8)}_{upload.filename}")
            upload.save(UPLOAD_DIR / filename)
            image_path = "/media/" + filename
        elif request.form.get("remove_image") == "on":
            image_path = None
        published = 1 if request.form.get("published") == "on" else 0
        with db() as con:
            if post:
                con.execute("UPDATE posts SET title=?,section=?,content=?,image=?,published=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (title, section, content, image_path, published, post_id))
            else:
                con.execute("INSERT INTO posts(title,section,content,image,published) VALUES(?,?,?,?,?)",
                            (title, section, content, image_path, published))
        if old_image and old_image != image_path:
            delete_image(old_image)
        flash("Article enregistré.", "success")
        return redirect(url_for("dashboard"))
    return render_template("editor.html", post=post, values={})

@app.post("/admin/toggle/<int:post_id>")
@login_required
def toggle_post(post_id):
    with db() as con:
        con.execute("UPDATE posts SET published=1-published, updated_at=CURRENT_TIMESTAMP WHERE id=?", (post_id,))
    flash("Visibilité mise à jour.", "success")
    return redirect(url_for("dashboard"))

@app.post("/admin/delete/<int:post_id>")
@login_required
def delete_post(post_id):
    with db() as con:
        row = con.execute("SELECT image FROM posts WHERE id=?", (post_id,)).fetchone()
        con.execute("DELETE FROM posts WHERE id=?", (post_id,))
    if row:
        delete_image(row["image"])
    flash("Article supprimé.", "success")
    return redirect(url_for("dashboard"))

# ---------- Erreurs ----------

@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404

@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(413)
@app.errorhandler(500)
def error_page(e):
    code = getattr(e, "code", 500)
    msgs = {400: getattr(e, "description", "Requête invalide."),
            403: "Accès refusé.", 413: "Fichier trop volumineux (8 Mo maximum).",
            500: "Erreur interne du serveur. Les équipes techniques ont été alertées."}
    return render_template("error.html", code=code, message=msgs.get(code, "Erreur.")), code

if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1", host="127.0.0.1", port=int(os.getenv("PORT", "5000")))

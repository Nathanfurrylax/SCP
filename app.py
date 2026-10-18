import os, re, sqlite3, secrets, time
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode
import requests
from flask import Flask, render_template, request, redirect, url_for, session, abort, flash, send_from_directory
from werkzeug.utils import secure_filename
import markdown

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "scp_site.db"
UPLOAD_DIR = ROOT / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

SECTIONS = ["Dossiers SCP", "Rapports d'incident", "Journaux de recherche", "Histoires", "Annonces"]

def allowed_file(name):
    return "." in name and name.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            section TEXT NOT NULL, content TEXT NOT NULL, image TEXT,
            published INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        count = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        if count == 0:
            samples = [
                ("SCP-173 — Protocole de confinement","Dossiers SCP",
                 "## Résumé\n**Objet :** Sculpture. **Classe :** Euclide.\n\nLe personnel doit maintenir une surveillance constante. Toute procédure doit être suivie selon le protocole de sécurité.\n\n> DOCUMENT FICTIF — MAQUETTE SCP.",1),
                ("Incident 914 — Rapport d'analyse","Rapports d'incident",
                 "## Rapport interne\nLes résultats de l'expérience doivent rester confidentiels jusqu'à validation du directeur de recherche.",1),
                ("Journal 07 — Les portes du secteur C","Journaux de recherche",
                 "## Extrait de journal\nLes capteurs ont détecté une anomalie non répertoriée. Les équipes sont priées de consulter le protocole de confinement.",1),
            ]
            con.executemany("INSERT INTO posts(title,section,content,published) VALUES(?,?,?,?)", samples)

init_db()

def allowed_ids():
    return {x.strip() for x in os.getenv("ADMIN_DISCORD_IDS", "").split(",") if x.strip()}

def current_user():
    return session.get("discord_user")

def is_admin():
    user = current_user()
    return bool(user and str(user.get("id")) in allowed_ids() and allowed_ids())

@app.context_processor
def inject_globals():
    return {"sections": SECTIONS, "is_admin": is_admin(), "discord_user": current_user()}

@app.template_filter("markdown")
def render_markdown(value):
    return markdown.markdown(value or "", extensions=["extra", "sane_lists", "nl2br"])

def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not is_admin():
            flash("Accès réservé aux éditeurs Discord autorisés.", "error")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped

@app.route("/")
def home():
    with db() as con:
        posts = con.execute("SELECT * FROM posts WHERE published=1 ORDER BY created_at DESC LIMIT 6").fetchall()
    return render_template("home.html", posts=posts)

@app.route("/archives")
def archives():
    section = request.args.get("section", "")
    q = request.args.get("q", "").strip()
    sql = "SELECT * FROM posts WHERE published=1"
    params = []
    if section in SECTIONS:
        sql += " AND section=?"; params.append(section)
    if q:
        sql += " AND (title LIKE ? OR content LIKE ?)"; params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY created_at DESC"
    with db() as con:
        posts = con.execute(sql, params).fetchall()
    return render_template("archives.html", posts=posts, selected_section=section, q=q)

@app.route("/article/<int:post_id>")
def article(post_id):
    with db() as con:
        post = con.execute("SELECT * FROM posts WHERE id=? AND published=1", (post_id,)).fetchone()
    if not post:
        abort(404)
    return render_template("article.html", post=post)

@app.route("/section/<path:section>")
def section_page(section):
    if section not in SECTIONS:
        abort(404)
    with db() as con:
        posts = con.execute("SELECT * FROM posts WHERE published=1 AND section=? ORDER BY created_at DESC", (section,)).fetchall()
    return render_template("archives.html", posts=posts, selected_section=section, q="")

@app.route("/login")
def login():
    client_id = os.getenv("DISCORD_CLIENT_ID", "")
    if not client_id:
        return render_template("login.html", setup_missing=True)
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    redirect_uri = os.getenv("DISCORD_REDIRECT_URI", request.url_root.rstrip("/") + "/auth/callback")
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
              "scope": "identify", "state": state, "prompt": "consent"}
    return redirect("https://discord.com/oauth2/authorize?" + urlencode(params))

@app.route("/auth/callback")
def auth_callback():
    if request.args.get("state") != session.pop("oauth_state", None):
        abort(400, "État OAuth invalide.")
    code = request.args.get("code")
    if not code:
        return redirect(url_for("login"))
    redirect_uri = os.getenv("DISCORD_REDIRECT_URI", request.url_root.rstrip("/") + "/auth/callback")
    try:
        token_response = requests.post("https://discord.com/api/oauth2/token", data={
            "client_id": os.getenv("DISCORD_CLIENT_ID"), "client_secret": os.getenv("DISCORD_CLIENT_SECRET"),
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri
        }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
        token_response.raise_for_status()
        token = token_response.json()["access_token"]
        user_response = requests.get("https://discord.com/api/users/@me",
                                     headers={"Authorization": f"Bearer {token}"}, timeout=15)
        user_response.raise_for_status()
        user = user_response.json()
    except Exception:
        app.logger.exception("Discord OAuth failed")
        flash("La connexion Discord a échoué. Vérifie la configuration du portail développeur.", "error")
        return redirect(url_for("login"))
    if str(user.get("id")) not in allowed_ids() or not allowed_ids():
        session.clear()
        return render_template("forbidden.html"), 403
    session["discord_user"] = {"id": str(user["id"]), "username": user.get("global_name") or user.get("username", "Éditeur")}
    session.permanent = True
    app.permanent_session_lifetime = 60 * 60 * 12
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

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
        title = request.form.get("title", "").strip()
        section = request.form.get("section", "")
        content = request.form.get("content", "").strip()
        if not title or not content or section not in SECTIONS:
            flash("Titre, section et contenu sont obligatoires.", "error")
            return render_template("editor.html", post=post, values=request.form)
        image_path = post["image"] if post else None
        upload = request.files.get("image")
        if upload and upload.filename:
            if not allowed_file(upload.filename):
                flash("Format image non accepté. Utilise PNG, JPG, WEBP ou GIF.", "error")
                return render_template("editor.html", post=post, values=request.form)
            filename = secure_filename(f"{secrets.token_hex(8)}_{upload.filename}")
            upload.save(UPLOAD_DIR / filename)
            image_path = "/static/uploads/" + filename
        published = 1 if request.form.get("published") == "on" else 0
        with db() as con:
            if post:
                con.execute("""UPDATE posts SET title=?,section=?,content=?,image=?,published=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                            (title, section, content, image_path, published, post_id))
            else:
                con.execute("""INSERT INTO posts(title,section,content,image,published) VALUES(?,?,?,?,?)""",
                            (title, section, content, image_path, published))
        flash("Article enregistré.", "success")
        return redirect(url_for("dashboard"))
    return render_template("editor.html", post=post, values={})

@app.post("/admin/delete/<int:post_id>")
@login_required
def delete_post(post_id):
    with db() as con:
        con.execute("DELETE FROM posts WHERE id=?", (post_id,))
    flash("Article supprimé.", "success")
    return redirect(url_for("dashboard"))

@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404

if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1", host="127.0.0.1", port=int(os.getenv("PORT", "5000")))

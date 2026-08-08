import os
from datetime import datetime

from flask import Flask, render_template, redirect, url_for, request, flash, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuración básica
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cambia-esto-en-produccion")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///app.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Subida de fotos de perfil
UPLOAD_FOLDER = os.path.join(app.root_path, "static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_UPLOAD_MB = 5

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Inicia sesión para continuar."

oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


# ---------------------------------------------------------------------------
# Modelo de usuario
# ---------------------------------------------------------------------------
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=True)
    password_hash = db.Column(db.String(255), nullable=True)  # null si viene de Google
    auth_provider = db.Column(db.String(20), default="email")  # "email" o "google"
    avatar_url = db.Column(db.String(500), nullable=True)  # foto de Google
    profile_photo = db.Column(db.String(255), nullable=True)  # foto subida por el usuario
    bio = db.Column(db.String(280), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def display_photo(self):
        """Foto que debe mostrarse: la subida manualmente tiene prioridad sobre la de Google."""
        if self.profile_photo:
            return url_for("static", filename=f"uploads/{self.profile_photo}")
        return self.avatar_url

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ---------------------------------------------------------------------------
# Modelo de mensajes
# ---------------------------------------------------------------------------
class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.String(2000), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    sender = db.relationship("User", foreign_keys=[sender_id])
    receiver = db.relationship("User", foreign_keys=[receiver_id])


# ---------------------------------------------------------------------------
# Rutas: páginas
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if current_user.is_authenticated:
        return render_template("dashboard.html", user=current_user)
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()

        if user is None or not user.check_password(password):
            flash("Email o contraseña incorrectos.", "error")
            return render_template("login.html")

        login_user(user)
        return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")

        if not name or not email or not password:
            flash("Rellena todos los campos.", "error")
            return render_template("register.html")

        if password != password_confirm:
            flash("Las contraseñas no coinciden.", "error")
            return render_template("register.html")

        if len(password) < 8:
            flash("La contraseña debe tener al menos 8 caracteres.", "error")
            return render_template("register.html")

        if User.query.filter_by(email=email).first():
            flash("Ya existe una cuenta con ese email.", "error")
            return render_template("register.html")

        user = User(email=email, name=name, auth_provider="email")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        login_user(user)
        return redirect(url_for("index"))

    return render_template("register.html")


@app.route("/profile")
@login_required
def profile():
    return render_template("profile.html", user=current_user)


@app.route("/profile/edit", methods=["GET", "POST"])
@login_required
def profile_edit():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        bio = request.form.get("bio", "").strip()

        if not name:
            flash("El nombre no puede quedar vacío.", "error")
            return render_template("profile_edit.html", user=current_user)

        if len(bio) > 280:
            flash("La bio no puede superar los 280 caracteres.", "error")
            return render_template("profile_edit.html", user=current_user)

        photo = request.files.get("photo")
        if photo and photo.filename:
            if not allowed_file(photo.filename):
                flash("Formato de imagen no permitido. Usa PNG, JPG o WEBP.", "error")
                return render_template("profile_edit.html", user=current_user)

            ext = photo.filename.rsplit(".", 1)[1].lower()
            filename = secure_filename(f"user_{current_user.id}.{ext}")
            photo.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
            current_user.profile_photo = filename

        current_user.name = name
        current_user.bio = bio
        db.session.commit()

        flash("Perfil actualizado.", "success")
        return redirect(url_for("profile"))

    return render_template("profile_edit.html", user=current_user)


@app.route("/messages")
@login_required
def messages_inbox():
    sent_to = db.session.query(Message.receiver_id).filter_by(sender_id=current_user.id)
    received_from = db.session.query(Message.sender_id).filter_by(receiver_id=current_user.id)
    contact_ids = {row[0] for row in sent_to.union(received_from).all()}

    conversations = []
    for other_id in contact_ids:
        other = User.query.get(other_id)
        if not other:
            continue
        last_message = (
            Message.query
            .filter(
                db.or_(
                    db.and_(Message.sender_id == current_user.id, Message.receiver_id == other_id),
                    db.and_(Message.sender_id == other_id, Message.receiver_id == current_user.id),
                )
            )
            .order_by(Message.created_at.desc())
            .first()
        )
        conversations.append((other, last_message))

    conversations.sort(key=lambda c: c[1].created_at if c[1] else datetime.min, reverse=True)

    return render_template("messages_inbox.html", conversations=conversations)


@app.route("/messages/new")
@login_required
def messages_new():
    users = User.query.filter(User.id != current_user.id).order_by(User.name).all()
    return render_template("messages_new.html", users=users)


@app.route("/messages/<int:user_id>", methods=["GET", "POST"])
@login_required
def messages_chat(user_id):
    other = User.query.get_or_404(user_id)

    if other.id == current_user.id:
        flash("No puedes enviarte mensajes a ti mismo.", "error")
        return redirect(url_for("messages_inbox"))

    if request.method == "POST":
        content = request.form.get("content", "").strip()
        if content:
            msg = Message(sender_id=current_user.id, receiver_id=other.id, content=content)
            db.session.add(msg)
            db.session.commit()
        return redirect(url_for("messages_chat", user_id=other.id))

    chat_messages = (
        Message.query
        .filter(
            db.or_(
                db.and_(Message.sender_id == current_user.id, Message.receiver_id == other.id),
                db.and_(Message.sender_id == other.id, Message.receiver_id == current_user.id),
            )
        )
        .order_by(Message.created_at.asc())
        .all()
    )

    return render_template("messages_chat.html", other=other, messages=chat_messages)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Rutas: Google OAuth
# ---------------------------------------------------------------------------
@app.route("/auth/google")
def auth_google():
    redirect_uri = url_for("auth_google_callback", _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def auth_google_callback():
    token = google.authorize_access_token()
    user_info = token.get("userinfo")

    if not user_info or not user_info.get("email"):
        flash("No se pudo obtener tu email de Google.", "error")
        return redirect(url_for("login"))

    email = user_info["email"].lower()
    user = User.query.filter_by(email=email).first()

    if user is None:
        user = User(
            email=email,
            name=user_info.get("name", ""),
            auth_provider="google",
            avatar_url=user_info.get("picture"),
        )
        db.session.add(user)
        db.session.commit()

    login_user(user)
    return redirect(url_for("index"))


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)

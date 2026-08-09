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
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv
import stripe

load_dotenv()

# ---------------------------------------------------------------------------
# Configuración básica
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cambia-esto-en-produccion")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///app.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Stripe: procesa el pago con tarjeta cuando alguien compra tokens.
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")

# Paquetes de tokens disponibles para comprar. (id, nombre, tokens, precio en centavos de dólar)
TOKEN_PACKAGES = [
    {"id": "small", "name": "Paquete pequeño", "tokens": 100, "price_cents": 500},
    {"id": "medium", "name": "Paquete medio", "tokens": 500, "price_cents": 2000},
    {"id": "large", "name": "Paquete grande", "tokens": 1200, "price_cents": 4000},
]

# Render (y la mayoría de servicios de hosting) reciben la conexión segura (https)
# en su propio servidor y la reenvían a la app como http normal. Sin esto, Flask
# no sabe que la conexión original era https, y eso puede romper el login con
# Google (la URL de redirección generada no coincide con la registrada, o la
# cookie de sesión no se valida bien). ProxyFix corrige eso leyendo las
# cabeceras que el proxy añade.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Ajustes de la cookie de sesión: SameSite=Lax permite que la cookie sobreviva
# la redirección de vuelta desde Google.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Socket.IO: es el "cartero" que conecta a dos personas para que puedan
# ponerse de acuerdo y empezar una videollamada (WebRTC). El video y audio
# de la llamada en sí NO pasan por aquí, van directos entre los dos
# navegadores; esto solo sirve para el "apretón de manos" inicial.
# async_mode="threading" funciona con el mismo servidor (gunicorn) que ya
# usas, sin necesitar configuración extra en Render.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

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
    # Todavía no está conectado a pagos reales; de momento es solo el número
    # que se muestra en la pantalla de Inicio, para tener la parte visual
    # lista antes de decidir cómo se compran/gastan los tokens de verdad.
    tokens = db.Column(db.Integer, default=0)
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
# Preparar la base de datos al arrancar
# ---------------------------------------------------------------------------
# En este proyecto (todavía en fase de pruebas) no usamos migraciones, así que
# si el modelo de datos cambia, la base de datos vieja puede quedar
# desactualizada (columnas o tablas que faltan). Para evitarlo, en cada
# arranque se borra la base de datos anterior y se crea una nueva con el
# esquema actual. Esto es válido mientras se prueba la app; cuando quieras
# conservar los datos de verdad entre reinicios, se cambia por una base de
# datos externa (Postgres) y migraciones reales.
os.makedirs(app.instance_path, exist_ok=True)
_db_file_path = os.path.join(app.instance_path, "app.db")

with app.app_context():
    if os.path.exists(_db_file_path):
        os.remove(_db_file_path)
    db.create_all()


# ---------------------------------------------------------------------------
# Rutas: páginas
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if current_user.is_authenticated:
        return render_template("home.html", user=current_user)
    return redirect(url_for("login"))


@app.route("/users")
@login_required
def users_list():
    users = User.query.filter(User.id != current_user.id).order_by(User.name).all()
    return render_template("dashboard.html", user=current_user, users=users)


@app.route("/menu")
@login_required
def menu_page():
    return render_template("menu.html", user=current_user)


@app.route("/tokens/deposit")
@login_required
def tokens_deposit():
    return render_template("tokens_deposit.html", packages=TOKEN_PACKAGES)


@app.route("/tokens/checkout/<package_id>")
@login_required
def tokens_checkout(package_id):
    package = next((p for p in TOKEN_PACKAGES if p["id"] == package_id), None)
    if not package:
        flash("Paquete no válido.", "error")
        return redirect(url_for("tokens_deposit"))

    checkout_session = stripe.checkout.Session.create(
        mode="payment",
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": f"{package['tokens']} tokens · Conecta"},
                "unit_amount": package["price_cents"],
            },
            "quantity": 1,
        }],
        metadata={"user_id": current_user.id, "tokens": package["tokens"]},
        success_url=url_for("tokens_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=url_for("tokens_deposit", _external=True),
    )
    return redirect(checkout_session.url, code=303)


@app.route("/tokens/success")
@login_required
def tokens_success():
    session_id = request.args.get("session_id")
    if not session_id:
        return redirect(url_for("index"))

    checkout_session = stripe.checkout.Session.retrieve(session_id)

    if checkout_session.payment_status == "paid":
        tokens_bought = int(checkout_session.metadata.get("tokens", 0))
        paid_user_id = int(checkout_session.metadata.get("user_id", 0))

        # Solo se acreditan los tokens si el pago es de la persona que tiene la
        # sesión abierta ahora mismo, para evitar que alguien reutilice un
        # enlace de otra persona.
        if paid_user_id == current_user.id:
            current_user.tokens = (current_user.tokens or 0) + tokens_bought
            db.session.commit()
            flash(f"¡Listo! Se añadieron {tokens_bought} tokens a tu cuenta.", "success")
        else:
            flash("Este pago no corresponde a tu cuenta.", "error")
    else:
        flash("El pago no se completó.", "error")

    return redirect(url_for("index"))


@app.route("/tokens/withdraw", methods=["GET", "POST"])
@login_required
def tokens_withdraw():
    flash("La retirada de tokens todavía no está activada.", "error")
    return redirect(url_for("index"))


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
@app.route("/profile/<int:user_id>")
@login_required
def profile(user_id=None):
    shown_user = User.query.get_or_404(user_id) if user_id else current_user
    is_own_profile = shown_user.id == current_user.id
    return render_template("profile.html", user=shown_user, is_own_profile=is_own_profile)


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
# Videollamadas (WebRTC + Socket.IO para la señalización)
# ---------------------------------------------------------------------------
@app.route("/call/<int:user_id>")
@login_required
def call_page(user_id):
    other = User.query.get_or_404(user_id)
    if other.id == current_user.id:
        flash("No puedes llamarte a ti mismo.", "error")
        return redirect(url_for("index"))
    return render_template("call.html", other=other)


@socketio.on("connect")
def on_connect():
    # Cada usuario tiene su propia "sala" (con su id), así podemos enviarle
    # mensajes de señalización directamente sin que otros los vean.
    if current_user.is_authenticated:
        join_room(str(current_user.id))


@socketio.on("call_request")
def on_call_request(data):
    if not current_user.is_authenticated:
        return
    target_id = str(data.get("target_id"))
    emit(
        "incoming_call",
        {
            "from_id": current_user.id,
            "from_name": current_user.name or current_user.email,
            "from_photo": current_user.display_photo,
        },
        to=target_id,
    )


@socketio.on("call_response")
def on_call_response(data):
    if not current_user.is_authenticated:
        return
    target_id = str(data.get("target_id"))
    emit(
        "call_response",
        {"accepted": data.get("accepted", False), "from_id": current_user.id},
        to=target_id,
    )


@socketio.on("webrtc_offer")
def on_webrtc_offer(data):
    if not current_user.is_authenticated:
        return
    target_id = str(data.get("target_id"))
    emit("webrtc_offer", {"sdp": data.get("sdp"), "from_id": current_user.id}, to=target_id)


@socketio.on("webrtc_answer")
def on_webrtc_answer(data):
    if not current_user.is_authenticated:
        return
    target_id = str(data.get("target_id"))
    emit("webrtc_answer", {"sdp": data.get("sdp"), "from_id": current_user.id}, to=target_id)


@socketio.on("ice_candidate")
def on_ice_candidate(data):
    if not current_user.is_authenticated:
        return
    target_id = str(data.get("target_id"))
    emit("ice_candidate", {"candidate": data.get("candidate"), "from_id": current_user.id}, to=target_id)


@socketio.on("call_end")
def on_call_end(data):
    if not current_user.is_authenticated:
        return
    target_id = str(data.get("target_id"))
    emit("call_end", {"from_id": current_user.id}, to=target_id)


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
    # use_reloader=False evita que Flask arranque el proceso dos veces al
    # iniciar. En Windows, la segunda vez fallaba al no poder borrar/recrear
    # la base de datos porque el primer proceso aún la tenía abierta.
    # host="0.0.0.0" permite que otros dispositivos de tu misma red (móvil,
    # otro ordenador) puedan entrar usando tu IP local, no solo 127.0.0.1.
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, use_reloader=False)

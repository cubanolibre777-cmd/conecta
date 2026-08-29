import os
import random
import secrets
from datetime import datetime, date

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
import requests as http_requests
import stripe

from translations import (
    t, SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE, RTL_LANGUAGES, LANGUAGE_NAMES,
    detect_language_from_request,
)
from chat_translate import maybe_translate_message

load_dotenv()

# ---------------------------------------------------------------------------
# Configuración básica
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "cambia-esto-en-produccion")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///app.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Render, ngrok, etc. reciben la conexión segura (https) y la reenvían a la
# app como http normal. ProxyFix corrige eso leyendo las cabeceras que el
# proxy añade, para que el login con Google y las cookies funcionen bien.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

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
# El mensaje se traduce dinámicamente en unauthorized_handler (más abajo),
# porque en el momento de configurar esto no sabemos aún el idioma del visitante.
login_manager.login_message = None

oauth = OAuth(app)
google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# Stripe: procesa el pago con tarjeta cuando alguien compra tokens.
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")

# Paquetes de tokens disponibles para comprar. (id, nombre, tokens, precio en centavos de dólar)
TOKEN_PACKAGES = [
    {"id": "small", "name": "Paquete pequeño", "tokens": 100, "price_cents": 500},
    {"id": "medium", "name": "Paquete medio", "tokens": 500, "price_cents": 2000},
    {"id": "large", "name": "Paquete grande", "tokens": 1200, "price_cents": 4000},
]

# Costo de la videollamada: solo paga quien la inicia, 25 tokens por cada
# minuto conectado (se cobra al empezar y luego cada 60 segundos).
CALL_COST_PER_MINUTE = 25

# Catálogo de regalos que se pueden mandar en el chat, pagados con tokens.
GIFT_CATALOG = [
    {"emoji": "❤️", "name": "Corazón", "cost": 5},
    {"emoji": "🌹", "name": "Rosa", "cost": 10},
    {"emoji": "🧸", "name": "Peluche", "cost": 20},
    {"emoji": "💎", "name": "Diamante", "cost": 50},
    {"emoji": "👑", "name": "Corona", "cost": 100},
    {"emoji": "🏆", "name": "Trofeo", "cost": 200},
    {"emoji": "🚀", "name": "Cohete", "cost": 500},
]

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


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
    tokens = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Onboarding
    gender = db.Column(db.String(10), nullable=True)  # "masculino" / "femenino"
    looking_for = db.Column(db.String(10), default="todos")  # "masculino" / "femenino" / "todos"
    birthdate = db.Column(db.Date, nullable=True)
    onboarding_completed = db.Column(db.Boolean, default=False)
    active_session_token = db.Column(db.String(64), nullable=True)
    do_not_disturb = db.Column(db.Boolean, default=False)
    verification_status = db.Column(db.String(20), default="no_verificado")  # no_verificado / verificado / restringido_edad

    # Presencia y país (detectado automáticamente por IP al conectarse)
    is_online = db.Column(db.Boolean, default=False)
    country = db.Column(db.String(100), nullable=True)

    # Idioma de la interfaz. Se detecta automáticamente del navegador al
    # registrarse, pero el usuario puede cambiarlo luego desde el menú.
    language = db.Column(db.String(5), default=DEFAULT_LANGUAGE)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    @property
    def display_photo(self):
        """Foto que debe mostrarse: la subida manualmente tiene prioridad sobre la de Google."""
        if self.profile_photo:
            return url_for("static", filename=f"uploads/{self.profile_photo}")
        return self.avatar_url


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


@login_manager.unauthorized_handler
def unauthorized_handler():
    flash(t("flash_login_required", get_active_language()), "error")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Modelo de mensajes
# ---------------------------------------------------------------------------
class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.String(2000), nullable=True)
    image_filename = db.Column(db.String(255), nullable=True)
    is_gift = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    read = db.Column(db.Boolean, default=False)

    sender = db.relationship("User", foreign_keys=[sender_id])
    receiver = db.relationship("User", foreign_keys=[receiver_id])


class ProfileVisit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    visitor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    visited_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    visitor = db.relationship("User", foreign_keys=[visitor_id])
    visited = db.relationship("User", foreign_keys=[visited_id])


class CallLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    caller_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    callee_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    ended_at = db.Column(db.DateTime, nullable=True)
    duration_seconds = db.Column(db.Integer, default=0)
    tokens_spent = db.Column(db.Integer, default=0)
    outcome = db.Column(db.String(20), default="perdida")  # "conectada" / "perdida" / "rechazada"

    caller = db.relationship("User", foreign_keys=[caller_id])
    callee = db.relationship("User", foreign_keys=[callee_id])


class FriendRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    status = db.Column(db.String(10), default="pending")  # pending / accepted
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    sender = db.relationship("User", foreign_keys=[sender_id])
    receiver = db.relationship("User", foreign_keys=[receiver_id])


class Follow(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    follower_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    followed_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    follower = db.relationship("User", foreign_keys=[follower_id])
    followed = db.relationship("User", foreign_keys=[followed_id])


class UserPhoto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id])


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)  # quién la recibe
    from_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    message = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    read = db.Column(db.Boolean, default=False)

    user = db.relationship("User", foreign_keys=[user_id])
    from_user = db.relationship("User", foreign_keys=[from_user_id])


class VerificationRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    match_score = db.Column(db.Float, nullable=True)  # qué tan parecida era la cara (0 a 1, más alto = más parecida)
    estimated_age = db.Column(db.Float, nullable=True)  # edad estimada por la IA
    result = db.Column(db.String(30), nullable=False)  # "aprobada_auto" / "rechazada_no_coincide" / "alerta_edad"
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id])


def create_notification(user_id, from_user_id, message):
    """Crea una notificación y conserva solo las 20 más recientes de esa persona."""
    db.session.add(Notification(user_id=user_id, from_user_id=from_user_id, message=message))
    db.session.commit()

    all_notifs = (
        Notification.query.filter_by(user_id=user_id)
        .order_by(Notification.created_at.desc())
        .all()
    )
    if len(all_notifs) > 20:
        for old in all_notifs[20:]:
            db.session.delete(old)
        db.session.commit()


def is_user_busy(user_id):
    """True si esa persona está en una llamada en curso (recibiéndola o ya conectada)."""
    return CallLog.query.filter(
        db.or_(CallLog.caller_id == user_id, CallLog.callee_id == user_id),
        CallLog.ended_at.is_(None),
    ).first() is not None


# ---------------------------------------------------------------------------
# Preparar la base de datos al arrancar
# ---------------------------------------------------------------------------
# En este proyecto (todavía en fase de pruebas) no usamos migraciones, así que
# si el modelo de datos cambia, la base de datos vieja puede quedar
# desactualizada. Para evitarlo, en cada arranque se borra la base de datos
# anterior y se crea una nueva con el esquema actual. Cuando quieras conservar
# los datos entre reinicios, se cambia por una base de datos externa
# (Postgres) y migraciones reales.
os.makedirs(app.instance_path, exist_ok=True)
_db_file_path = os.path.join(app.instance_path, "app.db")

with app.app_context():
    if os.path.exists(_db_file_path):
        os.remove(_db_file_path)
    db.create_all()


# ---------------------------------------------------------------------------
# Sesión única: iniciar sesión en un dispositivo nuevo cierra la sesión
# anterior automáticamente (no se puede estar conectado en dos sitios a la vez).
# ---------------------------------------------------------------------------
def start_new_session(user):
    """Genera un código de sesión nuevo y cierra cualquier otra sesión activa
    de esta cuenta en otros dispositivos."""
    token = secrets.token_hex(24)
    user.active_session_token = token
    db.session.commit()
    session["session_token"] = token


# ---------------------------------------------------------------------------
# Idioma: detección automática (navegador) + selector manual guardado en el
# usuario. Los visitantes no autenticados usan lo detectado, guardado en la
# sesión para no repetir el cálculo en cada petición.
# ---------------------------------------------------------------------------
def get_active_language():
    if current_user.is_authenticated and current_user.language:
        return current_user.language

    if "detected_language" not in session:
        session["detected_language"] = detect_language_from_request(request.accept_languages)

    return session["detected_language"]


@app.context_processor
def inject_i18n():
    lang = get_active_language()

    def _t(key, **kwargs):
        # Ata automáticamente el idioma activo, así en las plantillas basta
        # con escribir {{ t('clave') }} sin repetir el idioma cada vez.
        return t(key, lang, **kwargs)

    return {
        "t": _t,
        "active_language": lang,
        "is_rtl": lang in RTL_LANGUAGES,
        "supported_languages": SUPPORTED_LANGUAGES,
        "language_names": LANGUAGE_NAMES,
    }


@app.route("/settings/language/<lang_code>")
def set_language(lang_code):
    if lang_code not in SUPPORTED_LANGUAGES:
        flash(t("flash_no_access", get_active_language()), "error")
        return redirect(request.referrer or url_for("index"))

    if current_user.is_authenticated:
        current_user.language = lang_code
        db.session.commit()
    else:
        session["detected_language"] = lang_code

    flash(t("settings_language_changed", lang_code), "success")
    return redirect(request.referrer or url_for("index"))


SESSION_CHECK_EXEMPT_ENDPOINTS = {"login", "register", "logout", "auth_google", "auth_google_callback", "static"}


@app.before_request
def enforce_single_session():
    if request.endpoint and request.endpoint in SESSION_CHECK_EXEMPT_ENDPOINTS:
        return
    if current_user.is_authenticated:
        if session.get("session_token") != current_user.active_session_token:
            logout_user()
            flash(t("flash_session_taken_over", get_active_language()), "error")
            return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Onboarding (género y perfil), obligatorio tras registrarse
# ---------------------------------------------------------------------------
ONBOARDING_EXEMPT_ENDPOINTS = {"onboarding_gender", "onboarding_profile", "logout", "static"}


@app.before_request
def enforce_onboarding():
    if current_user.is_authenticated and not current_user.onboarding_completed:
        if request.endpoint and request.endpoint not in ONBOARDING_EXEMPT_ENDPOINTS:
            return redirect(url_for("onboarding_gender"))


@app.context_processor
def inject_unread_count():
    # Disponible automáticamente en todas las plantillas, para mostrar el
    # contador en el icono de Chat sin tener que pasarlo en cada ruta.
    if current_user.is_authenticated:
        count = Message.query.filter_by(receiver_id=current_user.id, read=False).count()
        visitor_count = (
            db.session.query(ProfileVisit.visitor_id)
            .filter_by(visited_id=current_user.id)
            .distinct()
            .count()
        )
        notif_count = Notification.query.filter_by(user_id=current_user.id, read=False).count()
        return {"unread_messages_count": count, "visitor_count": visitor_count, "notif_count": notif_count, "admin_emails": ADMIN_EMAILS}
    return {"unread_messages_count": 0, "visitor_count": 0, "notif_count": 0, "admin_emails": ADMIN_EMAILS}


@app.route("/onboarding/gender", methods=["GET", "POST"])
@login_required
def onboarding_gender():
    if request.method == "POST":
        gender = request.form.get("gender")
        if gender not in ("masculino", "femenino"):
            flash(t("flash_choose_option", get_active_language()), "error")
            return render_template("onboarding_gender.html")

        current_user.gender = gender
        db.session.commit()
        return redirect(url_for("onboarding_profile"))

    return render_template("onboarding_gender.html")


@app.route("/onboarding/profile", methods=["GET", "POST"])
@login_required
def onboarding_profile():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        birthdate_str = request.form.get("birthdate", "").strip()

        if not name:
            flash(t("flash_enter_display_name", get_active_language()), "error")
            return render_template("onboarding_profile.html")

        if not birthdate_str:
            flash(t("flash_enter_birthdate", get_active_language()), "error")
            return render_template("onboarding_profile.html")

        try:
            birthdate_value = datetime.strptime(birthdate_str, "%Y-%m-%d").date()
        except ValueError:
            flash(t("flash_invalid_birthdate", get_active_language()), "error")
            return render_template("onboarding_profile.html")

        today = date.today()
        age = today.year - birthdate_value.year - (
            (today.month, today.day) < (birthdate_value.month, birthdate_value.day)
        )
        if age < 18:
            flash(t("flash_min_age", get_active_language()), "error")
            return render_template("onboarding_profile.html")

        photo = request.files.get("photo")
        if photo and photo.filename:
            if allowed_file(photo.filename):
                ext = photo.filename.rsplit(".", 1)[1].lower()
                filename = secure_filename(f"user_{current_user.id}.{ext}")
                photo.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
                current_user.profile_photo = filename
            else:
                flash(t("flash_invalid_image_format", get_active_language()), "error")
                return render_template("onboarding_profile.html")

        current_user.name = name
        current_user.birthdate = birthdate_value
        current_user.onboarding_completed = True
        db.session.commit()

        return redirect(url_for("index"))

    return render_template("onboarding_profile.html")


# ---------------------------------------------------------------------------
# Rutas: páginas
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if not current_user.is_authenticated:
        return redirect(url_for("login"))

    country_filter = request.args.get("country") or None

    online_query = User.query.filter(User.id != current_user.id, User.is_online.is_(True))

    countries = sorted({
        u.country for u in online_query.all() if u.country
    })

    if country_filter:
        online_query = online_query.filter(User.country == country_filter)

    online_users = online_query.order_by(User.name).all()

    busy_logs = CallLog.query.filter(CallLog.ended_at.is_(None)).all()
    busy_ids = set()
    for bl in busy_logs:
        busy_ids.add(bl.caller_id)
        busy_ids.add(bl.callee_id)

    return render_template(
        "explorar.html",
        user=current_user,
        online_users=online_users,
        countries=countries,
        selected_country=country_filter,
        looking_for=current_user.looking_for,
        busy_ids=busy_ids,
    )


@app.route("/match/preference/<pref>")
@login_required
def match_preference(pref):
    if pref in ("masculino", "femenino", "todos"):
        current_user.looking_for = pref
        db.session.commit()
    return redirect(url_for("index", country=request.args.get("country")))


@app.route("/match/start")
@login_required
def match_start():
    if current_user.verification_status == "restringido_edad":
        flash(t("flash_account_under_review", get_active_language()), "error")
        return redirect(url_for("index"))

    candidates = User.query.filter(
        User.id != current_user.id, User.is_online.is_(True), User.do_not_disturb.is_(False)
    )

    if current_user.looking_for in ("masculino", "femenino"):
        candidates = candidates.filter(User.gender == current_user.looking_for)

    country_filter = request.args.get("country")
    if country_filter:
        candidates = candidates.filter(User.country == country_filter)

    candidates = candidates.all()

    if not candidates:
        flash(t("flash_no_candidates", get_active_language()), "error")
        return redirect(url_for("index", country=country_filter))

    chosen = random.choice(candidates)
    return redirect(url_for("call_page", user_id=chosen.id))


@app.route("/users")
@login_required
def users_list():
    online_users = User.query.filter(User.id != current_user.id, User.is_online.is_(True)).all()
    return render_template(
        "emparejar.html",
        user=current_user,
        online_users=online_users,
        call_cost=CALL_COST_PER_MINUTE,
    )


@app.route("/bonus")
@login_required
def bonus_page():
    flash(t("flash_bonus_soon", get_active_language()), "error")
    return redirect(url_for("users_list"))


@app.route("/menu")
@login_required
def menu_page():
    return render_template("configuracion.html", user=current_user)


# ---------------------------------------------------------------------------
# Rutas: páginas de autenticación
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()

        if user is None or not user.check_password(password):
            flash(t("flash_wrong_credentials", get_active_language()), "error")
            return render_template("login.html")

        login_user(user)
        start_new_session(user)
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
            flash(t("flash_fill_all_fields", get_active_language()), "error")
            return render_template("register.html")

        if password != password_confirm:
            flash(t("flash_passwords_dont_match", get_active_language()), "error")
            return render_template("register.html")

        if len(password) < 8:
            flash(t("flash_password_too_short", get_active_language()), "error")
            return render_template("register.html")

        if User.query.filter_by(email=email).first():
            flash(t("flash_email_taken", get_active_language()), "error")
            return render_template("register.html")

        user = User(email=email, name=name, auth_provider="email", language=get_active_language())
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        login_user(user)
        start_new_session(user)
        return redirect(url_for("index"))

    return render_template("register.html")


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
        flash(t("flash_google_no_email", get_active_language()), "error")
        return redirect(url_for("login"))

    email = user_info["email"].lower()
    user = User.query.filter_by(email=email).first()

    if user is None:
        user = User(
            email=email,
            name=user_info.get("name", ""),
            auth_provider="google",
            avatar_url=user_info.get("picture"),
            language=get_active_language(),
        )
        db.session.add(user)
        db.session.commit()

    login_user(user)
    start_new_session(user)
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Rutas: perfil
# ---------------------------------------------------------------------------
@app.route("/profile")
@app.route("/profile/<int:user_id>")
@login_required
def profile(user_id=None):
    shown_user = User.query.get_or_404(user_id) if user_id else current_user
    is_own_profile = shown_user.id == current_user.id

    if not is_own_profile:
        visit = ProfileVisit(visitor_id=current_user.id, visited_id=shown_user.id)
        db.session.add(visit)
        db.session.commit()

    friends_count = FriendRequest.query.filter(
        db.and_(
            db.or_(FriendRequest.sender_id == shown_user.id, FriendRequest.receiver_id == shown_user.id),
            FriendRequest.status == "accepted",
        )
    ).count()
    following_count = FriendRequest.query.filter_by(sender_id=shown_user.id, status="pending").count()
    fans_count = FriendRequest.query.filter_by(receiver_id=shown_user.id, status="pending").count()

    friendship_status = None
    pending_request_id = None
    is_following = False

    if not is_own_profile:
        fr = FriendRequest.query.filter(
            db.or_(
                db.and_(FriendRequest.sender_id == current_user.id, FriendRequest.receiver_id == shown_user.id),
                db.and_(FriendRequest.sender_id == shown_user.id, FriendRequest.receiver_id == current_user.id),
            )
        ).first()
        if fr:
            if fr.status == "accepted":
                friendship_status = "friends"
            elif fr.sender_id == current_user.id:
                friendship_status = "pending_sent"
            else:
                friendship_status = "pending_received"
                pending_request_id = fr.id

        is_following = Follow.query.filter_by(
            follower_id=current_user.id, followed_id=shown_user.id
        ).first() is not None

    age = None
    if shown_user.birthdate:
        today = date.today()
        age = today.year - shown_user.birthdate.year - (
            (today.month, today.day) < (shown_user.birthdate.month, shown_user.birthdate.day)
        )

    photos = UserPhoto.query.filter_by(user_id=shown_user.id).order_by(UserPhoto.created_at.asc()).all()

    return render_template(
        "profile.html",
        user=shown_user,
        is_own_profile=is_own_profile,
        friends_count=friends_count,
        following_count=following_count,
        fans_count=fans_count,
        friendship_status=friendship_status,
        pending_request_id=pending_request_id,
        is_following=is_following,
        age=age,
        photos=photos,
    )


@app.route("/friends/request/<int:user_id>")
@login_required
def friend_request_send(user_id):
    if user_id != current_user.id:
        existing = FriendRequest.query.filter(
            db.or_(
                db.and_(FriendRequest.sender_id == current_user.id, FriendRequest.receiver_id == user_id),
                db.and_(FriendRequest.sender_id == user_id, FriendRequest.receiver_id == current_user.id),
            )
        ).first()

        if existing:
            if existing.status == "accepted":
                # El interruptor también funciona siendo ya amigos: los deja de ser.
                db.session.delete(existing)
                db.session.commit()
                socketio.emit("friend_update", {"from_id": current_user.id, "notify": False}, to=str(user_id))
            elif existing.sender_id == current_user.id and existing.status == "pending":
                # Cancela tu propia solicitud pendiente.
                db.session.delete(existing)
                db.session.commit()
                socketio.emit("friend_update", {"from_id": current_user.id, "notify": False}, to=str(user_id))
        else:
            db.session.add(FriendRequest(sender_id=current_user.id, receiver_id=user_id, status="pending"))
            db.session.commit()
            create_notification(
                user_id,
                current_user.id,
                f"{current_user.name or current_user.email} te envió una solicitud de amistad.",
            )
            socketio.emit("friend_update", {"from_id": current_user.id, "notify": True}, to=str(user_id))

    return redirect(url_for("profile", user_id=user_id))


@app.route("/friends/accept/<int:request_id>")
@login_required
def friend_request_accept(request_id):
    fr = FriendRequest.query.get_or_404(request_id)
    if fr.receiver_id == current_user.id:
        fr.status = "accepted"
        db.session.commit()
        create_notification(
            fr.sender_id,
            current_user.id,
            f"{current_user.name or current_user.email} aceptó tu solicitud de amistad.",
        )
        socketio.emit("friend_update", {"from_id": current_user.id, "notify": True}, to=str(fr.sender_id))
    return redirect(url_for("friends_list"))


@app.route("/friends/reject/<int:request_id>")
@login_required
def friend_request_reject(request_id):
    fr = FriendRequest.query.get_or_404(request_id)
    if fr.receiver_id == current_user.id:
        db.session.delete(fr)
        db.session.commit()
        socketio.emit("friend_update", {"from_id": current_user.id, "notify": False}, to=str(fr.sender_id))
    return redirect(url_for("friends_list"))


@app.route("/friends")
@login_required
def friends_list():
    pending = FriendRequest.query.filter_by(receiver_id=current_user.id, status="pending").all()
    accepted = FriendRequest.query.filter(
        db.and_(
            db.or_(FriendRequest.sender_id == current_user.id, FriendRequest.receiver_id == current_user.id),
            FriendRequest.status == "accepted",
        )
    ).all()
    friends = [f.receiver if f.sender_id == current_user.id else f.sender for f in accepted]
    return render_template("friends.html", pending=pending, friends=friends)


@app.route("/follow/<int:user_id>")
@login_required
def toggle_follow(user_id):
    if user_id != current_user.id:
        existing = Follow.query.filter_by(follower_id=current_user.id, followed_id=user_id).first()
        if existing:
            db.session.delete(existing)
        else:
            db.session.add(Follow(follower_id=current_user.id, followed_id=user_id))
        db.session.commit()
    return redirect(url_for("profile", user_id=user_id))


@app.route("/coming-soon")
@login_required
def coming_soon():
    flash(t("flash_feature_soon", get_active_language()), "error")
    return redirect(request.referrer or url_for("profile"))


# ---------------------------------------------------------------------------
# Verificación de cuenta: automática, con foto de referencia + captura en
# vivo por cámara. Todo se procesa en el navegador del usuario (no se sube
# ninguna imagen al servidor, solo el resultado final).
# ---------------------------------------------------------------------------
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}

# Umbral de similitud facial (face-api.js): más bajo = exige más parecido.
FACE_MATCH_DISTANCE_THRESHOLD = 0.6

# Por debajo de esta edad estimada, se marca como posible menor de edad
# (con margen de seguridad, ya que la IA no es exacta).
AGE_ALERT_THRESHOLD = 21


def is_admin():
    return current_user.is_authenticated and current_user.email.lower() in ADMIN_EMAILS


@app.route("/verification")
@login_required
def verification_request():
    if current_user.verification_status == "verificado":
        return redirect(url_for("profile"))
    return render_template("verification.html")


@app.route("/verification/submit", methods=["POST"])
@login_required
def verification_submit():
    data = request.get_json(silent=True) or {}
    match_score = data.get("match_score")
    estimated_age = data.get("estimated_age")

    if match_score is None or estimated_age is None:
        return {"ok": False, "error": "Datos incompletos."}, 400

    if estimated_age < AGE_ALERT_THRESHOLD:
        current_user.verification_status = "restringido_edad"
        result = "alerta_edad"
        db.session.add(VerificationRequest(
            user_id=current_user.id, match_score=match_score, estimated_age=estimated_age, result=result,
        ))
        db.session.commit()

        admins = User.query.filter(db.func.lower(User.email).in_(ADMIN_EMAILS)).all()
        for admin in admins:
            create_notification(
                admin.id, current_user.id,
                f"⚠️ Alerta de edad: {current_user.name or current_user.email} podría ser menor de edad (revisión necesaria).",
            )

        return {"ok": True, "status": "restringido_edad"}

    if match_score >= FACE_MATCH_DISTANCE_THRESHOLD:
        current_user.verification_status = "verificado"
        result = "aprobada_auto"
    else:
        result = "rechazada_no_coincide"

    db.session.add(VerificationRequest(
        user_id=current_user.id, match_score=match_score, estimated_age=estimated_age, result=result,
    ))
    db.session.commit()

    return {"ok": True, "status": current_user.verification_status}


@app.route("/admin/age-alerts")
@login_required
def admin_age_alerts():
    if not is_admin():
        flash(t("flash_no_access", get_active_language()), "error")
        return redirect(url_for("profile"))

    flagged = User.query.filter_by(verification_status="restringido_edad").all()
    return render_template("admin_age_alerts.html", flagged_users=flagged)


@app.route("/admin/age-alerts/<int:user_id>/clear")
@login_required
def admin_age_alert_clear(user_id):
    if not is_admin():
        return redirect(url_for("profile"))

    target = User.query.get_or_404(user_id)
    target.verification_status = "no_verificado"
    db.session.commit()

    create_notification(target.id, current_user.id, "Tu cuenta fue revisada y ya puedes usar todas las funciones con normalidad.")
    flash(t("flash_restriction_lifted", get_active_language()), "success")
    return redirect(url_for("admin_age_alerts"))


@app.route("/settings/dnd/toggle")
@login_required
def toggle_dnd():
    current_user.do_not_disturb = not current_user.do_not_disturb
    db.session.commit()
    return redirect(url_for("menu_page"))


@app.route("/legal/privacy")
def privacy_policy():
    return render_template("privacy_policy.html")


@app.route("/legal/terms")
def terms_of_service():
    return render_template("terms_of_service.html")


@app.route("/account/delete", methods=["GET", "POST"])
@login_required
def delete_account():
    if request.method == "POST":
        user_id = current_user.id

        Message.query.filter(
            db.or_(Message.sender_id == user_id, Message.receiver_id == user_id)
        ).delete(synchronize_session=False)
        ProfileVisit.query.filter(
            db.or_(ProfileVisit.visitor_id == user_id, ProfileVisit.visited_id == user_id)
        ).delete(synchronize_session=False)
        CallLog.query.filter(
            db.or_(CallLog.caller_id == user_id, CallLog.callee_id == user_id)
        ).delete(synchronize_session=False)
        FriendRequest.query.filter(
            db.or_(FriendRequest.sender_id == user_id, FriendRequest.receiver_id == user_id)
        ).delete(synchronize_session=False)
        Follow.query.filter(
            db.or_(Follow.follower_id == user_id, Follow.followed_id == user_id)
        ).delete(synchronize_session=False)
        UserPhoto.query.filter_by(user_id=user_id).delete(synchronize_session=False)

        user = User.query.get(user_id)
        logout_user()
        db.session.delete(user)
        db.session.commit()

        flash(t("flash_account_deleted", get_active_language()), "success")
        return redirect(url_for("login"))

    return render_template("delete_account.html")


@app.route("/profile/photos/upload", methods=["POST"])
@login_required
def upload_gallery_photo():
    existing_count = UserPhoto.query.filter_by(user_id=current_user.id).count()
    if existing_count >= 5:
        flash(t("flash_max_photos", get_active_language()), "error")
        return redirect(url_for("profile"))

    photo = request.files.get("gallery_photo")
    if photo and photo.filename and allowed_file(photo.filename):
        ext = photo.filename.rsplit(".", 1)[1].lower()
        filename = secure_filename(f"gallery_{current_user.id}_{secrets.token_hex(6)}.{ext}")
        photo.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
        db.session.add(UserPhoto(user_id=current_user.id, filename=filename))
        db.session.commit()
    else:
        flash(t("flash_invalid_image_format", get_active_language()), "error")

    return redirect(url_for("profile"))


@app.route("/profile/photos/delete/<int:photo_id>")
@login_required
def delete_gallery_photo(photo_id):
    photo = UserPhoto.query.get_or_404(photo_id)
    if photo.user_id == current_user.id:
        db.session.delete(photo)
        db.session.commit()
    return redirect(url_for("profile"))


@app.route("/notifications")
@login_required
def notifications_list():
    notifs = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).all()

    unread_ids = [n.id for n in notifs if not n.read]
    if unread_ids:
        Notification.query.filter(Notification.id.in_(unread_ids)).update(
            {"read": True}, synchronize_session=False
        )
        db.session.commit()

    return render_template("notifications.html", notifications=notifs)


@app.route("/visitors")
@login_required
def visitors_list():
    visits = (
        ProfileVisit.query
        .filter_by(visited_id=current_user.id)
        .order_by(ProfileVisit.created_at.desc())
        .all()
    )

    seen_ids = set()
    unique_visits = []
    for v in visits:
        if v.visitor_id not in seen_ids:
            seen_ids.add(v.visitor_id)
            unique_visits.append(v)

    return render_template("visitors.html", visits=unique_visits)


@app.route("/calls/history")
@login_required
def calls_history():
    logs = (
        CallLog.query
        .filter(db.or_(CallLog.caller_id == current_user.id, CallLog.callee_id == current_user.id))
        .order_by(CallLog.started_at.desc())
        .all()
    )
    return render_template("calls_history.html", logs=logs)


@app.route("/profile/edit", methods=["GET", "POST"])
@login_required
def profile_edit():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        bio = request.form.get("bio", "").strip()

        if not name:
            flash(t("flash_name_empty", get_active_language()), "error")
            return render_template("profile_edit.html", user=current_user)

        if len(bio) > 280:
            flash(t("flash_bio_too_long", get_active_language()), "error")
            return render_template("profile_edit.html", user=current_user)

        photo = request.files.get("photo")
        if photo and photo.filename:
            if not allowed_file(photo.filename):
                flash(t("flash_invalid_image_format", get_active_language()), "error")
                return render_template("profile_edit.html", user=current_user)

            ext = photo.filename.rsplit(".", 1)[1].lower()
            filename = secure_filename(f"user_{current_user.id}.{ext}")
            photo.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
            current_user.profile_photo = filename

        current_user.name = name
        current_user.bio = bio
        db.session.commit()

        flash(t("flash_profile_updated", get_active_language()), "success")
        return redirect(url_for("profile"))

    return render_template("profile_edit.html", user=current_user)


# ---------------------------------------------------------------------------
# Rutas: mensajería
# ---------------------------------------------------------------------------
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
        unread_count = Message.query.filter_by(
            sender_id=other_id, receiver_id=current_user.id, read=False
        ).count()
        conversations.append((other, last_message, unread_count))

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
        flash(t("flash_no_self_message", get_active_language()), "error")
        return redirect(url_for("messages_inbox"))

    if request.method == "POST":
        content = request.form.get("content", "").strip()
        image_filename = None

        photo = request.files.get("chat_photo")
        if photo and photo.filename:
            if allowed_file(photo.filename):
                ext = photo.filename.rsplit(".", 1)[1].lower()
                image_filename = secure_filename(f"chat_{current_user.id}_{secrets.token_hex(6)}.{ext}")
                photo.save(os.path.join(app.config["UPLOAD_FOLDER"], image_filename))
            else:
                flash(t("flash_invalid_image_format", get_active_language()), "error")

        if content or image_filename:
            msg = Message(
                sender_id=current_user.id,
                receiver_id=other.id,
                content=content or None,
                image_filename=image_filename,
            )
            db.session.add(msg)
            db.session.commit()

            # Traducción automática: si quien envía y quien recibe tienen
            # idiomas distintos, se traduce al idioma de quien lo recibe.
            # El texto original nunca se pierde (queda guardado tal cual en
            # la base de datos); esto solo afecta lo que se muestra.
            translation = None
            if content:
                translation = maybe_translate_message(content, current_user.language, other.language)

            # Avisa en tiempo real a quien recibe el mensaje (notificación
            # flotante + contador en el icono de Chat), esté donde esté.
            socketio.emit(
                "new_message",
                {
                    "from_id": current_user.id,
                    "from_name": current_user.name or current_user.email,
                    "from_photo": current_user.display_photo,
                    "content": content or "",
                    "translated_content": translation["translated"] if translation and translation["was_translated"] else None,
                    "image_url": url_for("static", filename=f"uploads/{image_filename}") if image_filename else None,
                },
                to=str(other.id),
            )
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

    # Al abrir la conversación, se marcan como leídos los mensajes que el
    # otro usuario te haya enviado.
    unread = [m for m in chat_messages if m.receiver_id == current_user.id and not m.read]
    if unread:
        for m in unread:
            m.read = True
        db.session.commit()

    # Traducción automática del historial: a los mensajes que el otro
    # usuario te escribió (en su idioma) se les añade la traducción a tu
    # idioma, sin tocar el texto original. Si ambos hablan el mismo idioma,
    # no se traduce nada (ahorra costo de la API).
    for m in chat_messages:
        m.translated_content = None
        if m.sender_id != current_user.id and m.content and not m.is_gift:
            translation = maybe_translate_message(m.content, other.language, current_user.language)
            if translation["was_translated"]:
                m.translated_content = translation["translated"]

    return render_template("messages_chat.html", other=other, messages=chat_messages, gifts=GIFT_CATALOG)


@app.route("/messages/<int:user_id>/gift/<int:gift_index>", methods=["POST"])
@login_required
def send_gift(user_id, gift_index):
    other = User.query.get_or_404(user_id)

    if other.id == current_user.id or gift_index < 0 or gift_index >= len(GIFT_CATALOG):
        return redirect(url_for("messages_chat", user_id=user_id))

    gift = GIFT_CATALOG[gift_index]

    if (current_user.tokens or 0) < gift["cost"]:
        flash(t("flash_need_tokens_gift", get_active_language(), cost=gift["cost"]), "error")
        return redirect(url_for("tokens_deposit", next=url_for("messages_chat", user_id=user_id)))

    current_user.tokens -= gift["cost"]
    gift_text = f"{gift['emoji']} Te envió un regalo: {gift['name']}"
    msg = Message(sender_id=current_user.id, receiver_id=other.id, content=gift_text, is_gift=True)
    db.session.add(msg)
    db.session.commit()

    socketio.emit(
        "new_message",
        {
            "from_id": current_user.id,
            "from_name": current_user.name or current_user.email,
            "from_photo": current_user.display_photo,
            "content": gift_text,
            "image_url": None,
            "is_gift": True,
        },
        to=str(other.id),
    )

    return redirect(url_for("messages_chat", user_id=user_id))


# ---------------------------------------------------------------------------
# Rutas: tokens (Stripe)
# ---------------------------------------------------------------------------
@app.route("/tokens/deposit")
@login_required
def tokens_deposit():
    next_url = request.args.get("next", "")
    return render_template("tokens_deposit.html", packages=TOKEN_PACKAGES, next_url=next_url)


@app.route("/tokens/checkout/<package_id>")
@login_required
def tokens_checkout(package_id):
    package = next((p for p in TOKEN_PACKAGES if p["id"] == package_id), None)
    if not package:
        flash(t("flash_invalid_package", get_active_language()), "error")
        return redirect(url_for("tokens_deposit"))

    next_url = request.args.get("next", "")
    metadata = {"user_id": current_user.id, "tokens": package["tokens"]}
    if next_url:
        metadata["next_url"] = next_url

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
        metadata=metadata,
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
    next_url = checkout_session.metadata.get("next_url")

    if checkout_session.payment_status == "paid":
        tokens_bought = int(checkout_session.metadata.get("tokens", 0))
        paid_user_id = int(checkout_session.metadata.get("user_id", 0))

        if paid_user_id == current_user.id:
            current_user.tokens = (current_user.tokens or 0) + tokens_bought
            db.session.commit()
            flash(t("flash_tokens_added", get_active_language(), tokens=tokens_bought), "success")
        else:
            flash(t("flash_payment_mismatch", get_active_language()), "error")
    else:
        flash(t("flash_payment_incomplete", get_active_language()), "error")

    return redirect(next_url or url_for("index"))


@app.route("/tokens/withdraw", methods=["GET", "POST"])
@login_required
def tokens_withdraw():
    flash(t("flash_withdraw_not_active", get_active_language()), "error")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Videollamadas (WebRTC + Socket.IO para la señalización)
# ---------------------------------------------------------------------------
@app.route("/call/<int:user_id>")
@login_required
def call_page(user_id):
    other = User.query.get_or_404(user_id)
    if other.id == current_user.id:
        flash(t("flash_no_self_call", get_active_language()), "error")
        return redirect(url_for("index"))

    is_incoming = request.args.get("incoming") == "1"

    if not is_incoming and current_user.verification_status == "restringido_edad":
        flash(t("flash_account_under_review", get_active_language()), "error")
        return redirect(url_for("index"))
    if not is_incoming and (current_user.tokens or 0) < CALL_COST_PER_MINUTE:
        flash(t("flash_need_tokens_call", get_active_language(), cost=CALL_COST_PER_MINUTE), "error")
        return redirect(url_for("tokens_deposit"))

    return render_template("call.html", other=other, call_cost=CALL_COST_PER_MINUTE)


def _detect_country(ip_address):
    """Detecta el país a partir de la IP usando un servicio gratuito.
    Si la IP es local (pruebas en tu propio PC) o falla la petición,
    devuelve None sin romper la conexión."""
    if not ip_address or ip_address in ("127.0.0.1", "::1") or ip_address.startswith("192.168.") or ip_address.startswith("10."):
        return None
    try:
        resp = http_requests.get(f"http://ip-api.com/json/{ip_address}?fields=status,country", timeout=3)
        data = resp.json()
        if data.get("status") == "success":
            return data.get("country")
    except Exception:
        pass
    return None


@socketio.on("connect")
def on_connect():
    if current_user.is_authenticated:
        join_room(str(current_user.id))
        current_user.is_online = True

        if not current_user.country:
            client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
            if client_ip:
                client_ip = client_ip.split(",")[0].strip()
            detected = _detect_country(client_ip)
            if detected:
                current_user.country = detected

        db.session.commit()
        socketio.emit("presence_update", {"user_id": current_user.id, "online": True})


@socketio.on("disconnect")
def on_disconnect():
    if current_user.is_authenticated:
        current_user.is_online = False
        db.session.commit()
        socketio.emit("presence_update", {"user_id": current_user.id, "online": False})


@socketio.on("call_request")
def on_call_request(data):
    if not current_user.is_authenticated:
        return
    target_id = str(data.get("target_id"))
    target_id_int = int(target_id)

    target_user = User.query.get(target_id_int)

    if target_user and target_user.do_not_disturb:
        emit("call_response", {"accepted": False, "from_id": target_id_int, "reason": "no_molestar"})
        _log_immediately_missed(target_id_int)
        return

    if is_user_busy(target_id_int) or is_user_busy(current_user.id):
        emit("call_response", {"accepted": False, "from_id": target_id_int, "reason": "ocupado"})
        _log_immediately_missed(target_id_int)
        return

    # Se crea el registro de la llamada en cuanto empiezas a marcar, no solo
    # si llega a conectar. Por defecto queda como "perdida" hasta que se
    # confirme cómo terminó (conectada, rechazada, o de verdad perdida).
    log = CallLog(caller_id=current_user.id, callee_id=target_id_int, started_at=datetime.utcnow())
    db.session.add(log)
    db.session.commit()
    emit("call_log_started", {"call_log_id": log.id})

    emit(
        "incoming_call",
        {
            "from_id": current_user.id,
            "from_name": current_user.name or current_user.email,
            "from_photo": current_user.display_photo,
        },
        to=target_id,
    )


def _log_immediately_missed(target_id_int):
    """Registra el intento de llamada como perdida al instante (no molestar u ocupado)."""
    log = CallLog(
        caller_id=current_user.id,
        callee_id=target_id_int,
        started_at=datetime.utcnow(),
        ended_at=datetime.utcnow(),
        outcome="perdida",
    )
    db.session.add(log)
    db.session.commit()
    create_notification(
        target_id_int,
        current_user.id,
        f"Llamada perdida de {current_user.name or current_user.email}.",
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


@socketio.on("charge_minute")
def on_charge_minute(data):
    if not current_user.is_authenticated:
        return
    target_id = str(data.get("target_id"))

    if (current_user.tokens or 0) >= CALL_COST_PER_MINUTE:
        current_user.tokens -= CALL_COST_PER_MINUTE
        db.session.commit()
        emit("tokens_charged", {"tokens": current_user.tokens})
    else:
        emit("call_end", {"from_id": current_user.id, "reason": "sin_tokens"})
        emit("call_end", {"from_id": current_user.id, "reason": "sin_tokens"}, to=target_id)


@socketio.on("call_end")
def on_call_end(data):
    if not current_user.is_authenticated:
        return
    target_id = str(data.get("target_id"))
    emit("call_end", {"from_id": current_user.id}, to=target_id)


@socketio.on("call_finished")
def on_call_finished(data):
    """El que llama avisa cómo terminó la llamada: conectada, rechazada o
    perdida (colgó antes de que contestaran)."""
    if not current_user.is_authenticated:
        return

    log_id = data.get("call_log_id")
    if not log_id:
        return

    log = CallLog.query.get(log_id)
    if not log or log.caller_id != current_user.id:
        return

    log.ended_at = datetime.utcnow()
    log.outcome = data.get("outcome", "perdida")
    log.duration_seconds = data.get("duration_seconds", 0)
    log.tokens_spent = data.get("tokens_spent", 0)
    db.session.commit()

    if log.outcome == "perdida":
        create_notification(
            log.callee_id,
            current_user.id,
            f"Llamada perdida de {current_user.name or current_user.email}.",
        )


if __name__ == "__main__":
    # use_reloader=False evita que Flask arranque el proceso dos veces al
    # iniciar (en Windows eso rompía el borrado/recreado de la base de datos).
    # host="0.0.0.0" permite conexiones desde otros dispositivos de tu red.
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, use_reloader=False)

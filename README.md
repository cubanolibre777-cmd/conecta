# Conecta — Autenticación (login/registro)

Base de autenticación con **email + contraseña** y **Google**, hecha con Flask.
Pensada como primera pieza de tu red social (mensajes, fotos, videollamadas de pago).

## 1. Requisitos

- Python 3.10+
- Visual Studio Code con la extensión de Python instalada

## 2. Instalación

Abre una terminal en la carpeta del proyecto (dentro de VS Code: `Terminal > Nueva terminal`) y ejecuta:

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

## 3. Configurar variables de entorno

Copia `.env.example` como `.env`:

```bash
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux
```

Abre `.env` y cambia `SECRET_KEY` por cualquier cadena larga aleatoria.
(Puedes dejar las variables de Google vacías por ahora — el login con email funcionará igual, solo el botón de Google fallará hasta que las configures).

## 4. Obtener credenciales de Google (para el botón "Continuar con Google")

1. Ve a [Google Cloud Console](https://console.cloud.google.com/) y crea un proyecto nuevo (o usa uno existente).
2. Ve a **APIs y servicios > Pantalla de consentimiento OAuth**. Elige "Externo", rellena el nombre de la app y tu email. Guarda.
3. Ve a **APIs y servicios > Credenciales > Crear credenciales > ID de cliente de OAuth**.
4. Tipo de aplicación: **Aplicación web**.
5. En **URIs de redirección autorizados**, añade exactamente:
   ```
   http://127.0.0.1:5000/auth/google/callback
   ```
6. Copia el **Client ID** y el **Client Secret** que te da Google y pégalos en tu `.env`:
   ```
   GOOGLE_CLIENT_ID=...
   GOOGLE_CLIENT_SECRET=...
   ```

## 5. Ejecutar el proyecto

```bash
python app.py
```

Abre [http://127.0.0.1:5000](http://127.0.0.1:5000) en tu navegador. La primera vez creará automáticamente el archivo `app.db` (base de datos SQLite) con la tabla de usuarios.

## 6. Estructura del proyecto

```
authapp/
├── app.py                  # Rutas, lógica de login/registro/Google
├── requirements.txt
├── .env.example
├── static/css/auth.css     # Estilos
└── templates/
    ├── base_auth.html      # Plantilla compartida (panel izquierdo)
    ├── login.html
    ├── register.html
    └── dashboard.html      # Página tras iniciar sesión (temporal, para probar)
```

## 7. Qué hace ahora mismo

- Registro por email + contraseña (con contraseña encriptada, nunca guardada en texto plano).
- Login por email + contraseña.
- Login/registro con Google (crea la cuenta automáticamente la primera vez).
- Sesión persistente con Flask-Login.
- Perfil de usuario: nombre, bio (hasta 280 caracteres) y foto subida por el usuario (`/profile` para verlo, `/profile/edit` para editarlo).
- Si el usuario entró con Google y no ha subido su propia foto, se usa la foto de su cuenta de Google automáticamente.
- Mensajería entre usuarios: bandeja de entrada (`/messages`), elegir con quién chatear (`/messages/new`) y conversación 1 a 1 (`/messages/<id>`).
- Videollamadas 1 a 1 con WebRTC (`/call/<id>`): se inician desde el chat o desde el perfil de otra persona. Mientras usas la app (dashboard, mensajes, perfil), si alguien te llama aparece una notificación flotante para aceptar o rechazar.

## 8. Próximos pasos sugeridos

1. Sustituir `dashboard.html` por el feed real de la red social (fotos).
2. Pagos con Stripe antes de permitir iniciar una llamada.
3. Guardar un historial de llamadas (quién llamó a quién y cuándo).

## 9. Nota sobre los mensajes

- Ahora mismo el chat se actualiza recargando la página (no es en tiempo real). Es una base sencilla y funcional; el salto a mensajes instantáneos (sin recargar) se hace más adelante con `Flask-SocketIO`, la misma pieza que necesitarás para las videollamadas.

## 10. Subir la app a internet (Render)

1. Sube este proyecto a un repositorio de GitHub. El archivo `.gitignore` ya está preparado para que tu `.env` (con tus secretos) **nunca** se suba.
2. En [render.com](https://render.com), crea una cuenta y un "New Web Service" conectado a tu repositorio.
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `gunicorn app:app`
5. En "Environment", añade `SECRET_KEY`, `GOOGLE_CLIENT_ID` y `GOOGLE_CLIENT_SECRET` con tus valores reales.
6. Cuando tengas tu URL pública (ej. `https://conecta.onrender.com`), añádela como nueva URI de redirección en Google Cloud Console: `https://conecta.onrender.com/auth/google/callback`

**Aviso:** en el plan gratuito de Render, el almacenamiento no es permanente — la base de datos y las fotos subidas se pueden borrar cuando el servidor se reinicia. Es perfecto para probar la app con otras personas, pero antes de un lanzamiento real conviene pasar a una base de datos externa (por ejemplo, Postgres, que Render también ofrece gratis) y a un almacenamiento de fotos externo (Amazon S3 o Cloudinary).

## 11. Notas sobre las videollamadas

- Funcionan navegador a navegador (WebRTC): el vídeo y el audio no pasan por tu servidor, solo la "señalización" inicial (ponerse de acuerdo para conectar).
- Funcionan bien en la mayoría de redes domésticas. En algunas redes con configuraciones de seguridad más estrictas (oficinas, universidades, ciertos routers), WebRTC puede necesitar un "servidor TURN" para conectar — algo que no está incluido todavía. Si algún usuario no logra conectar la llamada mientras otros sí, es la señal de que hace falta añadir uno (hay servicios gratuitos como el de Twilio o Metered para probar esto más adelante).
- Pide permiso de cámara y micrófono la primera vez — el usuario debe aceptarlo en su navegador.

## 9. Notas sobre las fotos de perfil

- Se guardan en `static/uploads/`, con el nombre `user_<id>.<extensión>`.
- Formatos permitidos: PNG, JPG, JPEG, WEBP. Tamaño máximo: 5 MB.
- Esto es válido para desarrollo local. Si más adelante despliegas la app en un servidor real, es recomendable mover las fotos a un almacenamiento externo (Amazon S3, Cloudinary) en vez de guardarlas en el propio servidor.

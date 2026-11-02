import json
import os
from pathlib import Path

from .config import parse_origin

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("RAVEN_WEB_STATE", "/data"))
CONFIG = json.loads((STATE_DIR / "config.json").read_text())
ORIGIN = os.environ.get("RAVEN_WEB_ORIGIN", CONFIG["origin"])
ORIGIN_PARTS = parse_origin(ORIGIN)
SECRET_KEY = (STATE_DIR / "secret.key").read_text()
DEBUG = False
ALLOWED_HOSTS = [ORIGIN_PARTS.hostname]
CSRF_TRUSTED_ORIGINS = [ORIGIN]
ROOT_URLCONF = "raven_web.urls"
ASGI_APPLICATION = "raven_web.asgi.application"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "raven_web",
]
MIDDLEWARE = [
    "raven_web.middleware.SecurityHeadersMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "raven_web.middleware.ExactHostMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": False,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
            ]
        },
    }
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": STATE_DIR / "db.sqlite3"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
SESSION_COOKIE_NAME = "__Host-raven_session"
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
SESSION_COOKIE_AGE = 8 * 60 * 60
SESSION_SAVE_EVERY_REQUEST = False
CSRF_COOKIE_NAME = "__Host-raven_csrf"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Strict"
CSRF_FAILURE_VIEW = "raven_web.views.csrf_failure"
SECURE_SSL_REDIRECT = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
DATA_UPLOAD_MAX_MEMORY_SIZE = 16 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 10
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "static"
WHITENOISE_MAX_AGE = 3600
RAVEN_BROKER = os.environ.get("RAVEN_WEB_BROKER", "/run/raven/broker.sock")
LOGIN_URL = "/login/"

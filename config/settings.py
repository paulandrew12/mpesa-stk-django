import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "insecure-dev-key-do-not-use-in-production")
DEBUG = env_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

# Render (and most PaaS) inject the public hostname at runtime rather than build time.
_render_host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
if _render_host:
    ALLOWED_HOSTS.append(_render_host)

# Daraja posts the callback from Safaricom's servers, so the tunnel or production
# host has to be trusted for CSRF as well as allowed.
CSRF_TRUSTED_ORIGINS = []
_callback_base = os.getenv("MPESA_CALLBACK_BASE_URL", "").strip().rstrip("/")
if _callback_base.startswith("https://"):
    CSRF_TRUSTED_ORIGINS.append(_callback_base)
    ALLOWED_HOSTS.append(_callback_base.removeprefix("https://"))
if _render_host:
    CSRF_TRUSTED_ORIGINS.append(f"https://{_render_host}")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "payments",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# SQLite keeps the demo to one command. The schema and constraints are the same
# ones you'd use on Postgres — see the note in README about switching.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "payments": {"handlers": ["console"], "level": "INFO"},
    },
}

# ---------------------------------------------------------------------------
# Daraja
# ---------------------------------------------------------------------------
MPESA = {
    "ENV": os.getenv("MPESA_ENV", "sandbox"),
    "CONSUMER_KEY": os.getenv("MPESA_CONSUMER_KEY", ""),
    "CONSUMER_SECRET": os.getenv("MPESA_CONSUMER_SECRET", ""),
    "SHORTCODE": os.getenv("MPESA_SHORTCODE", ""),
    "PASSKEY": os.getenv("MPESA_PASSKEY", ""),
    "CALLBACK_BASE_URL": _callback_base,
}

ALLOW_SIMULATION = env_bool("ALLOW_SIMULATION", False)

# Public-portfolio mode.
#
# With no Daraja credentials there is nothing to demonstrate: the push fails, no
# transaction is created, and a visitor sees an empty table and an error. DEMO_MODE
# skips the call to Safaricom and records the pending transaction directly, so the
# rest of the pipeline — callback handling, idempotency, timeout sweeping,
# reconciliation — can be exercised by anyone with a browser.
#
# It changes nothing downstream of the push. Every other line of code runs exactly
# as it does against the real API.
DEMO_MODE = env_bool("DEMO_MODE", False)

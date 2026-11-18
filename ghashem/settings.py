from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent

# Core

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if os.environ.get("DB_HOST"):
        raise RuntimeError(
            "DJANGO_SECRET_KEY environment variable is required in production. "
            "Set it in the App Service Configuration blade."
        )
    SECRET_KEY = "ghashem-local-dev-only-key-never-use-in-production"

DEBUG = os.environ.get("DJANGO_DEBUG", "True") == "True"

_allowed_hosts_env = os.environ.get("DJANGO_ALLOWED_HOSTS", "")
if _allowed_hosts_env:
    ALLOWED_HOSTS = [h.strip() for h in _allowed_hosts_env.split(",") if h.strip()]
elif DEBUG:
    ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
else:
    ALLOWED_HOSTS = []

# CSRF trusted origins -- required in Django 4.x when requests arrive via
# HTTPS or a non-standard host. Reads from env var if set, otherwise derives
# from ALLOWED_HOSTS automatically.
_csrf_env = os.environ.get("CSRF_TRUSTED_ORIGINS", "")
if _csrf_env:
    CSRF_TRUSTED_ORIGINS = [h.strip() for h in _csrf_env.split(",") if h.strip()]
else:
    CSRF_TRUSTED_ORIGINS = [
        f"https://{h}" if h not in ("localhost", "127.0.0.1") else f"http://{h}"
        for h in ALLOWED_HOSTS
    ]
    # ALLOWED_HOSTS has no port information, so the Django dev server's
    # default port (localhost:8000) needs to be added explicitly here —
    # it's a distinct CSRF origin from plain http://localhost.
    if "localhost" in ALLOWED_HOSTS:
        CSRF_TRUSTED_ORIGINS.append("http://localhost:8000")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # "axes",  # disabled - inet port issue
    "social_django",        # Microsoft SSO
    "core",
    "pipeline",
    "dashboard",
    "contracts",
]

# django-anymail is in requirements.txt, but a code-only deploy (build
# OFF — see "Fast deploy habits" in CLAUDE.md) never installs it on the
# App Service venv, and an unconditional "anymail" INSTALLED_APPS entry
# crashes the whole site (ModuleNotFoundError during django.setup(),
# every gunicorn worker fails to boot, site down) if the package isn't
# actually present yet — this is exactly what happened Sept 9 (see
# CLAUDE.md incident writeup). Gated on whether the import actually
# succeeds, NOT on whether SENDGRID_API_KEY is set — those two facts
# can get out of sync (app setting configured before/without a
# build-enabled deploy actually installing the package), and only the
# import check is safe under that mismatch. Once a build-enabled deploy
# installs the package, this becomes true automatically — no further
# settings change needed.
try:
    import anymail  # noqa: F401
    INSTALLED_APPS.append("anymail")
    _ANYMAIL_AVAILABLE = True
except ImportError:
    _ANYMAIL_AVAILABLE = False

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # "axes.middleware.AxesMiddleware",  # disabled - inet port issue
]

AUTHENTICATION_BACKENDS = [
    # "axes.backends.AxesStandaloneBackend",  # disabled - inet port issue
    "social_core.backends.microsoft.MicrosoftOAuth2",
    "django.contrib.auth.backends.ModelBackend",
]

ROOT_URLCONF = "ghashem.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "dashboard" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.environment_banner",
                "social_django.context_processors.backends",
                "social_django.context_processors.login_redirect",
            ],
        },
    },
]

WSGI_APPLICATION = "ghashem.wsgi.application"

# Database 

if os.environ.get("DB_HOST"):
    DATABASES = {
        "default": {
            "ENGINE":   "django.db.backends.postgresql",
            "NAME":     os.environ.get("DB_NAME", "ghashem"),
            "USER":     os.environ.get("DB_USER"),
            "PASSWORD": os.environ.get("DB_PASSWORD"),
            "HOST":     os.environ.get("DB_HOST"),
            "PORT":     os.environ.get("DB_PORT", "5432"),
            "OPTIONS":  {"sslmode": "require"},
            "CONN_MAX_AGE": 300,
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {"timeout": 30},
        }
    }

# Password policy

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Login brute-force protection (django-axes)

AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1
AXES_LOCKOUT_PARAMETERS = ["username", "ip_address"]
AXES_RESET_ON_SUCCESS = True

#  Microsoft SSO (social-auth-app-django)
# These credentials belong to the ONE Ghashem app registration that handles
# login for every client â not a per-client secret. Stored as a single secret
# in the COMPANY Key Vault (separate from the per-client secrets in
# kv-ghashem-secrets), since this credential has nothing to do with any one
# client's Azure tenant and everything to do with Ghashem-the-application's
# own identity.
#
# Secret name in the company vault: ghashem-sso-app-secret
# Format stored: "{client_id}:{client_secret}" (colon-separated, single secret)
#
# Falls back to AZURE_APP_CLIENT_ID / AZURE_APP_CLIENT_SECRET env vars only
# for local development without Azure access â never used in production.

def _load_sso_credentials():
    """
    Loads the SSO app registration's client_id and client_secret from the
    company Key Vault. Returns (client_id, client_secret) â both empty
    strings if Key Vault is unreachable or slow to respond, in which case
    the env var fallback below is used instead.

    Uses an explicit short timeout on the Key Vault client itself. Without
    this, a slow or not-yet-propagated Managed Identity grant can cause
    DefaultAzureCredential or the Key Vault HTTP call to hang well past
    App Service's container startup timeout (230s) -- which kills the
    container entirely before Django even finishes importing settings.
    A few seconds of waiting and falling back to env vars is always
    preferable to hanging the whole app.
    """
    company_vault_url = os.environ.get("COMPANY_VAULT_URL", "")
    if not company_vault_url:
        return "", ""
    try:
        from azure.keyvault.secrets import SecretClient
        from azure.identity import DefaultAzureCredential
        from azure.core.pipeline.policies import RetryPolicy

        # connection_timeout/read_timeout bound the actual HTTP calls.
        # retry_total=1 avoids the SDK's default multi-retry backoff,
        # which can otherwise multiply a single slow call into a long stall.
        kv = SecretClient(
            vault_url=company_vault_url,
            credential=DefaultAzureCredential(),
            connection_timeout=5,
            read_timeout=5,
            retry_total=1,
        )
        raw = kv.get_secret("ghashem-sso-app-secret").value or ""
        if ":" not in raw:
            return "", ""
        client_id, client_secret = raw.split(":", 1)
        return client_id, client_secret
    except Exception as exc:
        # Key Vault unreachable, slow, or not yet authorized â fall back to
        # env vars rather than blocking startup. Logged so this is visible
        # in App Service logs rather than silently degrading forever.
        import logging
        logging.getLogger(__name__).warning(
            f"SSO credential load from Key Vault failed, using env var "
            f"fallback: {exc}"
        )
        return "", ""

_kv_client_id, _kv_client_secret = _load_sso_credentials()

SOCIAL_AUTH_MICROSOFT_GRAPH_KEY    = _kv_client_id    or os.environ.get("AZURE_APP_CLIENT_ID", "")
SOCIAL_AUTH_MICROSOFT_GRAPH_SECRET = _kv_client_secret or os.environ.get("AZURE_APP_CLIENT_SECRET", "")

SOCIAL_AUTH_MICROSOFT_GRAPH_SCOPE = [
    "User.Read",
    "GroupMember.Read.All",
]

# "common" accepts any Microsoft tenant â the pipeline step enforces
# the per-client tenant and group whitelist, so this is safe.
SOCIAL_AUTH_MICROSOFT_TENANT = os.environ.get("SOCIAL_AUTH_TENANT", "common")

SOCIAL_AUTH_LOGIN_REDIRECT_URL    = "/"
SOCIAL_AUTH_NEW_USER_REDIRECT_URL = "/"
SOCIAL_AUTH_BACKEND_ERROR_URL     = "/login/"

# Azure App Service terminates TLS at its load balancer and forwards
# requests to Django as plain http internally, so python-social-auth
# (which builds the OAuth2 redirect_uri from the incoming request) would
# otherwise register an http:// callback with Microsoft and get rejected
# with AADSTS50011 (redirect URI mismatch). Forces the built redirect URI
# to https regardless of what the request itself looks like internally.
# SECURE_PROXY_SSL_HEADER is here too (not down in the "Production
# security hardening" `if not DEBUG:` block, where it lived before) for
# the same reason: confirmed live that DJANGO_DEBUG defaults to "True"
# when left unset in Azure Portal app settings, which silently skipped
# that whole block — SECURE_PROXY_SSL_HEADER read back as None at
# runtime even against the production App Service. All three need to
# work regardless of DEBUG state.
SOCIAL_AUTH_REDIRECT_IS_HTTPS = True
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# State parameter stored in session â this is how social-auth provides
# CSRF protection on the OAuth callback instead of Django's CSRF token.
# The complete/ endpoint is csrf_exempt in urls.py for the same reason.
SOCIAL_AUTH_FIELDS_STORED_IN_SESSION = ["state"]

SOCIAL_AUTH_PIPELINE = (
    "social_core.pipeline.social_auth.social_details",
    "social_core.pipeline.social_auth.social_uid",
    "social_core.pipeline.social_auth.auth_allowed",
    "social_core.pipeline.social_auth.social_user",
    "social_core.pipeline.user.get_username",
    "social_core.pipeline.user.create_user",
    "social_core.pipeline.social_auth.associate_user",
    "social_core.pipeline.social_auth.load_extra_data",
    "social_core.pipeline.user.user_details",
    # Grants UserClientAccess (viewer) if user is in the client's AAD group
    "core.sso_pipeline.auto_grant_client_access",
)

# Production security hardening

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 60 * 60 * 8
SESSION_EXPIRE_AT_BROWSER_CLOSE = True

LANGUAGE_CODE = "en-us"
TIME_ZONE     = "America/Toronto"
USE_I18N      = True
USE_TZ        = True

STATIC_URL  = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Dashboard view cache (summary(), ai_governance_page()) — DatabaseCache
# needs no extra infrastructure (Redis, Memcached) and works on the
# existing PostgreSQL connection. Requires `python manage.py
# createcachetable` to have been run once; dashboard/views.py wraps its
# cache.get/set calls defensively so a missing/not-yet-created table
# falls back to running the normal queries instead of 500ing the page.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "ghashem_cache_table",
        "TIMEOUT": 300,  # 5 minutes
    }
}
# Logging

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}

# Ghashem-specific

DATA_DIR    = BASE_DIR / "data"
CONFIG_DIR  = BASE_DIR / "config"
LOGIN_URL   = "/login/"
LOGIN_REDIRECT_URL = "/"
ENVIRONMENT = os.environ.get("GHASHEM_ENVIRONMENT", "development")

# ── Email (SendGrid via django-anymail) ──────────────────────────────────
# Used in production when SENDGRID_API_KEY is set in Azure Portal app
# settings AND django-anymail is actually installed on the container
# (_ANYMAIL_AVAILABLE, set above — requires a build-enabled deploy).
# Falls back to the console backend otherwise, so local dev needs zero
# email config, and a missing/not-yet-installed package can never crash
# the site the way an unconditional INSTALLED_APPS entry did on Sept 9.
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")

ANYMAIL = {
    "SENDGRID_API_KEY": SENDGRID_API_KEY,
}

if SENDGRID_API_KEY and _ANYMAIL_AVAILABLE:
    EMAIL_BACKEND = "anymail.backends.sendgrid.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@ghashem.ca")
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# Signs and reply-to's for client-facing alert emails — not a per-client
# value, this is Ghashem's own outbound identity.
GHASHEM_CONTACT_NAME  = os.environ.get("GHASHEM_CONTACT_NAME", "The Ghashem Team")
GHASHEM_CONTACT_EMAIL = os.environ.get("GHASHEM_CONTACT_EMAIL", "support@ghashem.ca")
SITE_URL              = os.environ.get("SITE_URL", "http://localhost:8000")

# Client onboarding (dashboard/views.py: onboard_new, onboard_next_steps)
# The ONE multi-tenant app registration and its service principal — see
# CLAUDE.md "Two-tenant model". Already set in the Azure Portal app
# settings; defaults here match the values documented there for local dev.
AZURE_APP_CLIENT_ID  = os.environ.get("AZURE_APP_CLIENT_ID", "")
GHASHEM_SP_OBJECT_ID = os.environ.get("GHASHEM_SP_OBJECT_ID", "")

# Per-client secret storage (tenant_id, subscription_id, access_group_id)
# — same vault core/config.py's clients.yaml vault_url points at. Not
# read from clients.yaml here because onboarding writes secrets before
# a client necessarily has a clients.yaml entry at all.
VAULT_URL = os.environ.get("VAULT_URL", "https://kv-ghashem-dev.vault.azure.net/")

# Company vault — holds ghashem-sso-app-secret ("{client_id}:{client_secret}",
# see _load_sso_credentials() above). onboard_new (dashboard/views.py) reads
# this to copy the client_secret half into the new client's own vault entry
# (client-{slug}-secret), since every client authenticates against the SAME
# multitenant app registration/secret — see CLAUDE.md "Two-tenant model".
COMPANY_VAULT_URL = os.environ.get("COMPANY_VAULT_URL", "https://kv-ghashem-company.vault.azure.net/")
# django-axes: strip port from IP addresses before storing
# Azure App Service forwards IPs in "host:port" format which PostgreSQL
# inet type rejects -- this extracts just the IP part.
AXES_IPWARE_PROXY_COUNT = 1
AXES_IPWARE_META_PRECEDENCE_ORDER = [
    "HTTP_X_FORWARDED_FOR",
    "REMOTE_ADDR",
]
AXES_CLIENT_IP_CALLABLE = "core.utils.get_client_ip"
# django-axes: strip port from IP addresses
# Azure App Service forwards IPs as "host:port" format
AXES_IPWARE_PROXY_COUNT = 1
AXES_IPWARE_META_PRECEDENCE_ORDER = [
    "HTTP_X_FORWARDED_FOR",
    "REMOTE_ADDR",
]
AXES_CLIENT_IP_CALLABLE = "core.utils.get_client_ip"

# Fix django-axes IP format issue with Azure App Service
AXES_IPWARE_PROXY_COUNT = 1
AXES_CLIENT_IP_CALLABLE = "core.utils.get_client_ip"

# Disable IP-based tracking to fix Azure App Service port-in-IP issue
AXES_LOCKOUT_PARAMETERS = ["username"]
AXES_NEVER_LOCKOUT_WHITELIST = True
AXES_IP_WHITELIST = ["0.0.0.0/0"]

# Temporarily disable axes to fix inet port issue
AXES_ENABLED = False


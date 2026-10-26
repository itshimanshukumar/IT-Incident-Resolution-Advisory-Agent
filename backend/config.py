import os
from pathlib import Path
from dotenv import load_dotenv

# Base paths
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

class Settings:
    PROJECT_NAME: str = "ResolveIT - IT Incident Resolution Advisory Agent"
    VERSION: str = "1.0.0"
    BASE_DIR: Path = BASE_DIR
    
    # Environment & LLM Configuration
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "groq").lower()
    
    # Data Directories and Files
    # DATA_DIR can be overridden by environment (e.g. /tmp/resolveit on Vercel)
    DATA_DIR: Path = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data" / "ui_data")))
    OUTAGES_FILE: Path = DATA_DIR / "outages.csv"
    INCIDENTS_FILE: Path = DATA_DIR / "incidents.csv"
    TICKETS_FILE: Path = DATA_DIR / "tickets.csv"
    ESCALATION_RULES_FILE: Path = DATA_DIR / "escalation_rules.json"
    GUIDES_DIR: Path = DATA_DIR / "guides"
    GUIDE_INDEX_FILE: Path = GUIDES_DIR / "guide_index.json"
    GUIDE_INDEX_META_FILE: Path = GUIDES_DIR / "guide_index_meta.json"
    APPROVED_GUIDES_FILE: Path = GUIDES_DIR / "approved_guides.json"
    
    # Database
    # DB_PATH can be overridden (e.g. /tmp/resolveit/resolveit.db on Vercel)
    DATABASE_URL: str = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'resolveit.db'}")
    DB_PATH: Path = Path(os.getenv("DB_PATH", str(DATA_DIR / "resolveit.db")))
    
    # Business Logic Config
    FRESHNESS_HOURS: int = int(os.getenv("FRESHNESS_HOURS", "24"))
    MAX_REWRITE_ATTEMPTS: int = 2
    SERVER_PORT: int = int(os.getenv("PORT", "8000"))
    
    # Security & Authentication Config
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development").lower()
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "")
    JWT_ALGORITHM: str = "HS256"
    SESSION_EXPIRE_MINUTES: int = int(os.getenv("SESSION_EXPIRE_MINUTES", "480"))
    
    # CORS
    # VERCEL_URL is automatically set by Vercel at runtime (without https://)
    VERCEL_URL: str = os.getenv("VERCEL_URL", "")
    ALLOWED_ORIGINS: list = [
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:5501",
        "http://127.0.0.1:5501",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        # Vercel deployment URLs (set dynamically below after class definition)
    ]

settings = Settings()

# Dynamically add Vercel deployment URL to CORS allowed origins
# VERCEL_URL is set automatically by Vercel (e.g. "my-app-abc123.vercel.app")
if settings.VERCEL_URL:
    _vercel_https = f"https://{settings.VERCEL_URL}"
    _vercel_http  = f"http://{settings.VERCEL_URL}"
    if _vercel_https not in settings.ALLOWED_ORIGINS:
        settings.ALLOWED_ORIGINS.append(_vercel_https)
    if _vercel_http not in settings.ALLOWED_ORIGINS:
        settings.ALLOWED_ORIGINS.append(_vercel_http)

# Also allow any *.vercel.app domain for preview deployments
# by switching to allow_origins_regex in the middleware (handled in backend/main.py)

# Secure fallback for JWT_SECRET_KEY
if not settings.JWT_SECRET_KEY:
    import hashlib
    if settings.ENVIRONMENT == "production":
        # On Vercel: generate a stable deterministic secret if not set via env vars dashboard
        # IMPORTANT: Set JWT_SECRET_KEY in Vercel Environment Variables for real security
        settings.JWT_SECRET_KEY = hashlib.sha256(
            b"ResolveIT_Vercel_Prod_AutoGen_2026_PleaseSetJWT_SECRET_KEY"
        ).hexdigest()
    else:
        # Dev: ephemeral deterministic secret
        settings.JWT_SECRET_KEY = hashlib.sha256(b"ResolveIT_Dev_Session_Secret_2026_AutoGen").hexdigest()


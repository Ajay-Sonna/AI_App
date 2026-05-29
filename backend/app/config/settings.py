import os
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME")
_GROQ_READY = bool((GROQ_API_KEY or "").strip() and (MODEL_NAME or "").strip())


def _env_bool(name: str, *, default: bool) -> bool:
    """Parse env flag; when unset, use ``default``."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")

# --- DST / SQL Server (optional; consumed by ``app.dst_db.service``)
# MSSQL_ODBC_CONN=Driver={ODBC Driver 17 for SQL Server};Server=HOST;Database=DST;Trusted_Connection=yes;TrustServerCertificate=yes
# MSSQL_SERVER=HOST
# MSSQL_DATABASE=DST
# MSSQL_UID=...
# MSSQL_PWD=...
# MSSQL_ODBC_DRIVER=ODBC Driver 17 for SQL Server
# MSSQL_TRUST_SERVER_CERTIFICATE=true
#
# --- DST fee schedule raw table (all states in one dbo table; UI lists fs_name per state) ---
# DST_FEE_SCHEDULE_TABLE=dst_fee_schedule_raw
# DST_FS_NAME_COLUMN=fsname   # physical column (often fsname or fs_name)
#
# --- Fee schedule app DB (artifacts, URLs, mappings) — NOT the DST warehouse ---
# Create database FeeScheduleApp (or your name), run sql/fee_schedule_app_schema.sql
# MSSQL_APP_DATABASE=FeeScheduleApp
# Optional: MSSQL_APP_ODBC_CONN=Driver={...};Server=...;Database=FeeScheduleApp;...
# Local fee files (default on Windows: C:\FeeScheduleVault; else ~/FeeScheduleVault):
#   FEE_SCHEDULE_LOCAL_ROOT=C:\Users\you\OneDrive\FeeScheduleVault
# Legacy override (same as root if you prefer one env):
#   ARTIFACT_ROOT=D:\fee-artifacts
#
# --- Post-sync notification email (Gmail: use App Password, not login password) ---
# SMTP_HOST=smtp.gmail.com
# SMTP_PORT=587
# SMTP_USER=you@gmail.com
# SMTP_PASSWORD=your_google_app_password
# NOTIFICATION_FROM_EMAIL=you@gmail.com
# NOTIFICATION_FROM_NAME=Fee Schedule Team

# Groq-assisted sync passes (auto-on when GROQ_API_KEY + MODEL_NAME are set):
# - LLM_FEE_DOC_FILTER: when sync falls back to a file-link list (no table rows).
# - LLM_CATALOG_LINK_LABELS: when table rows need clearer per-link labels (multi-link rows).
# Set either to false to disable. Unset = enabled if Groq is configured.
LLM_FEE_DOC_FILTER_ENABLED = _env_bool("LLM_FEE_DOC_FILTER", default=_GROQ_READY)
LLM_CATALOG_LINK_LABELS_ENABLED = _env_bool("LLM_CATALOG_LINK_LABELS", default=_GROQ_READY)

# MMIS GA: rewrite ASP.NET Grid ``Select`` postback links → real HTTPS file URLs via Playwright.
_GA_MMIS_RES_RAW = os.getenv("GA_MMIS_POSTBACK_RESOLVE", "true")
GA_MMIS_POSTBACK_RESOLVE_ENABLED = str(_GA_MMIS_RES_RAW).lower() in (
    "1",
    "true",
    "yes",
    "on",
)
try:
    GA_MMIS_POSTBACK_RESOLVE_MAX = max(3, min(500, int(os.getenv("GA_MMIS_POSTBACK_RESOLVE_MAX", "80"))))
except ValueError:
    GA_MMIS_POSTBACK_RESOLVE_MAX = 80

try:
    # Bounded so /run does not stall for excessively long resolver passes (override locally if needed).
    GA_MMIS_POSTBACK_RESOLVE_WALL_S = float(os.getenv("GA_MMIS_POSTBACK_RESOLVE_WALL_S", "180"))
except ValueError:
    GA_MMIS_POSTBACK_RESOLVE_WALL_S = 180.0

# ``/run`` pagination: each qualifying table spins up Playwright; wall clock bounds each pass separately.
try:
    RUN_PAGINATION_WALL_SECONDS_DEFAULT = float(os.getenv("RUN_PAGINATION_WALL_SECONDS", "90"))
except ValueError:
    RUN_PAGINATION_WALL_SECONDS_DEFAULT = 90.0

# Sequential artifact downloads after /run — cap avoids multi-hour loops on large portals / dead links.
try:
    ARTIFACT_DOWNLOAD_MAX_PER_RUN = max(0, min(2000, int(os.getenv("ARTIFACT_DOWNLOAD_MAX_PER_RUN", "60"))))
except ValueError:
    ARTIFACT_DOWNLOAD_MAX_PER_RUN = 60
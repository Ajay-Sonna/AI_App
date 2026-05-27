import os
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_NAME = os.getenv("MODEL_NAME")

# --- DST / SQL Server (optional; consumed by ``app.dst_db.service``)
# MSSQL_ODBC_CONN=Driver={ODBC Driver 17 for SQL Server};Server=HOST;Database=DST;Trusted_Connection=yes;TrustServerCertificate=yes
# MSSQL_SERVER=HOST
# MSSQL_DATABASE=DST
# MSSQL_UID=...
# MSSQL_PWD=...
# MSSQL_ODBC_DRIVER=ODBC Driver 17 for SQL Server
# MSSQL_TRUST_SERVER_CERTIFICATE=true
#
# --- Fee schedule app DB (artifacts, URLs, mappings) — NOT the DST warehouse ---
# Create database FeeScheduleApp (or your name), run sql/fee_schedule_app_schema.sql
# MSSQL_APP_DATABASE=FeeScheduleApp
# Optional: MSSQL_APP_ODBC_CONN=Driver={...};Server=...;Database=FeeScheduleApp;...
# Local fee files (default on Windows: C:\FeeScheduleVault; else ~/FeeScheduleVault):
#   FEE_SCHEDULE_LOCAL_ROOT=C:\FeeScheduleVault
# Legacy override (same as root if you prefer one env):
#   ARTIFACT_ROOT=D:\fee-artifacts

# When true, file-repository rows are passed through an LLM relevance pass (Groq).
# Default off so /run stays deterministic and avoids unexpected token use; set LLM_FEE_DOC_FILTER=true to enable.
_LL_FILTER_RAW = os.getenv("LLM_FEE_DOC_FILTER", "false")
LLM_FEE_DOC_FILTER_ENABLED = str(_LL_FILTER_RAW).lower() in ("1", "true", "yes", "on")

# Optional LLM pass: rewrite per-link labels for messy multi-link rows (e.g. CA DWC). Default off — opt in with LLM_CATALOG_LINK_LABELS=true.
_LL_LINK_LABELS_RAW = os.getenv("LLM_CATALOG_LINK_LABELS", "false")
LLM_CATALOG_LINK_LABELS_ENABLED = str(_LL_LINK_LABELS_RAW).lower() in ("1", "true", "yes", "on")

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
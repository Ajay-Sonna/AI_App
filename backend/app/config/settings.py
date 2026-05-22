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

# When true (default), file-repository rows are passed through an LLM relevance pass.
# Set LLM_FEE_DOC_FILTER=false to skip (deterministic extract only).
_LL_FILTER_RAW = os.getenv("LLM_FEE_DOC_FILTER", "true")
LLM_FEE_DOC_FILTER_ENABLED = str(_LL_FILTER_RAW).lower() in ("1", "true", "yes", "on")
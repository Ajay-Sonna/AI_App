"""Connect to the fee-schedule *app* database (not DST)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def app_db_configured() -> bool:
    raw = (os.getenv("MSSQL_APP_ODBC_CONN") or "").strip()
    if raw:
        return True
    return bool((os.getenv("MSSQL_SERVER") or "").strip()) and bool(
        (os.getenv("MSSQL_APP_DATABASE") or "").strip(),
    )


def app_db_connect():
    """pyodbc connection to MSSQL_APP_DATABASE (or MSSQL_APP_ODBC_CONN)."""
    import pyodbc

    raw = (os.getenv("MSSQL_APP_ODBC_CONN") or "").strip()
    if raw:
        return pyodbc.connect(raw, timeout=30)

    server = (os.getenv("MSSQL_SERVER") or "").strip()
    database = (os.getenv("MSSQL_APP_DATABASE") or "").strip()
    if not server or not database:
        raise RuntimeError(
            "App database not configured: set MSSQL_APP_ODBC_CONN or "
            "MSSQL_SERVER + MSSQL_APP_DATABASE (e.g. FeeScheduleApp).",
        )

    driver = (os.getenv("MSSQL_ODBC_DRIVER") or "ODBC Driver 17 for SQL Server").strip()
    uid = (os.getenv("MSSQL_UID") or os.getenv("MSSQL_USER") or "").strip()
    pwd = (os.getenv("MSSQL_PWD") or os.getenv("MSSQL_PASSWORD") or "").strip()
    trust_raw = (os.getenv("MSSQL_TRUST_SERVER_CERTIFICATE") or "true").lower()
    trust_cert = trust_raw in ("1", "true", "yes", "on")

    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={server}",
        f"DATABASE={database}",
    ]
    if uid:
        parts.append(f"UID={uid}")
        parts.append(f"PWD={pwd}")
    else:
        parts.append("Trusted_Connection=yes")

    if driver.strip("{}").upper().startswith("ODBC DRIVER 18"):
        parts.append(
            f"Encrypt={'yes' if trust_cert else 'optional'};TrustServerCertificate={'yes' if trust_cert else 'no'}"
        )
    elif trust_cert:
        parts.append("TrustServerCertificate=yes")

    return pyodbc.connect(";".join(parts), timeout=30)

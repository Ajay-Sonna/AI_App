"""
Read fee-schedule rows from the on-prem **DST** SQL Server database.

- Lists `dbo` tables in `MSSQL_DATABASE` (default `DST`).
- Detects JSON blob columns **by cell content** (not fixed names): values that parse as JSON
  objects expand into columns (FAC, NFC, CODE, MOD, …); ``FROM`` nesting is unwrapped.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_DROPPED_SQL_COLUMNS = frozenset(
    {"dst_row_id", "row_id", "state_code", "inserted_at", "fs_name"},
)

_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_FS_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,256}$")

_MAX_ROWS_CAP = 10_000
_DEFAULT_ROW_LIMIT = 2_000
_MIN_JSON_CELL_RATIO = 0.5


def _dst_configured() -> bool:
    return bool(os.getenv("MSSQL_ODBC_CONN", "").strip()) or bool(
        os.getenv("MSSQL_SERVER", "").strip(),
    )


def _connect():
    import pyodbc

    raw = (os.getenv("MSSQL_ODBC_CONN") or "").strip()
    if raw:
        return pyodbc.connect(raw, timeout=30)

    server = (os.getenv("MSSQL_SERVER") or "").strip()
    if not server:
        raise RuntimeError(
            "Database not configured: set MSSQL_ODBC_CONN or MSSQL_SERVER in the environment.",
        )

    database = (os.getenv("MSSQL_DATABASE") or "DST").strip()
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


def _json_serializable(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (str, int, bool)):
        return v
    if isinstance(v, float):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8", errors="replace")
        except Exception:
            return str(v)
    return str(v)


def _unwrap_nested_from(v: Any) -> Any:
    cur: Any = v
    while isinstance(cur, dict):
        if len(cur) == 1 and "FROM" in cur:
            cur = cur.get("FROM")
            continue
        if "FROM" in cur:
            cur = cur.get("FROM")
            continue
        break
    return cur


def _flatten_fee_json_object(obj: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, raw in obj.items():
        if not isinstance(k, str):
            k = str(k)
        unwrapped = _unwrap_nested_from(raw)
        out[k] = _json_serializable(unwrapped)
    return out


def _try_parse_json_value(s: str) -> Optional[Any]:
    """
    Parse JSON from a cell. Supports:
    - Normal JSON (table2-style: ``{"FAC": {"FROM": "36.98"}, ...}``).
    - T-SQL / SSMS literal style where every ``"`` is doubled to ``""`` inside stored text.
    - Extra text around the object (first ``{...}`` win via raw_decode).
    """
    s = s.strip().lstrip("\ufeff")
    if not s:
        return None

    variants: List[str] = [s]
    if '""' in s:
        variants.append(s.replace('""', '"'))

    for cand in variants:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()

    for cand in variants:
        start = 0
        while start < len(cand):
            brace = cand.find("{", start)
            if brace < 0:
                break
            try:
                obj, _end = decoder.raw_decode(cand, brace)
                if isinstance(obj, (dict, list)):
                    return obj
            except json.JSONDecodeError:
                pass
            start = brace + 1

    return None


def _parse_json_to_dict_maybe_double_encoded(raw: Any) -> Optional[Dict[str, Any]]:
    """
    Parse cell to dict. Handles dict from driver, UTF-8 BOM, doubled-quote SQL literals,
    double-encoded JSON strings, and leading/trailing junk around the object.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw

    if isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            raw = bytes(raw).decode("utf-8", errors="replace")
        except Exception:
            return None

    if not isinstance(raw, str):
        raw = str(raw)

    cur: Any = raw.strip().lstrip("\ufeff")
    for _ in range(4):
        if isinstance(cur, dict):
            return cur
        if not isinstance(cur, str):
            return None
        s = cur.strip().lstrip("\ufeff")
        if not s:
            return None

        parsed = _try_parse_json_value(s)
        if parsed is None:
            return None
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, str):
            cur = parsed
            continue
        return None
    return None


def _dict_has_nested_object_values(d: dict) -> bool:
    return any(isinstance(v, dict) for v in d.values())


def _column_should_expand_as_json(
    col: str,
    row_dicts: List[Dict[str, Any]],
) -> bool:
    nonempty: List[Any] = []
    for rd in row_dicts:
        v = rd.get(col)
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        nonempty.append(v)

    if not nonempty:
        return False

    nested_hit = 0
    dict_parse_hits = 0
    for v in nonempty:
        if isinstance(v, dict):
            d = v
        else:
            d = _parse_json_to_dict_maybe_double_encoded(v)
            if d is None:
                continue
        dict_parse_hits += 1
        if _dict_has_nested_object_values(d):
            nested_hit += 1

    if nested_hit >= 1:
        return True

    ratio = dict_parse_hits / len(nonempty)
    return dict_parse_hits >= 1 and ratio >= _MIN_JSON_CELL_RATIO


def _flatten_json_cell(raw: Any) -> Dict[str, Any]:
    d = raw if isinstance(raw, dict) else _parse_json_to_dict_maybe_double_encoded(raw)
    if not isinstance(d, dict):
        return {}
    return _flatten_fee_json_object(d)


def validate_table_name(name: str) -> str:
    n = (name or "").strip()
    if not _TABLE_NAME_RE.fullmatch(n):
        raise ValueError("Invalid table name")
    return n


def validate_fs_name(name: str) -> str:
    """Logical fee schedule id stored in ``fs_name`` (not a SQL table name)."""
    n = (name or "").strip()
    if not _FS_NAME_RE.fullmatch(n):
        raise ValueError("Invalid fee schedule name (fs_name)")
    return n


def get_fee_schedule_table() -> str:
    """Configured warehouse table holding all states' fee schedule rows."""
    raw = (os.getenv("DST_FEE_SCHEDULE_TABLE") or "dst_fee_schedule_raw").strip()
    return validate_table_name(raw)


def _configured_fs_name_column_logical() -> str:
    return (os.getenv("DST_FS_NAME_COLUMN") or "fs_name").strip().lower() or "fs_name"


_STATE_CODE_COLUMN_Q = """
SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = N'dbo' AND TABLE_NAME = ? AND LOWER(COLUMN_NAME) = N'state_code'
"""

_FS_NAME_COLUMN_Q = """
SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = N'dbo' AND TABLE_NAME = ? AND LOWER(COLUMN_NAME) = ?
"""


def _resolve_state_code_column_using(cur: Any, validated_table: str) -> Optional[str]:
    """``state_code`` column physical name; ``validated_table`` = ``validate_table_name`` result."""
    cur.execute(_STATE_CODE_COLUMN_Q, (validated_table,))
    row = cur.fetchone()
    return str(row[0]) if row else None


def _resolve_state_code_column(table: str) -> Optional[str]:
    """Physical ``state_code`` column name on ``dbo.[table]``, or None."""
    t = validate_table_name(table)
    with _connect() as cx:
        cur = cx.cursor()
        return _resolve_state_code_column_using(cur, t)


def _resolve_fs_name_column_using(cur: Any, validated_table: str) -> Optional[str]:
    logical = _configured_fs_name_column_logical()
    cur.execute(_FS_NAME_COLUMN_Q, (validated_table, logical))
    row = cur.fetchone()
    return str(row[0]) if row else None


def _resolve_fs_name_column(table: str) -> Optional[str]:
    t = validate_table_name(table)
    with _connect() as cx:
        cur = cx.cursor()
        return _resolve_fs_name_column_using(cur, t)


def list_dst_fee_schedules(*, state_code: str) -> List[str]:
    """
    Distinct ``fs_name`` values for a state in the configured fee schedule table
    (``DST_FEE_SCHEDULE_TABLE``, default ``dst_fee_schedule_raw``).
    """
    t = get_fee_schedule_table()
    sc = (state_code or "").strip().upper()[:8]
    if not sc:
        return []
    with _connect() as cx:
        cur = cx.cursor()
        sc_col = _resolve_state_code_column_using(cur, t)
        fs_col = _resolve_fs_name_column_using(cur, t)
        if not sc_col or not fs_col:
            fs_logical = _configured_fs_name_column_logical()
            missing: List[str] = []
            if not sc_col:
                missing.append("state_code")
            if not fs_col:
                missing.append(fs_logical)
            hint = ""
            if not fs_col and fs_logical == "fs_name":
                hint = " If your column is named fsname, set DST_FS_NAME_COLUMN=fsname."
            raise ValueError(
                f"Fee schedule table dbo.{t} is missing column(s): {', '.join(missing)}.{hint}",
            )
        bracket_sc = f"[{sc_col.replace(']', ']]')}]"
        bracket_fs = f"[{fs_col.replace(']', ']]')}]"
        sql = f"""
            SELECT DISTINCT {bracket_fs} AS _fs
            FROM [dbo].[{t}]
            WHERE {bracket_sc} = ?
              AND {bracket_fs} IS NOT NULL
              AND LTRIM(RTRIM(CAST({bracket_fs} AS NVARCHAR(512)))) <> N''
            ORDER BY {bracket_fs}
        """
        cur.execute(sql, (sc,))
        rows = cur.fetchall()
    out: List[str] = []
    for r in rows:
        if r and r[0] is not None:
            s = str(r[0]).strip()
            if s:
                out.append(s)
    return out


def _tables_with_state_code_column() -> List[str]:
    """dbo tables that expose a ``state_code`` column (name from INFORMATION_SCHEMA)."""
    q = """
    SELECT t.TABLE_NAME
    FROM INFORMATION_SCHEMA.TABLES t
    WHERE t.TABLE_TYPE = N'BASE TABLE'
      AND t.TABLE_SCHEMA = N'dbo'
      AND EXISTS (
          SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS c
          WHERE c.TABLE_SCHEMA = t.TABLE_SCHEMA
            AND c.TABLE_NAME = t.TABLE_NAME
            AND LOWER(c.COLUMN_NAME) = N'state_code'
      )
    ORDER BY t.TABLE_NAME
    """
    with _connect() as cx:
        cur = cx.cursor()
        cur.execute(q)
        rows = cur.fetchall()
    return [str(r[0]) for r in rows]


def _table_has_row_for_state_using(cur: Any, validated_table: str, state_upper: str) -> bool:
    """True iff ``dbo.[validated_table]`` has ``state_code`` and at least one matching row."""
    col = _resolve_state_code_column_using(cur, validated_table)
    if not col:
        return False
    bracket_col = f"[{col.replace(']', ']]')}]"
    sql = f"SELECT TOP (1) 1 AS _hit FROM [dbo].[{validated_table}] WHERE {bracket_col} = ?"
    cur.execute(sql, (state_upper,))
    return cur.fetchone() is not None


def list_dst_tables(*, state_filter: Optional[str] = None) -> List[str]:
    """
    Lists ``dbo`` base tables:

    - **No ``state_filter``:** every dbo BASE TABLE (admin / exploratory).
    - **With ``state_filter`` (e.g. NY):** only dbo tables that have a ``state_code``
      column **and** at least one row where ``state_code`` matches ``state_filter``
      (case-normalized USPS-style code).

    Rows are verified with ``SELECT TOP (1) …`` per candidate table — cheap when
    ``state_code`` is indexed.
    """
    sc = (state_filter or "").strip().upper()[:8] or None
    if not sc:
        q = """
        SELECT TABLE_SCHEMA, TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE = 'BASE TABLE'
          AND TABLE_SCHEMA = N'dbo'
        ORDER BY TABLE_NAME
        """
        with _connect() as cx:
            cur = cx.cursor()
            cur.execute(q)
            rows = cur.fetchall()
        return [str(r[1]) for r in rows]

    candidates = _tables_with_state_code_column()
    out: List[str] = []
    with _connect() as cx:
        cur = cx.cursor()
        for t_name in candidates:
            try:
                vt = validate_table_name(t_name)
            except ValueError:
                continue
            try:
                if _table_has_row_for_state_using(cur, vt, sc):
                    out.append(vt)
            except Exception as exc:
                logger.warning("DST probe for state rows failed on dbo.%s: %s", vt, exc)
    return out


def fetch_dst_table_rows(
    table: str,
    *,
    limit: int = _DEFAULT_ROW_LIMIT,
    state_code: Optional[str] = None,
    fs_name: Optional[str] = None,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    t = validate_table_name(table)
    lim = max(1, min(int(limit), _MAX_ROWS_CAP))

    quoted = f"[dbo].[{t}]"
    sc_val = (state_code or "").strip().upper()[:8] or None
    fs_val = validate_fs_name(fs_name) if (fs_name or "").strip() else None

    sc_col = _resolve_state_code_column(t) if sc_val else None
    fs_col = _resolve_fs_name_column(t) if fs_val else None

    where_parts: List[str] = []
    params: List[Any] = []
    if sc_col and sc_val:
        bracket_col = f"[{sc_col.replace(']', ']]')}]"
        where_parts.append(f"{bracket_col} = ?")
        params.append(sc_val)
    if fs_col and fs_val:
        bracket_fs = f"[{fs_col.replace(']', ']]')}]"
        where_parts.append(f"{bracket_fs} = ?")
        params.append(fs_val)

    if where_parts:
        sql = f"SELECT TOP ({lim}) * FROM {quoted} WHERE " + " AND ".join(where_parts)
    else:
        sql = f"SELECT TOP ({lim}) * FROM {quoted}"
        params = []

    with _connect() as cx:
        cur = cx.cursor()
        cur.execute(sql, params)
        col_names = [d[0] for d in cur.description]
        tuples = cur.fetchall()

    row_dicts: List[Dict[str, Any]] = []
    for tup in tuples:
        row_dicts.append(dict(zip(col_names, tup)))

    sql_base_order = [c for c in col_names if c.lower() not in _DROPPED_SQL_COLUMNS]

    expandable: Set[str] = set()
    for c in sql_base_order:
        if _column_should_expand_as_json(c, row_dicts):
            expandable.add(c)

    expandable_ordered = [c for c in sql_base_order if c in expandable]

    json_key_union: Set[str] = set()
    out_rows: List[Dict[str, Any]] = []

    for rd in row_dicts:
        flat: Dict[str, Any] = {}
        for sql_col in sql_base_order:
            if sql_col in expandable:
                continue
            flat[sql_col] = _json_serializable(rd.get(sql_col))

        merged = dict(flat)
        for sql_col in expandable_ordered:
            jflat = _flatten_json_cell(rd.get(sql_col))
            for jk, jv in jflat.items():
                json_key_union.add(jk)
                merged[jk] = jv

        out_rows.append(merged)

    base_cols_kept = [c for c in sql_base_order if c not in expandable]
    json_cols_sorted = sorted(json_key_union, key=lambda x: x)
    columns_order = base_cols_kept + json_cols_sorted

    finalized: List[Dict[str, Any]] = []
    for r in out_rows:
        finalized.append({c: r.get(c) for c in columns_order})
    return columns_order, finalized

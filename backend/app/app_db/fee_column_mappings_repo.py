"""Persist fee-schedule column mapping rows in FeeScheduleApp (companion DB)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.app_db.connection import app_db_connect


def _norm_state_code(state_code: Optional[str]) -> str:
    if not state_code or not str(state_code).strip():
        return ""
    return str(state_code).strip().upper()[:8]


def _strip_dst_fsname(dst_fsname: Optional[str]) -> str:
    return (dst_fsname or "").strip()[:256]


def resolve_schedule_key_for_artifact(artifact: Dict[str, Any]) -> str:
    """Stable key for fee_schedule_column_mapping.state_logical_schedule_key."""
    lsk = (artifact.get("logical_schedule_key") or "").strip()
    if lsk:
        return lsk[:256]
    aid = artifact.get("artifact_id")
    if aid is None:
        raise ValueError("artifact_id required")
    return f"artifact:{int(aid)}"


def lookup_latest_mapping(
    *,
    state_code: str,
    state_logical_schedule_key: str,
    dst_fsname: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return mapping row.

    With ``dst_fsname`` — exact triple (state_code, logical key, DST table).
    Without — most recently updated mapping for this state + logical key (any DST).
    """
    sc = _norm_state_code(state_code)
    sk = (state_logical_schedule_key or "").strip()[:256]
    dn = _strip_dst_fsname(dst_fsname)
    if not sc or not sk:
        return None
    with app_db_connect() as cx:
        cur = cx.cursor()
        if dn:
            cur.execute(
                """
                SELECT TOP (1)
                    mapping_id, state_code, state_logical_schedule_key, dst_fsname,
                    column_map_json, created_at_utc, updated_at_utc, updated_by
                FROM dbo.fee_schedule_column_mapping
                WHERE state_code = ? AND state_logical_schedule_key = ? AND dst_fsname = ?
                ORDER BY COALESCE(updated_at_utc, created_at_utc) DESC
                """,
                (sc, sk, dn),
            )
        else:
            cur.execute(
                """
                SELECT TOP (1)
                    mapping_id, state_code, state_logical_schedule_key, dst_fsname,
                    column_map_json, created_at_utc, updated_at_utc, updated_by
                FROM dbo.fee_schedule_column_mapping
                WHERE state_code = ? AND state_logical_schedule_key = ?
                ORDER BY COALESCE(updated_at_utc, created_at_utc) DESC
                """,
                (sc, sk),
            )
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))


def list_mappings_for_state(
    state_code: str,
    *,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """All mapping rows for a state, newest activity first."""
    sc = _norm_state_code(state_code)
    if not sc:
        return []
    lim = max(1, min(int(limit), 2000))
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            f"""
            SELECT TOP ({lim})
                mapping_id, state_code, state_logical_schedule_key, dst_fsname,
                column_map_json, created_at_utc, updated_at_utc, updated_by
            FROM dbo.fee_schedule_column_mapping
            WHERE state_code = ?
            ORDER BY COALESCE(updated_at_utc, created_at_utc) DESC
            """,
            (sc,),
        )
        colnames = [c[0] for c in cur.description]
        rows = cur.fetchall()
    return [dict(zip(colnames, r)) for r in rows]


def get_mapping_by_id_for_state(*, mapping_id: int, state_code: str) -> Optional[Dict[str, Any]]:
    """Return one row iff it belongs to the given state."""
    sc = _norm_state_code(state_code)
    if not sc:
        return None
    mid = int(mapping_id)
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            """
            SELECT
                mapping_id, state_code, state_logical_schedule_key, dst_fsname,
                column_map_json, created_at_utc, updated_at_utc, updated_by
            FROM dbo.fee_schedule_column_mapping
            WHERE mapping_id = ? AND state_code = ?
            """,
            (mid, sc),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))


def delete_mapping_by_id(*, mapping_id: int, state_code: str) -> bool:
    """Delete one row iff it belongs to the given state. Returns True when a row was removed."""
    sc = _norm_state_code(state_code)
    if not sc:
        return False
    mid = int(mapping_id)
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            "DELETE FROM dbo.fee_schedule_column_mapping WHERE mapping_id = ? AND state_code = ?",
            (mid, sc),
        )
        cnt = getattr(cur, "rowcount", 0) or 0
        cx.commit()
    return cnt > 0


def upsert_fee_column_mapping(
    *,
    state_code: str,
    state_logical_schedule_key: str,
    dst_fsname: str,
    column_map_json: Any,
    updated_by: Optional[str] = None,
) -> Dict[str, Any]:
    sc = _norm_state_code(state_code)
    sk = (state_logical_schedule_key or "").strip()[:256]
    dn = (dst_fsname or "").strip()[:256]
    ub = ((updated_by or "").strip()[:128] or None)

    cm = "{}"
    if column_map_json is None:
        cm = "{}"
    elif isinstance(column_map_json, str):
        raw = column_map_json.strip() or "{}"
        json.loads(raw)
        cm = raw
    else:
        cm = json.dumps(column_map_json, ensure_ascii=False)

    if not sc or not sk or not dn:
        raise ValueError("state_code, state_logical_schedule_key, and dst_fsname are required")

    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            """
            SELECT mapping_id FROM dbo.fee_schedule_column_mapping
            WHERE state_code = ? AND state_logical_schedule_key = ? AND dst_fsname = ?
            """,
            (sc, sk, dn),
        )
        exist = cur.fetchone()
        if exist:
            cur.execute(
                """
                UPDATE dbo.fee_schedule_column_mapping
                SET column_map_json = ?,
                    updated_at_utc = SYSUTCDATETIME(),
                    updated_by = ?
                WHERE mapping_id = ?
                """,
                (cm, ub, exist[0]),
            )
            mid = int(exist[0])
        else:
            cur.execute(
                """
                INSERT INTO dbo.fee_schedule_column_mapping (
                    state_code, state_logical_schedule_key, dst_fsname,
                    column_map_json, updated_at_utc, updated_by
                )
                OUTPUT INSERTED.mapping_id
                VALUES (?, ?, ?, ?, SYSUTCDATETIME(), ?)
                """,
                (sc, sk, dn, cm, ub),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("INSERT fee_schedule_column_mapping returned no mapping_id")
            mid = int(row[0])
        cx.commit()

    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            """
            SELECT mapping_id, state_code, state_logical_schedule_key, dst_fsname,
                   column_map_json, created_at_utc, updated_at_utc, updated_by
            FROM dbo.fee_schedule_column_mapping
            WHERE mapping_id = ?
            """,
            (mid,),
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"fee_schedule_column_mapping row missing after upsert id={mid}")
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))

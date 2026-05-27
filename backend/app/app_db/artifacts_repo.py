"""Persist downloaded fee-schedule file metadata in the app database."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from app.app_db.connection import app_db_connect

logger = logging.getLogger(__name__)

_ARTIFACT_SELECT = """
            artifact_id, state_code, logical_schedule_key, source_url,
            content_sha256, stored_rel_path, original_filename, mime_type, bytes_size,
            fetched_at_utc, is_current, source_label,
            remote_etag, remote_last_modified_utc,
            portal_effective_date, effective_date_source, is_superseded_hint
"""


def _norm_state_code(state_code: Optional[str]) -> Optional[str]:
    if not state_code or not str(state_code).strip():
        return None
    return str(state_code).strip().upper()[:8]


def _to_date_key(val: Any) -> date:
    """Comparable calendar date for versioning (portal date or fetch day)."""
    if val is None:
        return date.min
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    return date.min


def _row_effective_for_sort(row: Dict[str, Any]) -> date:
    ped = row.get("portal_effective_date")
    if ped is not None and ped is not False:
        dk = _to_date_key(ped)
        if dk != date.min:
            return dk
    return _to_date_key(row.get("fetched_at_utc"))


def supersede_all_current_for_logical_key(*, state_code: Optional[str], logical_schedule_key: str) -> None:
    """Legacy helper: mark every current row for this state + logical schedule folder as not current."""
    sc = _norm_state_code(state_code)
    lsk = (logical_schedule_key or "").strip()[:256]
    if not lsk:
        return
    sc_key = sc if sc else ""
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            """
            UPDATE dbo.fee_schedule_artifact
            SET is_current = 0
            WHERE is_current = 1
              AND ISNULL(state_code, N'') = ?
              AND logical_schedule_key = ?
            """,
            (sc_key, lsk),
        )
        cx.commit()


def _is_superseded_row(row: Dict[str, Any]) -> bool:
    v = row.get("is_superseded_hint")
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "yes")


def recompute_is_current_for_logical_key(*, state_code: Optional[str], logical_schedule_key: str) -> None:
    """
    Single "current" row per (state_code, logical_schedule_key):
    prefer non-superseded; highest portal_effective_date (else fetch date); tie-break artifact_id.
    """
    sc = _norm_state_code(state_code)
    lsk = (logical_schedule_key or "").strip()[:256]
    if not lsk:
        return
    sc_key = sc if sc else ""
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            f"""
            SELECT {_ARTIFACT_SELECT.strip()}
            FROM dbo.fee_schedule_artifact
            WHERE ISNULL(state_code, N'') = ?
              AND logical_schedule_key = ?
            """,
            (sc_key, lsk),
        )
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not rows:
        return
    non_sup = [r for r in rows if not _is_superseded_row(r)]
    pool = non_sup if non_sup else rows

    def sort_key(r: Dict[str, Any]) -> Tuple[date, int]:
        return (_row_effective_for_sort(r), int(r.get("artifact_id") or 0))

    winner_id = int(max(pool, key=sort_key)["artifact_id"])

    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            """
            UPDATE dbo.fee_schedule_artifact
            SET is_current = 0
            WHERE ISNULL(state_code, N'') = ?
              AND logical_schedule_key = ?
            """,
            (sc_key, lsk),
        )
        cur.execute(
            """
            UPDATE dbo.fee_schedule_artifact
            SET is_current = 1
            WHERE artifact_id = ?
            """,
            (winner_id,),
        )
        cx.commit()


def recompute_is_current_for_state(*, state_code: Optional[str]) -> None:
    """Recompute current flags for every logical_schedule_key in this state."""
    sc = _norm_state_code(state_code)
    if not sc:
        return
    keys: List[str] = []
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            """
            SELECT DISTINCT logical_schedule_key
            FROM dbo.fee_schedule_artifact
            WHERE ISNULL(state_code, N'') = ?
              AND logical_schedule_key IS NOT NULL
              AND LTRIM(RTRIM(logical_schedule_key)) <> N''
            """,
            (sc,),
        )
        for (lsk,) in cur.fetchall():
            if lsk and str(lsk).strip():
                keys.append(str(lsk).strip()[:256])
    for lsk in keys:
        try:
            recompute_is_current_for_logical_key(state_code=sc, logical_schedule_key=lsk)
        except Exception as ex:
            logger.warning("recompute failed for %s / %s: %s", sc, lsk, ex)


def get_artifact_by_state_lsk_content_sha256(
    *,
    state_code: Optional[str],
    logical_schedule_key: str,
    content_sha256: str,
) -> Optional[Dict[str, Any]]:
    """Return newest row with same fingerprint for this folder (including historical).

    Used to skip inserting another DB row when a re-fetch returns byte-identical content.
    """
    sc = _norm_state_code(state_code)
    lsk = (logical_schedule_key or "").strip()[:256]
    dg = (content_sha256 or "").strip()[:64]
    if not lsk or not dg:
        return None
    sc_key = sc if sc else ""
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            f"""
            SELECT TOP (1) {_ARTIFACT_SELECT.strip()}
            FROM dbo.fee_schedule_artifact
            WHERE ISNULL(state_code, N'') = ?
              AND logical_schedule_key = ?
              AND content_sha256 = ?
            ORDER BY artifact_id DESC
            """,
            (sc_key, lsk, dg),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))


def get_current_artifact_for_logical_key(
    *,
    state_code: Optional[str],
    logical_schedule_key: str,
) -> Optional[Dict[str, Any]]:
    sc = _norm_state_code(state_code)
    lsk = (logical_schedule_key or "").strip()[:256]
    if not lsk:
        return None
    sc_key = sc if sc else ""
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            f"""
            SELECT TOP (1) {_ARTIFACT_SELECT.strip()}
            FROM dbo.fee_schedule_artifact
            WHERE is_current = 1
              AND ISNULL(state_code, N'') = ?
              AND logical_schedule_key = ?
            ORDER BY portal_effective_date DESC, fetched_at_utc DESC, artifact_id DESC
            """,
            (sc_key, lsk),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))


def insert_artifact_row(
    *,
    state_code: Optional[str],
    logical_schedule_key: Optional[str],
    source_url: str,
    content_sha256: str,
    stored_rel_path: str,
    original_filename: Optional[str],
    mime_type: Optional[str],
    bytes_size: int,
    source_label: Optional[str],
    remote_etag: Optional[str] = None,
    remote_last_modified_utc: Optional[Any] = None,
    portal_effective_date: Optional[Any] = None,
    effective_date_source: Optional[str] = None,
    is_superseded_hint: bool = False,
) -> int:
    sc = _norm_state_code(state_code)
    lsk = (logical_schedule_key or "").strip()[:256] or None
    etag = (remote_etag or "").strip()[:256] or None
    lm = remote_last_modified_utc
    src = (effective_date_source or "").strip()[:32] or None
    sup = 1 if is_superseded_hint else 0
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            """
            INSERT INTO dbo.fee_schedule_artifact (
                state_code, logical_schedule_key, source_url, content_sha256,
                stored_rel_path, original_filename, mime_type, bytes_size,
                is_current, source_label, remote_etag, remote_last_modified_utc,
                portal_effective_date, effective_date_source, is_superseded_hint
            )
            OUTPUT INSERTED.artifact_id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                sc,
                lsk,
                source_url.strip()[:2048],
                content_sha256[:64],
                stored_rel_path[:1024],
                (original_filename or None) and original_filename[:512] or None,
                (mime_type or None) and mime_type[:256] or None,
                int(bytes_size),
                (source_label or None) and source_label[:512] or None,
                etag,
                lm,
                portal_effective_date,
                src,
                sup,
            ),
        )
        row = cur.fetchone()
        cx.commit()
    if not row:
        raise RuntimeError("INSERT artifact did not return artifact_id")
    return int(row[0])


def list_artifacts(
    *,
    state_code: Optional[str] = None,
    current_only: bool = True,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    lim = max(1, min(int(limit), 5000))
    sc = _norm_state_code(state_code)
    with app_db_connect() as cx:
        cur = cx.cursor()
        if sc:
            if current_only:
                cur.execute(
                    f"""
                    SELECT TOP (?) {_ARTIFACT_SELECT.strip()}
                    FROM dbo.fee_schedule_artifact
                    WHERE ISNULL(state_code, N'') = ? AND is_current = 1
                    ORDER BY logical_schedule_key ASC, portal_effective_date DESC, fetched_at_utc DESC
                    """,
                    (lim, sc),
                )
            else:
                cur.execute(
                    f"""
                    SELECT TOP (?) {_ARTIFACT_SELECT.strip()}
                    FROM dbo.fee_schedule_artifact
                    WHERE ISNULL(state_code, N'') = ?
                    ORDER BY logical_schedule_key ASC,
                             CASE WHEN portal_effective_date IS NULL THEN 1 ELSE 0 END,
                             portal_effective_date DESC,
                             fetched_at_utc DESC,
                             artifact_id DESC
                    """,
                    (lim, sc),
                )
        else:
            if current_only:
                cur.execute(
                    f"""
                    SELECT TOP (?) {_ARTIFACT_SELECT.strip()}
                    FROM dbo.fee_schedule_artifact
                    WHERE is_current = 1
                    ORDER BY state_code ASC, logical_schedule_key ASC, portal_effective_date DESC, fetched_at_utc DESC
                    """,
                    (lim,),
                )
            else:
                cur.execute(
                    f"""
                    SELECT TOP (?) {_ARTIFACT_SELECT.strip()}
                    FROM dbo.fee_schedule_artifact
                    ORDER BY state_code ASC, logical_schedule_key ASC,
                             CASE WHEN portal_effective_date IS NULL THEN 1 ELSE 0 END,
                             portal_effective_date DESC,
                             fetched_at_utc DESC,
                             artifact_id DESC
                    """,
                    (lim,),
                )
        cols = [c[0] for c in cur.description]
        out: List[Dict[str, Any]] = []
        for r in cur.fetchall():
            out.append(dict(zip(cols, r)))
    return out


def get_artifact_by_id(artifact_id: int) -> Optional[Dict[str, Any]]:
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            f"""
            SELECT {_ARTIFACT_SELECT.strip()}
            FROM dbo.fee_schedule_artifact
            WHERE artifact_id = ?
            """,
            (int(artifact_id),),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))


def delete_artifact_row(artifact_id: int) -> None:
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            "DELETE FROM dbo.fee_schedule_artifact WHERE artifact_id = ?",
            (int(artifact_id),),
        )
        cx.commit()

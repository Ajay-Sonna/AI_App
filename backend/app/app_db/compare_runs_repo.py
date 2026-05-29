"""Persist fee schedule compare runs and changed-workbook paths."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.app_db.connection import app_db_connect
from app.state_codes import resolve_us_state_code


def _norm_state_code(state_code: Optional[str]) -> str:
    return resolve_us_state_code(str(state_code or ""))


def insert_compare_run(
    *,
    state_code: str,
    artifact_id: int,
    mapping_id: Optional[int],
    logical_schedule_key: Optional[str],
    dst_fsname: str,
    trigger_source: str,
    status: str,
    error_message: Optional[str] = None,
    summary: Optional[Dict[str, Any]] = None,
    changes_workbook_rel_path: Optional[str] = None,
    changes_workbook_bytes: Optional[int] = None,
    result_snapshot: Optional[Dict[str, Any]] = None,
) -> int:
    sc = _norm_state_code(state_code)
    aid = int(artifact_id)
    mid = int(mapping_id) if mapping_id is not None else None
    lsk = (logical_schedule_key or "").strip()[:256] or None
    dst = (dst_fsname or "").strip()[:256]
    trig = (trigger_source or "manual").strip()[:16]
    st = (status or "error").strip()[:16]
    err = (error_message or "").strip()[:2000] or None
    rel = (changes_workbook_rel_path or "").strip().replace("\\", "/")[:1024] or None
    summary_json = json.dumps(summary, separators=(",", ":"), default=str) if summary else None
    snapshot_json = json.dumps(result_snapshot, separators=(",", ":"), default=str) if result_snapshot else None
    bbytes = int(changes_workbook_bytes) if changes_workbook_bytes is not None else None

    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            """
            INSERT INTO dbo.fee_schedule_compare_run (
                state_code, artifact_id, mapping_id, logical_schedule_key, dst_fsname,
                trigger_source, status, error_message, summary_json,
                changes_workbook_rel_path, changes_workbook_bytes, result_snapshot_json
            )
            OUTPUT INSERTED.compare_run_id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sc, aid, mid, lsk, dst, trig, st, err, summary_json, rel, bbytes, snapshot_json),
        )
        row = cur.fetchone()
        cx.commit()
    if not row:
        raise RuntimeError("INSERT compare_run did not return id")
    return int(row[0])


def get_compare_run(*, compare_run_id: int, state_code: Optional[str] = None) -> Optional[Dict[str, Any]]:
    cid = int(compare_run_id)
    sc = _norm_state_code(state_code) if state_code else None
    with app_db_connect() as cx:
        cur = cx.cursor()
        if sc:
            cur.execute(
                """
                SELECT TOP (1)
                    cr.compare_run_id, cr.state_code, cr.artifact_id, cr.mapping_id,
                    cr.logical_schedule_key, cr.dst_fsname, cr.trigger_source, cr.status,
                    cr.error_message, cr.summary_json, cr.changes_workbook_rel_path,
                    cr.changes_workbook_bytes, cr.compared_at_utc, cr.result_snapshot_json,
                    a.source_label, a.original_filename
                FROM dbo.fee_schedule_compare_run cr
                LEFT JOIN dbo.fee_schedule_artifact a ON a.artifact_id = cr.artifact_id
                WHERE cr.compare_run_id = ? AND cr.state_code = ?
                """,
                (cid, sc),
            )
        else:
            cur.execute(
                """
                SELECT TOP (1)
                    cr.compare_run_id, cr.state_code, cr.artifact_id, cr.mapping_id,
                    cr.logical_schedule_key, cr.dst_fsname, cr.trigger_source, cr.status,
                    cr.error_message, cr.summary_json, cr.changes_workbook_rel_path,
                    cr.changes_workbook_bytes, cr.compared_at_utc, cr.result_snapshot_json,
                    a.source_label, a.original_filename
                FROM dbo.fee_schedule_compare_run cr
                LEFT JOIN dbo.fee_schedule_artifact a ON a.artifact_id = cr.artifact_id
                WHERE cr.compare_run_id = ?
                """,
                (cid,),
            )
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))


def list_compare_runs_for_state(*, state_code: str, limit: int = 50) -> List[Dict[str, Any]]:
    sc = _norm_state_code(state_code)
    lim = max(1, min(int(limit), 200))
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            f"""
            SELECT TOP ({lim})
                cr.compare_run_id, cr.state_code, cr.artifact_id, cr.mapping_id,
                cr.logical_schedule_key, cr.dst_fsname, cr.trigger_source, cr.status,
                cr.error_message, cr.summary_json, cr.changes_workbook_rel_path,
                cr.changes_workbook_bytes, cr.compared_at_utc, cr.result_snapshot_json,
                a.source_label, a.original_filename
            FROM dbo.fee_schedule_compare_run cr
            LEFT JOIN dbo.fee_schedule_artifact a ON a.artifact_id = cr.artifact_id
            WHERE cr.state_code = ?
            ORDER BY cr.compared_at_utc DESC, cr.compare_run_id DESC
            """,
            (sc,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

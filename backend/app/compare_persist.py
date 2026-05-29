"""Run compare, export changed workbook, persist compare_run row."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.app_db import compare_runs_repo, fee_column_mappings_repo
from app.app_db.artifacts_repo import get_artifact_by_id
from app.compare_fee_schedules import _triple_row_budget, compare_artifact_to_dst
from app.notifications.compare_export import export_compare_changes_xlsx
from app.storage.artifact_download import _artifact_root, _slug_folder

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _workbook_filename_stem(logical_schedule_key: str, *, display_label: Optional[str] = None) -> str:
    raw = (logical_schedule_key or display_label or "fee_schedule").strip().lower()
    if raw.endswith(".xlsx"):
        raw = raw[:-5]
    stem = _SAFE_NAME_RE.sub("_", raw).strip("._-")[:80]
    return stem or "fee_schedule"


def _schedule_slug(logical_schedule_key: str, *, display_label: Optional[str] = None) -> str:
    raw = (logical_schedule_key or display_label or "fee_schedule").strip()
    if raw.lower().endswith(".xlsx"):
        raw = raw[:-5]
    return _slug_folder(raw)


def build_changes_workbook_target(
    *,
    state_code: str,
    logical_schedule_key: str,
    display_label: Optional[str] = None,
) -> tuple[Path, str]:
    """
    Vault path: ``{state}/compare_runs/{schedule_slug}/{schedule_stem}_changed_{stamp}.xlsx``.
    Returns absolute path and vault-relative posix path.
    """
    root = _artifact_root()
    sc = (state_code or "").strip().lower()[:8] or "unknown"
    slug = _schedule_slug(logical_schedule_key, display_label=display_label)
    stem = _workbook_filename_stem(logical_schedule_key, display_label=display_label)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{stem}_changed_{stamp}.xlsx"
    rel = f"{sc}/compare_runs/{slug}/{filename}"
    abs_path = (root / rel).resolve()
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    return abs_path, rel.replace("\\", "/")


def _diff_count_from_summary(summary: Dict[str, Any]) -> int:
    return (
        int(summary.get("mismatch_count") or 0)
        + int(summary.get("state_only_count") or 0)
        + int(summary.get("dst_only_row_count") or 0)
    )


def _build_result_snapshot(cmp: Dict[str, Any], *, max_ui_rows: int = 1500) -> Dict[str, Any]:
    """Rows/columns for reopening the compare modal — capped diff rows for fast load."""
    rows = cmp.get("rows") if isinstance(cmp.get("rows"), list) else []
    mish = [r for r in rows if isinstance(r, dict) and r.get("status") == "mismatch"]
    st_only = [r for r in rows if isinstance(r, dict) and r.get("status") == "state_only"]
    dst_only = [r for r in rows if isinstance(r, dict) and r.get("status") == "dst_only"]
    bm, bso, bdo = _triple_row_budget(max_ui_rows, len(mish), len(st_only), len(dst_only))
    capped: List[Dict[str, Any]] = [*mish[:bm], *st_only[:bso], *dst_only[:bdo]]
    total_diff = len(mish) + len(st_only) + len(dst_only)
    return {
        "column_pairs": cmp.get("column_pairs") if isinstance(cmp.get("column_pairs"), list) else [],
        "rows": capped,
        "mapping_warnings": cmp.get("mapping_warnings") if isinstance(cmp.get("mapping_warnings"), list) else [],
        "logical_schedule_key": cmp.get("logical_schedule_key"),
        "snapshot_truncated": total_diff > len(capped),
    }


def compare_run_replay_payload(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build API compare shape from a persisted compare_run row."""
    import json

    raw_snap = row.get("result_snapshot_json")
    if not raw_snap:
        return None
    try:
        snapshot = json.loads(str(raw_snap))
    except json.JSONDecodeError:
        return None
    if not isinstance(snapshot, dict):
        return None

    summary: Dict[str, Any] = {}
    raw_summary = row.get("summary_json")
    if raw_summary:
        try:
            parsed = json.loads(str(raw_summary))
            if isinstance(parsed, dict):
                summary = parsed
        except json.JSONDecodeError:
            summary = {}

    return {
        "ok": True,
        "state_code": str(row.get("state_code") or "").upper(),
        "artifact_id": int(row.get("artifact_id") or 0),
        "dst_fsname": str(row.get("dst_fsname") or ""),
        "logical_schedule_key": snapshot.get("logical_schedule_key") or row.get("logical_schedule_key"),
        "summary": summary,
        "column_pairs": snapshot.get("column_pairs") if isinstance(snapshot.get("column_pairs"), list) else [],
        "rows": snapshot.get("rows") if isinstance(snapshot.get("rows"), list) else [],
        "mapping_warnings": snapshot.get("mapping_warnings") if isinstance(snapshot.get("mapping_warnings"), list) else [],
        "compare_run_id": int(row.get("compare_run_id") or 0),
        "compare_run_status": str(row.get("status") or ""),
        "from_saved_snapshot": True,
        "snapshot_truncated": bool(snapshot.get("snapshot_truncated")),
    }


def run_compare_and_persist(
    *,
    state_code: str,
    artifact_id: int,
    dst_fsname: str,
    trigger_source: str,
    display_label: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compare artifact vs DST, write changed workbook when diffs exist, insert DB row.
    Returns the compare payload plus ``compare_run_id`` and workbook metadata.
    """
    sc = str(state_code or "").strip().upper()[:8]
    aid = int(artifact_id)
    dst = str(dst_fsname or "").strip()
    trig = (trigger_source or "manual").strip()[:16]
    row_art = get_artifact_by_id(aid) or {}
    lsk = fee_column_mappings_repo.resolve_schedule_key_for_artifact(row_art)
    label = (display_label or row_art.get("source_label") or row_art.get("original_filename") or lsk or "").strip()
    mapping_id: Optional[int] = None

    try:
        map_row = fee_column_mappings_repo.lookup_latest_mapping(
            state_code=sc,
            state_logical_schedule_key=lsk,
            dst_fsname=dst,
        )
        if map_row:
            mapping_id = int(map_row["mapping_id"]) if map_row.get("mapping_id") is not None else None

        cmp = compare_artifact_to_dst(state_code=sc, artifact_id=aid, dst_fsname=dst)
        summary = cmp.get("summary") if isinstance(cmp.get("summary"), dict) else {}
        diff_n = _diff_count_from_summary(summary)
        status = "success" if diff_n > 0 else "no_changes"
        rel_path: Optional[str] = None
        bytes_size: Optional[int] = None

        if diff_n > 0:
            abs_path, rel_path = build_changes_workbook_target(
                state_code=sc,
                logical_schedule_key=lsk,
                display_label=label,
            )
            written = export_compare_changes_xlsx(compare_result=cmp, output_path=abs_path)
            if written and written.is_file():
                bytes_size = written.stat().st_size
            else:
                status = "no_changes"
                rel_path = None
                bytes_size = None

        run_id = compare_runs_repo.insert_compare_run(
            state_code=sc,
            artifact_id=aid,
            mapping_id=mapping_id,
            logical_schedule_key=lsk,
            dst_fsname=dst,
            trigger_source=trig,
            status=status,
            summary=summary,
            changes_workbook_rel_path=rel_path,
            changes_workbook_bytes=bytes_size,
            result_snapshot=_build_result_snapshot(cmp),
        )
        out = dict(cmp)
        out["compare_run_id"] = run_id
        out["compare_run_status"] = status
        out["changes_workbook_rel_path"] = rel_path
        if rel_path:
            out["changes_workbook_absolute_path"] = str((_artifact_root() / rel_path).resolve())
        return out
    except Exception as ex:
        logger.warning("Compare persist failed for artifact %s (%s): %s", aid, label, ex)
        try:
            compare_runs_repo.insert_compare_run(
                state_code=sc,
                artifact_id=aid,
                mapping_id=mapping_id,
                logical_schedule_key=lsk,
                dst_fsname=dst,
                trigger_source=trig,
                status="error",
                error_message=str(ex)[:2000],
            )
        except Exception as log_ex:
            logger.warning("Could not log compare_run error row: %s", log_ex)
        raise

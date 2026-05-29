"""Build and send post-sync notification emails for a single state run."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.app_db import fee_column_mappings_repo, notification_contacts_repo
from app.app_db.artifacts_repo import get_artifact_by_id
from app.compare_persist import run_compare_and_persist
from app.notifications.smtp_mailer import send_email, smtp_configured
from app.storage.artifact_download import resolve_artifact_path

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


def _format_run_time_ist(when: Optional[datetime] = None) -> str:
    dt = when or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(_IST)
    text = local.strftime("%d %b %Y, %I:%M %p IST")
    return re.sub(r", 0(\d):", r", \1:", text)


def _display_name(artifact_row: Dict[str, Any], saved_meta: Dict[str, Any]) -> str:
    for key in ("source_label", "original_filename", "logical_schedule_key"):
        val = str(saved_meta.get(key) or artifact_row.get(key) or "").strip()
        if val:
            return val
    return f"artifact #{artifact_row.get('artifact_id')}"


def _new_artifacts_from_run(artifacts_saved: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in artifacts_saved or []:
        if not isinstance(item, dict):
            continue
        if item.get("skipped"):
            continue
        aid = item.get("artifact_id")
        if aid is None:
            continue
        out.append(item)
    return out


def _build_sync_status_email(
    *,
    state_code: str,
    run_label: str,
    new_items: List[Dict[str, Any]],
    unmapped: List[Dict[str, str]],
    compare_failures: Optional[List[Dict[str, str]]] = None,
) -> tuple[str, str]:
    sc = state_code.upper()
    if not new_items:
        subject = f"Fee Schedule Sync — {sc} — no new files"
        lines = [
            f"Fee schedule sync completed for {sc}.",
            f"Run time: {run_label}",
            "",
            "No new fee schedules were downloaded this run.",
            "",
            "— Fee Schedule Comparison Tool (automated)",
        ]
        return subject, "\n".join(lines)

    subject = f"Fee Schedule Sync — {sc} — {len(new_items)} new file(s)"
    lines = [
        f"Fee schedule sync completed for {sc}.",
        f"Run time: {run_label}",
        "",
        f"{len(new_items)} new fee schedule(s) found:",
        "",
    ]
    for i, item in enumerate(new_items, start=1):
        name = str(item.get("display_name") or item.get("logical_schedule_key") or f"File {i}")
        path = str(item.get("absolute_path") or item.get("stored_rel_path") or "")
        lines.append(f"{i}. {name}")
        if path:
            lines.append(f"   {path}")
        lines.append("")

    if unmapped:
        lines.append("The following new schedule(s) have no column mapping yet — please map them in the tool:")
        lines.append("")
        for row in unmapped:
            lines.append(f"• {row.get('display_name') or row.get('logical_schedule_key')}")
            if row.get("absolute_path"):
                lines.append(f"  {row['absolute_path']}")
        lines.append("")

    failures = compare_failures or []
    if failures:
        lines.append("Compare could not run for the following mapped schedule(s):")
        lines.append("")
        for row in failures:
            lines.append(f"• {row.get('display_name') or row.get('logical_schedule_key')}")
            if row.get("error"):
                lines.append(f"  {row['error']}")
        lines.append("")

    lines.append("— Fee Schedule Comparison Tool (automated)")
    return subject, "\n".join(lines)


def _build_compare_results_email(
    *,
    state_code: str,
    run_label: str,
    compare_entries: List[Dict[str, Any]],
) -> tuple[str, str]:
    sc = state_code.upper()
    subject = f"Fee Schedule Compare — {sc} — {len(compare_entries)} result(s)"
    lines = [
        f"Compare completed after sync for {sc}.",
        f"Run time: {run_label}",
        "",
    ]
    for i, entry in enumerate(compare_entries, start=1):
        name = str(entry.get("display_name") or "Schedule")
        dst = str(entry.get("dst_fsname") or "")
        summary = entry.get("summary") if isinstance(entry.get("summary"), dict) else {}
        lines.append(f"{i}. {name}" + (f" vs DST «{dst}»" if dst else ""))
        if summary:
            lines.append(
                "   Summary: "
                f"{summary.get('mismatch_count', 0)} modified, "
                f"{summary.get('state_only_count', 0)} added in state, "
                f"{summary.get('dst_only_row_count', 0)} DST-only"
            )
        diff_path = str(entry.get("diff_path") or "").strip()
        if diff_path:
            lines.append(f"   Changed workbook: {diff_path}")
        elif entry.get("compare_note"):
            lines.append(f"   Note: {entry['compare_note']}")
        err = str(entry.get("error") or "").strip()
        if err:
            lines.append(f"   Error: {err}")
        src_path = str(entry.get("absolute_path") or "").strip()
        if src_path:
            lines.append(f"   State file: {src_path}")
        lines.append("")

    lines.append("— Fee Schedule Comparison Tool (automated)")
    return subject, "\n".join(lines)


def handle_post_sync_notifications(
    *,
    state_code: str,
    artifacts_saved: Optional[List[Dict[str, Any]]],
    run_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Send sync-status and compare-result emails for one state run.

    - Sync email (``notify_new_state_file``): always — no-new or new file paths + unmapped list.
    - Compare email (``notify_compare_result``): when at least one mapped new file compares successfully.
    """
    sc = str(state_code or "").strip().upper()[:8]
    result: Dict[str, Any] = {"ok": True, "state_code": sc, "sync_email": None, "compare_email": None}

    if not sc:
        result["ok"] = False
        result["error"] = "state_code required"
        return result

    if not smtp_configured():
        result["ok"] = False
        result["skipped"] = "smtp_not_configured"
        logger.info("Post-sync notifications skipped for %s: SMTP not configured", sc)
        return result

    run_label = _format_run_time_ist(run_at)
    new_saved = _new_artifacts_from_run(artifacts_saved)

    enriched_new: List[Dict[str, Any]] = []
    unmapped: List[Dict[str, str]] = []
    compare_entries: List[Dict[str, Any]] = []
    compare_failures: List[Dict[str, str]] = []

    for meta in new_saved:
        aid = int(meta["artifact_id"])
        row = get_artifact_by_id(aid) or {}
        display = _display_name(row, meta)
        abs_path = str(meta.get("absolute_path") or "").strip()
        if not abs_path:
            rel = str(row.get("stored_rel_path") or meta.get("stored_rel_path") or "")
            if rel:
                try:
                    abs_path = str(resolve_artifact_path(rel))
                except ValueError:
                    abs_path = rel

        item = {
            **meta,
            "display_name": display,
            "absolute_path": abs_path,
            "logical_schedule_key": str(row.get("logical_schedule_key") or meta.get("logical_schedule_key") or ""),
        }
        enriched_new.append(item)

        lsk = fee_column_mappings_repo.resolve_schedule_key_for_artifact(row)
        map_row = fee_column_mappings_repo.lookup_latest_mapping(state_code=sc, state_logical_schedule_key=lsk)
        if not map_row:
            unmapped.append(
                {
                    "display_name": display,
                    "logical_schedule_key": lsk,
                    "absolute_path": abs_path,
                }
            )
            continue

        dst_fsname = str(map_row.get("dst_fsname") or "").strip()
        entry: Dict[str, Any] = {
            "display_name": display,
            "dst_fsname": dst_fsname,
            "absolute_path": abs_path,
            "artifact_id": aid,
        }
        try:
            cmp = run_compare_and_persist(
                state_code=sc,
                artifact_id=aid,
                dst_fsname=dst_fsname,
                trigger_source="sync",
                display_label=display,
            )
            entry["summary"] = cmp.get("summary") or {}
            entry["compare_run_id"] = cmp.get("compare_run_id")
            diff_path = str(cmp.get("changes_workbook_absolute_path") or "").strip()
            if diff_path:
                entry["diff_path"] = diff_path
            elif cmp.get("compare_run_status") == "no_changes":
                entry["compare_note"] = "Compare completed with no modified/added/DST-only rows."
            compare_entries.append(entry)
        except Exception as ex:
            logger.warning("Auto-compare failed for artifact %s (%s): %s", aid, display, ex)
            entry["error"] = str(ex)
            compare_entries.append(entry)
            compare_failures.append(
                {
                    "display_name": display,
                    "logical_schedule_key": lsk,
                    "error": str(ex),
                }
            )

    sync_recipients = notification_contacts_repo.list_notification_recipient_emails(
        state_code=sc,
        notify_new_state_file=True,
    )
    sync_subject, sync_body = _build_sync_status_email(
        state_code=sc,
        run_label=run_label,
        new_items=enriched_new,
        unmapped=unmapped,
        compare_failures=compare_failures,
    )
    if sync_recipients:
        try:
            send_email(to=sync_recipients, subject=sync_subject, body_text=sync_body)
            result["sync_email"] = {
                "sent": True,
                "recipients": sync_recipients,
                "subject": sync_subject,
                "new_file_count": len(enriched_new),
            }
        except Exception as ex:
            logger.exception("Sync notification email failed for %s", sc)
            result["sync_email"] = {"sent": False, "error": str(ex)}
            result["ok"] = False
    else:
        result["sync_email"] = {"sent": False, "skipped": "no_recipients_notify_new_state_file"}

    successful_compares = [e for e in compare_entries if not e.get("error")]
    if successful_compares:
        compare_recipients = notification_contacts_repo.list_notification_recipient_emails(
            state_code=sc,
            notify_compare_result=True,
        )
        compare_subject, compare_body = _build_compare_results_email(
            state_code=sc,
            run_label=run_label,
            compare_entries=compare_entries,
        )
        if compare_recipients:
            try:
                send_email(to=compare_recipients, subject=compare_subject, body_text=compare_body)
                result["compare_email"] = {
                    "sent": True,
                    "recipients": compare_recipients,
                    "subject": compare_subject,
                    "compare_count": len(compare_entries),
                }
            except Exception as ex:
                logger.exception("Compare notification email failed for %s", sc)
                result["compare_email"] = {"sent": False, "error": str(ex)}
                result["ok"] = False
        else:
            result["compare_email"] = {"sent": False, "skipped": "no_recipients_notify_compare_result"}
    elif compare_entries:
        result["compare_email"] = {"sent": False, "skipped": "all_compares_failed", "entries": len(compare_entries)}

    return result

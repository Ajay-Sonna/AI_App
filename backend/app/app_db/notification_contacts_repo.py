"""CRUD for per-state notification contacts (future email / automation)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.app_db.connection import app_db_connect
from app.state_codes import resolve_us_state_code


def _norm_email(email: str) -> str:
    return str(email or "").strip().lower()[:320]


def list_notification_contacts(*, state_code: str, limit: int = 500) -> List[Dict[str, Any]]:
    sc = resolve_us_state_code(str(state_code or ""))
    lim = max(1, min(int(limit), 2000))
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            f"""
            SELECT TOP ({lim})
                notification_contact_id, state_code, contact_name, email, team_name, department_name,
                notifications_enabled, notify_new_state_file, notify_compare_result,
                created_at_utc, updated_at_utc
            FROM dbo.fee_schedule_notification_contact
            WHERE state_code = ?
            ORDER BY department_name ASC, team_name ASC, contact_name ASC, notification_contact_id ASC
            """,
            (sc,),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_notification_contact(*, contact_id: int, state_code: str) -> Optional[Dict[str, Any]]:
    sc = resolve_us_state_code(str(state_code or ""))
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            """
            SELECT TOP (1)
                notification_contact_id, state_code, contact_name, email, team_name, department_name,
                notifications_enabled, notify_new_state_file, notify_compare_result,
                created_at_utc, updated_at_utc
            FROM dbo.fee_schedule_notification_contact
            WHERE notification_contact_id = ? AND state_code = ?
            """,
            (int(contact_id), sc),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [c[0] for c in cur.description]
        return dict(zip(cols, row))


def insert_notification_contact(
    *,
    state_code: str,
    contact_name: str,
    email: str,
    team_name: Optional[str] = None,
    department_name: Optional[str] = None,
    notifications_enabled: bool = True,
    notify_new_state_file: bool = True,
    notify_compare_result: bool = True,
) -> int:
    sc = resolve_us_state_code(str(state_code or ""))
    cn = str(contact_name or "").strip()[:256]
    em = _norm_email(email)
    if not cn:
        raise ValueError("contact_name is required")
    if not em or "@" not in em:
        raise ValueError("email is required")
    tn = (team_name or "").strip()[:256] or None
    dn = (department_name or "").strip()[:256] or None
    ne = 1 if notifications_enabled else 0
    nn = 1 if notify_new_state_file else 0
    nc = 1 if notify_compare_result else 0

    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            """
            INSERT INTO dbo.fee_schedule_notification_contact (
                state_code, contact_name, email, team_name, department_name,
                notifications_enabled, notify_new_state_file, notify_compare_result
            )
            OUTPUT INSERTED.notification_contact_id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sc, cn, em, tn, dn, ne, nn, nc),
        )
        row = cur.fetchone()
        cx.commit()
    if not row:
        raise RuntimeError("INSERT notification contact did not return id")
    return int(row[0])


def update_notification_contact(
    *,
    contact_id: int,
    state_code: str,
    contact_name: str,
    email: str,
    team_name: Optional[str] = None,
    department_name: Optional[str] = None,
    notifications_enabled: bool = True,
    notify_new_state_file: bool = True,
    notify_compare_result: bool = True,
) -> bool:
    sc = resolve_us_state_code(str(state_code or ""))
    cn = str(contact_name or "").strip()[:256]
    em = _norm_email(email)
    if not cn:
        raise ValueError("contact_name is required")
    if not em or "@" not in em:
        raise ValueError("email is required")
    tn = (team_name or "").strip()[:256] or None
    dn = (department_name or "").strip()[:256] or None
    ne = 1 if notifications_enabled else 0
    nn = 1 if notify_new_state_file else 0
    ncr = 1 if notify_compare_result else 0

    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            """
            UPDATE dbo.fee_schedule_notification_contact
            SET contact_name = ?,
                email = ?,
                team_name = ?,
                department_name = ?,
                notifications_enabled = ?,
                notify_new_state_file = ?,
                notify_compare_result = ?,
                updated_at_utc = SYSUTCDATETIME()
            WHERE notification_contact_id = ? AND state_code = ?
            """,
            (cn, em, tn, dn, ne, nn, ncr, int(contact_id), sc),
        )
        cnt = cur.rowcount or 0
        cx.commit()
    return cnt > 0


def delete_notification_contact(*, contact_id: int, state_code: str) -> bool:
    sc = resolve_us_state_code(str(state_code or ""))
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            """
            DELETE FROM dbo.fee_schedule_notification_contact
            WHERE notification_contact_id = ? AND state_code = ?
            """,
            (int(contact_id), sc),
        )
        cx.commit()
        return (cur.rowcount or 0) > 0

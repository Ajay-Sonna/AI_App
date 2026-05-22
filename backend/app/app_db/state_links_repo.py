"""CRUD for configured state portal URLs (one place for links)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.app_db.connection import app_db_connect
from app.state_codes import resolve_us_state_code

logger = logging.getLogger(__name__)

# None = not yet checked; True/False cached for process lifetime (restart after migration).
_has_last_agent_run_column: Optional[bool] = None


def _norm_state(state_code: str) -> str:
    return resolve_us_state_code(str(state_code or ""))


def _detect_last_agent_run_column(cx) -> bool:
    """Whether ``dbo.state_portal_link.last_agent_run_at_utc`` exists (migration 004)."""
    global _has_last_agent_run_column
    if _has_last_agent_run_column is not None:
        return _has_last_agent_run_column
    cur = cx.cursor()
    cur.execute(
        """
        SELECT 1
        FROM sys.columns c
        INNER JOIN sys.tables t ON c.object_id = t.object_id
        WHERE SCHEMA_NAME(t.schema_id) = N'dbo'
          AND t.name = N'state_portal_link'
          AND c.name = N'last_agent_run_at_utc'
        """
    )
    _has_last_agent_run_column = cur.fetchone() is not None
    if not _has_last_agent_run_column:
        logger.warning(
            "Column last_agent_run_at_utc missing on dbo.state_portal_link; "
            "run backend/sql/004_state_portal_last_run.sql. Using legacy queries without that column.",
        )
    return _has_last_agent_run_column


def list_state_portal_links(*, state_code: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    lim = max(1, min(int(limit), 500))
    with app_db_connect() as cx:
        has_last = _detect_last_agent_run_column(cx)
        cur = cx.cursor()
        if has_last:
            extra = ", last_agent_run_at_utc"
        else:
            extra = ""
        if state_code:
            cur.execute(
                f"""
                SELECT TOP (?) link_id, state_code, display_label, portal_url, sort_order,
                       created_at_utc, updated_at_utc{extra}
                FROM dbo.state_portal_link
                WHERE state_code = ?
                ORDER BY link_id ASC
                """,
                (lim, _norm_state(state_code)),
            )
        else:
            cur.execute(
                f"""
                SELECT TOP (?) link_id, state_code, display_label, portal_url, sort_order,
                       created_at_utc, updated_at_utc{extra}
                FROM dbo.state_portal_link
                ORDER BY state_code ASC, link_id ASC
                """,
                (lim,),
            )
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        if not has_last:
            for r in rows:
                r["last_agent_run_at_utc"] = None
        return rows


def get_portal_url_for_state(state_code: str) -> Optional[str]:
    """Single saved portal URL for this state, or None."""
    rows = list_state_portal_links(state_code=state_code, limit=1)
    if not rows:
        return None
    u = rows[0].get("portal_url")
    return str(u).strip() if u else None


def upsert_state_portal_link(
    *,
    state_code: str,
    display_label: str,
    portal_url: str,
    sort_order: int = 0,
) -> tuple[int, bool]:
    """
    At most one row per ``state_code``. If a row exists, update it; else insert.

    Returns ``(link_id, was_insert)``.
    """
    sc = _norm_state(state_code)
    if not sc:
        raise ValueError("state_code is required")
    label = display_label.strip()[:256] or portal_url.strip()[:256]
    url = portal_url.strip()[:2048]
    if not url:
        raise ValueError("portal_url is required")

    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(
            "SELECT link_id FROM dbo.state_portal_link WHERE state_code = ?",
            (sc,),
        )
        row = cur.fetchone()
        if row:
            lid = int(row[0])
            cur.execute(
                """
                UPDATE dbo.state_portal_link
                SET display_label = ?, portal_url = ?, sort_order = ?, updated_at_utc = SYSUTCDATETIME()
                WHERE link_id = ?
                """,
                (label, url, int(sort_order), lid),
            )
            cx.commit()
            return lid, False

        cur.execute(
            """
            INSERT INTO dbo.state_portal_link (state_code, display_label, portal_url, sort_order)
            OUTPUT INSERTED.link_id
            VALUES (?, ?, ?, ?)
            """,
            (sc, label, url, int(sort_order)),
        )
        row = cur.fetchone()
        cx.commit()
    if not row:
        raise RuntimeError("INSERT state_portal_link failed")
    return int(row[0]), True


def insert_state_portal_link(
    *,
    state_code: str,
    display_label: str,
    portal_url: str,
    sort_order: int = 0,
) -> int:
    """Deprecated name: use ``upsert_state_portal_link`` (one URL per state)."""
    lid, _ = upsert_state_portal_link(
        state_code=state_code,
        display_label=display_label,
        portal_url=portal_url,
        sort_order=sort_order,
    )
    return lid


def touch_last_agent_run(state_code: str) -> None:
    """Set ``last_agent_run_at_utc`` for the state's portal row (no-op if row missing)."""
    sc = _norm_state(state_code)
    with app_db_connect() as cx:
        if not _detect_last_agent_run_column(cx):
            return
        cur = cx.cursor()
        cur.execute(
            """
            UPDATE dbo.state_portal_link
            SET last_agent_run_at_utc = SYSUTCDATETIME(), updated_at_utc = SYSUTCDATETIME()
            WHERE state_code = ?
            """,
            (sc,),
        )
        cx.commit()


def delete_state_portal_link(link_id: int) -> bool:
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute("DELETE FROM dbo.state_portal_link WHERE link_id = ?", (int(link_id),))
        n = cur.rowcount
        cx.commit()
    return n > 0


def delete_state_portal_link_for_state(state_code: str) -> int:
    """Remove the single saved URL row for this state (if any). Returns rows deleted."""
    sc = _norm_state(state_code)
    if not sc:
        return 0
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute("DELETE FROM dbo.state_portal_link WHERE state_code = ?", (sc,))
        n = cur.rowcount
        cx.commit()
    return int(n)


def update_state_portal_link(
    link_id: int,
    *,
    display_label: Optional[str] = None,
    portal_url: Optional[str] = None,
    sort_order: Optional[int] = None,
) -> bool:
    parts: List[str] = []
    params: List[Any] = []
    if display_label is not None:
        parts.append("display_label = ?")
        params.append(display_label.strip()[:256])
    if portal_url is not None:
        parts.append("portal_url = ?")
        params.append(portal_url.strip()[:2048])
    if sort_order is not None:
        parts.append("sort_order = ?")
        params.append(int(sort_order))
    if not parts:
        return False
    parts.append("updated_at_utc = SYSUTCDATETIME()")
    params.append(int(link_id))
    sql = f"UPDATE dbo.state_portal_link SET {', '.join(parts)} WHERE link_id = ?"
    with app_db_connect() as cx:
        cur = cx.cursor()
        cur.execute(sql, tuple(params))
        n = cur.rowcount
        cx.commit()
    return n > 0

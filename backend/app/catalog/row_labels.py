"""Derive human-facing fee schedule titles and slugs from portal catalog rows (server-side)."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

_SAFE_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def ordered_column_names(table: Dict[str, Any]) -> List[str]:
    """Match frontend ``getTableColumns``: declared columns first, then other row keys (no _links)."""
    rows = table.get("rows") or []
    if not isinstance(rows, list):
        rows = []
    keys_from_rows: List[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for k in row.keys():
            if not isinstance(k, str) or k.startswith("_") or k == "_links":
                continue
            if k not in seen:
                seen.add(k)
                keys_from_rows.append(k)
    declared: List[str] = []
    for c in table.get("columns") or []:
        if isinstance(c, str) and c.strip():
            declared.append(c.strip())
    ordered: List[str] = []
    seen2: set[str] = set()
    for c in declared:
        if c not in seen2:
            ordered.append(c)
            seen2.add(c)
    for c in keys_from_rows:
        if c not in seen2:
            ordered.append(c)
            seen2.add(c)
    return ordered


def pick_fee_schedule_name_column(cols: List[str]) -> Optional[str]:
    if not cols:
        return None
    scored = [(c, str(c).lower()) for c in cols]
    for c, t in scored:
        if re.search(r"\bfee\s*schedule\b", t):
            return c
    for c, t in scored:
        if re.search(r"\bschedule\b", t) and "program" not in t:
            return c
    for c, t in scored:
        if re.search(r"\bschedule\b", t):
            return c
    for c, t in scored:
        if re.search(r"\btitle\b|\bname\b|\bdocument\b|\bdescription\b", t):
            return c
    if len(cols) >= 2 and "program" in str(cols[0]).lower():
        return cols[1]
    return cols[0]


def pick_program_column(cols: List[str], fee_col: Optional[str]) -> Optional[str]:
    """Column that names program / category / service line (not the fee-schedule-name column)."""
    for c in cols:
        if fee_col and c == fee_col:
            continue
        t = str(c).strip().lower()
        if not t or t.startswith("_"):
            continue
        if re.search(r"\b(program|service category|service line|category|department|division)\b", t):
            if "fee" in t and "schedule" in t:
                continue
            return c
    if len(cols) >= 2 and fee_col and cols[0] != fee_col:
        t0 = str(cols[0]).strip().lower()
        if re.search(r"\b(program|service category|category)\b", t0):
            return cols[0]
    return None


def format_portal_date_heading(iso_date: str) -> str:
    """Turn ``YYYY-MM-DD`` into a short heading like ``Jan 5, 2026`` (no zero-padded day)."""
    try:
        y, m, d = iso_date.strip()[:10].split("-", 2)
        dt = date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return iso_date.strip()[:10]
    month = dt.strftime("%b")
    day = int(dt.day)
    return f"{month} {day}, {dt.year}"


def compose_catalog_row_display_label(
    *,
    row: Dict[str, Any],
    cols: List[str],
    portal_date_iso: Optional[str],
    fallback_link_label: Optional[str] = None,
) -> str:
    """
    Human-facing label persisted as ``fee_schedule_artifact.source_label``.

    Prefer ``Program — Fee schedule`` when both exist; otherwise one column or row title.
    When a portal edition date is known, suffix `` — {abbrev date}`` so multiple dated rows distinguish.
    """
    fee_col = pick_fee_schedule_name_column(cols)
    prog_col = pick_program_column(cols, fee_col)
    fee = ""
    prog = ""

    if fee_col:
        fee = _cell_text(row.get(fee_col))
    if prog_col:
        prog = _cell_text(row.get(prog_col))

    if not fee:
        fee = fee_schedule_title_from_row(row, cols)

    base = ""
    if prog and fee and prog.strip().lower() != fee.strip().lower():
        base = f"{prog.strip()} — {fee.strip()}"
    elif fee:
        base = fee.strip()
    elif prog:
        base = prog.strip()
    elif fallback_link_label and str(fallback_link_label).strip():
        fb = str(fallback_link_label).strip()
        if len(fb) > 48 or fb.lower().startswith(("http://", "https://")):
            fb = ""
        if fb and fb.lower() not in ("download", "download file", "click here", "here"):
            base = fb
    else:
        base = ""

    if portal_date_iso and str(portal_date_iso).strip():
        iso = str(portal_date_iso).strip()[:10]
        suf = format_portal_date_heading(iso)
        if suf and suf.lower() not in base.lower():
            base = f"{base} — {suf}" if base else suf

    return base.strip()


def _cell_text(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, (int, float, bool)):
        return str(val).strip()
    return str(val).strip()


def fee_schedule_title_from_row(row: Dict[str, Any], cols: List[str]) -> str:
    # California DWC OMFS: heading before the table (e.g. "Pathology and clinical laboratory")
    topic = _cell_text(row.get("_schedule_section"))
    if topic:
        return topic
    col = pick_fee_schedule_name_column(cols)
    if not col:
        return ""
    return _cell_text(row.get(col))


def guess_portal_date_str(row: Dict[str, Any], cols: List[str]) -> Optional[str]:
    """Return YYYY-MM-DD if a plausible portal date cell is found."""
    hints = (
        "date",
        "effective",
        "posted",
        "updated",
        "revised",
        "modified",
        "published",
        "created",
        "release",
        "effective date",
        "posted date",
        "revision",
    )
    for c in cols:
        t = str(c).lower()
        if not any(h in t for h in hints):
            continue
        raw = _cell_text(row.get(c))
        if not raw:
            continue
        raw_clean = raw[:48].strip()
        fmts_lens: List[tuple[str, Optional[int]]] = [
            ("%Y-%m-%d", 10),
            ("%m-%d-%Y %H:%M:%S", 19),
            ("%m-%d-%Y %H:%M", 16),
            ("%m-%d-%Y", 10),
            ("%m/%d/%Y %H:%M:%S", 19),
            ("%m/%d/%Y %H:%M", 16),
            ("%m/%d/%Y", 10),
            ("%m/%d/%y", 8),
            ("%Y/%m/%d", 10),
            ("%d-%m-%Y %H:%M:%S", 19),
            ("%d-%m-%Y", 10),
        ]
        for fmt, n_chars in fmts_lens:
            if n_chars is not None and len(raw_clean) < n_chars:
                continue
            snippet = raw_clean[:n_chars] if n_chars else raw_clean
            try:
                d = datetime.strptime(snippet, fmt).date()
                return d.isoformat()
            except ValueError:
                continue
        m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
        if m:
            return m.group(1)
        # "January 1, 2026" / "Jan 1, 2026"
        m2 = re.search(
            r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})\b",
            raw,
            re.I,
        )
        if m2:
            try:
                d = _parse_month_day_year(m2.group(1), m2.group(2), m2.group(3))
                if d:
                    return d.isoformat()
            except Exception:
                pass
    return None


_MONTH_NAMES = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _parse_month_day_year(mon: str, day: str, year: str) -> Optional[date]:
    mk = mon.strip().lower()[:3] if len(mon.strip()) >= 3 else mon.strip().lower()
    mi = _MONTH_NAMES.get(mon.strip().lower()) or _MONTH_NAMES.get(mk)
    if not mi:
        return None
    y = int(year)
    d = int(day)
    return date(y, mi, d)


def guess_effective_date_from_link_text(text: str) -> Optional[str]:
    """
    Best-effort effective date from hyperlink anchor text (e.g. DWC order titles).
    Returns YYYY-MM-DD or None.
    """
    if not text or not str(text).strip():
        return None
    s = str(text).strip()
    # Effective January 1, 2026
    m = re.search(
        r"Effective\s+([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})",
        s,
        re.I,
    )
    if m:
        d0 = _parse_month_day_year(m.group(1), m.group(2), m.group(3))
        return d0.isoformat() if d0 else None
    # Effective 4/1/2025 or 04/01/2025
    m2 = re.search(r"Effective\s+(\d{1,2})/(\d{1,2})/(\d{2,4})", s, re.I)
    if m2:
        a, b, ys = int(m2.group(1)), int(m2.group(2)), m2.group(3)
        y = int(ys) if len(ys) == 4 else 2000 + int(ys) if int(ys) < 70 else 1900 + int(ys)
        try:
            return date(y, a, b).isoformat()
        except ValueError:
            try:
                return date(y, b, a).isoformat()
            except ValueError:
                pass
    m3 = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    if m3:
        return m3.group(1)
    return None


def row_or_label_superseded_hint(*, row: Dict[str, Any], link_label: Optional[str]) -> bool:
    """True if row/link text suggests a superseded order (still stored for history, not promoted to current)."""
    parts: List[str] = []
    if link_label:
        parts.append(str(link_label))
    if isinstance(row, dict):
        for k, v in row.items():
            if isinstance(k, str) and not k.startswith("_"):
                parts.append(str(v))
    blob = " ".join(parts).upper()
    return "SUPERSEDED" in blob


def slug_logical_schedule_key(name: str, *, max_len: int = 96) -> str:
    s = (name or "").strip() or "fee_schedule"
    s = _SAFE_SLUG.sub("_", s).strip("._-") or "fee_schedule"
    if len(s) > max_len:
        s = s[:max_len].rstrip("._-")
    return s.lower()

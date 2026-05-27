"""
California DIR / DWC Official Medical Fee Schedule (OMFS) HTML helpers.

OMFS pages use a dark section header (h2–h5) followed by a two-column table:
``Effective date`` | ``… documents``. Every document link was being treated as its own
``logical_schedule_key``; we instead group rows under the **section heading** and mark
**primary** vs **alternate** links per row for UI (main card vs versions / history).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List
from urllib.parse import urlparse

from bs4 import Tag

from app.catalog.row_labels import slug_logical_schedule_key

_DIR_HOST = "dir.ca.gov"


def is_ca_dwc_portal(base_url: str) -> bool:
    h = (urlparse((base_url or "").strip()).hostname or "").lower()
    return h.endswith(_DIR_HOST) and "/dwc/" in (base_url or "").lower()


def table_looks_like_effective_date_documents(columns: List[str]) -> bool:
    """True for OMFS-style ``Effective date`` + ``… documents`` tables."""
    if len(columns) < 2:
        return False
    blob = " ".join(str(c or "").lower() for c in columns)
    if "effective" not in blob or "date" not in blob:
        return False
    return "document" in blob or "regulation" in blob or "order" in blob


def infer_section_heading_before_table(table: Tag) -> str:
    """
    Walk backward from the table to the nearest section title (DWC uses h2–h5).
    """
    if not isinstance(table, Tag):
        return ""
    for tag_name in ("h5", "h4", "h3", "h2"):
        h = table.find_previous(tag_name)
        if h and isinstance(h, Tag):
            t = h.get_text(" ", strip=True)
            if t and len(t) > 3 and not t.lower().startswith("table "):
                return t
    # Fallback: bold line immediately before the table
    cur: Tag | None = table
    for _ in range(40):
        cur = cur.find_previous_sibling()
        if cur is None:
            break
        if not isinstance(cur, Tag):
            continue
        if cur.name in ("h2", "h3", "h4", "h5"):
            t = cur.get_text(" ", strip=True)
            if t:
                return t
        if cur.name == "p":
            strong = cur.find("strong")
            if strong:
                t = strong.get_text(" ", strip=True)
                if t and len(t) > 8:
                    return t
    return ""


def _link_superseded(lnk: Dict[str, Any]) -> bool:
    s = str(lnk.get("superseded_hint") or "").strip().lower()
    if s in ("1", "true", "yes"):
        return True
    text = (lnk.get("text") or lnk.get("label") or "").upper()
    return "SUPERSEDED" in text


def _primary_link_sort_key(lnk: Dict[str, Any]) -> tuple:
    """
    Lower tuple sorts first: non-superseded, Administrative Director orders, then PDF, etc.
    """
    sup = 1 if _link_superseded(lnk) else 0
    text = (lnk.get("text") or "").lower()
    url = (lnk.get("url") or "").lower()
    path = url.split("?", 1)[0]

    order_score = 0
    if "administrative director" in text or re.search(r"\borders of\b", text):
        order_score = 3
    elif re.search(r"\border\b", text) and "regulation" not in text[:20]:
        order_score = 2
    elif "order" in text:
        order_score = 1

    ext_rank = 0
    if path.endswith(".pdf"):
        ext_rank = 4
    elif path.endswith(".xlsx") or path.endswith(".xls"):
        ext_rank = 3
    elif path.endswith(".docx"):
        ext_rank = 2
    elif path.endswith(".doc"):
        ext_rank = 1

    reg_penalty = 0
    if "regulation" in text and "order" not in text:
        reg_penalty = 1

    return (sup, -order_score, -ext_rank, reg_penalty, len(text))


def annotate_ca_dwc_row_schedule_metadata(row: Dict[str, Any], section: str) -> None:
    """Attach section slug and primary / alternate link tiers (mutates row)."""
    sec = (section or "").strip()
    if not sec:
        return
    row["_schedule_section"] = sec
    row["_fee_topic_slug"] = slug_logical_schedule_key(sec)[:256]

    links = row.get("_links")
    if not isinstance(links, list) or not links:
        return
    link_dicts = [x for x in links if isinstance(x, dict)]
    if not link_dicts:
        return
    sorted_links = sorted(link_dicts, key=_primary_link_sort_key)
    for i, lk in enumerate(sorted_links):
        lk["_artifact_slot"] = "primary" if i == 0 else "alternate"
    row["_links"] = sorted_links

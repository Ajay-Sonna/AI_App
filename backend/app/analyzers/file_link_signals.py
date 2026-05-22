# backend/app/analyzers/file_link_signals.py
"""
Detect fee-schedule / document links when hrefs omit extensions (ASP.NET, CMS, etc.).
"""

from __future__ import annotations

import re
from typing import Optional

from bs4 import Tag

_FILE_EXT = (".pdf", ".xlsx", ".xls", ".csv", ".zip", ".doc", ".docx")

# Anchor / visible text (e.g. "Anesthesia ... - PDF", "(Excel)")
_ANCHOR_HINT = re.compile(
    r"\b(pdf|xlsx|xls|csv|excel|spreadsheet)\b"
    r"|\([^)]*(excel|pdf|xls|xlsx)[^)]*\)"
    r"| -\s*(PDF|Excel)\s*$",
    re.I,
)

# Common non-extension download URLs
_HREF_HINT = re.compile(
    r"(download|getfile|viewfile|fileid|documentid|document|attachment|\.ashx|/file/|"
    r"mediahandler|blob\.|contentdisposition)",
    re.I,
)


def looks_like_file_link(
    href: Optional[str],
    anchor_text: Optional[str],
    a_tag: Optional[Tag] = None,
) -> bool:
    """
    True if this anchor likely points to a downloadable schedule/document.
    Uses extension, anchor text, URL shape, and optional <tr> context (File Type column).
    """
    if not href:
        return False
    h = href.strip()
    if h.startswith("#") or h.lower().startswith("javascript:"):
        return False

    hl = h.lower()
    if any(ext in hl for ext in _FILE_EXT):
        return True

    if _ANCHOR_HINT.search(anchor_text or ""):
        return True

    if _HREF_HINT.search(hl):
        return True

    # Same-row hint: portal tables often have "PDF" / "XLSX" in a sibling cell
    if a_tag is not None and _row_suggests_file_link(a_tag):
        return True

    return False


def _row_suggests_file_link(a_tag: Tag) -> bool:
    tr = a_tag.find_parent("tr")
    if not tr:
        return False
    row = " ".join(
        c.get_text(" ", strip=True).lower()
        for c in tr.find_all(["td", "th"])
    )
    if not row:
        return False
    # Typical fee-schedule listing rows
    if re.search(r"\b(pdf|xlsx|xls|excel|csv)\b", row):
        return True
    return False


def collect_file_link_hrefs(soup) -> list[str]:
    """All anchor hrefs that look like file/document targets."""
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not href:
            continue
        if looks_like_file_link(href, a.get_text(" ", strip=True), a):
            if href not in seen:
                seen.add(href)
                out.append(href)
    return out


def file_type_hints_from_anchor(anchor_text: str) -> list[str]:
    t = (anchor_text or "").lower()
    hints: list[str] = []
    if "pdf" in t:
        hints.append("pdf")
    if "xlsx" in t or "excel" in t or "xls" in t:
        hints.append("xlsx")
    if "csv" in t:
        hints.append("csv")
    return hints or ["unknown"]

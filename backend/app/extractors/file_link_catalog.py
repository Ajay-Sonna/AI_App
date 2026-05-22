"""
Build a synthetic catalog table from direct file-download links under headings.

Use when pages are classified as ``file_repository`` (no usable HTML `<table>`
for schedules) — e.g. NY OMH Medicaid reimbursement landing pages where each
subsection lists Excel/PDF links with optional "File updated MM/DD/YY" notes.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

_FILE_EXTENSIONS = (
    ".pdf",
    ".xlsx",
    ".xls",
    ".csv",
    ".zip",
)
_HEADINGS = frozenset({"h1", "h2", "h3", "h4"})
_UPDATE_PATTERN = re.compile(
    r"file\s+(?:updated|update)\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
    re.I,
)


def _path_has_file_suffix(path_lower: str) -> bool:
    return any(path_lower.endswith(ext) for ext in _FILE_EXTENSIONS)


def _file_type_from_url(url: str) -> str:
    p = urlparse(url).path.lower()
    if p.endswith(".pdf"):
        return "pdf"
    if p.endswith(".xlsx"):
        return "xlsx"
    if p.endswith(".xls"):
        return "xls"
    if p.endswith(".csv"):
        return "csv"
    if p.endswith(".zip"):
        return "zip"
    return "unknown"


def _updated_note_near_anchor(anchor: Tag) -> str:
    parts: list[str] = []
    el: Tag | None = anchor.parent
    for _ in range(5):
        if el is None:
            break
        parts.append(el.get_text(" ", strip=True))
        el = el.parent
    blob = " ".join(parts)
    m = _UPDATE_PATTERN.search(blob)
    return m.group(1) if m else ""


def extract_file_link_catalog(
    html: str,
    base_url: str,
    *,
    max_rows: int = 500,
) -> dict[str, Any]:
    """
    Walk the DOM in depth-first order, track latest heading text, emit one row per
    same-domain file `<a href>` skipping ``<footer>`` / ``role=\"contentinfo\"``.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_netloc = urlparse(base_url).netloc
    section_title = ""

    rows: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for element in soup.find_all(True):  # tree order — headings update context before anchors
        if not isinstance(element, Tag):
            continue

        tag = element.name.lower()
        if tag in _HEADINGS:
            txt = element.get_text(strip=True)
            if txt:
                section_title = txt[:600]
            continue

        if tag != "a" or not element.get("href"):
            continue

        if element.find_parent("footer") or element.find_parent(attrs={"role": "contentinfo"}):
            continue

        raw = (element.get("href") or "").strip()
        if not raw or raw.startswith("#") or raw.lower().startswith("javascript:"):
            continue

        full = urljoin(base_url, raw)
        parsed = urlparse(full)
        if parsed.netloc != base_netloc:
            continue

        path_lower = parsed.path.lower()
        if not _path_has_file_suffix(path_lower):
            continue

        if full in seen_urls:
            continue
        seen_urls.add(full)

        title = element.get_text(" ", strip=True)[:600]
        if not title:
            title = parsed.path.split("/")[-1] or full

        row = {
            "Section": section_title,
            "Title": title,
            "File URL": full,
            "File type": _file_type_from_url(full),
            "Updated": _updated_note_near_anchor(element),
            "_links": [{"url": full, "text": title}],
        }
        rows.append(row)
        if len(rows) >= max_rows:
            break

    columns = ["Section", "Title", "File URL", "File type", "Updated"]

    out: dict[str, Any] = {
        "block_id": "file_repository_links",
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "pages_visited": 1,
        "paginated": False,
        "source": "file_link_catalog",
    }
    return out

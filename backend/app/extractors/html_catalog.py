# backend/app/extractors/html_catalog.py
"""Extract row records from deduped HTML tables (catalog / index pages)."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from app.analyzers.structure_analyzer import infer_catalog_table_split, list_unique_tables
from app.extractors.ca_dwc_omfs import (
    annotate_ca_dwc_row_schedule_metadata,
    infer_section_heading_before_table,
    is_ca_dwc_portal,
    table_looks_like_effective_date_documents,
)


def _split_header_and_body_rows(table: Tag) -> tuple[list[str], list[Tag]]:
    cols, body_trs = infer_catalog_table_split(table)
    return cols, body_trs


def _row_cell_texts(tr: Tag) -> list[str]:
    return [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]


def _align_vals_to_columns(vals: list[str], ncols: int) -> list[str]:
    """
    Many portal tables prepend an icon / selection column with no visible text.
    That yields more <td> nodes than header labels and shifts every value.

    Drop only **leading** empty cells until counts match (or no more leaders).
    """
    v = [("" if x is None else str(x)).strip() for x in vals]
    while len(v) > ncols and v and not v[0].strip():
        v = v[1:]
    if len(v) < ncols:
        v = v + [""] * (ncols - len(v))
    return v[:ncols]


def _is_pagination_control_row(links: list[dict[str, str]], vals: list[str]) -> bool:
    """Skip footer rows that only navigate pages (DNN / ASP.NET list paging)."""
    lowered = [(l.get("url") or "").lower() for l in links]
    for u in lowered:
        if "__dopostback" in u and (
            "nextpage" in u or "nextpagebutton" in u or "pager" in u
        ):
            return True
    joined = " ".join(x.strip() for x in vals if x and x.strip())
    if not joined:
        return False
    if re.search(r"(?i)\bnext\s*>\s*$", joined):
        return True
    if re.fullmatch(r"(?i)\d(?:\s+\d)*\s+next\s*>?", joined.replace("  ", " ").strip()):
        return True
    return False


def _row_links(tr: Tag, base_url: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for a in tr.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        out.append(
            {
                "url": urljoin(base_url, href),
                "text": a.get_text(" ", strip=True),
            }
        )
    return out


def _first_link_with_extension(links: list[dict], suffixes: tuple[str, ...]) -> Optional[str]:
    for ln in links or []:
        u = ((ln.get("url") or "")).strip()
        lu = u.lower()
        if not u:
            continue
        if any(lu.endswith(s) or s in lu for s in suffixes):
            return u
    return None


def _classify_link_url(url: str) -> str:
    """Coarse bucket from URL/query — works across states without caring about column titles."""
    u = (url or "").strip().lower()
    if "javascript:" in u or "__dopostback" in u:
        return "portal"
    if ".pdf" in u or "/pdf" in u:
        return "pdf"
    if ".xlsx" in u:
        return "xlsx"
    if ".xls" in u:
        return "xls"
    if ".csv" in u:
        return "csv"
    if ".zip" in u:
        return "zip"
    if u.startswith("http://") or u.startswith("https://"):
        return "http"
    return "other"


def _column_expects_link_kinds(header: str) -> set[str]:
    """
    Map arbitrary state portal headers → link kinds we can match.
    Generous synonyms so GA/TX/NY/etc. headings still line up without site-specific rules.
    """
    t = (header or "").lower().strip()
    kinds: set[str] = set()
    if not t:
        return kinds

    spreadsheet = (
        "excel",
        "xls",
        "spreadsheet",
        "workbook",
        "csv",
        "download csv",
    )
    pdfish = ("pdf", "acrobat", "adobe")
    archive = ("zip", "archive", "compressed")
    generic_file = (
        "download",
        "attachment",
        "document",
        "file",
        "format",
        "formats",
        "resource",
        "manual",
        "publication",
        "link",
        "links",
    )

    if any(k in t for k in spreadsheet):
        kinds.update({"xls", "xlsx", "csv"})
    if any(k in t for k in pdfish):
        kinds.add("pdf")
    if any(k in t for k in archive):
        kinds.add("zip")
    if any(k in t for k in generic_file):
        kinds.add("any_http")

    return kinds


def _hydrate_placeholder_file_cells(row: dict, columns: list[str]) -> None:
    """
    Many portals put file URLs only on icon links (empty TD text). Copy into the best-matching
    column by header keywords + URL shape — state-agnostic.
    """
    links = row.get("_links")
    if not links:
        return

    kind_by_idx: list[str] = []
    for ln in links:
        kind_by_idx.append(_classify_link_url((ln.get("url") or "")))

    used_link_idx: set[int] = set()

    def _take_first_match(want: set[str]) -> Optional[str]:
        def try_match(include_portal: bool) -> Optional[str]:
            for i, ln in enumerate(links):
                if i in used_link_idx:
                    continue
                url_raw = ((ln.get("url") or "")).strip()
                k = kind_by_idx[i]

                if k == "portal":
                    if not include_portal or "any_http" not in want:
                        continue
                    used_link_idx.add(i)
                    return url_raw

                if "pdf" in want and k == "pdf":
                    used_link_idx.add(i)
                    return url_raw
                if ("xls" in want or "xlsx" in want) and k in ("xls", "xlsx"):
                    used_link_idx.add(i)
                    return url_raw
                if "csv" in want and k == "csv":
                    used_link_idx.add(i)
                    return url_raw
                if "zip" in want and k == "zip":
                    used_link_idx.add(i)
                    return url_raw
                if "any_http" in want and k in ("pdf", "xls", "xlsx", "csv", "zip", "http"):
                    used_link_idx.add(i)
                    return url_raw
            return None

        hit = try_match(include_portal=False)
        if hit:
            return hit
        return try_match(include_portal=True)

    # Pass 1: columns whose titles strongly imply a single file type.
    for col in columns:
        if col not in row:
            continue
        if row.get(col) is not None and str(row.get(col)).strip():
            continue
        want = _column_expects_link_kinds(col)
        if not want:
            continue
        filled = _take_first_match(want)
        if filled:
            row[col] = filled

    # Pass 2: any still-empty columns that broadly mean "download / file".
    loose_cols = [
        c
        for c in columns
        if c in row
        and not str(row.get(c) or "").strip()
        and "any_http" in _column_expects_link_kinds(c)
    ]
    for col in loose_cols:
        filled = _take_first_match({"any_http"})
        if filled:
            row[col] = filled


def extract_deduped_table_catalog(
    html: str,
    table_index: int,
    base_url: str,
) -> dict:
    """
    One catalog table → { "columns": [...], "rows": [ {col: val, ...}, ... ] }.

    Rows may include "_links": [{ "url", "text" }, ...] when anchors exist in that row.
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = list_unique_tables(soup)
    if table_index < 0 or table_index >= len(tables):
        return {
            "columns": [],
            "rows": [],
            "error": f"table_index {table_index} out of range (0–{len(tables) - 1})",
        }

    columns, body_trs = _split_header_and_body_rows(tables[table_index])
    if not columns:
        return {"columns": [], "rows": [], "error": "could not detect header row"}
    if len(columns) > 40:
        return {
            "columns": [],
            "rows": [],
            "error": "header row looks unreliable (too many columns); try a different table",
        }

    ca_dwc_section = ""
    if is_ca_dwc_portal(base_url) and table_looks_like_effective_date_documents(columns):
        ca_dwc_section = infer_section_heading_before_table(tables[table_index])

    rows_out: list[dict] = []
    for tr in body_trs:
        vals = _row_cell_texts(tr)
        if not any(vals):
            continue
        links = _row_links(tr, base_url)
        if _is_pagination_control_row(links, vals):
            continue
        vals = _align_vals_to_columns(vals, len(columns))
        row: dict = {}
        for j, col in enumerate(columns):
            row[col] = vals[j] if j < len(vals) else ""
        if links:
            row["_links"] = links
        _hydrate_placeholder_file_cells(row, columns)
        rows_out.append(row)

    if ca_dwc_section:
        for row in rows_out:
            if isinstance(row, dict):
                annotate_ca_dwc_row_schedule_metadata(row, ca_dwc_section)

    return {"columns": columns, "rows": rows_out}


def parse_table_block_index(block_id: str) -> Optional[int]:
    if not block_id.startswith("table_"):
        return None
    try:
        return int(block_id.split("_", 1)[1])
    except (IndexError, ValueError):
        return None


def catalog_row_signature(row: dict) -> tuple:
    """Stable dedupe key for catalog rows (column values + link URLs)."""
    cells = tuple((k, row[k]) for k in sorted(row.keys()) if k != "_links")
    link_urls = tuple(
        sorted(l.get("url", "") for l in (row.get("_links") or []))
    )
    return (cells, link_urls)

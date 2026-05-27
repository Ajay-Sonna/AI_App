"""
Collect direct file download URLs from ``run_pipeline`` / catalog output for local persistence.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from app.catalog.row_labels import (
    compose_catalog_row_display_label,
    fee_schedule_title_from_row,
    guess_effective_date_from_link_text,
    guess_portal_date_str,
    ordered_column_names,
    row_or_label_superseded_hint,
    slug_logical_schedule_key,
)

logger = logging.getLogger(__name__)

_FILE_HINT = (
    ".pdf",
    ".xlsx",
    ".xls",
    ".csv",
    ".zip",
    ".docx",
    ".doc",
)


def normalize_persistable_url(url: str) -> str:
    """
    Canonical form for comparing portal-discovered URLs with stored ``source_url`` rows
    (trailing slash, fragment; scheme/host lowercased; sorted query keys).
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    p = urlparse(raw)
    scheme = (p.scheme or "https").lower()
    netloc = (p.netloc or "").lower()
    path = (p.path or "").rstrip("/")
    q = ""
    if p.query:
        pairs = parse_qsl(p.query, keep_blank_values=True)
        pairs.sort(key=lambda kv: (kv[0], kv[1]))
        q = urlencode(pairs)
    return urlunparse((scheme, netloc, path, "", q, ""))


def _abs_url(base: str, href: str) -> str:
    h = (href or "").strip()
    if not h:
        return ""
    if h.startswith("http://") or h.startswith("https://"):
        return h
    if h.startswith("javascript:") or "__dopostback" in h.lower():
        return ""
    if not base:
        return ""
    return urljoin(base.rstrip("/") + "/", h)


def _looks_like_file_download(url: str) -> bool:
    u = (url or "").strip().lower()
    if not u.startswith("http"):
        return False
    path = urlparse(u).path.lower()
    if any(path.endswith(ext) for ext in _FILE_HINT):
        return True
    # ServiceNow guest-friendly attachment streaming (path often ends with sys_id, no extension).
    if "/pubatt/dl/" in u or "pubatt/dl/" in path:
        return True
    if "/api/now/attachment/" in u and "/file" in u:
        return True
    if "sys_attachment.do" in u or "attachment.do" in path:
        return True
    if "format=pdf" in u or "filetype=pdf" in u or "type=pdf" in u:
        return True
    if "contenttype=application/pdf" in u or "content-type=application/pdf" in u:
        return True
    if ".ashx" in path and ("pdf" in u or "xlsx" in u or "xls" in u or "csv" in u):
        return True
    if "getfile" in path or "download.aspx" in path or "filedownload" in path:
        return True
    # Drupal-style public files, and export endpoints that declare format in the query string.
    if "/media/" in path and any(ext in path for ext in _FILE_HINT):
        return True
    if "export" in path and any(x in u for x in ("format=pdf", "format=xlsx", "type=pdf", "type=xlsx")):
        return True
    return False


def collect_file_urls_from_pipeline_result(
    payload: Dict[str, Any],
    *,
    base_url: str,
) -> List[Dict[str, str]]:
    """
    Walk ``catalog_tables`` rows and ``_links`` for HTTP(S) URLs that look like downloadable files.

    Returns deduped list of dicts with at least ``url`` and ``label``; also ``logical_schedule_key``
    (stable folder slug) and optional ``portal_date`` (YYYY-MM-DD) for versioning / filenames.
    """
    seen: Set[str] = set()
    out: List[Dict[str, str]] = []
    base = (base_url or "").strip()

    def _add(u: str, label: Optional[str], row: Dict[str, Any], table: Dict[str, Any], *, link: Optional[Dict[str, Any]] = None) -> None:
        u = (u or "").strip()
        if not u or u in seen:
            return
        if not _looks_like_file_download(u):
            return
        seen.add(u)
        cols = ordered_column_names(table)
        link_text = (label or "").strip()
        row_title = fee_schedule_title_from_row(row, cols) or ""
        fee_topic_slug = str(row.get("_fee_topic_slug") or "").strip()
        link_d = guess_effective_date_from_link_text(link_text) or ""
        row_d = guess_portal_date_str(row, cols) or ""
        portal_date = link_d or row_d
        display = compose_catalog_row_display_label(
            row=row,
            cols=cols,
            portal_date_iso=portal_date.strip()[:10] if portal_date else None,
            fallback_link_label=link_text or None,
        )
        src = ""
        if link_d:
            src = "link_text"
        elif row_d:
            src = "row_cell"

        if fee_topic_slug:
            # One stable key per DWC section — do not derive from each document anchor.
            lsk = fee_topic_slug[:256]
        else:
            lt_norm = link_text.strip().lower()
            link_is_generic = lt_norm in ("", "download", "download file", "click here", "here", "file")
            if row_title and link_is_generic:
                lsk = slug_logical_schedule_key(row_title)
            elif link_text and not link_is_generic:
                lsk = slug_logical_schedule_key(link_text)
            elif row_title:
                lsk = slug_logical_schedule_key(row_title)
            else:
                lsk = slug_logical_schedule_key("fee_schedule")

        superseded = row_or_label_superseded_hint(row=row, link_label=link_text)
        if isinstance(link, dict) and str(link.get("superseded_hint") or "").strip() in (
            "1",
            "true",
            "yes",
        ):
            superseded = True
        artifact_slot = ""
        if isinstance(link, dict):
            artifact_slot = str(link.get("_artifact_slot") or "").strip()[:32]
        payload_entry: Dict[str, str] = {
            "url": u,
            "label": link_text[:400] if link_text else (row_title[:400] if row_title else "")[:400],
            "catalog_display_label": (display.strip()[:512] if display.strip() else ""),
            "logical_schedule_key": lsk[:256],
            "portal_date": portal_date[:32],
            "effective_date_source": src[:32],
            "superseded_hint": "1" if superseded else "",
        }
        if artifact_slot:
            payload_entry["artifact_slot"] = artifact_slot
        out.append(payload_entry)

    for table in payload.get("catalog_tables") or []:
        if not isinstance(table, dict):
            continue
        for row in table.get("rows") or []:
            if not isinstance(row, dict):
                continue
            for link in row.get("_links") or []:
                if not isinstance(link, dict):
                    continue
                raw = (link.get("url") or "").strip()
                lab = (link.get("text") or link.get("label") or "")[:400]
                u = _abs_url(base, raw)
                _add(u, lab, row, table, link=link if isinstance(link, dict) else None)
            for _k, val in row.items():
                if isinstance(_k, str) and _k.startswith("_"):
                    continue
                if isinstance(val, str) and val.strip().startswith(("http://", "https://")):
                    u = _abs_url(base, val.strip())
                    _add(u, None, row, table)
    return out


def _row_looks_like_published_file_row(row: Dict[str, Any]) -> bool:
    """Catalog index row that likely represents a downloadable fee document (not paging chrome)."""
    if not isinstance(row, dict):
        return False
    keys_lower = " ".join(str(k).strip().lower() for k in row if isinstance(k, str) and not k.startswith("_"))
    if "effective date" in keys_lower and row.get("_links"):
        # California DWC OMFS9914-style “Effective date | … documents” tables
        return True
    title = str(row.get("Title") or row.get("title") or "").strip()
    low = title.lower()
    if not title or low.startswith("< previous") or "previous 1" in low:
        return False
    if re.fullmatch(r"(?i)\s*\d+\s*", title):
        return False
    ft = str(row.get("File Type") or row.get("file type") or "").strip().upper()
    if ft and any(x in ft for x in ("PDF", "XLS", "CSV", "ZIP", "EXCEL", "WORD", "DOC")):
        return True
    if any(k in low for k in ("fee schedule", "crosswalk", "allowable", "zipped fee", "base unit", "drug list")):
        return True
    return False


def summarize_artifact_link_availability(
    payload: Dict[str, Any],
    *,
    base_url: str,
) -> Dict[str, Any]:
    """
    Explain why ``artifact_download_candidates`` may be zero even when the catalog has many rows.

    Sites like Georgia MMIS (DNN + ASP.NET) expose ``javascript:__doPostBack(...)`` links; those are
    not HTTP file URLs, so we cannot auto-download without a browser postback / session capture pass.
    """
    base = (base_url or "").strip()
    postback_only_file_rows = 0
    http_file_rows = 0
    catalog_file_like_rows = 0

    for table in payload.get("catalog_tables") or []:
        if not isinstance(table, dict):
            continue
        for row in table.get("rows") or []:
            if not isinstance(row, dict):
                continue
            links = row.get("_links") or []
            if not links:
                continue
            if not _row_looks_like_published_file_row(row):
                continue
            catalog_file_like_rows += 1
            has_http = False
            has_post = False
            for link in links:
                if not isinstance(link, dict):
                    continue
                raw = (link.get("url") or "").strip()
                u = _abs_url(base, raw)
                if u and _looks_like_file_download(u):
                    has_http = True
                lu = raw.lower()
                if "javascript:" in lu or "__dopostback" in lu:
                    has_post = True
            if has_http:
                http_file_rows += 1
            elif has_post:
                postback_only_file_rows += 1

    user_message: Optional[str] = None
    ga_meta = payload.get("mmis_ga_postback_resolve")
    ga_resolved = 0
    if isinstance(ga_meta, dict):
        try:
            ga_resolved = int(ga_meta.get("urls_resolved") or 0)
        except (TypeError, ValueError):
            ga_resolved = 0

    if ga_resolved > 0:
        user_message = (
            f"Browser replay resolved {ga_resolved} Georgia MMIS postback link(s) into direct HTTPS file URLs "
            "(see mmis_ga_postback_resolve)."
        )
        if postback_only_file_rows:
            user_message += (
                f" About {postback_only_file_rows} catalog row(s) still look postback-only or were unmatched."
            )

    elif catalog_file_like_rows and postback_only_file_rows and http_file_rows == 0:
        user_message = (
            f"This portal lists {postback_only_file_rows} fee file row(s) that use ASP.NET postbacks "
            "(javascript:__doPostBack) instead of direct http(s) file links. "
            "For Georgia MMIS we can auto-resolve a bounded batch when GA_MMIS_POSTBACK_RESOLVE=true; "
            "for other portals, open each download in the browser or add a portal adapter."
        )
    elif catalog_file_like_rows == 0 and not payload.get("blocked"):
        user_message = (
            "No catalog rows with recognizable file links were found. "
            "The page may use a format we do not parse yet, or fee files may live behind extra navigation."
        )

    return {
        "catalog_file_like_rows": catalog_file_like_rows,
        "rows_with_http_download_urls": http_file_rows,
        "rows_with_postback_only_links": postback_only_file_rows,
        "user_message": user_message,
    }

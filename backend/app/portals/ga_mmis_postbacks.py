"""
Georgia MMIS (DNN + ASP.NET) fee publication lists expose downloads as javascript:__doPostBack(...)
``Select`` links. The generic crawler records table text but cannot download until those postbacks fire.

When enabled, this module replays pagination in Chromium, clicks eligible ``Select`` links, and attaches
discovered HTTPS document URLs to the matching catalog row's ``_links``.

This is deliberately **hosts-scoped + bounded**: not a universal autopilot for every postback-heavy site.
Tune with ``GA_MMIS_POSTBACK_RESOLVE_MAX`` and ``GA_MMIS_POSTBACK_RESOLVE_WALL_S``.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from app.config.settings import (
    GA_MMIS_POSTBACK_RESOLVE_ENABLED,
    GA_MMIS_POSTBACK_RESOLVE_MAX,
    GA_MMIS_POSTBACK_RESOLVE_WALL_S,
)
from app.extractors.html_catalog import _align_vals_to_columns, parse_table_block_index
from app.extractors.paginated_catalog import _find_next_control

logger = logging.getLogger(__name__)

# Bounds so resolution cannot scan thousands of nested ``<tr>`` rows or chase pagination forever.
_GA_MMIS_MAX_PAGINATION_ROUNDS = 45
_GA_MMIS_MAX_TR_SCAN_PER_PAGE = 130
_GA_MMIS_MAX_STAGNANT_PAGES = 14

# Faster fail than ASP.NET navigations hitting the full Playwright defaults (often minutes per row).
_NAV_WAIT_MS = 18_000
_CLICK_WAIT_MS = 15_000
_CELL_TEXT_MS = 4_500
_BACK_RELOAD_WAIT_MS = 35_000
_DOWNLOAD_WAIT_MS = 24_000
_POST_CLICK_SETTLE_MS = 3_200


def _is_mmis_georgia_host(hostname: str) -> bool:
    """Match ``mmis.georgia.gov`` and ``www.mmis.georgia.gov`` (and other ``*.mmis.georgia.gov``)."""
    h = (hostname or "").lower().rstrip(".")
    return h == "mmis.georgia.gov" or h.endswith(".mmis.georgia.gov")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _normalized_host_path(u: str) -> tuple[str, str]:
    """Lowercased hostname + decoded pathname (detects ASP.NET shells that recycle the listing path)."""
    p = urlparse((u or "").strip())
    host = (p.hostname or "").lower()
    raw = (p.path or "").replace("\\", "/").strip()
    path = unquote(raw).rstrip("/").lower()
    return host, path


def _is_portal_listing_echo_url(candidate: str, portal_entry_url: str) -> bool:
    """``Default.aspx`` re-post replies often leave the visible URL unchanged — not a downloadable asset."""
    c = (candidate or "").strip()
    if not c.startswith(("http://", "https://")):
        return True
    h1, path1 = _normalized_host_path(c)
    h2, path2 = _normalized_host_path(portal_entry_url)
    if not h1 or not h2 or h1 != h2:
        return False
    return bool(path2) and path1 == path2


def _looks_like_direct_mmis_attachment_url(candidate: str) -> bool:
    """Lightweight heuristic for real file/handler URLs (not the Fee Schedules shell)."""
    u = (candidate or "").strip()
    if not u.startswith(("http://", "https://")):
        return False
    lu = u.lower()
    if any(lu.endswith(s) for s in (".pdf", ".xlsx", ".xls", ".csv", ".zip", ".doc", ".docx")):
        return True
    if any(tok in lu for tok in ("attachment", "download", "mediahandler", "/desktopmodules/", "/resources/", "/documents/", "/documentslibrary/", "/providers/", "/api/")):
        return True
    ql = urlparse(u).query.lower()
    if any(tok in ql for tok in ("fileid=", "cd=", "contentid=", "documentid=", "mediaid=", "mid=")):
        return True
    return False


def _is_acceptable_mmis_resolve_url(resolved: str, portal_entry_url: str) -> bool:
    if not resolved.startswith(("http://", "https://")):
        return False
    if "__dopostback" in resolved.lower():
        return False
    if _is_portal_listing_echo_url(resolved, portal_entry_url) and not _looks_like_direct_mmis_attachment_url(resolved):
        return False
    return True


def _row_has_only_postback_downloads(row: dict[str, Any]) -> bool:
    links = row.get("_links") or []
    if not links:
        return False
    for ln in links:
        u = (ln.get("url") or "").strip().lower()
        if u.startswith(("http://", "https://")):
            return False
    return any("__dopostback" in (ln.get("url") or "").lower() for ln in links)


def _row_needs_ga_mmis_postback_resolve(row: dict[str, Any], portal_entry_url: str) -> bool:
    """
    Row should be queued for Chromium replay:

    - only ``javascript:__doPostBack`` links (original case), OR
    - HTTPS links that are bogus echoes of the listing shell (prior bad resolves).
    """
    if _row_has_only_postback_downloads(row):
        return True
    portal = (portal_entry_url or "").strip()
    if not portal:
        return False
    links = row.get("_links") or []
    http_like = [(ln.get("url") or "").strip() for ln in links if isinstance(ln, dict)]
    if not http_like:
        return False
    if any("__dopostback" in u.lower() for u in http_like):
        return False
    return all(u.startswith(("http://", "https://")) and _is_portal_listing_echo_url(u, portal) for u in http_like)


def _is_select_attachment_postback(url: str) -> bool:
    u = url.lower().strip()
    if "javascript:" not in u:
        return False
    if "__dopostback" not in u:
        return False
    if "select" not in u:
        return False
    if any(tok in u for tok in ("prevpagebutton", "nextpagebutton", "$pager")):
        return False
    return True


def _normalize_catalog_compare_value(raw: str) -> str:
    """
    Harmonize fingerprints between BeautifulSoup-derived rows and Playwright ``inner_text``.

    ASP.NET/HTML often emits doubled quotes (``''``) vs typographic apostrophes; portals use NBSP / odd spaces.
    """
    t = unicodedata.normalize("NFKC", (raw or ""))
    t = t.replace("\u00a0", " ").replace("\xa0", " ")
    t = " ".join(t.split()).strip().lower()
    # Common HTML / SQL-ish doubling in scraped titles vs DOM text:
    while "''" in t:
        t = t.replace("''", "'", 1)
    t = (
        t.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u2032", "'")
        .replace("&amp;", "&")
        .replace("&nbsp;", " ")
    )
    t = " ".join(t.split())
    return t


def _canonical_row_pairs(row: Dict[str, Any]) -> tuple[tuple[str, str], ...]:
    pairs = [
        (str(k).strip().lower(), _normalize_catalog_compare_value(str(row.get(k) or "")))
        for k in sorted(row.keys())
        if k != "_links"
    ]
    return tuple(kv for kv in pairs if kv[1])


def _normalize_pw_cell_text(raw: str) -> str:
    """Same fingerprint rules as HTML rows (see ``_normalize_catalog_compare_value``)."""
    return _normalize_catalog_compare_value(raw)


def _canonical_from_pw_cells(columns: List[str], vals: List[str]) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for i, col in enumerate(columns):
        if i >= len(vals):
            break
        v = _normalize_pw_cell_text(vals[i])
        if not v:
            continue
        pairs.append((col.strip().lower(), v))
    return tuple(sorted(pairs))


def _mmis_publication_grid_table(page: Any, nth_fallback_index: int) -> tuple[Any, str]:
    """
    Dedup-aware table targeting:

    Static HTML catalogs use ``list_unique_tables()`` (fingerprints remove duplicate shells).

    Live Playwright indexing via ``nth(i)`` counts **every** ``<table>`` in DOM order, which
    often disagrees with the dedup ``table_*`` ids. Publication grids can be matched directly
    from their DNN PublicationList ``Select`` postback anchors.

    Falls back to ``nth(nth_fallback_index)`` best-effort if no publication grid detected.
    """
    try:
        scoped = page.locator("table").filter(has=page.locator("a[href*='PublicationListPage'][href*='Select']"))
        nscoped = scoped.count()
    except Exception:
        scoped = None
        nscoped = 0

    if nscoped and scoped is not None:
        # Playwright Python: ``first`` is a property (``.first()`` raises TypeError).
        best_tbl = scoped.first
        best_hits = -1
        for i in range(min(nscoped, 40)):
            t = scoped.nth(i)
            try:
                hits = t.locator("a[href*='__doPostBack'][href*='Select']").count()
            except Exception:
                hits = 0
            if hits > best_hits:
                best_hits = hits
                best_tbl = t
        chosen = (
            "publicationlist_select_postbacks"
            if best_hits >= 3
            else "publicationlist_select_postbacks(few_hits;_verify_portal_dom)"
        )
        return best_tbl, chosen

    return page.locator("table").nth(nth_fallback_index), f"raw_nth_fallback({nth_fallback_index})"


def _listing_tbody_rows(table_loc: Any) -> Any:
    """Top-level listing rows under this ``<table>`` only — not nested grids inside cells."""
    try:
        by_tbody = table_loc.locator("xpath=./tbody/tr")
        if int(by_tbody.count() or 0) > 0:
            return by_tbody
    except Exception:
        pass
    try:
        by_tr = table_loc.locator("xpath=./tr")
        if int(by_tr.count() or 0) > 0:
            return by_tr
    except Exception:
        pass
    return table_loc.locator("tr")


def _pick_target_table_with_postbacks(
    catalog_tables: List[Dict[str, Any]],
    portal_entry_url: str = "",
) -> tuple[Dict[str, Any] | None, int]:
    best_tbl: Dict[str, Any] | None = None
    best_need = -1
    best_idx = 0
    portal = (portal_entry_url or "").strip()
    for tbl in catalog_tables:
        if not isinstance(tbl, dict):
            continue
        rows = tbl.get("rows") or []
        need = sum(
            1 for r in rows if isinstance(r, dict) and _row_needs_ga_mmis_postback_resolve(r, portal)
        )
        if need <= best_need:
            continue
        bid = str(tbl.get("block_id") or "")
        ix = parse_table_block_index(bid)
        if ix is None:
            ix = 0
        best_need = need
        best_tbl = tbl
        best_idx = ix
    if best_tbl is None or best_need < 3:
        return None, best_idx
    return best_tbl, best_idx


_FILE_TYPEISH = re.compile(
    r"\b(pdf|xlsx?|docx?|csv|zip|txt|htm|html|xml|rtf)\b",
    re.I,
)


def _row_looks_like_publication_entry(columns_ref: List[str], vals_aligned: List[str]) -> bool:
    """
    Skip DNN pager / chrome rows that still carry ``$Select``-looking anchors.

    Real publication rows on MMIS include a recognizable file type token.
    """
    blob = " ".join(_normalize_catalog_compare_value(v) for v in vals_aligned if v and str(v).strip())
    if _FILE_TYPEISH.search(blob):
        return True
    col_to_val: Dict[str, str] = {}
    for i, col in enumerate(columns_ref):
        if i < len(vals_aligned):
            col_to_val[str(col).strip().lower()] = vals_aligned[i]
    for key in ("file type", "type", "format"):
        ft = col_to_val.get(key, "")
        if ft and len(_normalize_catalog_compare_value(ft)) >= 2:
            # "pdf", "ms word", ...
            lowered = _normalize_catalog_compare_value(ft)
            if lowered in ("pdf", "xlsx", "xls", "csv", "doc", "docx", "zip", "txt"):
                return True
            if lowered[:3] == "pdf" or "pdf" in lowered:
                return True
    titleish = ""
    for k in ("title", "document", "name", "publication"):
        if k in col_to_val:
            titleish = str(col_to_val.get(k) or "")
            break
    if titleish and "." in titleish:
        suf = titleish.rsplit(".", 1)[-1].lower()
        if len(suf) <= 5 and suf.isalpha() and suf in {"pdf", "xlsx", "xls", "csv", "doc", "docx", "zip"}:
            return True
    return False


def _pick_resolve_url(nav_url: str, captured: List[str], portal_entry_url: str = "") -> str:
    portal = (portal_entry_url or "").strip()
    nu = nav_url.strip()
    nl = nu.lower()

    # Prefer URLs that look like real documents/handlers rather than ASP.NET shells.
    for u in reversed(captured):
        if u.startswith("http"):
            lu = u.lower()
            if any(x in lu for x in (".pdf", ".xlsx", ".xls", ".csv", ".docx")):
                return u
            if any(tok in lu for tok in ("attachment", "download", "/media/", "mediahandler")):
                return u
            if lu != nl and portal and not _is_portal_listing_echo_url(u, portal):
                return u

    if nu.startswith("http"):
        if any(x in nl for x in (".pdf", ".xlsx", ".xls", ".csv", ".docx")):
            return nu
        if "attachment" in nl or "/media/" in nl or "mediahandler" in nl:
            return nu

    # Never latch onto the untouched listing URL unless we positively saw attachment-like traffic above.
    if nu.startswith("http") and portal and _is_portal_listing_echo_url(nu, portal):
        return ""
    return nu if nu.startswith("http") else ""


def _maybe_remove_response_handler(page: Any, handler: Any) -> None:
    for meth in ("remove_listener", "off"):
        remover = getattr(page, meth, None)
        if callable(remover):
            try:
                remover("response", handler)
                return
            except Exception:
                continue


_DOPOSTBACK_ARG_RE = re.compile(r"__doPostBack\s*\(\s*['\"]([^'\"]*)['\"]", re.I)


def _dopostback_first_argument(href: str) -> str:
    """First argument inside ``javascript:__doPostBack(...)`` (event target token)."""
    if not href:
        return ""
    um = href if href.startswith("javascript:") else href
    m = _DOPOSTBACK_ARG_RE.search(unquote(um))
    raw = (m.group(1) if m else "").strip()
    return raw.replace("%24", "$").replace("\\'", "'")


def _catalog_primary_postback(row: Dict[str, Any]) -> str:
    for ln in row.get("_links") or []:
        if isinstance(ln, dict):
            t = _dopostback_first_argument(str(ln.get("url") or ""))
            if t:
                return t
    return ""


def _catalog_display_title_normalized(row: Dict[str, Any]) -> str:
    for k in ("Title", "title", "Document", "document", "Name", "name", "Publication", "publication"):
        if k in row:
            raw = row.get(k)
            if raw is None:
                continue
            s = str(raw).strip()
            if s:
                return _normalize_catalog_compare_value(s)
    pairs = [(str(a).strip().lower(), _normalize_catalog_compare_value(str(b or ""))) for a, b in row.items()]
    best = ""
    for _, vv in pairs:
        if vv and len(vv) > len(best) and _FILE_TYPEISH.search(vv) is None:
            if re.fullmatch(r"[\d.,]+\s*", vv):
                continue
            best = vv
    return best


_DT_SLASH = re.compile(r"^\s*\d{1,2}/\d{1,2}/\d{4}\s*$")


def _title_from_pw_row(columns_ref: List[str], vals_aligned: List[str]) -> str:
    col_ix = {str(c).strip().lower(): i for i, c in enumerate(columns_ref)}
    for key in ("title", "document", "name", "publication"):
        i = col_ix.get(key)
        if i is None or i >= len(vals_aligned):
            continue
        raw = vals_aligned[i]
        if raw and str(raw).strip():
            return str(raw).strip()
    return ""


def _guess_title_from_td_texts(vals: List[str]) -> str:
    """
    When column alignment misses, pick the richest non-metadata cell text as the publication title.

    Skips KB sizes, slash dates, and lone file-type tokens.
    """
    best = ""
    for cell in vals:
        s = " ".join((cell or "").replace("\u00a0", " ").split()).strip()
        if not s:
            continue
        low = _normalize_catalog_compare_value(s)
        if not low:
            continue
        if low in {"pdf", "xlsx", "xls", "csv", "doc", "docx"}:
            continue
        if _DT_SLASH.match(s):
            continue
        if re.fullmatch(r"[\d.,]+\s*$", low.replace(",", "")):
            continue
        if len(s) <= 3:
            continue
        if len(s) > len(best):
            best = s
    return best


def _resolve_row_key_for_playwright_row(
    remaining_by_key: Dict[tuple[tuple[str, str], ...], Dict[str, Any]],
    rk_cells: tuple[tuple[str, str], ...],
    href_try: str,
    pw_title_candidates: List[str],
) -> Optional[tuple[tuple[str, str], ...]]:
    row_obj = remaining_by_key.get(rk_cells)
    if row_obj is not None:
        return rk_cells
    pb_pw = _dopostback_first_argument(href_try)
    if not pb_pw:
        return None
    title_norms: List[str] = []
    seen: set[str] = set()
    for t in pw_title_candidates:
        n = _normalize_catalog_compare_value(t)
        if n and n not in seen:
            seen.add(n)
            title_norms.append(n)
    if not title_norms:
        return None

    exact_hits: List[tuple[tuple[str, str], ...]] = []
    substr_hits: List[tuple[tuple[str, str], ...]] = []
    for rk_left, brow in remaining_by_key.items():
        cat_pb = _catalog_primary_postback(brow)
        if cat_pb and cat_pb != pb_pw:
            continue
        rn = _catalog_display_title_normalized(brow)
        if not rn:
            continue
        if any(tn == rn for tn in title_norms):
            exact_hits.append(rk_left)
            continue
        if any(tn in rn or rn in tn for tn in title_norms):
            substr_hits.append(rk_left)

    if len(exact_hits) == 1:
        return exact_hits[0]
    if not exact_hits and len(substr_hits) == 1:
        return substr_hits[0]
    return None


def _first_select_postback_anchor(tr: Any) -> Any | None:
    """Return first ``<a>`` whose href looks like an ASP.NET document ``Select`` postback."""
    try:
        n = tr.locator("a").count()
    except Exception:
        return None
    for ji in range(min(n, 80)):
        a = tr.locator("a").nth(ji)
        try:
            h = a.get_attribute("href") or ""
        except Exception:
            continue
        if _is_select_attachment_postback(h):
            return a
    return None


def maybe_resolve_ga_mmis_postbacks(url: str, catalog_tables: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Mutate GA MMIS catalogs in-place when possible.

    Returns a small meta dict merged into `/run`.
    """
    meta: Dict[str, Any] = {
        "enabled": GA_MMIS_POSTBACK_RESOLVE_ENABLED,
        "eligible_mmis_ga_host": False,
        "attempted_host": False,
        "skipped_reason": "",
        "table_index_used": None,
        "playwright_table_source": None,
        "urls_resolved": 0,
        "rows_pending_start": 0,
        "rows_pending_end": None,
        "pages_visited": 0,
        "pagination_round_cap": _GA_MMIS_MAX_PAGINATION_ROUNDS,
        "tbody_row_scan_cap": _GA_MMIS_MAX_TR_SCAN_PER_PAGE,
        "stopped_due_to_pagination_cap": False,
        "stopped_due_to_stagnant_pages": False,
    }

    host = _host(url)
    if _is_mmis_georgia_host(host):
        meta["eligible_mmis_ga_host"] = True

    if not GA_MMIS_POSTBACK_RESOLVE_ENABLED:
        meta["skipped_reason"] = "GA_MMIS_POSTBACK_RESOLVE disabled (env)."
        return meta

    if not _is_mmis_georgia_host(host):
        meta["skipped_reason"] = f"not_mmis_ga_host (hostname was {host!r})"
        return meta

    if not catalog_tables:
        meta["attempted_host"] = True
        meta["skipped_reason"] = "no_catalog_tables_to_scan"
        return meta

    tgt_tbl, table_index = _pick_target_table_with_postbacks(catalog_tables, url)
    meta["attempted_host"] = True
    meta["table_index_used"] = table_index
    if tgt_tbl is None:
        meta["skipped_reason"] = "no_postback-heavy_table_found"
        return meta

    rows = tgt_tbl.get("rows") or []
    pending_before = sum(
        1 for r in rows if isinstance(r, dict) and _row_needs_ga_mmis_postback_resolve(r, url)
    )
    meta["rows_pending_start"] = pending_before
    if pending_before == 0:
        meta["skipped_reason"] = "nothing_to_resolve"
        return meta

    remaining_by_key: Dict[tuple[tuple[str, str], ...], Dict[str, Any]] = {}
    for r in rows:
        if not isinstance(r, dict) or not _row_needs_ga_mmis_postback_resolve(r, url):
            continue
        remaining_by_key[_canonical_row_pairs(r)] = r

    columns_ref: List[str] = list((tgt_tbl.get("columns") or []))

    urls_resolved = 0
    t_wall = time.monotonic()
    pages_seen = 0
    stagnant_iterations = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = context.new_page()
            page.set_default_timeout(90_000)
            page.set_default_navigation_timeout(60_000)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                page.goto(url, wait_until="commit", timeout=60_000)
            try:
                page.wait_for_selector("table", timeout=25000)
            except Exception:
                pass
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            page.wait_for_timeout(6500)

            while (
                urls_resolved < GA_MMIS_POSTBACK_RESOLVE_MAX
                and remaining_by_key
                and time.monotonic() - t_wall < GA_MMIS_POSTBACK_RESOLVE_WALL_S
                and pages_seen < _GA_MMIS_MAX_PAGINATION_ROUNDS
            ):
                pages_seen += 1
                tbl_pw, pw_src = _mmis_publication_grid_table(page, table_index)
                if pages_seen == 1:
                    meta["playwright_table_source"] = pw_src

                rows_loc = _listing_tbody_rows(tbl_pw)
                nrow = int(rows_loc.count() or 0)
                n_scan = max(0, min(nrow, _GA_MMIS_MAX_TR_SCAN_PER_PAGE))
                before_remaining = len(remaining_by_key)

                for ri in range(n_scan):
                    if urls_resolved >= GA_MMIS_POSTBACK_RESOLVE_MAX:
                        break
                    if time.monotonic() - t_wall > GA_MMIS_POSTBACK_RESOLVE_WALL_S:
                        break

                    tr = rows_loc.nth(ri)
                    cand = _first_select_postback_anchor(tr)
                    if cand is None:
                        continue
                    href_try = ""
                    try:
                        href_try = cand.get_attribute("href") or ""
                    except Exception:
                        continue
                    if not _is_select_attachment_postback(href_try):
                        continue

                    vals: List[str] = []
                    nc = tr.locator("td, th").count()
                    for jc in range(nc):
                        cell = tr.locator("td, th").nth(jc)
                        try:
                            vals.append(cell.inner_text(timeout=_CELL_TEXT_MS))
                        except Exception:
                            vals.append("")

                    if not vals or not columns_ref:
                        continue

                    vals_aligned = [
                        (" ".join((v or "").replace("\u00a0", " ").replace("\xa0", " ").split())).strip()
                        for v in _align_vals_to_columns(vals, len(columns_ref))
                    ]
                    if not _row_looks_like_publication_entry(columns_ref, vals_aligned):
                        continue

                    rk_cells = _canonical_from_pw_cells(columns_ref, vals_aligned)
                    pw_title_candidates = [
                        t
                        for t in (
                            _title_from_pw_row(columns_ref, vals_aligned),
                            _guess_title_from_td_texts(vals_aligned),
                            _guess_title_from_td_texts(vals),
                        )
                        if t and str(t).strip()
                    ]
                    rk_final = _resolve_row_key_for_playwright_row(
                        remaining_by_key, rk_cells, href_try, pw_title_candidates
                    )
                    if rk_final is None:
                        continue
                    row_obj = remaining_by_key.get(rk_final)
                    if row_obj is None:
                        continue

                    captured: List[str] = []

                    def handler(resp):  # noqa: ANN001
                        try:
                            if resp.status >= 400:
                                return
                            ru = getattr(resp, "url", "") or ""
                            if not ru.startswith("http"):
                                return
                            cd = (resp.headers.get("content-disposition") or "").lower()
                            ct = (resp.headers.get("content-type") or "").lower()
                            if "attachment" in cd:
                                captured.append(ru)
                                return
                            if any(
                                tok in ct
                                for tok in ("pdf", "spreadsheetml", "ms-excel", "excel", "csv", "octet-stream")
                            ):
                                captured.append(ru)
                                return
                            if ru.lower().endswith((".pdf", ".xlsx", ".xls", ".csv", ".zip")):
                                captured.append(ru)
                        except Exception:
                            pass

                    page.on("response", handler)
                    pre_click = page.url
                    navigation_happened = False
                    download_obj = None
                    navigated_here = False
                    clicked_once = False

                    try:
                        try:
                            with page.expect_download(timeout=_DOWNLOAD_WAIT_MS) as dl_info:
                                cand.click(timeout=_CLICK_WAIT_MS)
                                clicked_once = True
                            download_obj = dl_info.value
                        except PlaywrightTimeoutError:
                            # Single click likely already fired; capture network / navigation fallout.
                            if not clicked_once:
                                try:
                                    with page.expect_navigation(timeout=_NAV_WAIT_MS):
                                        cand.click(timeout=_CLICK_WAIT_MS)
                                    navigated_here = True
                                    clicked_once = True
                                except Exception:
                                    try:
                                        cand.click(timeout=_CLICK_WAIT_MS)
                                        clicked_once = True
                                    except Exception as ex_click:
                                        logger.debug("GA MMIS row click failed: %s", ex_click)

                        page.wait_for_timeout(_POST_CLICK_SETTLE_MS)
                        post_click = page.url
                        navigation_happened = navigated_here or (post_click != pre_click)

                        resolved = ""
                        if download_obj is not None:
                            try:
                                resolved = (getattr(download_obj, "url", None) or "").strip()
                            except Exception:
                                resolved = ""

                        if (not resolved) or (not resolved.startswith("http")):
                            resolved = _pick_resolve_url(
                                post_click if navigation_happened else pre_click,
                                captured,
                                url,
                            )

                        navigated_off_listing_view = bool(str(post_click).strip() != str(pre_click).strip())
                        acceptable = _is_acceptable_mmis_resolve_url(resolved, url)

                        if acceptable:
                            label = ""
                            olds = row_obj.get("_links") or []
                            if isinstance(olds, list) and olds and isinstance(olds[0], dict):
                                label = str(olds[0].get("text") or "").strip()

                            row_obj["_links"] = [{"url": resolved, "text": label}]
                            urls_resolved += 1
                            remaining_by_key.pop(rk_final, None)
                            stagnant_iterations = 0
                            if download_obj is not None:
                                try:
                                    download_obj.cancel()
                                except Exception:
                                    pass
                        elif navigated_off_listing_view:
                            try:
                                page.go_back(wait_until="domcontentloaded", timeout=_BACK_RELOAD_WAIT_MS)
                            except Exception:
                                logger.warning(
                                    "GA MMIS resolver could not go_back cleanly; reloading portal URL (%s)",
                                    url,
                                )
                                try:
                                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                                    page.wait_for_timeout(5500)
                                except Exception:
                                    pass
                            else:
                                page.wait_for_timeout(2200)
                            if download_obj is not None:
                                try:
                                    download_obj.cancel()
                                except Exception:
                                    pass
                        else:
                            if download_obj is not None:
                                try:
                                    download_obj.cancel()
                                except Exception:
                                    pass
                    finally:
                        _maybe_remove_response_handler(page, handler)

                if len(remaining_by_key) >= before_remaining:
                    stagnant_iterations += 1
                if stagnant_iterations >= _GA_MMIS_MAX_STAGNANT_PAGES:
                    meta["stopped_due_to_stagnant_pages"] = True
                    break

                if not remaining_by_key or urls_resolved >= GA_MMIS_POSTBACK_RESOLVE_MAX:
                    break

                nxt = _find_next_control(page)
                if nxt is None:
                    break
                try:
                    nxt.click()
                except Exception:
                    break
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15_000)
                except Exception:
                    pass
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                page.wait_for_timeout(4300)

        finally:
            browser.close()

    if remaining_by_key and pages_seen >= _GA_MMIS_MAX_PAGINATION_ROUNDS:
        meta["stopped_due_to_pagination_cap"] = True

    meta["urls_resolved"] = urls_resolved
    meta["pages_visited"] = pages_seen
    meta["rows_pending_end"] = len(remaining_by_key)
    meta["stopped_early"] = bool(remaining_by_key and urls_resolved >= GA_MMIS_POSTBACK_RESOLVE_MAX)
    if remaining_by_key and meta["stopped_early"]:
        meta["skipped_reason"] = (
            "hit GA_MMIS_POSTBACK_RESOLVE_MAX — raise GA_MMIS_POSTBACK_RESOLVE_MAX or rerun to continue."
        )
    elif remaining_by_key and meta.get("stopped_due_to_pagination_cap"):
        meta["skipped_reason"] = (
            f"stopped after {_GA_MMIS_MAX_PAGINATION_ROUNDS} pagination rounds "
            "(internal resolver cap — raise pagination cap in code / env tuning if needed)."
        )
    elif remaining_by_key and meta.get("stopped_due_to_stagnant_pages"):
        meta["skipped_reason"] = "stopped after repeated non-progress pagination rounds."
    elif remaining_by_key:
        meta["skipped_reason"] = (
            "some rows unresolved (portal timing, differing row layout vs columns, or non-navigation downloads)."
        )
    else:
        meta["skipped_reason"] = ""
    return meta

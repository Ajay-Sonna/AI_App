"""Generic hub → inner HTML pages → file-link catalog harvesting (recipe-driven)."""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from app.extractors.file_link_catalog import extract_file_link_catalog
from app.preview.preview_service import _DEFAULT_UA

logger = logging.getLogger(__name__)

_FILE_SUFFIXES_DEFAULT = (
    ".pdf",
    ".xlsx",
    ".xls",
    ".docx",
    ".doc",
    ".csv",
    ".zip",
    ".xlsm",
)


def _fetch_html(url: str, *, timeout: float) -> Optional[str]:
    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": _DEFAULT_UA.strip(),
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        if r.status_code >= 400:
            return None
        return r.text
    except Exception as ex:
        logger.info("recipe expand fetch failed %s: %s", url, ex)
        return None


def _norm_u(u: str) -> str:
    return (u or "").strip().split("#")[0].rstrip()


def _collect_inner_html_urls(
    html: str,
    base_url: str,
    *,
    path_must_contain: str,
    exclude_suffixes: Tuple[str, ...],
    same_host_only: bool,
    max_links: int,
) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc.lower()
    need = path_must_contain.lower().strip()

    seen: Set[str] = set()
    out: List[str] = []
    for tag in soup.find_all("a", href=True):
        raw = str(tag.get("href") or "").strip()
        if not raw or raw.startswith("#") or raw.lower().startswith("javascript:"):
            continue
        full = urljoin(base_url, raw)
        pu = urlparse(full)
        if pu.scheme not in ("http", "https"):
            continue
        if same_host_only and pu.netloc.lower() != base_host:
            continue
        low_path = pu.path.lower()
        if need and need not in low_path:
            continue
        if low_path.endswith(tuple(exclude_suffixes)):
            continue
        nu = _norm_u(full)
        if nu in seen:
            continue
        seen.add(nu)
        out.append(full)
        if len(out) >= max_links:
            break
    return out


def run_expand_internal_html_then_collect_files(
    url: str,
    *,
    bundle: Dict[str, Any],
    recipe_id: str,
    params: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Breadth-first crawl (depth-limited): each HTML page visited runs ``file_link_catalog``;
    same-site links matching path rules extend the frontier.
    """
    nav = params.get("navigation") if isinstance(params.get("navigation"), dict) else {}
    fetch = params.get("fetch") if isinstance(params.get("fetch"), dict) else {}

    max_depth = max(1, min(4, int(params.get("max_nav_depth") or 2)))
    max_pages_total = max(2, min(300, int(params.get("max_leaf_pages") or 80)))
    timeout = float(fetch.get("timeout_seconds") or 28.0)

    path_must_contain = str(nav.get("path_must_contain") or "").strip()
    if not path_must_contain:
        path_must_contain = str(urlparse(url).path or "")[:160]

    exclude_suffixes = tuple(
        str(s).lower().strip() for s in (nav.get("href_exclude_suffixes") or list(_FILE_SUFFIXES_DEFAULT)) if s
    )
    same_host = bool(nav.get("same_host_only", True))
    max_links_per_page = max(4, min(400, int(nav.get("max_links_per_page") or 120)))

    start = _norm_u((url or "").strip())
    base_page_url = _norm_u(str(bundle.get("url") or start))

    visited: Set[str] = set()
    q: deque[tuple[str, int]] = deque([(start, 0)])
    all_rows: List[Dict[str, Any]] = []
    pages_scanned = 0
    meta: Dict[str, Any] = {"recipe_id": recipe_id, "strategy": "expand_internal_html_then_collect_files"}

    while q and pages_scanned < max_pages_total:
        page_u, depth = q.popleft()
        nu = _norm_u(page_u)
        if nu in visited:
            continue
        visited.add(nu)

        html: Optional[str] = None
        if nu == base_page_url and bundle.get("html"):
            html = str(bundle["html"])
        else:
            html = _fetch_html(nu, timeout=timeout)

        if not html:
            continue

        pages_scanned += 1

        fc = extract_file_link_catalog(html, nu, max_rows=500)
        for row in fc.get("rows") or []:
            if isinstance(row, dict):
                r2 = dict(row)
                r2.setdefault("Visited page", nu)
                all_rows.append(r2)

        if depth + 1 >= max_depth or pages_scanned >= max_pages_total:
            continue

        children = _collect_inner_html_urls(
            html,
            nu,
            path_must_contain=path_must_contain,
            exclude_suffixes=exclude_suffixes,
            same_host_only=same_host,
            max_links=max_links_per_page,
        )
        for c in children:
            cn = _norm_u(c)
            if cn not in visited:
                q.append((cn, depth + 1))

        if pages_scanned >= max_pages_total:
            meta["truncated_pages"] = True

    cols = ["Visited page", "Section", "Title", "File URL", "File type", "Updated"]
    table = {
        "block_id": f"recipe_expand:{recipe_id}",
        "columns": cols,
        "rows": all_rows[:2000],
        "row_count": len(all_rows),
        "pages_visited": pages_scanned,
        "paginated": False,
        "source": "recipe:expand_internal_html_then_collect_files",
        "recipe_id": recipe_id,
    }
    meta.update(
        {
            "ok": True,
            "rows": len(all_rows),
            "pages_scanned": pages_scanned,
            "visited_html_pages": len(visited),
            "depth_limit": max_depth,
        }
    )
    return table, meta

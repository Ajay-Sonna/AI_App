# backend/app/extractors/servicenow_catalog.py
"""
ServiceNow Catalog Extractor
---------------------------
Extracts structured table data from ServiceNow Service Portal widgets
(typically /api/now/sp/rectangle/{widget_id}).

Design goals:
- NO LLM usage
- Deterministic, production-safe
- Works from captured api_calls (Playwright network capture)
- Paginates using page index ``p`` (Service Portal list widgets) and/or
  ``window_start`` / ``window_size`` for offset-based widgets
- Returns rows + metadata
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests


# -----------------------------
# Helpers
# -----------------------------

def _is_servicenow_rectangle_call(call: Dict[str, Any]) -> bool:
    """True if the call looks like a ServiceNow rectangle widget API."""
    url = (call.get("url") or "").lower()
    return "/api/now/sp/rectangle/" in url and call.get("method") == "POST"


def _rectangle_row_count_hint(call: Dict[str, Any]) -> int:
    """Prefer the widget instance that actually drives the main data table."""
    ds = _safe_get(call.get("data_sample") or {}, ["result", "data"], {}) or {}
    try:
        return int(ds.get("row_count") or 0)
    except (TypeError, ValueError):
        return 0


def _widget_catalog_priority(call: Dict[str, Any]) -> int:
    """
    Prefer the portal widget that lists downloadable fee schedule files.

    ServiceNow sites often fire multiple /sp/rectangle/ calls (filters, code tables,
    etc.). Larger ``row_count`` alone is a bad signal — procedure-code widgets can be
    10× bigger than the fee schedule index.
    """
    ds = _safe_get(call.get("data_sample") or {}, ["result", "data"], {}) or {}
    table = (ds.get("table") or "").lower()
    title = (ds.get("title") or "").lower()

    if "fee_codes" in table or "covered_code" in table:
        return 0
    if "fee_schedule" in table:
        return 4
    if "fee schedule" in title or "current fee" in title:
        return 3
    if "excel" in title or "download" in title:
        return 2
    return 1


def _prefer_current_fee_list(ds: Dict[str, Any]) -> int:
    """Archived vs current both use the same table name; prefer the active list."""
    title = (ds.get("title") or "").lower()
    if "archiv" in title:
        return 0
    return 1


def _rect_sort_key(call: Dict[str, Any]) -> tuple:
    """
    Prefer the main fee-schedule index, then capturable POST bodies, then coverage.
    """
    ds = _safe_get(call.get("data_sample") or {}, ["result", "data"], {}) or {}
    pr = _widget_catalog_priority(call)
    current_pick = _prefer_current_fee_list(ds)
    pl = 1 if _normalize_rectangle_payload(call.get("payload")) else 0
    nlist = len(ds.get("list") or [])
    rc = _rectangle_row_count_hint(call)
    return (pr, current_pick, pl, nlist, rc)


def _pick_best_rectangle_call(api_calls: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    rects = [c for c in api_calls if _is_servicenow_rectangle_call(c)]
    if not rects:
        return None
    return max(rects, key=_rect_sort_key)


def _extract_widget_id(url: str) -> Optional[str]:
    try:
        parts = urlparse(url).path.strip("/").split("/")
        return parts[-1] if parts else None
    except Exception:
        return None


def _safe_get(d: Dict, path: List[str], default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def _origin_from_url(url: str) -> str:
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return ""


def _pubatt_dl_prefix(api_calls: List[Dict[str, Any]]) -> Optional[str]:
    """Use the same download path the portal used (e.g. /api/g_ncd2/pubatt/dl/)."""
    for c in api_calls:
        u = c.get("url") or ""
        if "pubatt/dl/" in u:
            return u.split("pubatt/dl/")[0] + "pubatt/dl/"
    return None


def _pubatt_dl_prefix_from_rectangle_template(origin: str, rectangle_data_sample: Any) -> Optional[str]:
    """
    Service Portal widgets embed attachment hrefs in Angular HTML ``result.template``.

    Guest-visible downloads often live at ``/api/{scope}/pubatt/dl/{sys_id}`` while the
    generic fallback ``/api/now/attachment/{sys_id}/file`` requires authenticated table
    ACLs and returns XML ``User Not Authenticated`` when opened as a guest.
    """
    if not isinstance(rectangle_data_sample, dict):
        return None
    tmpl = _safe_get(rectangle_data_sample, ["result", "template"], "") or ""
    if not isinstance(tmpl, str) or "pubatt/dl" not in tmpl.lower():
        return None
    m = re.search(r"(https?://[^\"'\\s>]+/pubatt/dl/)", tmpl, re.I)
    if m:
        return m.group(1)
    m = re.search(r"(/api/[A-Za-z0-9_]+/pubatt/dl/)", tmpl)
    if not m:
        return None
    return origin.rstrip("/") + m.group(1)


def resolve_pubatt_download_base(
    origin: str,
    api_calls: List[Dict[str, Any]],
    rectangle_data_sample: Any,
) -> Optional[str]:
    return _pubatt_dl_prefix(api_calls) or _pubatt_dl_prefix_from_rectangle_template(
        origin, rectangle_data_sample
    )


def _attachment_download_url(
    origin: str,
    attachment_id: str,
    api_calls: List[Dict[str, Any]],
    *,
    pub_download_base: Optional[str] = None,
) -> str:
    pub = pub_download_base or _pubatt_dl_prefix(api_calls)
    if pub:
        return pub + attachment_id
    base = origin.rstrip("/")
    return f"{base}/api/now/attachment/{attachment_id}/file"


def _normalize_glide_cell(
    cell: Any,
    *,
    origin: str,
    api_calls: List[Dict[str, Any]],
    links_acc: List[Dict[str, str]],
    pub_download_base: Optional[str] = None,
) -> str:
    """Plain string for display; file_attachment also pushes download into links_acc."""
    if cell is None:
        return ""
    if not isinstance(cell, dict):
        return str(cell).strip()
    disp = cell.get("display_value")
    if disp is None and cell.get("value") is not None:
        disp = cell["value"]
    text = "" if disp is None else str(disp).strip()
    ctype = (cell.get("type") or "").lower()
    raw_val = cell.get("value")
    if ctype == "file_attachment" and raw_val:
        href = _attachment_download_url(
            origin,
            str(raw_val).strip(),
            api_calls,
            pub_download_base=pub_download_base,
        )
        links_acc.append({"url": href, "text": text or "Download"})
    return text


def _flatten_servicenow_row(
    raw: Dict[str, Any],
    fields: List[str],
    column_labels: Dict[str, Any],
    *,
    origin: str,
    api_calls: List[Dict[str, Any]],
    pub_download_base: Optional[str] = None,
) -> Dict[str, Any]:
    """Keys match API `columns` (human labels)."""
    out: Dict[str, Any] = {}
    links_acc: List[Dict[str, str]] = []

    for f in fields:
        label = str(column_labels.get(f, f))
        out[label] = _normalize_glide_cell(
            raw.get(f),
            origin=origin,
            api_calls=api_calls,
            links_acc=links_acc,
            pub_download_base=pub_download_base,
        )

    sid = raw.get("sys_id")
    if sid is not None and sid != "":
        if isinstance(sid, dict):
            sid = sid.get("value") or sid.get("display_value") or ""
        out["sys_id"] = str(sid).strip()

    if links_acc:
        out["_links"] = links_acc
    return out


# -----------------------------
# Pagination replay
# -----------------------------

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _normalize_rectangle_payload(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            j = json.loads(raw)
            return j if isinstance(j, dict) else {}
        except Exception:
            return {}
    return {}


def _session_with_playwright_cookies(
    session: Optional[requests.Session],
    cookies: Optional[List[Dict[str, Any]]],
    *,
    csrf_token: Optional[str] = None,
) -> requests.Session:
    sess = session or requests.Session()
    sess.headers.setdefault("User-Agent", _DEFAULT_UA)
    sess.headers.setdefault("Accept", "application/json")
    if csrf_token:
        sess.headers["X-UserToken"] = csrf_token
    for c in cookies or []:
        name = c.get("name")
        if not name:
            continue
        value = c.get("value") or ""
        domain = c.get("domain") or ""
        path = c.get("path") or "/"
        try:
            sess.cookies.set(name, value, domain=domain, path=path)
        except Exception:
            sess.cookies.set(name, value, path=path)
    return sess


def _clone_paginate_payload(
    base_payload: Dict[str, Any],
    *,
    page_num: int,
    page_size: int,
    total_rows: int,
) -> Dict[str, Any]:
    """
    Mirror the Service Portal widget: set ``p`` and matching window slice.

    ServiceNow ``sp/rectangle`` replays use a **flat** JSON body: widget instance
    fields (table, filter, …) sit beside runtime fields (``p``, ``list``, …) —
    not nested under ``data``.
    """
    out = copy.deepcopy(base_payload)
    ws = max(0, (page_num - 1) * page_size)
    we = min(ws + page_size, max(total_rows, ws + page_size))
    for k in ("list", "loading", "loadingData", "invalid_table"):
        out.pop(k, None)
    inner = out.get("data")
    if isinstance(inner, dict) and "p" in inner:
        for k in ("list", "loading", "loadingData", "invalid_table"):
            inner.pop(k, None)
        inner["p"] = page_num
        inner["window_start"] = ws
        inner["window_end"] = we
        inner["page_index"] = page_num - 1
        return out
    out["p"] = page_num
    out["window_start"] = ws
    out["window_end"] = we
    out["page_index"] = page_num - 1
    return out


def _merge_runtime_data_into_payload(
    base_payload: Dict[str, Any],
    first_data: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Playwright captures the widget *instance* options on first POST; the browser's
    next POST merges those options with the entire ``result.data`` object as a
    **single flat JSON** (see ServiceNow ``spUtil.update``). Overlay the first
    response onto the template payload so pagination replay matches the browser.
    """
    if not first_data:
        return copy.deepcopy(base_payload)
    out = copy.deepcopy(base_payload)
    # If a nested ``data`` block already contains pagination scope, keep it.
    inner = out.get("data")
    if isinstance(inner, dict) and inner.get("table") and "p" in inner:
        return out
    for k, v in copy.deepcopy(first_data).items():
        out[k] = v
    return out


def _dedupe_glide_rows(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for it in items:
        sid = it.get("sys_id")
        if isinstance(sid, dict):
            sid = sid.get("value") or sid.get("display_value")
        key = str(sid or "").strip()
        if not key:
            key = f"anon:{len(out)}"
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


# -----------------------------
# Core extractor
# -----------------------------


def extract_servicenow_catalog(
    *,
    api_calls: List[Dict[str, Any]],
    max_pages: int = 200,
    session: Optional[requests.Session] = None,
    cookies: Optional[List[Dict[str, Any]]] = None,
    csrf_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Given captured api_calls from Playwright, find the ServiceNow rectangle
    widget call and paginate it to extract rows.

    Returns:
        {
          "source": "servicenow",
          "widget_id": str,
          "table": str,
          "columns": [..],
          "rows": [ {col: value, ...}, ... ],
          "row_count": int,
          "pages_visited": int,
          "paginated": bool,
          "expected_total_rows": int | None,
          "pagination_complete": bool,
          "pagination_mode": str | None,
        }
    """

    rectangle_call = _pick_best_rectangle_call(api_calls)

    if not rectangle_call:
        return {
            "source": "servicenow",
            "error": "No ServiceNow rectangle API call found",
            "rows": [],
            "row_count": 0,
            "pages_visited": 0,
            "paginated": False,
            "expected_total_rows": None,
            "pagination_complete": False,
            "pagination_mode": None,
        }

    url = rectangle_call.get("url")
    widget_id = _extract_widget_id(url or "") or ""
    origin = _origin_from_url(url or "")

    first_resp = rectangle_call.get("data_sample") or {}
    data = _safe_get(first_resp, ["result", "data"], {}) or {}

    pub_download_base = resolve_pubatt_download_base(origin, api_calls, first_resp)

    table_name = data.get("table")
    fields = (data.get("fields") or "").split(",") if data.get("fields") else []
    column_labels = data.get("column_labels") or {}

    window_size = int(data.get("window_size", len(data.get("list") or []))) or 20
    try:
        total_rows = int(data.get("row_count", len(data.get("list") or [])))
    except (TypeError, ValueError):
        total_rows = len(data.get("list") or [])

    try:
        num_pages = int(data.get("num_pages") or 0)
    except (TypeError, ValueError):
        num_pages = 0

    raw_rows: List[Dict[str, Any]] = []
    first_list = data.get("list") or []
    for item in first_list:
        if isinstance(item, dict):
            raw_rows.append(item)

    base_payload = _normalize_rectangle_payload(rectangle_call.get("payload"))
    merged_payload = _merge_runtime_data_into_payload(
        base_payload if base_payload else {},
        data if isinstance(data, dict) else {},
    )
    sess = _session_with_playwright_cookies(session, cookies, csrf_token=csrf_token)

    pages_visited = 1
    paginated = False
    pagination_mode: Optional[str] = None
    pagination_complete = False

    # ---- Strategy A: explicit page count (list widgets, e.g. NCDHHS fee schedules)
    if (
        num_pages > 1
        and isinstance(merged_payload, dict)
        and merged_payload
        and pages_visited < max_pages
    ):
        pagination_mode = "page_index"
        paginated = True
        for pnum in range(2, min(num_pages + 1, max_pages + 1)):
            payload = _clone_paginate_payload(
                merged_payload,
                page_num=pnum,
                page_size=window_size,
                total_rows=total_rows,
            )
            try:
                r = sess.post(url, json=payload, timeout=45)
                r.raise_for_status()
                resp_json = r.json()
            except Exception:
                break
            page_data = _safe_get(resp_json, ["result", "data"], {}) or {}
            page_list = page_data.get("list") or []
            if not page_list:
                break
            for item in page_list:
                if isinstance(item, dict):
                    raw_rows.append(item)
            pages_visited += 1

    # ---- Strategy B: offset window (widgets without num_pages / p)
    elif total_rows > len(raw_rows) and isinstance(merged_payload, dict) and merged_payload:
        pagination_mode = "window_offset"
        paginated = True
        window_start = int(data.get("window_start", 0))
        next_start = window_start + window_size
        while next_start < total_rows and pages_visited < max_pages:
            payload = _clone_paginate_payload(
                merged_payload,
                page_num=int(next_start // max(window_size, 1)) + 1,
                page_size=window_size,
                total_rows=total_rows,
            )
            inner = payload.get("data")
            if isinstance(inner, dict):
                inner["window_start"] = next_start
                inner["window_end"] = min(next_start + window_size, total_rows)
            try:
                r = sess.post(url, json=payload, timeout=45)
                r.raise_for_status()
                resp_json = r.json()
            except Exception:
                break
            page_data = _safe_get(resp_json, ["result", "data"], {}) or {}
            page_list = page_data.get("list") or []
            if not page_list:
                break
            for item in page_list:
                if isinstance(item, dict):
                    raw_rows.append(item)
            pages_visited += 1
            next_start += window_size

    raw_rows = _dedupe_glide_rows(raw_rows)

    if total_rows > 0:
        pagination_complete = len(raw_rows) >= total_rows
    elif num_pages > 0:
        pagination_complete = pages_visited >= num_pages

    if not fields and raw_rows:
        fields = list(raw_rows[0].keys())

    columns = [str(column_labels.get(f, f)) for f in fields]

    flat_rows = [
        _flatten_servicenow_row(
            r,
            fields,
            column_labels,
            origin=origin,
            api_calls=api_calls,
            pub_download_base=pub_download_base,
        )
        for r in raw_rows
    ]
    columns_out = list(columns)
    if any("sys_id" in fr for fr in flat_rows):
        if "sys_id" not in columns_out:
            columns_out.append("sys_id")

    out: Dict[str, Any] = {
        "source": "servicenow",
        "widget_id": widget_id,
        "table": table_name,
        "columns": columns_out,
        "fields": fields,
        "rows": flat_rows,
        "row_count": len(flat_rows),
        "pages_visited": pages_visited,
        "paginated": paginated,
        "expected_total_rows": total_rows if total_rows else None,
        "pagination_complete": pagination_complete,
        "pagination_mode": pagination_mode,
    }
    if paginated and not base_payload:
        out["pagination_note"] = (
            "Pagination needed but POST body was not captured; "
            "ensure Playwright records JSON post_data for the rectangle call."
        )
    elif paginated and not pagination_complete:
        out["pagination_note"] = (
            f"Collected {len(raw_rows)} rows"
            + (f", expected {total_rows}" if total_rows else "")
            + "; replay may need cookies, payload, or a higher max_pages."
        )
    return out

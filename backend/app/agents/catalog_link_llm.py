"""
Focused LLM pass: enrich downloadable row link anchor text into stable display labels.

Used for portals (e.g. California DWC) where the table mixes several fee documents under one
effective-date row anchor text dominates the UI/sync label.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Set, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.agents.catalog_file_urls import (
    _abs_url,
    _looks_like_file_download,
    normalize_persistable_url,
)
from app.catalog.row_labels import ordered_column_names, slug_logical_schedule_key
from app.extractors.ca_dwc_omfs import annotate_ca_dwc_row_schedule_metadata
from app.config.settings import LLM_CATALOG_LINK_LABELS_ENABLED
from app.llm.llm_client import disambiguate_fee_catalog_rows, groq_daily_token_budget_exceeded

logger = logging.getLogger(__name__)

_MAX_ROWS_PER_CALL = 5
_ANCHOR_MAX = 100
_CELL_VAL_MAX = 64
_MAX_COL_SNAPSHOT = 5


def _collect_allowed_http_file_urls(html: str, base_url: str) -> Set[str]:
    allowed: Set[str] = set()
    base = (base_url or "").strip()
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:
        return allowed
    for tag in soup.find_all("a", href=True):
        raw_h = tag.get("href")
        if not raw_h:
            continue
        h = str(raw_h).strip()
        lu = h.lower()
        if "javascript:" in lu or "__dopostback" in lu:
            continue
        abs_u = _abs_url(base, h)
        if not abs_u:
            continue
        if _looks_like_file_download(abs_u):
            allowed.add(normalize_persistable_url(abs_u))
    return allowed


def _http_file_candidates_for_row(row: Dict[str, Any], base_url: str) -> List[Dict[str, str]]:
    """Links on this row that look like downloadable http(s) files."""
    out: List[Dict[str, str]] = []
    for link in row.get("_links") or []:
        if not isinstance(link, dict):
            continue
        raw = (link.get("url") or "").strip()
        abs_u = _abs_url(base_url, raw)
        if not abs_u or not _looks_like_file_download(abs_u):
            continue
        anch = str(link.get("text") or link.get("label") or "").strip()
        out.append({"url": abs_u, "anchor": anch[:_ANCHOR_MAX]})
    seen: Set[str] = set()
    deduped: List[Dict[str, str]] = []
    for c in out:
        k = normalize_persistable_url(c["url"])
        if not k or k in seen:
            continue
        seen.add(k)
        deduped.append(c)
    return deduped


def _row_cell_snapshot(row: Dict[str, Any], table: Dict[str, Any]) -> Dict[str, str]:
    cols = ordered_column_names(table)
    snap: Dict[str, str] = {}
    for c in cols[: _MAX_COL_SNAPSHOT]:
        v = row.get(c)
        if v is None:
            continue
        s = str(v).strip().replace("\n", " ")
        if len(s) > _CELL_VAL_MAX:
            s = s[: _CELL_VAL_MAX - 3] + "..."
        snap[c] = s
    return snap


def _row_needs_enrichment(
    row: Dict[str, Any],
    *,
    host: str,
    base_url: str,
) -> bool:
    cand = _http_file_candidates_for_row(row, base_url)
    low_host = host.lower().rstrip(".")
    is_dir_ca = low_host.endswith("dir.ca.gov")
    cols_join = " ".join(str(k).lower() for k in row if isinstance(k, str) and not k.startswith("_"))
    omfs_style = ("effective date" in cols_join or "documents" in cols_join) and len(cand) >= 1
    if len(cand) >= 2:
        return True
    if is_dir_ca and omfs_style:
        return True
    return False


def _apply_llm_documents_to_row(
    row: Dict[str, Any],
    documents: List[Dict[str, Any]],
    *,
    allowed: Set[str],
    base_url: str,
) -> int:
    """
    Mutate ``row['_links']`` text labels from LLM output. Returns count of labels applied.
    url_index maps normalized URLs to suggested display strings.
    """
    if not documents or not isinstance(documents, list):
        return 0

    desired: Dict[str, Dict[str, Any]] = {}
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        u = normalize_persistable_url(str(doc.get("url") or "").strip())
        if not u:
            continue
        if u not in allowed:
            continue
        lbl = str(doc.get("display_label") or "").strip()
        if not lbl or len(lbl) > 500:
            continue
        desired[u] = {
            "display_label": lbl[:500],
            "superseded": bool(doc.get("superseded")),
        }

    if not desired:
        return 0

    n = 0
    fee_topic = ""
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        ft = str(doc.get("fee_topic") or "").strip()
        if ft and not fee_topic:
            fee_topic = ft[:200]

    links = row.get("_links") or []
    if not isinstance(links, list):
        return 0
    new_links = []
    for link in links:
        if not isinstance(link, dict):
            new_links.append(link)
            continue
        lk = dict(link)
        raw = (lk.get("url") or "").strip()
        abs_u = normalize_persistable_url(_abs_url(base_url, raw))
        hint = desired.get(abs_u)
        if hint:
            lk["text"] = hint["display_label"]
            if hint["superseded"]:
                lk["superseded_hint"] = "1"
            n += 1
        new_links.append(lk)

    def _lnk_key(lnk: Any) -> tuple:
        if not isinstance(lnk, dict):
            return (2, "")
        sup = str(lnk.get("superseded_hint") or "").strip().lower()
        tier = 1 if sup in ("1", "true", "yes") else 0
        return (tier, (lnk.get("text") or "")[:180])

    row["_links"] = sorted(new_links, key=_lnk_key)

    sec_existing = str(row.get("_schedule_section") or "").strip()
    if fee_topic and not sec_existing and not str(row.get("_fee_topic_slug") or "").strip():
        row["_fee_topic_slug"] = slug_logical_schedule_key(fee_topic)[:256]

    # Restore DWC primary vs alternate ordering after LLM re-sorts link labels.
    if sec_existing:
        annotate_ca_dwc_row_schedule_metadata(row, sec_existing)

    return n


def _resolve_documents_with_url_ids(
    documents: List[Dict[str, Any]],
    url_by_id: List[str],
) -> List[Dict[str, Any]]:
    """Map model ``url_id`` indices back to URLs for validators that expect ``url``."""
    out: List[Dict[str, Any]] = []
    for doc in documents or []:
        if not isinstance(doc, dict):
            continue
        d = dict(doc)
        uid = d.get("url_id")
        if uid is not None:
            try:
                i = int(uid)
                if 0 <= i < len(url_by_id):
                    d["url"] = url_by_id[i]
            except (ValueError, TypeError):
                pass
        elif "url" not in d and isinstance(d.get("href"), str):
            d["url"] = d["href"]
        out.append(d)
    return out


def apply_llm_catalog_link_enrichment(
    html: str,
    base_url: str,
    catalog_tables: List[Dict[str, Any]],
) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "enabled": LLM_CATALOG_LINK_LABELS_ENABLED,
        "batches_run": 0,
        "rows_considered": 0,
        "rows_enriched": 0,
        "link_labels_written": 0,
        "skipped_reason": "",
    }

    if not LLM_CATALOG_LINK_LABELS_ENABLED:
        meta["skipped_reason"] = "LLM_CATALOG_LINK_LABELS disabled."
        return meta

    if not (html or "").strip() or not catalog_tables:
        meta["skipped_reason"] = "no html or no catalog tables."
        return meta

    base = (base_url or "").strip()
    host = urlparse(base).hostname or ""

    eligible: List[Tuple[int, int, Dict[str, Any]]] = []
    for ti, table in enumerate(catalog_tables):
        if not isinstance(table, dict):
            continue
        rows = table.get("rows") or []
        if not isinstance(rows, list):
            continue
        for ri, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            if not _row_needs_enrichment(row, host=host, base_url=base):
                continue
            cand = _http_file_candidates_for_row(row, base)
            if len(cand) < 1:
                continue
            eligible.append((ti, ri, {"cells": _row_cell_snapshot(row, table), "candidates": cand}))

    meta["rows_considered"] = len(eligible)
    if not eligible:
        meta["skipped_reason"] = "no qualifying rows."
        return meta

    allowed = _collect_allowed_http_file_urls(html, base)
    if not allowed:
        meta["skipped_reason"] = "no http file hrefs detected in HTML (cannot validate)."
        return meta

    total_labels = 0
    for start in range(0, len(eligible), _MAX_ROWS_PER_CALL):
        batch = eligible[start : start + _MAX_ROWS_PER_CALL]
        url_by_id: List[str] = []
        norm_to_ix: Dict[str, int] = {}

        def _register_candidate_url(abs_u: str) -> int:
            nu = normalize_persistable_url((abs_u or "").strip())
            if not nu:
                return -1
            if nu in norm_to_ix:
                return norm_to_ix[nu]
            ix = len(url_by_id)
            url_by_id.append((abs_u or "").strip())
            norm_to_ix[nu] = ix
            return ix

        rows_spec: List[Dict[str, Any]] = []
        for ti, ri, spec in batch:
            cand = spec.get("candidates") or []
            ids_list: List[int] = []
            anchors_list: List[str] = []
            for c in cand:
                raw_u = (c.get("url") or "").strip()
                ix = _register_candidate_url(raw_u)
                if ix < 0:
                    continue
                ids_list.append(ix)
                anchors_list.append(str(c.get("anchor") or "").strip()[:_ANCHOR_MAX])
            if not ids_list:
                continue
            rows_spec.append(
                {
                    "table_index": ti,
                    "row_index": ri,
                    "cells": spec.get("cells") or {},
                    "candidate_url_ids": ids_list,
                    "anchors": anchors_list,
                }
            )

        if not url_by_id or not rows_spec:
            continue

        try:
            parsed = disambiguate_fee_catalog_rows(
                page_url=base,
                url_by_id=url_by_id,
                rows_spec=rows_spec,
            )
        except Exception as exc:
            logger.warning(
                "catalog link LLM batch failed (no further batches will run): %s", exc
            )
            meta.setdefault("warnings", []).append(str(exc))
            if groq_daily_token_budget_exceeded(exc):
                meta[
                    "skipped_reason"
                ] = "Groq daily token budget reached — stopped link-label LLM passes. Retry after quota reset, upgrade tier, or set LLM_CATALOG_LINK_LABELS=false in .env."
            else:
                meta["skipped_reason"] = (
                    f"Catalog link-label LLM stopped after error ({type(exc).__name__}). "
                    "Earlier batches are kept; retry later or set LLM_CATALOG_LINK_LABELS=false."
                )
            break

        meta["batches_run"] += 1
        out_rows = parsed.get("rows") or []
        if not isinstance(out_rows, list):
            continue

        keyed: Dict[str, Dict[str, Any]] = {}
        for item in out_rows:
            if not isinstance(item, dict):
                continue
            k = f'{int(item.get("table_index", -1))}:{int(item.get("row_index", -1))}'
            keyed[k] = item

        for ti, ri, _spec in batch:
            item = keyed.get(f"{ti}:{ri}")
            if not item:
                continue
            docs = item.get("documents")
            if not isinstance(docs, list):
                continue
            table = catalog_tables[ti]
            rows = table.get("rows") or []
            if ri >= len(rows) or not isinstance(rows[ri], dict):
                continue
            resolved_docs = _resolve_documents_with_url_ids(docs, url_by_id)
            cnt = _apply_llm_documents_to_row(
                rows[ri], resolved_docs, allowed=allowed, base_url=base
            )
            if cnt:
                meta["rows_enriched"] += 1
                total_labels += cnt

        # Small pacing between successful batches only (helps short TPM bursts; keep low so /run does not hang).
        if start + _MAX_ROWS_PER_CALL < len(eligible):
            time.sleep(1.0)

    meta["link_labels_written"] = total_labels
    if meta["batches_run"] == 0:
        meta["skipped_reason"] = meta.get("skipped_reason") or "no successful LLM batches."
    return meta

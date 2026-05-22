"""
LLM-assisted filtering for file-repository catalog rows.

Python extracts candidate file links from the DOM; the model only decides relevance
and optional display titles. Every kept URL must still exist in the extracted rows.
If the model excludes every row, we fail open and restore the deterministic extract.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config.settings import GROQ_API_KEY, LLM_FEE_DOC_FILTER_ENABLED
from app.llm.llm_client import normalize_fee_document_candidates

_LOG = logging.getLogger(__name__)


def _is_rate_limited(msg: str) -> bool:
    s = msg.lower()
    return "429" in s or "rate limit" in s or "rate_limit" in s


def _slim_rows_for_llm(rows: list[dict[str, Any]], *, max_rows: int = 56) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for r in rows[:max_rows]:
        if not isinstance(r, dict):
            continue
        url = (r.get("File URL") or "").strip()
        if not url:
            continue
        out.append(
            {
                "url": url,
                "section": (str(r.get("Section") or ""))[:500],
                "title": (str(r.get("Title") or ""))[:400],
                "file_type": (str(r.get("File type") or ""))[:24],
            }
        )
    return out


def apply_llm_fee_document_filter(
    catalog_table: dict[str, Any],
    page_url: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Mutate a file-repository catalog table in-place with filtered rows, or leave unchanged.

    Returns (catalog_table, meta) where meta describes what happened (for API payload).
    """
    meta: dict[str, Any] = {
        "enabled": bool(LLM_FEE_DOC_FILTER_ENABLED and GROQ_API_KEY),
        "applied": False,
        "rows_in": 0,
        "rows_out": 0,
        "skipped_reason": None,
        "summary": None,
        "confidence": None,
        "degraded_restore_unfiltered": False,
        "skipped_detail": None,
    }

    if catalog_table.get("source") != "file_link_catalog":
        meta["skipped_reason"] = "not_file_link_catalog"
        catalog_table["llm_fee_document_filter"] = meta
        return catalog_table, meta

    if not LLM_FEE_DOC_FILTER_ENABLED:
        meta["skipped_reason"] = "disabled_by_config"
        catalog_table["llm_fee_document_filter"] = meta
        return catalog_table, meta

    if not GROQ_API_KEY:
        meta["skipped_reason"] = "no_groq_api_key"
        catalog_table["llm_fee_document_filter"] = meta
        return catalog_table, meta

    rows = catalog_table.get("rows") or []
    if not rows:
        meta["skipped_reason"] = "empty_rows"
        catalog_table["llm_fee_document_filter"] = meta
        return catalog_table, meta

    meta["rows_in"] = len(rows)
    slim = _slim_rows_for_llm(rows)
    if not slim:
        meta["skipped_reason"] = "no_slim_candidates"
        meta["rows_out"] = meta["rows_in"]
        catalog_table["llm_fee_document_filter"] = meta
        return catalog_table, meta

    url_set = {
        (r.get("File URL") or "").strip()
        for r in rows
        if isinstance(r, dict) and (r.get("File URL") or "").strip()
    }

    try:
        parsed = normalize_fee_document_candidates(slim, page_url)
    except Exception as e:
        _LOG.warning("LLM fee document normalization failed: %s", e)
        err_text = str(e)
        meta["rows_out"] = meta["rows_in"]
        if _is_rate_limited(err_text):
            meta["skipped_reason"] = "groq_rate_limit"
            meta["skipped_detail"] = err_text[:800]
        else:
            meta["skipped_reason"] = "llm_error"
            meta["skipped_detail"] = err_text[:800]
        catalog_table["llm_fee_document_filter"] = meta
        return catalog_table, meta

    if not isinstance(parsed, dict):
        meta["skipped_reason"] = "llm_invalid_response"
        meta["rows_out"] = meta["rows_in"]
        catalog_table["llm_fee_document_filter"] = meta
        return catalog_table, meta

    decisions = parsed.get("decisions")
    if not isinstance(decisions, list):
        meta["skipped_reason"] = "llm_no_decisions"
        meta["rows_out"] = meta["rows_in"]
        catalog_table["llm_fee_document_filter"] = meta
        return catalog_table, meta

    allow: dict[str, dict[str, Any]] = {}
    for d in decisions:
        if not isinstance(d, dict):
            continue
        u = (d.get("url") or "").strip()
        if u not in url_set:
            continue
        allow[u] = d

    filtered: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        u = (r.get("File URL") or "").strip()
        if u not in url_set:
            continue
        dec = allow.get(u)
        if dec is None:
            filtered.append(r)
            continue
        if dec.get("include") is False:
            continue
        nr = dict(r)
        alt = dec.get("display_title")
        if isinstance(alt, str) and alt.strip():
            nr["Title"] = alt.strip()[:600]
        filtered.append(nr)

    meta["rows_out"] = len(filtered)
    meta["summary"] = parsed.get("summary")
    meta["confidence"] = parsed.get("confidence")

    if len(filtered) == 0 and meta["rows_in"] > 0:
        meta["degraded_restore_unfiltered"] = True
        meta["applied"] = False
        meta["skipped_reason"] = "all_excluded_reverted_to_unfiltered"
        meta["rows_out"] = meta["rows_in"]
        catalog_table["llm_fee_document_filter"] = meta
        return catalog_table, meta

    catalog_table["rows"] = filtered
    catalog_table["row_count"] = len(filtered)
    meta["applied"] = True
    catalog_table["llm_fee_document_filter"] = meta
    return catalog_table, meta

# backend/app/analyzers/semantic_page_summary.py
"""
Semantic Page Summary
---------------------
Deterministic, human-readable explanation of what a webpage contains.
NO scraping. NO LLM. NO execution.
Consumes already-analyzed artifacts and produces a single confident summary.
"""

from __future__ import annotations
from typing import Dict, Any, List, Optional


def _pick_primary_table(blocks_selected: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Pick the most likely primary data table (human-first heuristic)."""
    tables = [b for b in blocks_selected if b.get("block_type") == "table"]
    if not tables:
        return None

    def score(b: Dict[str, Any]) -> tuple:
        return (
            (b.get("row_count") or 0),
            1 if b.get("has_file_links") else 0,
            len(b.get("columns") or []),
        )

    tables.sort(key=score, reverse=True)
    return tables[0]


def _infer_row_semantics(table_block: Dict[str, Any]) -> Dict[str, Any]:
    cols = " ".join((table_block.get("columns") or [])).lower()
    has_files = table_block.get("has_file_links")

    if has_files and ("fee" in cols or "schedule" in cols or "rate" in cols):
        return {
            "row_represents": "one fee schedule",
            "row_action": "download_file",
            "file_types": table_block.get("file_types", []),
        }

    return {
        "row_represents": "one record",
        "row_action": "none",
        "file_types": [],
    }


def _infer_layout(sections: List[Dict[str, Any]], page_analysis: Dict[str, Any]) -> Dict[str, bool]:
    return {
        "header_present": bool(sections),
        "intro_present": any(
            s.get("text_sample") and not s.get("has_table")
            for s in sections[:2]
        ),
        "navigation_present": bool(page_analysis.get("structure", {}).get("has_buttons")),
    }


def _map_page_type(website_class: Dict[str, Any]) -> str:
    cls = website_class.get("website_class")
    if cls == "C1_STATIC_HTML":
        return "static_catalog"
    if cls == "C3_SPA_APP":
        return "dynamic_application"
    if cls == "C4_BLOCKED":
        return "not_accessible"
    return "unknown"


def _compute_confidence(
    primary_table: Optional[Dict[str, Any]],
    llm_decision: Dict[str, Any],
) -> float:
    score = 0.6
    if primary_table:
        score += 0.2
        if primary_table.get("has_file_links"):
            score += 0.1
        if primary_table.get("columns"):
            score += 0.05
    if llm_decision.get("confidence", 0) > 0.7:
        score += 0.05
    return min(score, 1.0)


def build_semantic_page_summary(
    *,
    page_analysis: Dict[str, Any],
    website_class: Dict[str, Any],
    sections: List[Dict[str, Any]],
    blocks_selected: List[Dict[str, Any]],
    llm_decision: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Produce a single, confident, human-style explanation of the page.
    """

    primary_table = _pick_primary_table(blocks_selected)
    layout = _infer_layout(sections, page_analysis)
    page_type = _map_page_type(website_class)
    confidence = _compute_confidence(primary_table, llm_decision)

    summary: Dict[str, Any] = {
        "page_type": page_type,
        "confidence": confidence,
        "layout": layout,
    }

    if primary_table:
        summary["primary_data_block"] = {
            "block_id": primary_table.get("id"),
            "type": "table",
            "title": primary_table.get("heading_hint") or primary_table.get("title") or "",
            "structure": {
                "columns": primary_table.get("columns", []),
                "row_count": primary_table.get("row_count"),
            },
            "row_semantics": _infer_row_semantics(primary_table),
        }
    else:
        summary["primary_data_block"] = None

    summary["secondary_blocks"] = [
        {
            "type": "navigation",
            "description": "header/footer menus",
        }
    ]

    return {"semantic_page_summary": summary}

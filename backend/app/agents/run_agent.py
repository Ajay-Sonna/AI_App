# backend/app/agents/run_agent.py
"""
Unified Run Agent
-----------------
Single entry point used by the frontend.

Flow:
1. Analyze ONCE (load_page_bundle)
2. Decide extraction strategy based on analysis
3. Extract actual data (HTML or ServiceNow API)
4. Return final data to the user

This avoids re-analysis, classification flip-flops, and pagination bugs.
"""

from __future__ import annotations
from typing import Any, Dict, List

from app.agents.catalog_link_llm import apply_llm_catalog_link_enrichment
from app.agents.fee_document_normalize import apply_llm_fee_document_filter
from app.agents.ingestion_agent import load_page_bundle
from app.extractors.file_link_catalog import extract_file_link_catalog
from app.extractors.html_catalog import (
    extract_deduped_table_catalog,
    parse_table_block_index,
)
from app.config.settings import RUN_PAGINATION_WALL_SECONDS_DEFAULT
from app.extractors.paginated_catalog import extract_table_catalog_paginated
from app.extractors.servicenow_catalog import extract_servicenow_catalog
from app.portals import collect_portal_catalog_extensions
from app.portals.ga_mmis_postbacks import maybe_resolve_ga_mmis_postbacks
from app.preview.session_store import (
    preview_authority_ttl_seconds,
    register_preview_authority,
)


def run_pipeline(
    url: str,
    *,
    paginate: bool = True,
    max_pages: int = 200,
    max_tables: int = 12,
    pagination_wall_seconds: float | None = None,
) -> Dict[str, Any]:
    """
    Public, user-facing pipeline.
    The frontend should call THIS function via a single endpoint.
    """

    # --------------------------------------------------
    # 1) ANALYZE (ONCE)
    # --------------------------------------------------
    bundle = load_page_bundle(url)

    if bundle.get("blocked"):
        return {
            "url": url,
            "blocked": True,
            "analysis": bundle.get("analysis"),
        }

    website_class = bundle.get("website_class", {}).get("website_class")

    html = bundle.get("html")
    base = bundle.get("url")

    # --------------------------------------------------
    # 2) EXTRACT (TRUST ANALYSIS)
    # --------------------------------------------------
    catalog_tables: List[Dict[str, Any]] = []
    fee_document_llm_meta: Dict[str, Any] | None = None

    def _tables_have_rows(tables: List[Dict[str, Any]]) -> bool:
        for t in tables:
            rows = t.get("rows") or []
            if len(rows) > 0:
                return True
        return False

    wall_s = (
        float(pagination_wall_seconds)
        if pagination_wall_seconds is not None
        else float(RUN_PAGINATION_WALL_SECONDS_DEFAULT)
    )

    spa_path = website_class == "C3_SPA_APP" or bool(bundle.get("spa_render_used"))

    # ===== CASE A: SERVICE NOW / SPA (API rows from captured network calls) =====
    if spa_path:
        spa_bundle = bundle.get("spa_bundle") or {}

        catalog = extract_servicenow_catalog(
            api_calls=spa_bundle.get("api_calls", []),
            max_pages=max_pages,
            cookies=spa_bundle.get("cookies"),
            csrf_token=spa_bundle.get("csrf_token"),
        )

        catalog_tables.append(catalog)

    # ===== CASE B: STATIC HTML / CMS (also fallback when SPA ran but no rectangle API rows) =====
    if not spa_path or not _tables_have_rows(catalog_tables):
        table_blocks = [
            b
            for b in bundle.get("blocks_selected", [])
            if b.get("block_type") == "table"
        ]
        # When the relevance LLM returns no table ids, we would extract nothing from large
        # portals (e.g. CA OMFS index). Fall back to every detected table block.
        if not table_blocks:
            table_blocks = [
                b
                for b in bundle.get("schedule_blocks", [])
                if b.get("block_type") == "table"
            ]

        def _table_priority(block: Dict[str, Any]) -> tuple:
            """
            Prefer fee-schedule / file-catalog tables. Navigation chrome often has many
            <tr> and generic outbound links but no __doPostBack / file extensions.
            """
            links = block.get("link_samples") or []
            score = 0
            for l in links:
                h = (str(l.get("href") or "")).lower()
                t = (str(l.get("text") or "")).lower()
                if "__dopostback" in h or h.startswith("javascript:"):
                    score += 150
                if any(ext in h or ext in t for ext in (".pdf", ".xlsx", ".xls", ".csv", ".docx", ".doc", ".zip")):
                    score += 60
                if any(k in t for k in ("fee", "schedule", "cpt", "hcpcs", "reimburse")):
                    score += 12
            score += len(links)
            row_count = int(block.get("row_count") or 0)
            return (score, row_count)

        table_blocks = sorted(table_blocks, key=_table_priority, reverse=True)[
            : max(1, int(max_tables))
        ]

        for block in table_blocks:
            bid = block.get("id") or ""
            idx = parse_table_block_index(bid)
            if idx is None:
                continue

            if paginate:
                cat = extract_table_catalog_paginated(
                    url,
                    idx,
                    max_pages=max_pages,
                    max_wall_seconds=wall_s,
                    fallback_html=html,
                    fallback_base=base,
                )
            else:
                cat = extract_deduped_table_catalog(html, idx, base)
                cat.setdefault("pages_visited", 1)
                cat.setdefault("paginated", False)

            cat["block_id"] = bid
            catalog_tables.append(cat)

        pa = bundle.get("page_analysis") or {}
        st = pa.get("structure") or {}
        if html and base and st.get("has_file_links") and not _tables_have_rows(catalog_tables):
            fc = extract_file_link_catalog(html, base)
            if fc.get("rows"):
                fc, fee_document_llm_meta = apply_llm_fee_document_filter(fc, base or url)
                catalog_tables.append(fc)

    # Interaction-gated portals: merge optional tables from registered adapters (see app.portals.registry).
    portal_extra_tables, portal_adapters_meta = collect_portal_catalog_extensions(url, bundle)
    if portal_extra_tables:
        catalog_tables.extend(portal_extra_tables)

    mmis_ga_resolve_meta = maybe_resolve_ga_mmis_postbacks(url, catalog_tables)

    # Drop empty ServiceNow placeholder when DOM/file extraction produced usable rows.
    if _tables_have_rows(catalog_tables) and len(catalog_tables) > 1:
        catalog_tables = [
            t
            for t in catalog_tables
            if len(t.get("rows") or []) > 0 or t.get("source") != "servicenow"
        ]

    catalog_link_llm_meta: Dict[str, Any] | None = None
    if _tables_have_rows(catalog_tables) and html:
        catalog_link_llm_meta = apply_llm_catalog_link_enrichment(
            str(html),
            str(base or url or "").strip(),
            catalog_tables,
        )

    # --------------------------------------------------
    # 3) FINAL RESPONSE (FOR FRONTEND)
    # --------------------------------------------------
    def _body_row_count(t: Dict[str, Any]) -> int:
        rc = t.get("row_count")
        if isinstance(rc, int) and rc >= 0:
            return rc
        return len(t.get("rows") or [])

    def _table_present_score(t: Dict[str, Any]) -> tuple:
        """Prefer tables users should see first: rows, outbound links, no hard errors."""
        rows = t.get("rows") or []
        n_rows = len(rows)
        err = bool(t.get("error"))
        n_linked = sum(1 for r in rows if isinstance(r, dict) and r.get("_links"))
        return (-n_rows, -n_linked, err, str(t.get("block_id") or ""))

    ordered_tables = sorted(catalog_tables, key=_table_present_score)

    preview_auth_payload: Dict[str, Any] | None = None
    spa_outer = bundle.get("spa_bundle")
    if isinstance(spa_outer, dict):
        ck = spa_outer.get("cookies")
        if not isinstance(ck, list):
            ck = []
        ct = str(spa_outer.get("csrf_token") or "").strip()
        if ck or ct:
            preview_auth_payload = {
                "session_id": register_preview_authority(
                    referrer_url=str(base or url).strip(),
                    cookies=ck,
                    csrf_token=ct or None,
                ),
                "ttl_seconds": preview_authority_ttl_seconds(),
            }

    out: Dict[str, Any] = {
        "url": url,
        "website_class": bundle.get("website_class"),
        "strategy": bundle.get("strategy"),
        "spa_render_used": bundle.get("spa_render_used"),
        "row_count": sum(_body_row_count(t) for t in catalog_tables),
        "catalog_tables": ordered_tables,
    }
    if preview_auth_payload is not None:
        out["preview_auth"] = preview_auth_payload
    if fee_document_llm_meta is not None:
        out["fee_document_llm"] = fee_document_llm_meta
    if catalog_link_llm_meta is not None:
        out["catalog_link_llm"] = catalog_link_llm_meta
    if portal_adapters_meta:
        out["portal_adapters"] = portal_adapters_meta
    if mmis_ga_resolve_meta.get("attempted_host") or mmis_ga_resolve_meta.get("eligible_mmis_ga_host"):
        out["mmis_ga_postback_resolve"] = mmis_ga_resolve_meta
    return out

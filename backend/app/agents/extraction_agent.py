# # backend/app/agents/extraction_agent.py
# from __future__ import annotations

# from typing import Any

# from app.agents.ingestion_agent import load_page_bundle
# from app.extractors.html_catalog import extract_deduped_table_catalog, parse_table_block_index
# from app.extractors.paginated_catalog import extract_table_catalog_paginated


# def run_catalog_extraction(
#     url: str,
#     *,
#     paginate: bool = True,
#     max_pages: int = 200,
# ):
#     """
#     Block selection matches /analyze. Tables become structured rows; with paginate=True
#     opens the page in Playwright and walks generic "next" controls until exhausted.
#     """
#     try:
#         bundle = load_page_bundle(url)
#         if bundle.get("blocked"):
#             return {"url": url, "analysis": bundle["analysis"]}

#         html = bundle["html"]
#         base = bundle["url"]

#         catalog_tables: list[dict[str, Any]] = []
#         for block in bundle["blocks_selected"]:
#             if block.get("block_type") != "table":
#                 continue
#             bid = block.get("id") or ""
#             idx = parse_table_block_index(bid)
#             if idx is None:
#                 continue
#             if paginate:
#                 cat = extract_table_catalog_paginated(
#                     url,
#                     idx,
#                     max_pages=max_pages,
#                     fallback_html=html,
#                     fallback_base=base,
#                 )
#             else:
#                 cat = extract_deduped_table_catalog(html, idx, base)
#                 cat.setdefault("pages_visited", 1)
#                 cat.setdefault("paginated", False)
#             cat["block_id"] = bid
#             catalog_tables.append(cat)

#         return {
#             "url": url,
#             "website_class": bundle["website_class"],
#             "strategy": bundle["strategy"],
#             "spa_render_used": bundle["spa_render_used"],
#             "paginate": paginate,
#             "catalog_tables": catalog_tables,
#         }
#     except Exception as e:
#         return {"error": str(e)}

from __future__ import annotations
from typing import Any

from app.agents.fee_document_normalize import apply_llm_fee_document_filter
from app.agents.ingestion_agent import load_page_bundle
from app.extractors.file_link_catalog import extract_file_link_catalog
from app.extractors.html_catalog import (
    extract_deduped_table_catalog,
    parse_table_block_index,
)
from app.extractors.paginated_catalog import extract_table_catalog_paginated
from app.extractors.servicenow_catalog import extract_servicenow_catalog
from app.portals import collect_portal_catalog_extensions
from app.portals.ga_mmis_postbacks import maybe_resolve_ga_mmis_postbacks


def run_catalog_extraction(
    url: str,
    *,
    paginate: bool = True,
    max_pages: int = 200,
):
    """
    Extracts actual catalog data based on /analyze output.

    - C1_STATIC_HTML → HTML + PDF extraction
    - C3_SPA_APP     → ServiceNow API extraction
    """
    try:
        bundle = load_page_bundle(url)

        if bundle.get("blocked"):
            return {"url": url, "analysis": bundle["analysis"]}

        website_class = bundle["website_class"]["website_class"]
        html = bundle["html"]
        base = bundle["url"]

        catalog_tables: list[dict[str, Any]] = []
        fee_document_llm_meta: dict[str, Any] | None = None

        def _tables_have_rows_local(tables: list[dict[str, Any]]) -> bool:
            for t in tables:
                rs = t.get("rows") or []
                if len(rs) > 0:
                    return True
            return False

        spa_path = website_class == "C3_SPA_APP" or bool(bundle.get("spa_render_used"))

        # =========================================================
        # CASE 1: ServiceNow / SPA (API rows from captured network calls)
        # =========================================================
        if spa_path:
            spa_bundle = bundle.get("spa_bundle") or {}

            catalog = extract_servicenow_catalog(
                api_calls=spa_bundle.get("api_calls", []),
                max_pages=max_pages,
                cookies=spa_bundle.get("cookies"),
                csrf_token=spa_bundle.get("csrf_token"),
            )

            catalog_tables.append(catalog)

        # =========================================================
        # CASE 2: HTML / CMS (also when SPA ran but no rectangle API rows)
        # =========================================================
        if not spa_path or not _tables_have_rows_local(catalog_tables):
            for block in bundle["blocks_selected"]:
                if block.get("block_type") != "table":
                    continue

                bid = block.get("id") or ""
                idx = parse_table_block_index(bid)
                if idx is None:
                    continue

                if paginate:
                    cat = extract_table_catalog_paginated(
                        url,
                        idx,
                        max_pages=max_pages,
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
            if html and base and st.get("has_file_links") and not _tables_have_rows_local(catalog_tables):
                fc = extract_file_link_catalog(html, base)
                if fc.get("rows"):
                    fc, fee_document_llm_meta = apply_llm_fee_document_filter(fc, base or url)
                    catalog_tables.append(fc)

        portal_extra_tables, portal_adapters_meta = collect_portal_catalog_extensions(url, bundle)
        if portal_extra_tables:
            catalog_tables.extend(portal_extra_tables)

        mmis_ga_resolve_meta = maybe_resolve_ga_mmis_postbacks(url, catalog_tables)

        if _tables_have_rows_local(catalog_tables) and len(catalog_tables) > 1:
            catalog_tables = [
                t
                for t in catalog_tables
                if len(t.get("rows") or []) > 0 or t.get("source") != "servicenow"
            ]

        result: dict[str, Any] = {
            "url": url,
            "website_class": bundle["website_class"],
            "strategy": bundle["strategy"],
            "spa_render_used": bundle["spa_render_used"],
            "paginate": paginate,
            "catalog_tables": catalog_tables,
        }
        if fee_document_llm_meta is not None:
            result["fee_document_llm"] = fee_document_llm_meta
        if portal_adapters_meta:
            result["portal_adapters"] = portal_adapters_meta
        if mmis_ga_resolve_meta.get("attempted_host") or mmis_ga_resolve_meta.get("eligible_mmis_ga_host"):
            result["mmis_ga_postback_resolve"] = mmis_ga_resolve_meta
        return result

    except Exception as e:
        return {
            "url": url,
            "error": "Extraction failed safely",
            "details": str(e),
        }
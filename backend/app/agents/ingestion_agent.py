# # backend/app/agents/ingestion_agent.py

# from app.tools.scraper import fetch_webpage
# from app.tools.link_discovery import discover_related_links
# from app.analyzers.structure_analyzer import analyze_page_structure
# from app.analyzers.section_extractor import extract_page_sections, build_schedule_blocks
# from app.llm.llm_client import classify_schedule_blocks_relevance
# from app.strategies.api_observer import observe_api_calls
# from app.analyzers.website_classifier import classify_website
# from app.analyzers.semantic_page_summary import build_semantic_page_summary


# def _should_run_spa_render_pass(
#     html: str,
#     website_decision: dict,
#     structure: dict,
# ) -> bool:
#     """
#     Trigger SPA render when data is likely hidden behind JS-rendered UI
#     (ServiceNow, Angular portals, etc.)
#     """

#     cls = website_decision.get("website_class")
#     st = structure.get("structure", {})

#     # ✅ Always render confirmed SPA apps
#     if cls == "C3_SPA_APP":
#         return True

#     # ✅ KEY FIX:
#     # Interactive UI (buttons/pagination) but NO tables/files yet → render SPA
#     if cls == "C2_INTERACTIVE_HTML":
#         if not st.get("has_table") and not st.get("has_file_links"):
#             return True

#     # ✅ ServiceNow / Angular shell detection
#     low = html.lower()
#     if (
#         "servicenow" in low
#         or "glide" in low
#         or "sp_angular" in low
#         or "sn_angular" in low
#     ):
#         return True

#     return False


# def _select_blocks(blocks: list, relevance: dict) -> list:
#     ids = relevance.get("process_ids") or []
#     if ids:
#         wanted = set(ids)
#         return [b for b in blocks if b.get("id") in wanted]

#     titles = relevance.get("process") or []
#     if titles:
#         wanted_t = set(titles)
#         return [b for b in blocks if b.get("title") in wanted_t]

#     return []


# def _refine_blocks_selected(selected: list) -> list:
#     """
#     Drop prose-only heading sections when a real data table is present.
#     """
#     has_actionable_table = any(
#         b.get("block_type") == "table"
#         and (
#             b.get("link_samples")
#             or b.get("data_sample")
#             or (b.get("row_count") or 0) > 0
#         )
#         for b in selected
#     )

#     if not has_actionable_table:
#         return selected

#     refined = []
#     for b in selected:
#         if b.get("block_type") != "heading_section":
#             refined.append(b)
#             continue
#         if b.get("has_table") or b.get("has_file_links"):
#             refined.append(b)
#     return refined if refined else selected


# def load_page_bundle(url: str):
#     """
#     Fetch → analyze → classify → SPA render (if needed) → re-analyze → re-classify
#     """

#     result = fetch_webpage(url)

#     # ---------- BLOCKED ----------
#     if isinstance(result, dict) and result.get("type") == "blocked":
#         return {
#             "blocked": True,
#             "url": url,
#             "analysis": {
#                 "page_type": "blocked",
#                 "summary": "Page appears to be protected by anti-bot measures.",
#                 "reason": result.get("reason", "Unknown"),
#                 "recommended_strategy": ["manual_review"],
#             },
#         }

#     # ---------- INITIAL HTML ----------
#     html = result

#     structure = analyze_page_structure(html)
#     website_decision = classify_website(html, structure)

#     spa_bundle = None
#     spa_render_used = False

#     # ---------- SPA RENDER PASS ----------
#     if _should_run_spa_render_pass(html, website_decision, structure):
#         spa_bundle = observe_api_calls(url)
#         html = spa_bundle.get("html") or html
#         spa_render_used = True

#         # ✅ CRITICAL: re-analyze SPA-rendered DOM
#         structure = analyze_page_structure(html)

#         # ✅ CRITICAL: re-classify using FINAL structure
#         website_decision = classify_website(html, structure)

#     # ---------- BLOCK ANALYSIS ----------
#     schedule_blocks = build_schedule_blocks(html)
#     relevance_decision = classify_schedule_blocks_relevance(schedule_blocks)

#     blocks_selected = _refine_blocks_selected(
#         _select_blocks(schedule_blocks, relevance_decision)
#     )

#     strategy = "SPA_RENDER_PLUS_DOM" if spa_render_used else "DIRECT_DOM"

#     return {
#         "blocked": False,
#         "url": url,
#         "html": html,
#         "page_analysis": structure,
#         "website_class": website_decision,
#         "strategy": strategy,
#         "spa_render_used": spa_render_used,
#         "schedule_blocks": schedule_blocks,
#         "blocks_selected": blocks_selected,
#         "llm_decision": relevance_decision,
#         "spa_bundle": spa_bundle,
#     }


# def run_ingestion_agent(
#     url: str,
#     *,
#     discover_links: bool = True,
#     max_discovered_links: int = 25,
# ):
#     bundle = load_page_bundle(url)

#     if bundle.get("blocked"):
#         return {"url": url, "analysis": bundle["analysis"]}

#     html = bundle["html"]
#     structure = bundle["page_analysis"]
#     website_decision = bundle["website_class"]
#     spa_render_used = bundle["spa_render_used"]
#     schedule_blocks = bundle["schedule_blocks"]
#     relevance_decision = bundle["llm_decision"]
#     blocks_selected = bundle["blocks_selected"]
#     spa_bundle = bundle["spa_bundle"]

#     # ---------- SECTIONS ----------
#     sections_detected = extract_page_sections(html)

#     sections_selected = [
#         s
#         for s in sections_detected
#         if s["title"] in (relevance_decision.get("process") or [])
#     ]

#     if not sections_selected:
#         sections_selected = [
#             {
#                 "title": b.get("title"),
#                 "text_sample": b.get("text_sample"),
#                 "has_table": b.get("has_table"),
#                 "has_file_links": b.get("has_file_links"),
#                 "file_types": b.get("file_types", []),
#                 "estimated_file_count": b.get("estimated_file_count", 0),
#             }
#             for b in blocks_selected
#             if b.get("block_type") == "heading_section" and b.get("title")
#         ]

#     # ---------- SEMANTIC SUMMARY ----------
#     semantic_summary = build_semantic_page_summary(
#         page_analysis=structure,
#         website_class=website_decision,
#         sections=sections_detected,
#         blocks_selected=blocks_selected,
#         llm_decision=relevance_decision,
#     )

#     out = {
#         "url": url,
#         "website_class": website_decision,
#         "strategy": bundle["strategy"],
#         "spa_render_used": spa_render_used,
#         "page_analysis": structure,
#         "schedule_blocks": schedule_blocks,
#         "blocks_selected": blocks_selected,
#         "llm_decision": relevance_decision,
#         "sections_detected": sections_detected,
#         "sections_selected": sections_selected,
#         "semantic_summary": semantic_summary,
#     }

#     if spa_bundle is not None:
#         out["extracted_data"] = {
#             "api_calls": spa_bundle.get("api_calls", []),
#             "files": spa_bundle.get("files", []),
#         }

#     if discover_links:
#         out["nested_candidates"] = discover_related_links(
#             html, url, max_links=max_discovered_links
#         )

#     return out

# backend/app/agents/ingestion_agent.py

from app.tools.scraper import fetch_webpage
from app.tools.link_discovery import discover_related_links
from app.analyzers.structure_analyzer import analyze_page_structure
from app.analyzers.section_extractor import extract_page_sections, build_schedule_blocks
from app.llm.llm_client import classify_schedule_blocks_relevance
from app.strategies.api_observer import observe_api_calls
from app.analyzers.website_classifier import classify_website
from app.analyzers.semantic_page_summary import build_semantic_page_summary


def _should_run_spa_render_pass(html, website_decision, structure) -> bool:
    cls = website_decision.get("website_class")
    st = structure.get("structure", {})

    if cls == "C3_SPA_APP":
        return True

    if cls == "C2_INTERACTIVE_HTML":
        if not st.get("has_table") and not st.get("has_file_links"):
            return True

    low = html.lower()
    if "servicenow" in low or "sp_angular" in low or "sn_angular" in low:
        return True

    return False


def _safe_llm_relevance(blocks):
    """
    ✅ PRODUCTION-SAFE LLM WRAPPER
    LLM failure must NEVER crash /analyze
    """
    try:
        return classify_schedule_blocks_relevance(blocks)
    except Exception as e:
        table_blocks = [b for b in blocks if b.get("block_type") == "table"]
        return {
            "process_ids": [b["id"] for b in table_blocks],
            "ignore_ids": [b["id"] for b in blocks if b.get("block_type") != "table"],
            "process": ["table blocks (fallback)"],
            "ignore": ["LLM unavailable"],
            "reason": f"LLM failed safely: {str(e)}",
            "confidence": 0.5,
        }


def load_page_bundle(url: str):
    result = fetch_webpage(url)

    if isinstance(result, dict) and result.get("type") == "blocked":
        return {"blocked": True, "analysis": result}

    html = result

    structure = analyze_page_structure(html)
    website_decision = classify_website(html, structure)

    spa_bundle = None
    spa_render_used = False

    if _should_run_spa_render_pass(html, website_decision, structure):
        try:
            spa_bundle = observe_api_calls(url)
            html = spa_bundle.get("html") or html
            spa_render_used = True

            structure = analyze_page_structure(html)
            website_decision = classify_website(html, structure)
        except Exception:
            # SPA observer must not block the whole pipeline (timeouts, Playwright issues)
            spa_bundle = None
            spa_render_used = False

    schedule_blocks = build_schedule_blocks(html)

    # ✅ THIS IS THE KEY FIX
    relevance_decision = _safe_llm_relevance(schedule_blocks)

    blocks_selected = [
        b for b in schedule_blocks if b["id"] in relevance_decision.get("process_ids", [])
    ]

    strategy = "SPA_RENDER_PLUS_DOM" if spa_render_used else "DIRECT_DOM"

    return {
        "blocked": False,
        "url": url,
        "html": html,
        "page_analysis": structure,
        "website_class": website_decision,
        "strategy": strategy,
        "spa_render_used": spa_render_used,
        "schedule_blocks": schedule_blocks,
        "blocks_selected": blocks_selected,
        "llm_decision": relevance_decision,
        "spa_bundle": spa_bundle,
    }


def run_ingestion_agent(url, *, discover_links=True, max_discovered_links=25):
    try:
        bundle = load_page_bundle(url)

        if bundle.get("blocked"):
            return bundle

        html = bundle["html"]

        sections_detected = extract_page_sections(html)

        semantic_summary = build_semantic_page_summary(
            page_analysis=bundle["page_analysis"],
            website_class=bundle["website_class"],
            sections=sections_detected,
            blocks_selected=bundle["blocks_selected"],
            llm_decision=bundle["llm_decision"],
        )

        out = {
            "url": url,
            "website_class": bundle["website_class"],
            "strategy": bundle["strategy"],
            "spa_render_used": bundle["spa_render_used"],
            "page_analysis": bundle["page_analysis"],
            "schedule_blocks": bundle["schedule_blocks"],
            "blocks_selected": bundle["blocks_selected"],
            "llm_decision": bundle["llm_decision"],
            "sections_detected": sections_detected,
            "semantic_summary": semantic_summary,
        }

        if bundle.get("spa_bundle"):
            out["extracted_data"] = {
                "api_calls": bundle["spa_bundle"].get("api_calls", []),
                "files": bundle["spa_bundle"].get("files", []),
            }

        if discover_links:
            out["nested_candidates"] = discover_related_links(
                html, url, max_links=max_discovered_links
            )

        return out

    except Exception as e:
        # ✅ LAST-RESORT SAFETY NET
        return {
            "url": url,
            "error": "Analyze failed safely",
            "details": str(e),
        }

# backend/app/analyzers/website_classifier.py

def classify_website(page_access_result, page_structure):
    """
    Website classification:

    C1_STATIC_HTML        → HTML tables + file downloads (WordPress, CMS)
    C2_INTERACTIVE_HTML   → UI interactions but no data backend
    C3_SPA_APP            → table data backed by real data APIs
    C4_BLOCKED            → bot protection
    C5_NOT_DATA_PAGE      → irrelevant page
    """

    # ================================
    # C4: BLOCKED
    # ================================
    if isinstance(page_access_result, dict) and page_access_result.get("type") == "blocked":
        return {
            "website_class": "C4_BLOCKED",
            "reason": "Page blocked by security or bot protection.",
            "recommended_strategy": "NOT_ACCESSIBLE",
        }

    structure = page_structure.get("structure", {})
    ui = page_structure.get("ui", {})
    data_loc = page_structure.get("data_location", {})
    data_char = page_structure.get("data_characteristics", {})

    has_table = structure.get("has_table", False)
    has_file_links = structure.get("has_file_links", False)
    has_buttons = structure.get("has_buttons", False)
    has_pagination = structure.get("has_pagination", False)
    has_dropdowns = ui.get("dropdowns", False)

    api_info = data_loc.get("api", {})
    api_detected = api_info.get("detected", False)
    api_endpoints = api_info.get("endpoints", []) or []

    # ================================
    # ✅ C3: SPA / DATA APPLICATION (STRICT)
    # ================================
    # ONLY classify as SPA if there is evidence of a DATA API
    data_api_signals = [
        "/api/now/",
        "/servicenow",
        "/query",
        "/list",
        "/search",
        "/export",
        "/datatable",
    ]

    has_data_api = any(
        any(sig in ep.lower() for sig in data_api_signals)
        for ep in api_endpoints
    )

    if api_detected and has_data_api:
        return {
            "website_class": "C3_SPA_APP",
            "reason": (
                "Table data is rendered by application UI and backed by a real "
                "data API (ServiceNow / data-driven SPA)."
            ),
            "recommended_strategy": "API_OBSERVATION",
        }

    # ================================
    # ✅ C1: STATIC HTML CATALOG
    # ================================
    if has_table and has_file_links:
        return {
            "website_class": "C1_STATIC_HTML",
            "reason": "Fee schedule data present directly in HTML tables with downloadable files.",
            "recommended_strategy": "DIRECT_HTML_AND_FILES",
        }

    # ================================
    # C2: INTERACTIVE HTML
    # ================================
    if has_buttons or has_dropdowns or has_pagination:
        return {
            "website_class": "C2_INTERACTIVE_HTML",
            "reason": "UI interactions present, but no backend data API detected.",
            "recommended_strategy": "UI_INTERACTION_DOWNLOAD",
        }

    # ================================
    # C5: NOT A DATA PAGE
    # ================================
    return {
        "website_class": "C5_NOT_DATA_PAGE",
        "reason": "No extractable fee schedule data detected.",
        "recommended_strategy": "IGNORE_PAGE",
    }

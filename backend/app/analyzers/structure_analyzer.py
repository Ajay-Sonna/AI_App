# # backend/app/analyzers/structure_analyzer.py

# import re

# from bs4 import BeautifulSoup, Tag

# from app.analyzers.file_link_signals import collect_file_link_hrefs


# def _table_fingerprint(table) -> str:
#     first = table.find("tr")
#     if not first:
#         return ""
#     cells = first.find_all(["th", "td"])
#     return "|".join(c.get_text(strip=True)[:50] for c in cells[:10])


# _NAV_HEADER_NOISE = re.compile(
#     r"\b("
#     r"user information|login|logout|sign in|register|password|forgot|"
#     r"menu|navigation|contact us|privacy policy|skip to content)\b",
#     re.I,
# )

# _CATALOG_HEADER_HINTS = re.compile(
#     r"\b(fee|schedule|document|title|type|date|file|name|effective|code|description|"
#     r"download|format|version|category|posted|updated)\b",
#     re.I,
# )


# def _rows_belonging_to_table(table: Tag) -> list:
#     """`<tr>` nodes whose nearest `<table>` ancestor is this table (skip nested grids)."""
#     rows: list = []
#     for tr in table.find_all("tr"):
#         if tr.find_parent("table") is table:
#             rows.append(tr)
#     return rows


# def _row_looks_like_nav_header(non_empty: list[str]) -> bool:
#     blob = " ".join(non_empty).lower()
#     if _NAV_HEADER_NOISE.search(blob):
#         return True
#     if len(non_empty) == 2 and any("login" in t.lower() for t in non_empty):
#         return True
#     return False


# def _score_header_candidate(
#     cells: list,
#     cols: list[str],
#     non_empty: list[str],
#     all_trs: list,
#     row_index: int,
# ) -> int:
#     score = len(cols) * 4
#     joined = " ".join(cols)
#     if _CATALOG_HEADER_HINTS.search(joined):
#         score += 18
#     score += sum(2 for c in cells if getattr(c, "name", None) == "th")
#     score += sum(1 for t in non_empty if len(t) <= 45)
#     score -= sum(3 for t in non_empty if len(t) > 90)
#     if row_index + 1 < len(all_trs):
#         nxt_cells = all_trs[row_index + 1].find_all(["td", "th"])
#         n_vals = len([x for x in nxt_cells if x.get_text(strip=True)])
#         if n_vals >= max(2, len(cols) - 1):
#             score += 8
#     return score


# def infer_catalog_table_split(table: Tag) -> tuple[list[str], list]:
#     """
#     Columns plus body `<tr>` tags for this table only (ignores nested tables).

#     Picks a header row by scoring (catalog-like labels, th cells, following row shape)
#     and drops common portal/nav banner rows.
#     """
#     thead = table.find("thead")
#     if thead:
#         ths = thead.find_all("th")
#         if ths:
#             cols = _clean_column_labels([th.get_text(strip=True) for th in ths])
#             if cols:
#                 body: list = []
#                 for tr in _rows_belonging_to_table(table):
#                     if tr.find_parent("thead"):
#                         continue
#                     body.append(tr)
#                 return cols, body

#     all_trs = _rows_belonging_to_table(table)
#     best_idx = -1
#     best_score = -1
#     best_cols: list[str] = []

#     for i, tr in enumerate(all_trs[:22]):
#         cells = tr.find_all(["th", "td"])
#         texts = [c.get_text(strip=True) for c in cells]
#         non_empty = [t for t in texts if t]
#         if len(non_empty) < 2:
#             continue
#         if _row_looks_like_nav_header(non_empty):
#             continue
#         cols = _clean_column_labels(texts)
#         if len(cols) < 2:
#             continue
#         score = _score_header_candidate(cells, cols, non_empty, all_trs, i)
#         if score > best_score:
#             best_score = score
#             best_idx = i
#             best_cols = cols

#     if best_idx < 0:
#         return [], []
#     body_trs = all_trs[best_idx + 1 :]
#     return best_cols, body_trs


# def _extract_table_columns(table) -> list[str]:
#     """Prefer real catalog headers; avoid portal/nav rows mixed into layout tables."""
#     cols, _ = infer_catalog_table_split(table)
#     return cols


# def _clean_column_labels(texts: list[str]) -> list[str]:
#     out: list[str] = []
#     for raw in texts:
#         t = raw.strip()
#         if not t:
#             continue
#         if t.startswith("»"):
#             t = t.lstrip("»").strip()
#         tl = t.lower()
#         if "rows returned" in tl and "fee schedule" in tl:
#             continue
#         if len(t) > 120:
#             continue
#         out.append(t)
#     return out


# # Cap table metadata to keep responses bounded (very large pages still analyzed in full downstream).
# MAX_TABLES_METADATA = 48


# def list_unique_tables(soup: BeautifulSoup) -> list:
#     """Deduplicate visually identical tables (responsive / nested duplicates)."""
#     tables = soup.find_all("table")
#     seen_fp: set[str] = set()
#     unique_tables: list = []
#     for t in tables:
#         fp = _table_fingerprint(t)
#         if fp and fp in seen_fp:
#             continue
#         if fp:
#             seen_fp.add(fp)
#         unique_tables.append(t)
#     return unique_tables


# def analyze_page_structure(input_data):
#     """
#     Accepts:
#         - HTML string
#         - OR dict (API response from Playwright)

#     Returns:
#         Structured analysis for downstream extractor
#     """

#     # =========================
#     # 🔹 BASE RESULT TEMPLATE
#     # =========================
#     result = {
#         "page_type": None,
#         "confidence": 0.0,

#         "structure": {
#             "has_table": False,
#             "has_list": False,
#             "has_buttons": False,
#             "has_file_links": False,
#             "has_pagination": False
#         },

#         "data_location": {
#             "primary_source": None,

#             "api": {
#                 "detected": False,
#                 "endpoints": []
#             },

#             "html": {
#                 "tables": []
#             },

#             "files": {
#                 "types": [],
#                 "count": 0
#             }
#         },

#         "ui": {
#             "filters": False,
#             "search": False,
#             "dropdowns": False,
#             "pagination": False
#         },

#         "data_characteristics": {
#             "structured": False,
#             "format": None,
#             "estimated_rows": None
#         },

#         "recommended_strategy": []
#     }

#     # =========================
#     # 🔥 CASE 1: API RESPONSE
#     # =========================
#     if isinstance(input_data, dict) and input_data.get("type") == "api":
#         api_data = input_data.get("data", {})

#         result["page_type"] = "api_driven_app"
#         result["confidence"] = 0.95

#         result["data_location"]["primary_source"] = "api"
#         result["data_location"]["api"]["detected"] = True

#         # Try to extract useful metadata
#         try:
#             records = api_data.get("result", {}).get("data", {}).get("list", [])

#             if records:
#                 sample = records[0]

#                 columns = list(sample.keys())

#                 result["data_characteristics"]["structured"] = True
#                 result["data_characteristics"]["format"] = "json"
#                 result["data_characteristics"]["estimated_rows"] = len(records)

#                 result["data_location"]["api"]["endpoints"].append({
#                     "url": "captured_via_playwright",
#                     "method": "GET",
#                     "response_type": "json",
#                     "columns": columns
#                 })

#         except Exception:
#             pass

#         result["recommended_strategy"] = [
#             "use_api",
#             "paginate_api",
#             "normalize_json"
#         ]

#         return result

#     # =========================
#     # 🔹 CASE 2: HTML RESPONSE
#     # =========================
#     html = input_data

#     soup = BeautifulSoup(html, "html.parser")

#     # -------- TABLE DETECTION (dedupe identical responsive / nested duplicates) --------
#     unique_tables = list_unique_tables(soup)

#     if unique_tables:
#         result["structure"]["has_table"] = True

#         total_rows = 0
#         for idx, table in enumerate(unique_tables[:MAX_TABLES_METADATA]):
#             columns = _extract_table_columns(table)
#             row_count = len(table.find_all("tr"))
#             total_rows += row_count
#             result["data_location"]["html"]["tables"].append({
#                 "index": idx,
#                 "columns": columns,
#                 "row_count": row_count,
#             })
#         result["data_characteristics"]["estimated_rows"] = total_rows

#     # -------- LIST DETECTION --------
#     if soup.find("ul") or soup.find("ol"):
#         result["structure"]["has_list"] = True

#     # -------- BUTTON / LINK DETECTION --------
#     links = soup.find_all("a")
#     buttons = soup.find_all("button")

#     if links or buttons:
#         result["structure"]["has_buttons"] = True

#     # -------- FILE LINK DETECTION (extensions + anchor text + ASP.NET-style URLs) --------
#     file_links = collect_file_link_hrefs(soup)

#     if file_links:
#         result["structure"]["has_file_links"] = True
#         types_found: set[str] = set()
#         for href in file_links:
#             h = href.lower()
#             if ".pdf" in h:
#                 types_found.add("pdf")
#             if ".xlsx" in h or ".xls" in h:
#                 types_found.add("xlsx")
#             if ".csv" in h:
#                 types_found.add("csv")
#         result["data_location"]["files"]["types"] = sorted(types_found) if types_found else ["pdf", "xlsx"]
#         result["data_location"]["files"]["count"] = len(file_links)

#     # -------- PAGINATION DETECTION --------
#     if soup.find("a", href=True, string=re.compile(r"next", re.I)):
#         result["structure"]["has_pagination"] = True
#         result["ui"]["pagination"] = True
#     elif soup.find(string=re.compile(r"\bnext\b", re.I)):
#         result["structure"]["has_pagination"] = True
#         result["ui"]["pagination"] = True

#     # -------- UI DETECTION --------
#     if soup.find("select"):
#         result["ui"]["dropdowns"] = True

#     if soup.find("input"):
#         result["ui"]["search"] = True

#     # =========================
#     # 🔥 FINAL CLASSIFICATION
#     # =========================

#     if result["structure"]["has_table"]:
#         result["page_type"] = "static_html"
#         result["confidence"] = 0.9

#         if result["structure"]["has_file_links"]:
#             result["data_location"]["primary_source"] = "mixed"
#             result["recommended_strategy"] = [
#                 "parse_html_table",
#                 "extract_file_links",
#                 "download_files",
#             ]
#         else:
#             result["data_location"]["primary_source"] = "html"
#             result["recommended_strategy"] = [
#                 "parse_html_table",
#             ]

#         result["data_characteristics"]["structured"] = True
#         result["data_characteristics"]["format"] = "table"

#     elif result["structure"]["has_file_links"]:
#         result["page_type"] = "file_repository"
#         result["confidence"] = 0.85

#         result["data_location"]["primary_source"] = "files"

#         # Treat downloadable artifacts as structured targets for classification (not free-form prose).
#         result["data_characteristics"]["structured"] = True
#         result["data_characteristics"]["format"] = "files"

#         result["recommended_strategy"] = [
#             "extract_file_links",
#             "download_files"
#         ]

#     else:
#         result["page_type"] = "unstructured_page"
#         result["confidence"] = 0.5

#         result["data_location"]["primary_source"] = "unknown"

#         result["recommended_strategy"] = [
#             "llm_analysis"
#         ]

#     return result

# backend/app/analyzers/structure_analyzer.py

import re

from bs4 import BeautifulSoup, Tag

from app.analyzers.file_link_signals import collect_file_link_hrefs


def _table_fingerprint(table) -> str:
    first = table.find("tr")
    if not first:
        return ""
    cells = first.find_all(["th", "td"])
    return "|".join(c.get_text(strip=True)[:50] for c in cells[:10])


_NAV_HEADER_NOISE = re.compile(
    r"\b("
    r"user information|login|logout|sign in|register|password|forgot|"
    r"menu|navigation|contact us|privacy policy|skip to content)\b",
    re.I,
)

_CATALOG_HEADER_HINTS = re.compile(
    r"\b(fee|schedule|document|title|type|date|file|name|effective|code|description|"
    r"download|format|version|category|posted|updated)\b",
    re.I,
)


def _rows_belonging_to_table(table: Tag) -> list:
    rows: list = []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is table:
            rows.append(tr)
    return rows


def _row_looks_like_nav_header(non_empty: list[str]) -> bool:
    blob = " ".join(non_empty).lower()
    if _NAV_HEADER_NOISE.search(blob):
        return True
    if len(non_empty) == 2 and any("login" in t.lower() for t in non_empty):
        return True
    return False


def _score_header_candidate(
    cells: list,
    cols: list[str],
    non_empty: list[str],
    all_trs: list,
    row_index: int,
) -> int:
    score = len(cols) * 4
    if len(cols) > 24:
        score -= (len(cols) - 24) * 25
    joined = " ".join(cols)
    if _CATALOG_HEADER_HINTS.search(joined):
        score += 18
    score += sum(2 for c in cells if getattr(c, "name", None) == "th")
    score += sum(1 for t in non_empty if len(t) <= 45)
    score -= sum(3 for t in non_empty if len(t) > 90)
    if row_index + 1 < len(all_trs):
        nxt_cells = all_trs[row_index + 1].find_all(["td", "th"])
        n_vals = len([x for x in nxt_cells if x.get_text(strip=True)])
        if n_vals >= max(2, len(cols) - 1):
            score += 8
    return score


def infer_catalog_table_split(table: Tag) -> tuple[list[str], list]:
    thead = table.find("thead")
    if thead:
        ths = thead.find_all("th")
        if ths:
            cols = _clean_column_labels([th.get_text(strip=True) for th in ths])
            if cols:
                body: list = []
                for tr in _rows_belonging_to_table(table):
                    if tr.find_parent("thead"):
                        continue
                    body.append(tr)
                return cols, body

    all_trs = _rows_belonging_to_table(table)
    best_idx = -1
    best_score = -1
    best_cols: list[str] = []

    for i, tr in enumerate(all_trs[:22]):
        cells = tr.find_all(["th", "td"])
        texts = [c.get_text(strip=True) for c in cells]
        non_empty = [t for t in texts if t]
        if len(non_empty) < 2:
            continue
        if _row_looks_like_nav_header(non_empty):
            continue
        cols = _clean_column_labels(texts)
        if len(cols) < 2:
            continue
        score = _score_header_candidate(cells, cols, non_empty, all_trs, i)
        if score > best_score:
            best_score = score
            best_idx = i
            best_cols = cols

    if best_idx < 0:
        return [], []
    body_trs = all_trs[best_idx + 1 :]
    return best_cols, body_trs


def _extract_table_columns(table) -> list[str]:
    cols, _ = infer_catalog_table_split(table)
    return cols


def _clean_column_labels(texts: list[str]) -> list[str]:
    out: list[str] = []
    for raw in texts:
        t = raw.strip()
        if not t:
            continue
        if t.startswith("»"):
            t = t.lstrip("»").strip()
        tl = t.lower()
        if "rows returned" in tl and "fee schedule" in tl:
            continue
        if len(t) > 120:
            continue
        out.append(t)
    return out


MAX_TABLES_METADATA = 48


def list_unique_tables(soup: BeautifulSoup) -> list:
    tables = soup.find_all("table")
    seen_fp: set[str] = set()
    unique_tables: list = []
    for t in tables:
        fp = _table_fingerprint(t)
        if fp and fp in seen_fp:
            continue
        if fp:
            seen_fp.add(fp)
        unique_tables.append(t)
    return unique_tables


def analyze_page_structure(input_data):
    result = {
        "page_type": None,
        "confidence": 0.0,
        "structure": {
            "has_table": False,
            "has_list": False,
            "has_buttons": False,
            "has_file_links": False,
            "has_pagination": False,
        },
        "data_location": {
            "primary_source": None,
            "api": {"detected": False, "endpoints": []},
            "html": {"tables": []},
            "files": {"types": [], "count": 0},
        },
        "ui": {
            "filters": False,
            "search": False,
            "dropdowns": False,
            "pagination": False,
        },
        "data_characteristics": {
            "structured": False,
            "format": None,
            "estimated_rows": None,
        },
        "recommended_strategy": [],
    }

    # =========================
    # HTML RESPONSE
    # =========================
    html = input_data
    soup = BeautifulSoup(html, "html.parser")

    unique_tables = list_unique_tables(soup)
    if unique_tables:
        result["structure"]["has_table"] = True
        total_rows = 0
        for idx, table in enumerate(unique_tables[:MAX_TABLES_METADATA]):
            columns = _extract_table_columns(table)
            row_count = len(table.find_all("tr"))
            total_rows += row_count
            result["data_location"]["html"]["tables"].append(
                {"index": idx, "columns": columns, "row_count": row_count}
            )
        result["data_characteristics"]["estimated_rows"] = total_rows

    if soup.find("ul") or soup.find("ol"):
        result["structure"]["has_list"] = True

    links = soup.find_all("a")
    buttons = soup.find_all("button")
    if links or buttons:
        result["structure"]["has_buttons"] = True

    file_links = collect_file_link_hrefs(soup)
    if file_links:
        result["structure"]["has_file_links"] = True
        types_found: set[str] = set()
        for href in file_links:
            h = href.lower()
            if ".pdf" in h:
                types_found.add("pdf")
            if ".xlsx" in h or ".xls" in h:
                types_found.add("xlsx")
            if ".csv" in h:
                types_found.add("csv")
        result["data_location"]["files"]["types"] = sorted(types_found)
        result["data_location"]["files"]["count"] = len(file_links)

    if soup.find("a", href=True, string=re.compile(r"next", re.I)):
        result["structure"]["has_pagination"] = True
        result["ui"]["pagination"] = True
    elif soup.find(string=re.compile(r"\bnext\b", re.I)):
        result["structure"]["has_pagination"] = True
        result["ui"]["pagination"] = True

    if soup.find("select"):
        result["ui"]["dropdowns"] = True
    if soup.find("input"):
        result["ui"]["search"] = True

    # =========================
    # ✅ FINAL CLASSIFICATION PATCH
    # =========================

    if (
        result["structure"]["has_table"]
        and result["structure"]["has_buttons"]
        and result["structure"]["has_pagination"]
    ):
        result["page_type"] = "dynamic_application_rendered"
        result["confidence"] = 0.9
        result["data_location"]["primary_source"] = "api"
        result["data_location"]["api"]["detected"] = True
        result["recommended_strategy"] = [
            "observe_api_calls",
            "extract_via_api_or_dom_projection",
        ]

    elif result["structure"]["has_table"]:
        result["page_type"] = "static_html"
        result["confidence"] = 0.9
        result["data_location"]["primary_source"] = "mixed"
        result["data_characteristics"]["structured"] = True
        result["data_characteristics"]["format"] = "table"
        result["recommended_strategy"] = [
            "parse_html_table",
            "extract_file_links",
            "download_files",
        ]

    elif result["structure"]["has_file_links"]:
        result["page_type"] = "file_repository"
        result["confidence"] = 0.85
        result["data_location"]["primary_source"] = "files"
        result["data_characteristics"]["structured"] = True
        result["data_characteristics"]["format"] = "files"
        result["recommended_strategy"] = ["extract_file_links", "download_files"]

    else:
        result["page_type"] = "unstructured_page"
        result["confidence"] = 0.5
        result["data_location"]["primary_source"] = "unknown"
        result["recommended_strategy"] = ["llm_analysis"]

    return result

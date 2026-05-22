# backend/app/analyzers/section_extractor.py

from bs4 import BeautifulSoup, Tag
from typing import List, Dict, Optional, Any

from app.analyzers.file_link_signals import looks_like_file_link, file_type_hints_from_anchor
from app.analyzers.structure_analyzer import list_unique_tables, _extract_table_columns


def _appears_between_headers(header: Tag, node: Tag, next_header: Optional[Tag]) -> bool:
    """True if node appears in document order after header and before next_header."""
    seen = False
    for el in header.next_elements:
        if el is header:
            seen = True
            continue
        if not seen:
            continue
        if next_header is not None and el is next_header:
            return False
        if el is node:
            return True
    return False


def extract_page_sections(html: str) -> List[Dict]:
    """
    Breaks a webpage into logical sections the way a human would see them.

    A section is typically defined by:
    - Headings (h1, h2, h3)
    - Content following the heading until the next heading

    Returns:
        [
          {
            "title": "Current Fee Schedules",
            "text_sample": "...",
            "has_table": True,
            "has_file_links": True,
            "file_types": ["xlsx", "pdf"]
          },
          ...
        ]
    """

    soup = BeautifulSoup(html, "html.parser")

    sections = []

    # Find all meaningful headers (h4 often used in portal sidebars / widgets)
    headers = soup.find_all(["h1", "h2", "h3", "h4"])

    if not headers:
        # Fallback: entire page as one section
        return [_analyze_section("Entire Page", soup)]

    for idx, header in enumerate(headers):
        title = header.get_text(strip=True)

        following_headers = set(headers[idx + 1 :])
        next_bound = headers[idx + 1] if idx + 1 < len(headers) else None

        content_nodes = []
        next_node = header.find_next_sibling()

        while next_node:
            if next_node in following_headers:
                break
            content_nodes.append(next_node)
            next_node = next_node.find_next_sibling()

        combined = "".join(str(n) for n in content_nodes)

        # Portal layouts often wrap the table so it is not a direct sibling of the heading.
        frag = BeautifulSoup(combined, "html.parser")
        if not frag.find("table"):
            if next_bound is not None:
                combined += "".join(
                    str(t)
                    for t in soup.find_all("table")
                    if _appears_between_headers(header, t, next_bound)
                )
            else:
                combined += "".join(
                    str(t)
                    for t in soup.find_all("table")
                    if _appears_between_headers(header, t, None)
                )

        section_soup = (
            BeautifulSoup(combined, "html.parser")
            if combined.strip()
            else BeautifulSoup("", "html.parser")
        )

        section_data = _analyze_section(title, section_soup)
        sections.append(section_data)

    return sections


def _analyze_section(title: str, soup: BeautifulSoup) -> Dict:
    """
    Analyzes a single section for structural signals.
    No AI. Deterministic only.
    """

    text_sample = soup.get_text(separator=" ", strip=True)[:500]

    tables = soup.find_all("table")
    links = soup.find_all("a")

    file_links: list[str] = []
    ext_hints: set[str] = set()
    for link in links:
        href = link.get("href")
        if not href:
            continue
        text = link.get_text(" ", strip=True)
        if looks_like_file_link(href, text, link):
            file_links.append(href)
            ext_hints.update(file_type_hints_from_anchor(text))

    file_types = sorted(ext_hints) if ext_hints else (
        sorted({h.split(".")[-1].lower() for h in file_links if "." in h[-6:]}) or []
    )

    return {
        "title": title,
        "text_sample": text_sample,
        "has_table": bool(tables),
        "has_file_links": bool(file_links),
        "file_types": file_types,
        "estimated_file_count": len(file_links),
    }


def _table_heading_context(table: Tag) -> str:
    cap = table.find("caption")
    if cap:
        return cap.get_text(" ", strip=True)[:300]

    prev = table.find_previous(["h1", "h2", "h3", "h4", "strong", "legend"])
    attempts = 0
    while prev is not None and attempts < 8:
        text = prev.get_text(" ", strip=True)
        if text and len(text) < 500:
            return text[:300]
        prev = prev.find_previous(["h1", "h2", "h3", "h4", "strong", "legend"])
        attempts += 1
    return ""


def _sample_data_rows(table: Tag, max_rows: int = 3) -> list[list[str]]:
    rows_out: list[list[str]] = []
    for tr in table.find_all("tr"):
        if len(rows_out) >= max_rows:
            break
        cells = tr.find_all(["td"])
        if not cells:
            continue
        vals = [c.get_text(" ", strip=True)[:120] for c in cells[:14]]
        if any(vals):
            rows_out.append(vals)
    return rows_out


def _link_samples_from_table(table: Tag, limit: int = 8) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for a in table.find_all("a", href=True):
        if len(out) >= limit:
            break
        href = (a.get("href") or "").strip()
        text = a.get_text(" ", strip=True)
        if not href:
            continue
        out.append({"href": href[:500], "text": text[:200]})
    return out


def extract_table_candidates(html: str) -> List[Dict[str, Any]]:
    """
    One candidate per deduped <table>, with headers / samples for LLM disambiguation
    (e.g. current vs archived schedules).
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = list_unique_tables(soup)
    out: list[dict[str, Any]] = []
    for i, table in enumerate(tables):
        columns = _extract_table_columns(table)
        row_count = len(table.find_all("tr"))
        link_samples = _link_samples_from_table(table)
        out.append({
            "id": f"table_{i}",
            "block_type": "table",
            "heading_hint": _table_heading_context(table),
            "columns": columns,
            "row_count": row_count,
            "data_sample": _sample_data_rows(table),
            "link_samples": link_samples,
            "has_file_links": bool(link_samples),
        })
    return out


def build_schedule_blocks(html: str) -> List[Dict[str, Any]]:
    """
    Unified analysis blocks: table-level candidates plus heading-based sections.
    Table blocks use ids table_0, table_1, ...; sections use section_0, ...
    """
    blocks: list[dict[str, Any]] = []
    for tb in extract_table_candidates(html):
        blocks.append(tb)

    sections = extract_page_sections(html)
    for i, sec in enumerate(sections):
        blocks.append({
            "id": f"section_{i}",
            "block_type": "heading_section",
            "title": sec["title"],
            "text_sample": sec["text_sample"],
            "has_table": sec["has_table"],
            "has_file_links": sec["has_file_links"],
            "file_types": sec.get("file_types", []),
            "estimated_file_count": sec.get("estimated_file_count", 0),
        })
    return blocks

# backend/app/llm/llm_client.py
from groq import Groq
from app.config.settings import GROQ_API_KEY, MODEL_NAME
from bs4 import BeautifulSoup
import re
import json
import time
from typing import Any, Optional

client = Groq(api_key=GROQ_API_KEY)


def _extract_json_object(text: str) -> Optional[dict]:
    try:
        json_str = re.search(r"\{.*\}", text, re.DOTALL)
        if not json_str:
            return None
        return json.loads(json_str.group())
    except Exception:
        return None


def _slim_blocks_for_prompt(blocks: list, max_blocks: int = 28) -> list:
    """Keep prompts small; preserve ids and disambiguation fields."""
    slim: list = []
    for b in blocks[:max_blocks]:
        entry = {
            "id": b.get("id"),
            "block_type": b.get("block_type"),
            "heading_hint": b.get("heading_hint"),
            "title": b.get("title"),
            "columns": (b.get("columns") or [])[:20],
            "row_count": b.get("row_count"),
            "has_table": b.get("has_table"),
            "has_file_links": b.get("has_file_links"),
            "file_types": b.get("file_types"),
            "estimated_file_count": b.get("estimated_file_count"),
        }
        ds = b.get("data_sample") or []
        entry["data_sample"] = ds[:2]
        ls = b.get("link_samples") or []
        entry["link_samples"] = ls[:6]
        ts = b.get("text_sample") or ""
        entry["text_sample"] = ts[:450]
        slim.append({k: v for k, v in entry.items() if v is not None})
    return slim


def classify_schedule_blocks_relevance(blocks: list) -> dict:
    """
    Decide which analysis blocks (tables / heading sections) hold CURRENT, ACTIVE fee
    schedules. Uses stable block ids (e.g. table_0, section_1).
    """
    slim = _slim_blocks_for_prompt(blocks)

    prompt = f"""
You are analyzing blocks extracted from a state Medicaid / healthcare fee schedule web page.

Task: Identify blocks that contain CURRENT, ACTIVE fee schedules to ingest (as of the effective date the user cares about).

IGNORE blocks that are clearly: archived, historical, superseded, retired, prior year, or not fee schedule data (navigation, unrelated forms).

Each block has a stable "id". You MUST copy ids EXACTLY into process_ids or ignore_ids.

Blocks:
{json.dumps(slim, indent=2)}

Return ONLY valid JSON:
{{
  "process_ids": ["table_0"],
  "ignore_ids": ["table_2"],
  "process": ["optional short labels for processed blocks"],
  "ignore": ["optional short labels for ignored blocks"],
  "reason": "short explanation",
  "confidence": 0.0
}}

confidence is 0–1 (how sure you are given the evidence).
Rules:
- Prefer table_* blocks when they list programs, fee schedules, download links, or fee-like columns (code, rate, amount, HCPCS, CPT, modifier, description).
- Put heading_section ids in process_ids ONLY if they contain unique files/tables not already represented in a selected table_* block. Do NOT include heading_section blocks that are only introductory, legal, or disclaimer text.
- If one block is current and another archived, put current in process_ids and archived in ignore_ids.
- If uncertain, lower confidence; still choose the best guess.
"""

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
    )

    content = response.choices[0].message.content.strip()
    parsed = _extract_json_object(content)
    if not parsed:
        return {
            "process_ids": [],
            "ignore_ids": [],
            "process": [],
            "ignore": [],
            "reason": "Failed to classify schedule blocks (LLM JSON parse error).",
            "confidence": 0.0,
        }
    parsed.setdefault("process_ids", [])
    parsed.setdefault("ignore_ids", [])
    parsed.setdefault("process", [])
    parsed.setdefault("ignore", [])
    parsed.setdefault("reason", "")
    parsed.setdefault("confidence", 0.0)
    return parsed


def _groq_transient_error(exc: BaseException) -> bool:
    """Retry only when a short backoff might help; not for daily token (TPD) exhaustion."""
    sl = str(exc).lower()
    if "tokens per day" in sl or "tokens_per_day" in sl:
        return False
    if "429" in sl or "rate limit" in sl or "rate_limit" in sl:
        return True
    if "503" in sl or "529" in sl or "over capacity" in sl or "timeout" in sl:
        return True
    return False


def normalize_fee_document_candidates(
    candidates: list[dict[str, Any]],
    page_url: str,
    *,
    max_candidates: int = 56,
    max_retries: int = 4,
) -> dict[str, Any]:
    """
    Given deterministic file-link candidates (url + section + anchor title), return which
    rows are likely Medicaid/Payer fee schedules vs nav/help/unrelated PDFs.

    URLs in output must be copied EXACTLY from input — Python validates.
    Retries on Groq rate limits / transient errors with backoff to reduce token-day spikes from immediate fails.
    """
    if not GROQ_API_KEY or not MODEL_NAME:
        return {
            "decisions": [],
            "summary": "Skipped: missing GROQ_API_KEY or MODEL_NAME.",
            "confidence": 0.0,
        }

    slim = candidates[:max_candidates]
    # Compact JSON saves prompt tokens (important under daily TPD caps).
    candidates_json = json.dumps(slim, ensure_ascii=True, separators=(",", ":"))
    prompt = f"""Government Medicaid fee-schedule index assistant.

Page: {page_url}

Candidates (file links from HTML). Each: url, section, title, file_type.

Task:
1) include:true = Medicaid/payer fee schedules, rate books, reimbursement files, program rates (APG, HCBS, HARP, etc.).
2) include:false only when clearly NOT schedule data: consumer price transparency, help/legal/FOIL/nav/noise.
3) Optional display_title if cleaner (same meaning only).

Rules: Echo each url EXACTLY. One decision per candidate, SAME ORDER as input. If unsure, include:true.

Candidates:
{candidates_json}

Return ONLY JSON:
{{"decisions":[{{"url":"<exact>","include":true,"reason":"brief","display_title":null}}],"summary":"one sentence","confidence":0.0}}"""

    last_err: BaseException | None = None
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.05,
            )

            content = response.choices[0].message.content.strip()
            parsed = _extract_json_object(content)
            if not parsed or not isinstance(parsed, dict):
                return {
                    "decisions": [],
                    "summary": "Failed to parse LLM JSON for fee document normalization.",
                    "confidence": 0.0,
                }
            parsed.setdefault("decisions", [])
            parsed.setdefault("summary", "")
            parsed.setdefault("confidence", 0.0)
            return parsed
        except Exception as e:
            last_err = e
            if attempt < max_retries - 1 and _groq_transient_error(e):
                delay = min(3 * (2**attempt), 45)
                time.sleep(delay)
                continue
            raise last_err from e


# ✅ Clean HTML
def clean_html(html):
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style"]):
        tag.decompose()

    return soup.get_text(separator=" ", strip=True)[:3000]


# ✅ LLM analysis
def analyze_structure(html):
    cleaned = clean_html(html)

    prompt = f"""
    Analyze this webpage content and return ONLY JSON.

    {{
      "structure": "table | div_table | list | file_link | mixed | unknown",
      "tool": "parse_html_table | parse_div_table | parse_list | extract_file_links | none",
      "reason": "short explanation"
    }}

    Content:
    {cleaned}
    """

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
    )

    content = response.choices[0].message.content.strip()

    # ✅ Extract JSON safely
    try:
        json_str = re.search(r"\{.*\}", content, re.DOTALL).group()
        return json.loads(json_str)
    except:
        print("LLM RAW:", content)
        return {
            "structure": "unknown",
            "tool": "none",
            "reason": "JSON parsing failed"
        }
    

def classify_sections_relevance(sections):
    """
    sections = [
      {
        "title": "Current Fee Schedules",
        "has_table": True,
        "has_file_links": True,
        "file_types": ["xlsx"]
      },
      ...
    ]
    """

    prompt = f"""
You are analyzing sections of a Medicaid fee schedule website.

Your task:
Identify which sections contain CURRENT, ACTIVE fee schedules
that should be ingested.

Ignore sections that are archived, historical, superseded,
or clearly marked as old data.

Sections:
{json.dumps(sections, indent=2)}

Return ONLY valid JSON in this format:
{{
  "process": ["section titles to process"],
  "ignore": ["section titles to ignore"],
  "reason": "short explanation"
}}
"""

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1
    )

    content = response.choices[0].message.content.strip()

    parsed = _extract_json_object(content)
    if parsed:
        return parsed
    return {
        "process": [],
        "ignore": [],
        "reason": "Failed to classify sections"
    }

# backend/app/tools/link_discovery.py

"""
Discover likely fee-schedule / rate / provider-manual links on a page for nested navigation.
Same-domain only by default to avoid crawling the open web.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

_FILE_HINT = (".pdf", ".xlsx", ".xls", ".csv", ".zip")

# Phrases common on Medicaid / state payer sites (expand per rollout).
_POSITIVE_TERMS: tuple[tuple[str, int], ...] = (
    ("fee schedule", 10),
    ("feeschedule", 10),
    ("rate book", 9),
    ("rate manual", 9),
    ("provider manual", 8),
    ("reimbursement", 8),
    ("remittance", 6),
    ("medi-cal", 7),
    ("medicaid", 7),
    ("cpt", 4),
    ("hcpcs", 4),
    ("allowable", 5),
    ("vendor drug", 7),
    ("pharmacy", 5),
    ("physician", 4),
    ("dental", 4),
    ("behavioral health", 6),
    ("bulletin", 3),
    ("archive", 2),
)

_PATH_HINT = re.compile(
    r"(fee|rate|reimburs|provider|manual|schedule|bulletin|policy|chapter)",
    re.I,
)


def _score_anchor(href: str, anchor_text: str) -> int:
    href_l = (href or "").lower()
    text_l = (anchor_text or "").lower()
    blob = f"{text_l} {href_l}"
    score = 0
    for phrase, pts in _POSITIVE_TERMS:
        if phrase in blob:
            score += pts
    if any(ext in href_l for ext in _FILE_HINT):
        score += 12
    if _PATH_HINT.search(href_l):
        score += 5
    return score


def discover_related_links(
    html: str,
    base_url: str,
    *,
    max_links: int = 25,
    same_domain_only: bool = True,
    min_score: int = 3,
) -> list[dict]:
    """
    Return ranked same-domain links that plausibly lead to schedules, manuals, or downloads.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_netloc = urlparse(base_url).netloc
    seen: set[str] = set()
    ranked: list[dict] = []

    for a in soup.find_all("a", href=True):
        raw = (a.get("href") or "").strip()
        if not raw or raw.startswith("#") or raw.lower().startswith("javascript:"):
            continue
        full = urljoin(base_url, raw)
        if same_domain_only and urlparse(full).netloc != base_netloc:
            continue
        if full in seen:
            continue
        seen.add(full)

        text = a.get_text(" ", strip=True)
        s = _score_anchor(full, text)
        file_hit = any(ext in full.lower() for ext in _FILE_HINT)
        if s < min_score and not file_hit:
            continue

        ranked.append(
            {
                "url": full,
                "anchor_text": text[:300] if text else "",
                "score": max(s, 1),
                "is_file_url": file_hit,
            }
        )

    ranked.sort(key=lambda x: (-x["score"], x["url"]))
    return ranked[:max_links]

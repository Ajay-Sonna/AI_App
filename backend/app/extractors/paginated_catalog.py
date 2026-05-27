# backend/app/extractors/paginated_catalog.py
"""Walk paginated HTML tables via generic next / numbered-page controls (Playwright)."""

from __future__ import annotations

import re
import time

from playwright.sync_api import Page, sync_playwright

from app.extractors.html_catalog import (
    catalog_row_signature,
    extract_deduped_table_catalog,
)


def _ancestor_li_disabled(el) -> bool:
    try:
        return bool(
            el.evaluate(
                """e => {
                const li = e.closest('li');
                if (!li) return false;
                const c = li.className || '';
                return /\\bdisabled\\b/i.test(c);
            }"""
            )
        )
    except Exception:
        return False


def _usable_next(locator) -> bool:
    if locator.count() == 0:
        return False
    first = locator.first
    try:
        if not first.is_visible():
            return False
        if (first.get_attribute("aria-disabled") or "").lower() == "true":
            return False
        cls = (first.get_attribute("class") or "").lower()
        if "disabled" in cls:
            return False
        if _ancestor_li_disabled(first):
            return False
    except Exception:
        return False
    return True


def _find_numbered_page_next(page: Page):
    """Active page item → following sibling link (1,2,3-style pagination)."""
    try:
        cur = page.locator('[aria-current="page"]').first
        if cur.count():
            nxt = cur.locator(
                "xpath=ancestor::li[1]/following-sibling::li[1]"
            ).locator("a, button").first
            if nxt.count() and _usable_next(nxt):
                return nxt
    except Exception:
        pass

    for sel in (
        "ul.pagination li.active",
        "ul.pagination li.page-item.active",
        "li.page-item.active",
        "nav.pagination li.active",
    ):
        try:
            active = page.locator(sel).first
            if active.count() == 0:
                continue
            nxt = active.locator("xpath=following-sibling::li[1]").locator(
                "a, button"
            ).first
            if nxt.count() and _usable_next(nxt):
                return nxt
        except Exception:
            continue
    return None


def _find_aspnet_postback_next(page: Page):
    """
    ASP.NET GridView / DataPager: javascript:__doPostBack(..., 'Page$Next').
    Avoid unrelated __doPostBack controls (sort, export, tab switches).
    """
    try:
        loc = page.locator('a[href*="__doPostBack"]')
        n = loc.count()
    except Exception:
        return None
    for i in range(min(n, 160)):
        cand = loc.nth(i)
        if not _usable_next(cand):
            continue
        try:
            href = (cand.get_attribute("href") or "").lower()
        except Exception:
            continue
        compact = re.sub(r"\s+", "", href)
        if "page$next" in compact or "page%24next" in compact:
            return cand
    return None


def _find_next_control(page: Page):
    """Text/icon next controls, then numbered list fallback."""
    trials = [
        page.locator('a[rel="next"]'),
        page.locator('a[title*="next" i]'),
        page.locator('a[aria-label*="next page" i]'),
        page.locator('a[aria-label*="next" i], button[aria-label*="next" i]'),
        page.get_by_role("link", name=re.compile(r"^\s*next\s*$", re.I)),
        page.get_by_role("link", name=re.compile(r"next\s*page", re.I)),
        page.get_by_role("link", name=re.compile(r"^\s*next\s*>\s*$", re.I)),
        page.get_by_role("button", name=re.compile(r"^\s*next\s*$", re.I)),
        page.locator('[aria-label*="go to next" i]'),
        page.locator('button[aria-label*="next" i]'),
        page.locator("li.next:not(.disabled) a"),
        page.locator("li.pagination-next:not(.disabled) a"),
        page.get_by_role("link", name=re.compile(r"^›$")),
        page.get_by_role("link", name=re.compile(r"^»$")),
        page.locator("button[title*='next' i], a[title*='next' i]"),
        page.locator('a[ng-click*="next" i], button[ng-click*="next" i]'),
        page.locator("[data-pagination-next], [data-page='next']"),
        page.locator('input[type="submit"][value*="next" i]'),
        page.locator('input[type="button"][value*="next" i]'),
        page.locator('a[href*="NextPageButton"]'),
        page.locator('a[href*="nextpagebutton" i]'),
    ]

    for loc in trials:
        try:
            if loc.count() == 0:
                continue
            cand = loc.first
            if _usable_next(cand):
                return cand
        except Exception:
            continue

    post = _find_aspnet_postback_next(page)
    if post is not None:
        return post

    return _find_numbered_page_next(page)


def extract_table_catalog_paginated(
    url: str,
    table_index: int,
    *,
    max_pages: int = 200,
    settle_ms: int = 6500,
    post_click_ms: int = 4500,
    max_wall_seconds: float = 90.0,
    fallback_html: str | None = None,
    fallback_base: str | None = None,
) -> dict:
    """
    Load url in a browser, parse the deduped table on each page, follow next /
    numbered pagination until exhausted.

    If the live DOM has no tables but analyze captured HTML did (e.g. different
    fetch paths), pass fallback_html + fallback_base for the first successful parse.
    """
    seen: set[tuple] = set()
    all_rows: list[dict] = []
    columns_ref: list[str] | None = None
    pages_visited = 0
    stagnant_rounds = 0
    used_snapshot_fallback = False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )
            page = context.new_page()
            page.set_default_timeout(90000)
            page.set_default_navigation_timeout(60_000)
            t_wall_start = time.monotonic()
            try:
                # networkidle is unreliable on Medicaid portals (continuous requests)
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                page.goto(url, wait_until="commit", timeout=60_000)
            try:
                page.wait_for_selector("table", timeout=20000)
            except Exception:
                pass
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            page.wait_for_timeout(settle_ms)

            for _ in range(max_pages):
                if time.monotonic() - t_wall_start > max_wall_seconds:
                    break
                pages_visited += 1
                html = page.content()
                chunk = extract_deduped_table_catalog(html, table_index, page.url)
                if (
                    chunk.get("error")
                    and pages_visited == 1
                    and fallback_html
                    and fallback_base is not None
                ):
                    fb = extract_deduped_table_catalog(
                        fallback_html, table_index, fallback_base
                    )
                    if not fb.get("error"):
                        used_snapshot_fallback = True
                    chunk = fb

                if chunk.get("error"):
                    if pages_visited == 1:
                        return {
                            **chunk,
                            "pages_visited": pages_visited,
                            "paginated": False,
                            "used_snapshot_fallback": used_snapshot_fallback,
                        }
                    break

                if columns_ref is None and chunk.get("columns"):
                    columns_ref = chunk["columns"]

                added = 0
                for row in chunk.get("rows") or []:
                    sig = catalog_row_signature(row)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    all_rows.append(row)
                    added += 1

                if pages_visited > 1 and added == 0:
                    stagnant_rounds += 1
                    if stagnant_rounds >= 2:
                        break
                else:
                    stagnant_rounds = 0

                nxt = _find_next_control(page)
                if nxt is None:
                    break
                try:
                    nxt.click()
                except Exception:
                    break
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=12_000)
                except Exception:
                    pass
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                page.wait_for_timeout(post_click_ms)

            return {
                "columns": columns_ref or [],
                "rows": all_rows,
                "pages_visited": pages_visited,
                "paginated": pages_visited > 1,
                "used_snapshot_fallback": used_snapshot_fallback,
            }
        finally:
            browser.close()

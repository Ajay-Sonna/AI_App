"""Interactive discovery: bounded Playwright scans for dropdown + submit widgets (recipe-driven)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.preview.preview_service import _DEFAULT_UA

logger = logging.getLogger(__name__)


def _normalize_skip_exact(s: str) -> str:
    return str(s or "").strip().lower()


_EXACT_SKIP_DEFAULTS = frozenset({"", "---", "choose an option"})


def _should_skip_option(
    text: str,
    value: str,
    exact_skips: frozenset,
    substring_skips: Tuple[str, ...],
) -> bool:
    raw_t = str(text).strip().lower()
    raw_v = str(value).strip()
    if not raw_t and not raw_v.strip():
        return True
    if raw_t in exact_skips or raw_v.strip().lower() in exact_skips:
        return True
    blob = f"{text}|||{value}".lower()
    for s in substring_skips:
        st = str(s).strip().lower()
        if st and st in blob:
            return True
    return False


def run_playwright_dropdown_submit(
    url: str,
    *,
    bundle: Dict[str, Any],
    recipe_id: str,
    params: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Locates pairs of visible ``select`` widgets with a descendant ``button``.
    Iterates bounded options per widget; before each iteration reloads the start URL so the DOM stays fresh.
    Captures HTTP(S) target after clicking the button (downloads are not unpacked here).
    """
    del bundle  # future hook: hydrate storage/session from ingestion bundle when available.

    meta: Dict[str, Any] = {"recipe_id": recipe_id, "strategy": "playwright_dropdown_submit"}

    btn_text = str(params.get("button_text") or "View Report").strip()
    settle_ms = max(250, min(15_000, int(params.get("settle_ms") or 2800)))
    nav_timeout_ms = max(8000, min(120_000, int(params.get("navigation_timeout_ms") or 55000)))
    max_widgets = max(1, min(80, int(params.get("max_widgets") or 26)))
    max_opts = max(1, min(120, int(params.get("max_options_per_widget") or 26)))
    headless = params.get("headless", True)
    if isinstance(headless, str):
        headless = headless.lower() in ("1", "true", "yes")

    extra_exact = frozenset(
        _normalize_skip_exact(x)
        for x in (params.get("skip_option_values_exact") or [])
        if str(x).strip()
    )
    exact_skips = _EXACT_SKIP_DEFAULTS | extra_exact

    substring_skips = tuple(
        str(x).strip()
        for x in (params.get("skip_option_substrings") or [])
        if str(x).strip()
    )

    rows_out: List[Dict[str, Any]] = []

    start = str(url).strip()

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=bool(headless))
            ctx = browser.new_context(user_agent=_DEFAULT_UA.strip())
            page = ctx.new_page()
            page.set_default_navigation_timeout(nav_timeout_ms)

            def _reload_baseline() -> None:
                page.goto(start, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                page.wait_for_timeout(settle_ms)

            _reload_baseline()

            selects = page.locator("select")
            n_sel = selects.count()

            usable_widget_indices: List[int] = []
            widget_labels: Dict[int, str] = {}

            for i in range(n_sel):
                if len(usable_widget_indices) >= max_widgets:
                    break
                sel = selects.nth(i)
                if not sel.is_visible():
                    continue
                scope = sel.locator("xpath=ancestor::*[ .//button ][1]")
                try:
                    if scope.count() == 0:
                        continue
                    if scope.get_by_role("button", name=btn_text).count() < 1:
                        continue
                except Exception:
                    continue
                usable_widget_indices.append(i)
                lbl = ""
                try:
                    t = sel.evaluate(
                        """el => {
                            const s = el.closest('article, section, [class*=card], main');
                            return s ? (s.innerText || '').trim() : '';
                        }"""
                    )
                    lines = [ln.strip() for ln in str(t).splitlines() if ln.strip()]
                    if lines:
                        lbl = lines[0][:420]
                except Exception:
                    pass
                if not lbl:
                    lbl = f"Widget {len(usable_widget_indices)}"
                widget_labels[i] = lbl

            meta["widgets_matched"] = len(usable_widget_indices)

            for wi, i in enumerate(usable_widget_indices):
                _reload_baseline()
                sel = page.locator("select").nth(i)
                scope = sel.locator("xpath=ancestor::*[ .//button ][1]")
                buttons = scope.get_by_role("button", name=btn_text)

                opts_raw: List[Dict[str, str]] = []
                try:
                    opts_raw = sel.evaluate(
                        """el => [...el.options].map(o => ({
                            value: String(o.value ?? ""),
                            text: String(o.textContent ?? "").trim()
                        }))"""
                    )
                except Exception:
                    opts_raw = []

                section_head = widget_labels.get(i, f"Dropdown {wi + 1}")

                for opt in opts_raw[:max_opts]:
                    val = str(opt.get("value") or "").strip()
                    ot = str(opt.get("text") or "").strip()
                    if _should_skip_option(ot, val, exact_skips, substring_skips):
                        continue

                    _reload_baseline()
                    sel = page.locator("select").nth(i)
                    scope = sel.locator("xpath=ancestor::*[ .//button ][1]")
                    buttons = scope.get_by_role("button", name=btn_text)

                    try:
                        if val:
                            sel.select_option(value=val)
                        elif ot:
                            sel.select_option(label=ot[:800])
                        else:
                            continue
                    except PlaywrightTimeoutError:
                        continue
                    except Exception as ex:
                        logger.info("recipe %s select_option skip: %s", recipe_id, ex)
                        continue

                    page.wait_for_timeout(400)

                    clicked = False
                    try:
                        with page.expect_navigation(timeout=min(30_000, nav_timeout_ms), wait_until="domcontentloaded"):
                            buttons.first.click()
                            clicked = True
                    except PlaywrightTimeoutError:
                        buttons.first.click()
                        clicked = True
                    except Exception:
                        try:
                            buttons.first.click(force=True)
                            clicked = True
                        except Exception:
                            pass

                    if clicked:
                        page.wait_for_timeout(settle_ms)

                    final_u = ""
                    try:
                        final_u = str(page.url or "").strip()
                    except Exception:
                        final_u = ""

                    lu = final_u.lower()
                    file_u = ""
                    if any(lu.split("?", 1)[0].endswith(ext) for ext in (
                        ".pdf", ".xlsx", ".xls", ".csv", ".docx", ".doc", ".zip", ".xlsm",
                    )):
                        file_u = final_u

                    disp = ot or val or "(option)"
                    display_label = f"{section_head}: {disp}"
                    row = {
                        "Section": section_head[:600],
                        "Title": display_label[:980],
                        "File URL": file_u or final_u[:2000],
                        "After navigation": final_u[:2000],
                        "Widget index": wi,
                        "_links": [{"url": file_u or final_u or start, "text": display_label[:400]}],
                    }
                    rows_out.append(row)

            browser.close()

    except Exception as ex:
        logger.warning("recipe_playwright_failed %s: %s", recipe_id, ex)
        meta["ok"] = False
        meta["error"] = str(ex)
        return (
            {
                "block_id": f"recipe_playwright:{recipe_id}",
                "columns": ["Section", "Title", "File URL", "After navigation", "Widget index"],
                "rows": rows_out[:2000],
                "row_count": len(rows_out),
                "pages_visited": max(1, len(rows_out)),
                "paginated": False,
                "source": "recipe:playwright_dropdown_submit",
                "recipe_id": recipe_id,
            },
            meta,
        )

    meta["ok"] = True
    meta["widgets_scanned"] = len(set(r.get("Widget index") for r in rows_out))
    meta["resolved_rows"] = len(rows_out)
    tab = {
        "block_id": f"recipe_playwright:{recipe_id}",
        "columns": ["Section", "Title", "File URL", "After navigation", "Widget index"],
        "rows": rows_out[:2000],
        "row_count": len(rows_out),
        "pages_visited": max(1, len(rows_out)),
        "paginated": False,
        "source": "recipe:playwright_dropdown_submit",
        "recipe_id": recipe_id,
    }
    return tab, meta

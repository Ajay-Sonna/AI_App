# # backend/app/strategies/api_observer.py

# from playwright.sync_api import sync_playwright


# def _compact_json_for_sample(obj, max_depth: int = 4, max_str: int = 280, max_list: int = 8, max_dict_keys: int = 40):
#     """
#     Shrinks JSON captured from SPAs so responses/logs stay small (e.g. ServiceNow page JSON with embedded CSS).
#     """
#     if max_depth <= 0:
#         return "..."

#     if isinstance(obj, dict):
#         out = {}
#         for i, (k, v) in enumerate(obj.items()):
#             if i >= max_dict_keys:
#                 out["_truncated_keys"] = len(obj) - max_dict_keys
#                 break
#             out[str(k)[:120]] = _compact_json_for_sample(
#                 v, max_depth - 1, max_str, max_list, max_dict_keys
#             )
#         return out

#     if isinstance(obj, list):
#         out = [_compact_json_for_sample(x, max_depth - 1, max_str, max_list, max_dict_keys) for x in obj[:max_list]]
#         if len(obj) > max_list:
#             out.append(f"... (+{len(obj) - max_list} items)")
#         return out

#     if isinstance(obj, str):
#         if len(obj) > max_str:
#             return f"{obj[:max_str]}... ({len(obj)} chars)"
#         return obj

#     if isinstance(obj, (int, float, bool)) or obj is None:
#         return obj

#     return str(obj)[:max_str]


# def observe_api_calls(url, timeout_ms=10000):
#     """
#     Observes network traffic for API responses and file downloads.
#     Does NOT bypass security.
#     """

#     observed_calls = []
#     downloaded_files = []

#     with sync_playwright() as p:
#         browser = p.chromium.launch(headless=True)

#         context = browser.new_context(
#             user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
#             viewport={"width": 1280, "height": 800},
#             locale="en-US",
#             timezone_id="America/New_York"
#         )

#         page = context.new_page()

#         def handle_response(response):
#             try:
#                 content_type = response.headers.get("content-type", "").lower()

#                 # Capture JSON API responses
#                 if "application/json" in content_type:
#                     data = response.json()
#                     sample = None
#                     if isinstance(data, dict):
#                         sample = _compact_json_for_sample(data)
#                     observed_calls.append({
#                         "url": response.url,
#                         "method": response.request.method,
#                         "type": "json",
#                         "data_sample": sample,
#                     })

#                 # Capture file downloads (XLSX / PDF)
#                 if any(ext in response.url.lower() for ext in [".xls", ".xlsx", ".pdf", ".csv"]):
#                     downloaded_files.append({
#                         "url": response.url,
#                         "method": response.request.method,
#                         "type": "file"
#                     })

#             except Exception:
#                 pass

#         page.on("response", handle_response)

#         html = ""
#         try:
#             page.goto(url, timeout=60000)
#             page.wait_for_timeout(timeout_ms)
#             html = page.content()
#         finally:
#             browser.close()

#     return {
#         "api_calls": observed_calls,
#         "files": downloaded_files,
#         "html": html,
#     }


# backend/app/strategies/api_observer.py
"""
API Observer (Playwright)
------------------------
Captures backend API calls made by SPA pages (e.g., ServiceNow).

CRITICAL FIX:
- Capture FULL POST request bodies (payload)
- Without payload, ServiceNow pagination cannot be replayed

This module acts as a "flight recorder" for network traffic.
It does NOT interpret or modify requests.
"""

from __future__ import annotations
from typing import Any, Dict, List

from playwright.sync_api import sync_playwright


# --------------------------------------------------
# Core observer
# --------------------------------------------------

def observe_api_calls(url: str) -> Dict[str, Any]:
    """
    Opens the page in Playwright, captures API calls + responses.

    Returns:
        {
          "html": final_rendered_html,
          "api_calls": [
              {
                "url": str,
                "method": str,
                "payload": dict | None,
                "data_sample": dict | None
              }
          ],
          "files": []
        }
    """

    api_calls: List[Dict[str, Any]] = []
    files: List[Dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # ------------------------------
        # REQUEST CAPTURE
        # ------------------------------
        def on_request(request):
            if request.method not in ("POST", "GET"):
                return

            record = {
                "url": request.url,
                "method": request.method,
                "payload": None,
                "data_sample": None,
            }

            # ✅ CRITICAL: capture POST body
            if request.method == "POST":
                try:
                    payload = request.post_data_json
                except Exception:
                    payload = request.post_data

                record["payload"] = payload

            api_calls.append(record)

        # ------------------------------
        # RESPONSE CAPTURE
        # ------------------------------
        def on_response(response):
            try:
                req = response.request
                url = req.url
                method = req.method

                # Find matching request record
                for rec in reversed(api_calls):
                    if rec["url"] == url and rec["method"] == method and rec["data_sample"] is None:
                        try:
                            if "application/json" in (response.headers.get("content-type") or ""):
                                rec["data_sample"] = response.json()
                        except Exception:
                            pass
                        break
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)

        page.set_default_navigation_timeout(55_000)

        # Never use networkidle for public portals — analytics/long-poll prevent "idle"
        # and can hang or exceed reasonable timeouts (e.g. Georgia DNN/MMIS).
        page.goto(url, wait_until="domcontentloaded", timeout=55_000)
        try:
            page.wait_for_load_state("load", timeout=25_000)
        except Exception:
            pass

        # Allow late SPA / XHR hydration (ServiceNow, portals) without waiting forever
        page.wait_for_timeout(4500)

        html = page.content()
        browser_cookies = context.cookies()

        # ServiceNow guest / logged-in API calls require X-UserToken (g_ck) on POST replay
        csrf_token = ""
        try:
            csrf_token = page.evaluate(
                """() => {
                if (typeof g_ck !== 'undefined' && g_ck) return g_ck;
                if (window.NOW && window.NOW.g_ck) return window.NOW.g_ck;
                return '';
            }"""
            )
        except Exception:
            csrf_token = ""

        browser.close()

    return {
        "html": html,
        "api_calls": api_calls,
        "files": files,
        "cookies": browser_cookies,
        "csrf_token": csrf_token or "",
    }

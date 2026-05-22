# backend/app/tools/scraper.py

import requests
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup


def is_security_page(html: str) -> bool:
    """
    Detect real bot/WAF challenge pages. Avoid single-token matches (e.g. ``akamai``,
    ``cloudflare``) that appear on normal .gov pages that load CDN scripts.
    """
    if not html or len(html) < 400:
        return True

    text = html.lower()

    # Phrases typical of block/interstitial pages, not normal embedded CDN references.
    security_markers = [
        "access denied",
        "you don't have permission to access",
        "request id:",
        "errors.edgesuite.net",
        "akamai ghost",
        "akamaighost",
        "attention required! | cloudflare",  # Cloudflare 5xx interstitial title-like
        "just a moment...",  # CF / some challenges
        "request blocked",
        "enable javascript",
        "enable cookies",
        "checking your browser",
        "verify you are human",
        "bot detection",
        "automated access",
    ]

    return any(marker in text for marker in security_markers)


def looks_like_real_ui(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")

    if soup.find(["select", "button", "input"]):
        return True

    text = soup.get_text(separator=" ", strip=True).lower()
    keywords = [
        "fee schedule",
        "fee",
        "schedule",
        "reimbursement",
        "provider",
        "rate book",
        "rate manual",
        "medi-cal",
        "medicaid",
        "hcpcs",
        "cpt",
        "allowable",
    ]

    return any(k in text for k in keywords)


def has_structural_data_signals(html: str) -> bool:
    """
    Many state sites use neutral wording; accept obvious data/table/file signals
    so we do not false-negative into 'blocked'.
    """
    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):
        h = (a.get("href") or "").lower()
        if any(ext in h for ext in [".pdf", ".xlsx", ".xls", ".csv", ".docx", ".doc", ".zip"]):
            return True

    tables = soup.find_all("table")
    for table in tables[:5]:
        rows = table.find_all("tr")
        if len(rows) >= 3:
            return True

    return False


def validate_real_page(html: str) -> bool:
    if is_security_page(html):
        return False

    if looks_like_real_ui(html):
        return True

    if has_structural_data_signals(html):
        return True

    return False



def fetch_with_requests(url):
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    response = requests.get(url, headers=headers, timeout=15)
    return response.text


def fetch_with_playwright(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        )

        page = context.new_page()

        page.goto(url, timeout=60000)

        # ✅ WAIT FOR ACTUAL CONTENT (IMPORTANT)
        try:
            page.wait_for_selector("table", timeout=15000)
        except:
            pass  # fallback if no table

        # small buffer
        page.wait_for_timeout(2000)

        html = page.content()

        browser.close()
        return html

# def is_blocked(html):
#     text = html.lower()

#     return any([
#         "cloudflare" in text,
#         "enable cookies" in text,
#         "access denied" in text,
#         "bot protection" in text,
#         len(html) < 1000
#     ])


# def fetch_webpage(url):
#     try:
#         html = fetch_with_requests(url)

#         if is_blocked(html):
#             print("⚠️ Switching to Playwright...")
#             html = fetch_with_playwright(url)

#     except Exception:
#         html = fetch_with_playwright(url)

#     # ✅ ADD THIS PART (THIS IS WHAT YOU ASKED ABOUT)
#     api_data = fetch_api_data_with_playwright(url)

#     if api_data:
#         return {"type": "api", "data": api_data}

#     return html

def fetch_webpage(url):
    """
    Fetch webpage content with a single safe retry.
    Ensures only REAL application UI is returned.
    """

    # ---------- Attempt 1: lightweight request ----------
    try:
        html = fetch_with_requests(url)
    except Exception:
        html = None

    if html and validate_real_page(html):
        return html

    # ---------- Attempt 2: real browser (single retry) ----------
    print("[scraper] Retrying with Playwright (real browser)...")
    try:
        html = fetch_with_playwright(url)
    except Exception:
        html = None

    if html and validate_real_page(html):
        return html

    # ---------- Still not real UI ----------
    return {
        "type": "blocked",
        "reason": "Security or interstitial page detected",
        "url": url
    }

def fetch_api_data_with_playwright(url):
    from playwright.sync_api import sync_playwright

    api_data = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        def handle_response(response):
            try:
                if "api" in response.url or "now" in response.url:
                    if "json" in response.headers.get("content-type", ""):
                        data = response.json()
                        api_data.append({
                            "url": response.url,
                            "data": data
                        })
            except:
                pass

        page.on("response", handle_response)

        page.goto(url, timeout=60000)
        page.wait_for_timeout(8000)

        browser.close()

    return api_data
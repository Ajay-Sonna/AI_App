# backend/app/tools/scraper.py

import logging

import requests
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Fail-closed phrases that reliably indicate bot walls / blocking interstitials.
_STRONG_SECURITY_MARKERS = (
    "access denied",
    "you don't have permission to access",
    "request id:",
    "errors.edgesuite.net",
    "akamai ghost",
    "akamaighost",
    "attention required! | cloudflare",  # Cloudflare 5xx interstitial title-like
    "request blocked",
    "verify you are human",
    "bot detection",
    "automated access",
)

# Noscript/accessibility/CMS copy often repeats these phrases on legitimate pages —
# flag them only when the document does NOT look like a real content shell.
_SOFT_SECURITY_MARKERS = (
    "just a moment...",  # CF delays (but also abused in benign templates)
    "checking your browser",
    "enable javascript",
    "enable cookies",
)


def _probably_real_application_shell(html: str) -> bool:
    """Prefer false negatives on WAF (Playwright retries) over blocking real .gov CMS pages."""
    if has_structural_data_signals(html):
        return True
    if len(html or "") >= 28000:
        return True
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        if soup.find("table") and len(soup.find_all("tr")) >= 4:
            return True
        if soup.find({"main", "article"}) and (
            soup.find("table") or soup.find(["select", "form"])
        ):
            return True
    except Exception:
        pass
    return False


def is_security_page(html: str) -> bool:
    """
    Detect real bot/WAF challenge pages. Avoid single-token matches (e.g. ``akamai``,
    ``cloudflare``) that appear on normal .gov pages that load CDN scripts.
    """
    if not html or len(html) < 400:
        return True

    text = html.lower()

    if any(marker in text for marker in _STRONG_SECURITY_MARKERS):
        return True

    if any(marker in text for marker in _SOFT_SECURITY_MARKERS):
        if _probably_real_application_shell(html):
            return False
        return True

    return False


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



def diagnose_fetch_blocked(samples: list[str]) -> str:
    """
    Produce a clearer operator hint when automation is blocked upstream (CDN/WAF edges).
    """
    blob_parts: list[str] = []
    for s in samples:
        if isinstance(s, str):
            blob_parts.append(s[:8000])
    blob = " ".join(blob_parts).lower()
    if "access denied" in blob and (
        "errors.edgesuite.net" in blob or "akamai" in blob or "akamaighost" in blob
    ):
        return (
            "CDN edge (often Akamai) returned Access Denied for this automated client/IP. "
            "The site may load in a desktop browser but not from this server's egress "
            "(IP reputation/automation fingerprint). Options: VPN/allowlisted IP, sanctioned egress "
            "proxy to this host, curated PDF links, or ingesting uploads instead of crawling."
        )
    if "just a moment" in blob or "checking your browser" in blob:
        return (
            "Site issued an interactive browser checkpoint. Automated fetch is blocked until "
            "the checkpoint can clear from this environment."
        )
    if "automated access" in blob or "bot detection" in blob:
        return "Upstream anti-automation rejected the session fingerprint."
    return ""



def fetch_with_requests(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    response = requests.get(url, headers=headers, timeout=15)
    return response.text


def fetch_with_playwright(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )

        page = context.new_page()

        page.goto(url, timeout=60000)

        # ✅ WAIT FOR ACTUAL CONTENT (IMPORTANT)
        try:
            page.wait_for_selector("table", timeout=15000)
        except Exception:
            pass

        page.wait_for_timeout(2500)

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
    samples_for_diag: list[str] = []

    # ---------- Attempt 1: lightweight request ----------
    html: str | None = None
    try:
        html = fetch_with_requests(url)
        if isinstance(html, str):
            samples_for_diag.append(html)
    except Exception:
        html = None

    if html and validate_real_page(html):
        return html

    # ---------- Attempt 2: real browser (single retry) ----------
    logger.info("[scraper] Retrying with Playwright for %s", url)
    try:
        html = fetch_with_playwright(url)
        if isinstance(html, str):
            samples_for_diag.append(html)
    except Exception:
        html = None

    if html and validate_real_page(html):
        return html

    # ---------- Still not real UI ----------
    detail = diagnose_fetch_blocked(samples_for_diag)
    return {
        "type": "blocked",
        "reason": "Security or interstitial page detected",
        "url": url,
        "detail": detail or None,
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
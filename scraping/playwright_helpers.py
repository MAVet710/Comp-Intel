"""
Playwright-based browser helpers for menu scraping.

Provides:
- browser_fetch(url) -> (html, captured_responses, final_url)
- try_bypass_age_gate(page) -> bool

Requires playwright to be installed:  playwright install chromium
"""

try:
    from playwright.sync_api import sync_playwright

    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

# Common confirmation text found on 21+ age gates (matched case-insensitively)
_AGE_GATE_TEXTS = [
    "i'm 21",
    "im 21",
    "i am 21",
    "i am 21+",
    "over 21",
    "21+",
    "yes, i'm 21",
    "yes i'm 21",
    "i agree",
    "enter site",
    "enter the site",
    "confirm age",
    "verify age",
    "i am of legal age",
    "yes, i am",
    "yes i am",
    "yes",
    "enter",
]

# CSS selectors targeting typical age-gate containers/buttons
_AGE_GATE_SELECTORS = [
    "[class*='age-gate'] button",
    "[class*='agegate'] button",
    "[class*='age_gate'] button",
    "[id*='age-gate'] button",
    "[id*='agegate'] button",
    "[class*='age-verification'] button",
    "[class*='ageVerification'] button",
    ".age-gate-button",
    "button[data-testid*='age']",
    "button[aria-label*='21']",
    "button[aria-label*='age']",
]

# Domains/patterns for which we always capture JSON responses
_CAPTURE_URL_PATTERNS = [
    "dutchie",
    "graphql",
    "iheartjane",
    "jane.menu",
    "weedmaps",
    "dispenseapp",
    "dispense.io",
    "tymberapp",
    "/api/",
    "/menu/",
    "/products",
    "/catalog",
]


def try_bypass_age_gate(page) -> bool:
    """
    Best-effort attempt to dismiss 21+ age verification gates.

    Strategy:
    1. Pre-set common localStorage age-verified keys.
    2. Try clicking buttons/links matched by CSS selectors for age-gate components.
    3. Try clicking buttons/links whose visible text matches common confirmation phrases.

    Returns True if an element was clicked, False otherwise.
    """
    # 1) Try specific CSS selectors
    for selector in _AGE_GATE_SELECTORS:
        try:
            el = page.query_selector(selector)
            if el and el.is_visible():
                el.click(timeout=3000)
                page.wait_for_timeout(1500)
                return True
        except Exception:
            pass

    # 2) Try text-based matching for buttons and links
    for text in _AGE_GATE_TEXTS:
        for tag in ["button", "a", "[role='button']"]:
            try:
                locator = page.locator(f"{tag}:has-text('{text}')").first
                if locator.is_visible(timeout=1000):
                    locator.click(timeout=3000)
                    page.wait_for_timeout(1500)
                    return True
            except Exception:
                pass

    return False


def browser_fetch(url: str, timeout: int = 45000) -> tuple:
    """
    Use Playwright to navigate to *url*, attempt to bypass 21+ age gates,
    capture JSON network responses from API/menu/Dutchie endpoints,
    and return the rendered HTML.

    Args:
        url: The target URL to navigate to.
        timeout: Navigation timeout in milliseconds (default 45 s).

    Returns:
        (html, captured_responses, final_url)

        - html (str): Fully-rendered page HTML after JS execution.
          Empty string on failure.
        - captured_responses (list[dict]): Each entry is
          {
            "url": <response_url>,
            "status": <http_status_int>,
            "content_type": <content-type header>,
            "json": <parsed JSON body or None>,
            "text_snippet": <first 200 chars of body text or None>,
            "data": <alias for json field – kept for backward compat>,
          }
          Responses are filtered to API/menu/JSON endpoints.
        - final_url (str): URL after any redirects/navigation.
    """
    if not HAS_PLAYWRIGHT:
        return "", [], url

    captured: list[dict] = []
    html = ""
    final_url = url

    def _should_capture(req_url: str, ctype: str) -> bool:
        """Return True if this response looks like an API/menu JSON response."""
        url_lower = req_url.lower()
        if any(pat in url_lower for pat in _CAPTURE_URL_PATTERNS):
            return True
        if "json" in ctype or "graphql" in ctype:
            return True
        return False

    def _on_response(response) -> None:
        """Capture JSON payloads from API/menu endpoints."""
        req_url = response.url
        ctype = (response.headers.get("content-type") or "").lower()
        if not _should_capture(req_url, ctype):
            return
        if "json" not in ctype and "graphql" not in ctype:
            return
        try:
            body = response.json()
            # Skip empty bodies — there's nothing useful to parse
            if not body:
                return
            entry = {
                "url": req_url,
                "status": response.status,
                "content_type": ctype,
                "json": body,
                "data": body,  # backward-compat alias
                "text_snippet": None,
            }
            captured.append(entry)
        except Exception:
            pass

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1400, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0.0.0 Safari/537.36"
                ),
            )
            # Pre-set localStorage keys commonly checked by age gates
            context.add_init_script(
                """
                try {
                    localStorage.setItem('ageVerified', 'true');
                    localStorage.setItem('age_verified', 'true');
                    localStorage.setItem('isAgeVerified', 'true');
                    localStorage.setItem('over21', 'true');
                    localStorage.setItem('ageGatePassed', 'true');
                } catch(e) {}
                """
            )
            page = context.new_page()
            page.on("response", _on_response)

            # Navigate and wait for the initial DOM to be ready
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_timeout(2000)

            # Attempt to dismiss the age gate
            try_bypass_age_gate(page)
            page.wait_for_timeout(2000)

            # Scroll to trigger lazy-loaded menu items
            for _ in range(3):
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(1000)

            final_url = page.url
            html = page.content()
            browser.close()
    except Exception:
        html = ""

    return html, captured, final_url

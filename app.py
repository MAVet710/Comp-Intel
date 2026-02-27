import io
import json
import re
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

from io import BytesIO
from PIL import Image
import pytesseract

# Try to import Playwright, but don't die if it's not available
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

# Try to import the Playwright-based browser helpers and Dutchie parser
try:
    from scraping.playwright_helpers import (
        browser_fetch,
        try_bypass_age_gate,
        auto_install_playwright_chromium,
        is_missing_browser_error,
    )
    from scraping.dutchie_parser import parse_dutchie_responses
    from scraping.dutchie_graphql import crawl_dutchie
    HAS_BROWSER_HELPERS = True
except Exception:
    HAS_BROWSER_HELPERS = False

_PLAYWRIGHT_MISSING_BINARY_WARNING = (
    "⚠️ **Playwright Chromium binary not found.**  \n"
    "This usually means the `postBuild` script did not run during deployment.  \n\n"
    "**To fix:**\n"
    "1. Trigger a clean rebuild on Streamlit Community Cloud "
    "(*Manage app → Reboot app* or delete and re-deploy).\n"
    "2. Check the build logs for `postBuild` output confirming "
    "`python -m playwright install chromium` ran successfully.\n"
    "3. Alternatively, set the `AUTO_INSTALL_PLAYWRIGHT=true` secret in your "
    "Streamlit Cloud app settings to enable one-time automatic installation at "
    "runtime.\n\n"
    "Browser-based scraping is disabled until the binary is available. "
    "Non-browser scraping will continue."
)

# -------------------------
# Streamlit page config
# -------------------------

st.set_page_config(
    page_title="Comp-Intel – Dispensary Menu Scanner",
    layout="wide",
)

st.title("Comp-Intel: Competitor Menu Scanner (beta)")

st.markdown(
    """
Paste a **dispensary website or menu link** below.  

The app will:

- Auto-detect the menu engine (**Dutchie, Jane, Weedmaps, Dispense, Tymber, or Generic**)
- For **Dutchie** menus: use a full GraphQL crawler — discovers all categories,
  paginates every page, filters to **in-stock only**, and extracts one row per
  purchasable variant (size / weight)
- For other JS-heavy menus: capture live API/GraphQL responses via Playwright
- Fall back to HTML / JSON-LD parsing, then OCR screenshot if needed
- Let you scan **MED and REC menus separately** and export everything to **Excel**

> **Setup note:** Browser mode requires Playwright's Chromium binary.  
> Run once: `playwright install chromium`
"""
)

# Initialise combined data storage
if "all_competitors" not in st.session_state:
    st.session_state["all_competitors"] = pd.DataFrame()

# Get screenshot API key from secrets, if present
SCREENSHOT_API_KEY = ""
try:
    SCREENSHOT_API_KEY = st.secrets.get("SCREENSHOT_API_KEY", "")
except Exception:
    SCREENSHOT_API_KEY = ""


# -------------------------
# HTTP / HTML helpers
# -------------------------


def fetch_html(url: str, timeout: int = 20) -> str | None:
    """
    Fetch raw HTML for a URL, with a desktop user agent.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        st.error(f"Error fetching {url}: {e}")
        return None


def detect_engine(url: str, html: str) -> str:
    """
    Auto-detect which ecommerce engine is backing this menu.
    Returns one of: 'dutchie', 'jane', 'weedmaps', 'dispense', 'tymber', 'generic'

    Detection priority (highest → lowest):
      1. Hostname exact matches (dutchie.com, iheartjane.com, weedmaps.com)
      2. Strong Dutchie signals in HTML: iframe/script src, dtche config, graphql endpoint
      3. Jane / Weedmaps signals
      4. Dispense signals (only when no Dutchie signals present)
      5. Tymber signals
      6. Generic fallback
    """
    netloc = urlparse(url).netloc.lower()
    lower = html.lower()

    # 1) Direct host hints (strongest signal)
    if "dutchie" in netloc or "plus.dutchie" in netloc:
        return "dutchie"
    if "iheartjane" in netloc or "jane.menu" in netloc:
        return "jane"
    if "weedmaps" in netloc:
        return "weedmaps"

    # 2) Strong Dutchie signals in HTML (iframe, script, config objects)
    #    Check multiple patterns: dtche config, dutchie.com src, embedded iframe
    dutchie_signals = [
        "dutchie.com",
        "dtche[",
        "dtche%5b",
        '"dtche"',
        "window.dtche",
        "dutchie-embed",
        "dutchie_embed",
        "plus.dutchie",
        'src="https://dutchie',
        "src='https://dutchie",
        "dutchie/graphql",
        "menu.dutchie",
    ]
    if any(sig in lower for sig in dutchie_signals):
        return "dutchie"

    # 3) Jane / Weedmaps signals
    if "iheartjane.com" in lower or "jane-root" in lower or "data-jane-" in lower:
        return "jane"

    if "weedmaps.com" in lower or "wm-menu" in lower:
        return "weedmaps"

    # 4) Dispense signals — only if no Dutchie signals detected above.
    #    These are HTML content pattern checks for engine detection, not
    #    security-sensitive URL validation; substring matching is intentional.
    if "dispenseapp.com" in lower or "dispense.io" in lower or '"dispenseapp"' in lower:
        return "dispense"

    # 5) Tymber signals
    if "tymber" in lower:
        return "tymber"

    return "generic"


# -------------------------
# Generic parsers (HTML / JSON-LD)
# -------------------------


def parse_products_from_jsonld(html: str, category_hint: str | None = None) -> pd.DataFrame:
    """
    Look for <script type="application/ld+json"> blocks and pull out
    basic Product / Offer info where available.
    Returns a DataFrame with columns: Product, Category, Price, THC, Source
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")

    rows: list[dict] = []

    for tag in scripts:
        raw = tag.string
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        items = data if isinstance(data, list) else [data]

        for item in items:
            if not isinstance(item, dict):
                continue

            item_type = item.get("@type") or item.get("type")

            if isinstance(item_type, str) and "product" in item_type.lower():
                name = item.get("name")

                offers = item.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = offers.get("price")
                if isinstance(price, str):
                    try:
                        price = float(price)
                    except ValueError:
                        price = None

                # THC from additionalProperty
                thc_val = None
                add_props = item.get("additionalProperty") or item.get(
                    "additionalProperties"
                )
                if isinstance(add_props, list):
                    for p in add_props:
                        if not isinstance(p, dict):
                            continue
                        name_prop = (p.get("name") or "").lower()
                        if "thc" in name_prop:
                            val = p.get("value") or p.get("valueReference")
                            if isinstance(val, (int, float)):
                                thc_val = val
                                break
                            if isinstance(val, str):
                                m = re.search(r"(\d+(\.\d+)?)", val)
                                if m:
                                    thc_val = float(m.group(1))
                                    break

                if name:
                    rows.append(
                        {
                            "Product": name,
                            "Category": category_hint,
                            "Price": price,
                            "THC": thc_val,
                            "Source": "JSON-LD",
                        }
                    )

    if not rows:
        return pd.DataFrame(columns=["Product", "Category", "Price", "THC", "Source"])

    df = pd.DataFrame(rows)
    df["Price"] = pd.to_numeric(df["Price"], errors="coerce")
    df["THC"] = pd.to_numeric(df["THC"], errors="coerce")
    return df


def extract_generic_cards(soup: BeautifulSoup, category_hint: str | None = None) -> pd.DataFrame:
    """
    Super-generic fallback parser for HTML card grids.
    Tries to infer product name, price, and THC.
    """
    rows: list[dict] = []

    selectors = [
        "[class*='product-card']",
        "[class*='ProductCard']",
        "[class*='menu-item']",
        "[class*='MenuItem']",
        "[data-product-name]",
    ]

    for selector in selectors:
        for card in soup.select(selector):
            name = card.get("data-product-name")
            if not name:
                h = card.find(["h1", "h2", "h3", "h4"])
                if h:
                    name = h.get_text(strip=True)
            if not name:
                continue

            text = card.get_text(" ", strip=True)

            # Price
            price = None
            m_price = re.search(r"\$([\d.,]+)", text)
            if m_price:
                try:
                    price = float(m_price.group(1).replace(",", ""))
                except ValueError:
                    price = None

            # THC
            thc = None
            m_thc = re.search(r"(\d+(\.\d+)?)\s*%?\s*thc", text, re.I)
            if m_thc:
                try:
                    thc = float(m_thc.group(1))
                except ValueError:
                    thc = None

            rows.append(
                {
                    "Product": name,
                    "Category": category_hint,
                    "Price": price,
                    "THC": thc,
                    "Source": "HTML card",
                }
            )

    if not rows:
        return pd.DataFrame(columns=["Product", "Category", "Price", "THC", "Source"])

    df = pd.DataFrame(rows)
    df["Price"] = pd.to_numeric(df["Price"], errors="coerce")
    df["THC"] = pd.to_numeric(df["THC"], errors="coerce")
    return df


# -------------------------
# OCR-based fallback (Playwright + Screenshot API)
# -------------------------


def screenshot_page_playwright(url: str) -> bytes | None:
    """
    Use a headless browser (Playwright) to render the page and take
    a full-page PNG screenshot. Returns raw bytes or None on failure.
    """
    if not HAS_PLAYWRIGHT:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            page.goto(url, wait_until="networkidle", timeout=30000)
            # Basic scroll to trigger lazy loading
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(2000)
            screenshot_bytes = page.screenshot(full_page=True)
            browser.close()
        return screenshot_bytes
    except Exception as e:
        if HAS_BROWSER_HELPERS and is_missing_browser_error(e):
            st.warning(_PLAYWRIGHT_MISSING_BINARY_WARNING)
        else:
            st.info(f"Playwright screenshot failed: {e}")
        return None


def screenshot_page_api(url: str) -> bytes | None:
    """
    Use an external screenshot API as a fallback.
    You must set SCREENSHOT_API_KEY in Streamlit secrets for this to work.
    """
    if not SCREENSHOT_API_KEY:
        return None

    # Example: ScreenshotMachine-like URL. Adjust to your provider as needed.
    api_url = (
        "https://api.screenshotmachine.com/"
        f"?key={SCREENSHOT_API_KEY}"
        f"&url={requests.utils.quote(url, safe='')}"
        "&dimension=1280x720&format=png&cacheLimit=0"
    )
    try:
        resp = requests.get(api_url, timeout=30)
        resp.raise_for_status()
        if resp.headers.get("Content-Type", "").startswith("image"):
            return resp.content
        return None
    except Exception as e:
        st.info(f"Screenshot API fallback failed: {e}")
        return None


def screenshot_page(url: str) -> bytes | None:
    """
    Try Playwright first; if that fails or isn't available, try screenshot API.
    """
    # 1) Playwright
    img_bytes = screenshot_page_playwright(url)
    if img_bytes:
        return img_bytes

    # 2) Screenshot API
    img_bytes = screenshot_page_api(url)
    if img_bytes:
        return img_bytes

    return None


def ocr_text_from_image_bytes(img_bytes: bytes) -> str:
    """
    Run OCR on a PNG screenshot and return raw text.
    """
    img = Image.open(BytesIO(img_bytes))
    try:
        text = pytesseract.image_to_string(img)
    except Exception as e:
        st.info(f"OCR failed: {e}")
        return ""
    return text


def parse_products_from_ocr_text(text: str) -> pd.DataFrame:
    """
    Very rough OCR parser:
    - Looks for lines that contain a dollar price and some letters
    - Tries to pull Product, Price, THC

    This will NOT be perfect, but it gives you something to work with.
    """
    rows: list[dict] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # require a price to treat as a product line
        m_price = re.search(r"\$([\d.,]+)", line)
        if not m_price:
            continue

        # need at least one letter
        if not re.search(r"[A-Za-z]", line):
            continue

        try:
            price = float(m_price.group(1).replace(",", ""))
        except ValueError:
            price = None

        # THC if present
        thc = None
        m_thc = re.search(r"(\d+(\.\d+)?)\s*%?\s*THC", line, re.I)
        if m_thc:
            try:
                thc = float(m_thc.group(1))
            except ValueError:
                thc = None

        # product name: line with the price stripped out
        name_part = line.replace(m_price.group(0), "").strip(" -•|")
        if not name_part:
            name_part = line

        rows.append(
            {
                "Product": name_part,
                "Category": None,
                "Price": price,
                "THC": thc,
                "Source": "OCR screenshot",
            }
        )

    if not rows:
        return pd.DataFrame(columns=["Product", "Category", "Price", "THC", "Source"])

    df = pd.DataFrame(rows)
    df["Price"] = pd.to_numeric(df["Price"], errors="coerce")
    df["THC"] = pd.to_numeric(df["THC"], errors="coerce")
    return df


def ocr_menu_from_url(url: str) -> pd.DataFrame:
    """
    Full OCR pipeline:
      1) Screenshot the page (Playwright or Screenshot API)
      2) OCR the screenshot into text
      3) Parse that text into a loose product list
    """
    screenshot_bytes = screenshot_page(url)
    if not screenshot_bytes:
        return pd.DataFrame(columns=["Product", "Category", "Price", "THC", "Source"])

    text = ocr_text_from_image_bytes(screenshot_bytes)
    if not text.strip():
        return pd.DataFrame(columns=["Product", "Category", "Price", "THC", "Source"])

    df_ocr = parse_products_from_ocr_text(text)
    return df_ocr


# -------------------------
# Engine-specific stubs (safe, no fragile hard-coded APIs)
# -------------------------


def fetch_menu_dutchie(url: str, html: str) -> pd.DataFrame:
    """
    Dutchie menus: either a direct dutchie.com link, or a marketing site
    that embeds Dutchie via dtche[...] / iframe.

    API/GraphQL payloads are extracted upstream in fetch_competitor_menu before
    this function is called. This function handles HTML-based fallbacks only.

    Extraction priority:
      1) JSON-LD embedded in the HTML
      2) Generic HTML card parsing
    """
    # 1) JSON-LD
    df_ld = parse_products_from_jsonld(html)
    if not df_ld.empty:
        return df_ld

    # 2) Generic HTML cards
    soup = BeautifulSoup(html, "html.parser")
    df_cards = extract_generic_cards(soup)
    if not df_cards.empty:
        return df_cards

    return pd.DataFrame(columns=["Product", "Category", "Price", "THC", "Source"])


def fetch_menu_jane(url: str, html: str) -> pd.DataFrame:
    """
    Jane / iHeartJane stub.
    For now we lean on JSON-LD and generic cards.
    """
    soup = BeautifulSoup(html, "html.parser")
    df_ld = parse_products_from_jsonld(html)
    if not df_ld.empty:
        return df_ld
    df_cards = extract_generic_cards(soup)
    return df_cards


def fetch_menu_weedmaps(url: str, html: str) -> pd.DataFrame:
    """
    Weedmaps stub.
    """
    soup = BeautifulSoup(html, "html.parser")
    df_ld = parse_products_from_jsonld(html)
    if not df_ld.empty:
        return df_ld
    df_cards = extract_generic_cards(soup)
    return df_cards


def fetch_menu_dispense(url: str, html: str) -> pd.DataFrame:
    """
    Dispense / similar engines stub.
    """
    soup = BeautifulSoup(html, "html.parser")
    df_ld = parse_products_from_jsonld(html)
    if not df_ld.empty:
        return df_ld
    df_cards = extract_generic_cards(soup)
    return df_cards


def fetch_menu_tymber(url: str, html: str) -> pd.DataFrame:
    """
    Tymber stub.
    """
    soup = BeautifulSoup(html, "html.parser")
    df_ld = parse_products_from_jsonld(html)
    if not df_ld.empty:
        return df_ld
    df_cards = extract_generic_cards(soup)
    return df_cards


def fetch_menu_generic(url: str, html: str) -> pd.DataFrame:
    """
    Generic fallback: JSON-LD first, then HTML cards on the exact URL.
    """
    soup = BeautifulSoup(html, "html.parser")
    df_ld = parse_products_from_jsonld(html)
    if not df_ld.empty:
        return df_ld
    df_cards = extract_generic_cards(soup)
    return df_cards


# -------------------------
# Router: any URL -> DataFrame (with OCR fallback)
# -------------------------


def fetch_competitor_menu(
    url: str, use_browser: bool = False, menu_type: str | None = None
) -> tuple[pd.DataFrame, str | None, dict]:
    """
    Given ANY menu/website URL:
      1) Fetch HTML (static)
      2) Detect engine
      3) If Dutchie engine and browser helpers available, use the dedicated
         Dutchie GraphQL crawler (full category + pagination crawl).
      4) Otherwise, if browser mode requested or engine is JS-heavy,
         use Playwright to render page and capture API/GraphQL payloads.
      5) Prefer JSON/API payload extraction over HTML parsing.
      6) If still empty, try OCR fallback.
    Returns (df, engine, debug_info)
    """
    debug_info: dict = {
        "final_url": url,
        "engine_initial": None,
        "engine_final": None,
        "browser_used": False,
        "captured_count": 0,
        "captured_urls": [],
        "parse_notes": [],
        "categories": [],
        "per_page_counts": {},
        "graphql_details": [],
    }

    html = fetch_html(url)
    if not html:
        return pd.DataFrame(), None, debug_info

    engine = detect_engine(url, html)
    debug_info["engine_initial"] = engine

    browser_payloads: list | None = None

    # -----------------------------------------------------------------------
    # Dutchie fast-path: use dedicated GraphQL crawler
    # -----------------------------------------------------------------------
    if engine == "dutchie" and HAS_BROWSER_HELPERS:
        st.info(
            "Dutchie menu detected – launching GraphQL crawler "
            "(all categories + pages, in-stock filter)…"
        )
        df_dutchie, gql_debug = crawl_dutchie(url, menu_type=menu_type)
        # Merge debug info
        debug_info["browser_used"] = True
        debug_info["captured_count"] = gql_debug.get("captured_count", 0)
        debug_info["captured_urls"] = gql_debug.get("captured_urls", [])
        debug_info["categories"] = gql_debug.get("categories", [])
        debug_info["per_page_counts"] = gql_debug.get("per_page_counts", {})
        debug_info["graphql_details"] = gql_debug.get("graphql_details", [])
        debug_info["parse_notes"].extend(gql_debug.get("parse_notes", []))
        debug_info["engine_final"] = engine

        if not df_dutchie.empty:
            debug_info["parse_notes"].append(
                f"Dutchie GraphQL crawler found {len(df_dutchie)} rows"
            )
            return df_dutchie, engine, debug_info
        debug_info["parse_notes"].append(
            "Dutchie GraphQL crawler found 0 rows – falling back to HTML parsing"
        )

    # -----------------------------------------------------------------------
    # Generic browser mode (non-Dutchie JS-heavy engines)
    # -----------------------------------------------------------------------
    js_heavy = engine in {"dutchie", "jane", "weedmaps", "dispense"}
    if (use_browser or js_heavy) and HAS_BROWSER_HELPERS and engine != "dutchie":
        st.info(
            "Using browser mode to render page, bypass 21+ age gate, "
            "and capture API responses…"
        )
        try:
            bhtml, browser_payloads, final_url = browser_fetch(url)
        except Exception as _bfe:
            if is_missing_browser_error(_bfe):
                st.warning(_PLAYWRIGHT_MISSING_BINARY_WARNING)
            bhtml, browser_payloads = "", None
        debug_info["browser_used"] = True
        debug_info["final_url"] = final_url
        if browser_payloads:
            debug_info["captured_count"] = len(browser_payloads)
            debug_info["captured_urls"] = [p.get("url", "") for p in browser_payloads]
        if bhtml:
            html = bhtml
            detected = detect_engine(final_url, html)
            if detected:
                engine = detected

    debug_info["engine_final"] = engine

    # 1) Prefer API/JSON payloads when available (best for JS-heavy menus)
    if browser_payloads and HAS_BROWSER_HELPERS:
        df_api = parse_dutchie_responses(browser_payloads)
        if not df_api.empty:
            debug_info["parse_notes"].append(
                f"API extraction found {len(df_api)} products"
            )
            return df_api, engine, debug_info
        debug_info["parse_notes"].append(
            f"API extraction found 0 products from {len(browser_payloads)} responses"
        )

    # 2) Engine-specific / generic HTML parsing
    if engine == "dutchie":
        df = fetch_menu_dutchie(url, html)
    elif engine == "jane":
        df = fetch_menu_jane(url, html)
    elif engine == "weedmaps":
        df = fetch_menu_weedmaps(url, html)
    elif engine == "dispense":
        df = fetch_menu_dispense(url, html)
    elif engine == "tymber":
        df = fetch_menu_tymber(url, html)
    else:
        df = fetch_menu_generic(url, html)

    if not df.empty:
        debug_info["parse_notes"].append(
            f"HTML/JSON-LD extraction found {len(df)} products"
        )
        return df, engine, debug_info

    debug_info["parse_notes"].append("HTML/JSON-LD extraction found 0 products")

    # 3) OCR fallback if HTML/API parsing found nothing and engine is JS-heavy
    if js_heavy:
        st.info(
            "No products found in static HTML or API responses for this menu. "
            "Trying OCR-based screenshot parsing (Playwright / Screenshot API)…"
        )
        df_ocr = ocr_menu_from_url(url)
        if not df_ocr.empty:
            debug_info["parse_notes"].append(
                f"OCR fallback found {len(df_ocr)} products"
            )
            df = df_ocr
        else:
            debug_info["parse_notes"].append("OCR fallback found 0 products")

    return df, engine, debug_info


# -------------------------
# Excel helper
# -------------------------


def make_excel_bytes(df: pd.DataFrame) -> bytes:
    """
    Convert a DataFrame to an in-memory Excel file.
    """
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Competitor Menus")
    return output.getvalue()


# -------------------------
# Streamlit form & logic
# -------------------------

with st.form("scan_form"):
    url = st.text_input(
        "Menu URL",
        placeholder="https://exampledispensary.com/menu or https://dutchie.com/dispensary/...",
    )
    dispo_name = st.text_input(
        "Dispensary label (for the table)",
        placeholder="Solar Somerset (Rec), NEA Fall River (Med), etc.",
    )
    col_a, col_b = st.columns(2)
    with col_a:
        use_browser = st.checkbox(
            "Use browser mode (Playwright) – recommended for Dutchie / JS-heavy menus",
            value=HAS_BROWSER_HELPERS,
            disabled=not HAS_BROWSER_HELPERS,
            help=(
                "Launches a headless Chromium browser that bypasses 21+ age gates "
                "and captures live API/GraphQL responses. "
                "Requires: playwright install chromium"
            ),
        )
    with col_b:
        debug_mode = st.checkbox(
            "Debug mode – show engine signals, captured responses, and parse notes",
            value=False,
            help=(
                "Shows the detected engine, final URL after redirects, "
                "number of captured GraphQL responses, category list, "
                "per-page counts, and parsing notes."
            ),
        )

    st.markdown("**MED / REC options** (Dutchie menus)")
    col_med, col_rec = st.columns(2)
    with col_med:
        med_url = st.text_input(
            "MED menu URL (optional)",
            placeholder="https://dispensary.com/menu/location/med",
            help="Leave blank to skip. If provided, scanned separately with Menu_Type=med.",
        )
    with col_rec:
        rec_url = st.text_input(
            "REC (adult-use) menu URL (optional)",
            placeholder="https://dispensary.com/menu/location/rec",
            help="Leave blank to skip. If provided, scanned separately with Menu_Type=rec.",
        )

    submitted = st.form_submit_button("Scan & Add to Table")

if submitted and (url.strip() or med_url.strip() or rec_url.strip()):
    label = dispo_name.strip() or url.strip() or med_url.strip() or rec_url.strip()

    # Build list of (scan_url, menu_type_override) tuples
    scan_jobs: list[tuple[str, str | None]] = []

    # MED / REC specific URLs take priority
    if med_url.strip():
        scan_jobs.append((med_url.strip(), "med"))
    if rec_url.strip():
        scan_jobs.append((rec_url.strip(), "rec"))
    # Fall back to generic URL field
    if url.strip() and not scan_jobs:
        scan_jobs.append((url.strip(), None))

    for scan_url, menu_type_override in scan_jobs:
        type_label = (
            f" [{menu_type_override.upper()}]" if menu_type_override else ""
        )
        with st.spinner(f"Scanning{type_label}: {scan_url}…"):
            df, engine, debug_info = fetch_competitor_menu(
                scan_url,
                use_browser=use_browser,
                menu_type=menu_type_override,
            )

        # Always show debug panel when debug mode is on
        if debug_mode:
            with st.expander(f"🔍 Debug Info{type_label}", expanded=True):
                st.markdown(f"**Initial engine detected:** `{debug_info.get('engine_initial')}`")
                st.markdown(f"**Final engine after browser re-detect:** `{debug_info.get('engine_final')}`")
                st.markdown(f"**Browser (Playwright) used:** `{debug_info.get('browser_used')}`")
                st.markdown(f"**Final URL after redirects:** `{debug_info.get('final_url')}`")

                captured_count = debug_info.get("captured_count", 0)
                st.markdown(f"**Captured GraphQL responses:** `{captured_count}`")

                graphql_details = debug_info.get("graphql_details", [])
                if graphql_details:
                    st.markdown("**Top captured GraphQL URLs:**")
                    for gd in graphql_details[:10]:
                        status = gd.get("status", "?")
                        blen = gd.get("body_length")
                        ok = "✅" if gd.get("json_ok") else "❌"
                        blen_str = f"{blen:,} bytes" if blen else "n/a"
                        st.code(
                            f"{ok} HTTP {status} | {blen_str} | {gd.get('url', '')}"
                        )

                categories = debug_info.get("categories", [])
                if categories:
                    st.markdown(
                        f"**Categories discovered ({len(categories)}):** "
                        + ", ".join(f"`{c}`" for c in categories)
                    )

                per_page = debug_info.get("per_page_counts", {})
                if per_page:
                    st.markdown("**Per-page product counts:**")
                    for k, v in per_page.items():
                        st.markdown(f"- `{k}`: {v} rows")

                parse_notes = debug_info.get("parse_notes", [])
                if parse_notes:
                    st.markdown("**Parse notes:**")
                    for note in parse_notes:
                        st.markdown(f"- {note}")

        if df.empty:
            st.warning(
                f"No products detected from {scan_url}{type_label}. "
                "The menu may rely on protected APIs or complex rendering."
            )
            if engine:
                st.info(f"Detected menu engine: **{engine}**")
                if engine in {"dutchie", "jane", "weedmaps", "dispense"}:
                    st.caption(
                        "Tried HTML/JSON and OCR fallback. For precise data, you may still "
                        "need a CSV/Excel export from the platform or admin side."
                    )
        else:
            # Normalize basic columns
            if "Price" in df.columns:
                df["Price"] = pd.to_numeric(df["Price"], errors="coerce")
            if "THC" in df.columns:
                df["THC"] = pd.to_numeric(df["THC"], errors="coerce")

            df.insert(0, "Dispensary", label)
            # Add Menu_Type column if not already present (e.g. from GraphQL crawler)
            if "Menu_Type" not in df.columns:
                df["Menu_Type"] = menu_type_override
            df["Engine"] = engine or "unknown"
            if "Source_URL" not in df.columns:
                df["Source_URL"] = scan_url

            # Append to session_state master table
            if st.session_state["all_competitors"].empty:
                st.session_state["all_competitors"] = df
            else:
                st.session_state["all_competitors"] = pd.concat(
                    [st.session_state["all_competitors"], df], ignore_index=True
                )

            st.success(
                f"Added **{len(df)}** products from **{label}**{type_label} "
                f"(engine: **{engine or 'unknown'}**)."
            )

            with st.expander(f"Preview rows{type_label}"):
                st.dataframe(df.head(50), use_container_width=True)

st.markdown("---")
st.subheader("Combined Competitor Table (all scans this session)")

combined = st.session_state["all_competitors"]

if combined.empty:
    st.info("No scans yet. Paste a menu URL above and hit *Scan & Add to Table*.")
else:
    st.write(f"Total rows across all dispensaries: **{len(combined)}**")
    st.dataframe(combined, use_container_width=True, height=420)

    # Simple summary metrics
    if "Price" in combined.columns:
        avg_price = combined["Price"].mean()
    else:
        avg_price = None

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Products", len(combined))
    col2.metric(
        "Unique Dispensaries",
        combined["Dispensary"].nunique() if "Dispensary" in combined.columns else 0,
    )
    col3.metric(
        "Average Price (All)",
        f"${avg_price:,.2f}" if avg_price is not None and not pd.isna(avg_price) else "N/A",
    )

    excel_bytes = make_excel_bytes(combined)
    st.download_button(
        label="⬇️ Download Excel (All Dispos)",
        data=excel_bytes,
        file_name="comp_intel_all_dispensaries.xlsx",
        mime=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
    )

    if st.button("Clear all data (this session)"):
        st.session_state["all_competitors"] = pd.DataFrame()
        st.rerun()

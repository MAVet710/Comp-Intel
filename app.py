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
    from scraping.playwright_helpers import browser_fetch, try_bypass_age_gate
    from scraping.dutchie_parser import parse_dutchie_responses
    HAS_BROWSER_HELPERS = True
except Exception:
    HAS_BROWSER_HELPERS = False

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
- Try to scrape products (Product, Price, THC where possible) from HTML / JSON-LD
- If that fails on JS-heavy menus (Dutchie / Jane / Weedmaps), it will try:
  1. A **Playwright** screenshot + OCR (if supported)
  2. A **Screenshot API** screenshot + OCR (if configured)
- Let you scan multiple dispensaries and export everything to **Excel**
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
    """
    netloc = urlparse(url).netloc.lower()
    lower = html.lower()

    # Direct host hints
    if "dutchie" in netloc or "plus.dutchie" in netloc:
        return "dutchie"
    if "iheartjane" in netloc or "jane.menu" in netloc:
        return "jane"
    if "weedmaps" in netloc:
        return "weedmaps"

    # Embedded script / iframe hints
    if "dtche%5bpath%5d" in lower or "dtche[" in lower or "dutchie.com" in lower:
        return "dutchie"

    if "iheartjane.com" in lower or "jane-root" in lower or "data-jane-" in lower:
        return "jane"

    if "weedmaps.com" in lower or "wm-menu" in lower:
        return "weedmaps"

    if "dispenseapp" in lower or "dispense.io" in lower:
        return "dispense"

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


def fetch_menu_dutchie(
    url: str, html: str, browser_payloads: list | None = None
) -> pd.DataFrame:
    """
    Dutchie menus: either a direct dutchie.com link, or a marketing site
    that embeds Dutchie via dtche[...] / iframe.

    Extraction priority:
      1) Dutchie/GraphQL network payloads captured during a Playwright session
      2) JSON-LD embedded in the HTML
      3) Generic HTML card parsing
    """
    # 1) Prefer API payloads captured by the browser session
    if browser_payloads and HAS_BROWSER_HELPERS:
        df_api = parse_dutchie_responses(browser_payloads)
        if not df_api.empty:
            return df_api

    # 2) JSON-LD
    df_ld = parse_products_from_jsonld(html)
    if not df_ld.empty:
        return df_ld

    # 3) Generic HTML cards
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
    url: str, use_browser: bool = False
) -> tuple[pd.DataFrame, str | None]:
    """
    Given ANY menu/website URL:
      1) Fetch HTML (static)
      2) Detect engine
      3) If browser mode requested (or engine is JS-heavy and helpers available),
         use Playwright to render the page, bypass 21+ gates, and capture
         Dutchie/GraphQL network payloads.
      4) Use engine-specific / generic HTML parsers
      5) If no products found and engine looks JS/API-heavy, try OCR fallback.
    Returns (df, engine)
    """
    html = fetch_html(url)
    if not html:
        return pd.DataFrame(), None

    engine = detect_engine(url, html)

    browser_payloads: list | None = None

    # Decide whether to use the Playwright browser path
    js_heavy = engine in {"dutchie", "jane", "weedmaps"}
    if (use_browser or js_heavy) and HAS_BROWSER_HELPERS:
        st.info(
            "Using browser mode to render page, bypass 21+ age gate, "
            "and capture API responses…"
        )
        bhtml, browser_payloads, final_url = browser_fetch(url)
        if bhtml:
            html = bhtml
            # Re-detect engine with the fully rendered HTML / final URL
            detected = detect_engine(final_url, html)
            if detected:
                engine = detected

    # Engine-specific / generic HTML parsing
    if engine == "dutchie":
        df = fetch_menu_dutchie(url, html, browser_payloads)
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

    # OCR fallback if HTML/API parsing found nothing and engine is JS-heavy
    if df.empty and js_heavy:
        st.info(
            "No products found in static HTML or API responses for this menu. "
            "Trying OCR-based screenshot parsing (Playwright / Screenshot API)…"
        )
        df_ocr = ocr_menu_from_url(url)
        if not df_ocr.empty:
            df = df_ocr

    return df, engine


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
    submitted = st.form_submit_button("Scan & Add to Table")

if submitted and url.strip():
    url = url.strip()
    label = dispo_name.strip() or url

    with st.spinner("Scanning menu and parsing products…"):
        df, engine = fetch_competitor_menu(url, use_browser=use_browser)

    if df.empty:
        st.warning(
            "No products were detected from this URL, even after HTML/JSON and OCR attempts. "
            "This menu may rely on protected APIs or complex rendering."
        )
        if engine:
            st.info(f"Detected menu engine: **{engine}**")
            if engine in {"dutchie", "jane", "weedmaps"}:
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
        df["Engine"] = engine or "unknown"
        df["Source_URL"] = url

        # Append to session_state master table
        if st.session_state["all_competitors"].empty:
            st.session_state["all_competitors"] = df
        else:
            st.session_state["all_competitors"] = pd.concat(
                [st.session_state["all_competitors"], df], ignore_index=True
            )

        st.success(
            f"Added **{len(df)}** products from **{label}** "
            f"(engine detected: **{engine or 'unknown'}**)."
        )

        with st.expander("Preview rows from this scan"):
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

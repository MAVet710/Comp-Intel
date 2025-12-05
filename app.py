import io
import json
import re
from urllib.parse import urlparse, urljoin

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

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
- Try to scrape products (name, price, THC when possible)
- Let you scan multiple dispensaries and export everything to **Excel**
"""
)

# Initialise combined data storage
if "all_competitors" not in st.session_state:
    st.session_state["all_competitors"] = pd.DataFrame()

# -------------------------
# HTTP / HTML helpers
# -------------------------


def fetch_html(url, timeout=20):
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


def detect_engine(url, html):
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
# Generic parsers
# -------------------------


def parse_products_from_jsonld(html, category_hint=None):
    """
    Look for <script type="application/ld+json"> blocks and pull out
    basic Product / Offer info where available.
    Returns a DataFrame with columns: Product, Price, THC (if found)
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", type="application/ld+json")

    rows = []

    for tag in scripts:
        raw = tag.string
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        # Normalize to list
        items = data if isinstance(data, list) else [data]

        for item in items:
            if not isinstance(item, dict):
                continue

            item_type = item.get("@type") or item.get("type")

            # Direct Product
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

                # Try to pull THC from additionalProperty
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


def extract_generic_cards(soup, category_hint=None):
    """
    Super-generic fallback parser for HTML card grids.
    Tries to infer product name, price, and THC.
    """
    rows = []

    # Heuristic selectors. Easy to expand if we see new patterns.
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
# Engine-specific stubs
# -------------------------


def fetch_menu_dutchie(url, html):
    """
    Dutchie menus: either a direct dutchie.com link, or a marketing site
    that embeds Dutchie via dtche[...] / iframe.

    Right now this uses a generic HTML/JSON-LD pass on the best Dutchie-like URL
    we can find, so the app stays usable. Later you can upgrade this to call
    Dutchie's JSON / GraphQL endpoints directly.
    """
    # Try to find a dutchie URL inside the HTML
    m = re.search(r'https?://[^"\'\s]*dutchie[^"\'\s]*', html, re.I)
    dutchie_url = m.group(0) if m else url

    dutchie_html = fetch_html(dutchie_url)
    if not dutchie_html:
        return pd.DataFrame()

    soup = BeautifulSoup(dutchie_html, "html.parser")

    # 1) Try JSON-LD
    df_ld = parse_products_from_jsonld(dutchie_html)
    if not df_ld.empty:
        return df_ld

    # 2) Fallback: generic cards
    df_cards = extract_generic_cards(soup)
    if not df_cards.empty:
        return df_cards

    return pd.DataFrame()


def fetch_menu_jane(url, html):
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


def fetch_menu_weedmaps(url, html):
    """
    Weedmaps stub.
    """
    soup = BeautifulSoup(html, "html.parser")
    df_ld = parse_products_from_jsonld(html)
    if not df_ld.empty:
        return df_ld
    df_cards = extract_generic_cards(soup)
    return df_cards


def fetch_menu_dispense(url, html):
    """
    Dispense / similar engines stub.
    """
    soup = BeautifulSoup(html, "html.parser")
    df_ld = parse_products_from_jsonld(html)
    if not df_ld.empty:
        return df_ld
    df_cards = extract_generic_cards(soup)
    return df_cards


def fetch_menu_tymber(url, html):
    """
    Tymber stub.
    """
    soup = BeautifulSoup(html, "html.parser")
    df_ld = parse_products_from_jsonld(html)
    if not df_ld.empty:
        return df_ld
    df_cards = extract_generic_cards(soup)
    return df_cards


def fetch_menu_generic(url, html):
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
# Router: any URL -> DataFrame
# -------------------------


def fetch_competitor_menu(url):
    """
    Given ANY menu/website URL, auto-detect the engine and route to
    the right scraper.

    Returns (df, engine) where df has at least:
    ['Product', 'Price', 'THC', 'Category', 'Source']
    """
    html = fetch_html(url)
    if not html:
        return pd.DataFrame(), None

    engine = detect_engine(url, html)

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

    return df, engine


# -------------------------
# Excel helper
# -------------------------


def make_excel_bytes(df):
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
    submitted = st.form_submit_button("Scan & Add to Table")

if submitted and url.strip():
    url = url.strip()
    label = dispo_name.strip() or url

    with st.spinner("Scanning menu and parsing products…"):
        df, engine = fetch_competitor_menu(url)

    if df.empty:
        st.warning(
            "No products were detected from this URL. "
            "This menu may rely on protected APIs or heavy JavaScript."
        )
        if engine:
            st.info(f"Detected menu engine: **{engine}**")
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
        f"${avg_price:,.2f}" if avg_price and not pd.isna(avg_price) else "N/A",
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
        st.experimental_rerun()

import re
from io import BytesIO
from urllib.parse import urlparse, urljoin

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup


# ------------------ Platform Detection ------------------ #

def detect_platform(url: str) -> str:
    """
    Simple platform detection by hostname.
    We can expand this later with more engines.
    """
    netloc = urlparse(url).netloc.lower()

    if "dutchie" in netloc:
        return "dutchie"
    if "iheartjane" in netloc or "jane" in netloc:
        return "jane"
    # Example: Natures Medicines / Alpine IQ / AIQ
    if "naturesmedicines.com" in netloc or "alpineiq" in netloc or "aiq" in netloc:
        return "aiq"

    return "generic"


# ------------------ Shared Helpers ------------------ #

POSSIBLE_CATEGORY_KEYWORDS = [
    "flower", "pre-roll", "preroll", "pre roll",
    "vape", "cartridge", "cart", "pods",
    "edible", "gummy", "chocolate",
    "concentrate", "extract", "dab",
    "tincture", "topical", "balm",
    "accessories", "merch", "merchandise",
    "beverage", "drink"
]


def make_age_verified_session(root_url: str) -> requests.Session:
    """
    Creates a session that pretends to be a 21+ browser.
    """
    parsed = urlparse(root_url)
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Rebelle-Competitor-Intel/1.0)",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    })
    if parsed.hostname:
        s.cookies.set("ageVerified", "true", domain=parsed.hostname)
        s.cookies.set("is21", "true", domain=parsed.hostname)
    return s


def discover_category_links(session: requests.Session, root_url: str) -> dict:
    """
    Find category-specific pages from the main menu URL.
    Returns { 'Flower': 'https://.../categories/flower', ... }
    Fallback: {"All": root_url}
    """
    try:
        resp = session.get(root_url, timeout=20)
    except Exception:
        return {"All": root_url}

    if resp.status_code != 200 or not resp.text:
        return {"All": root_url}

    soup = BeautifulSoup(resp.text, "html.parser")
    links = soup.find_all("a", href=True)

    category_map: dict[str, str] = {}

    for a in links:
        label = (a.get_text() or "").strip()
        href = a["href"]
        if not label or not href:
            continue

        combined_text = f"{label} {href}".lower()

        for kw in POSSIBLE_CATEGORY_KEYWORDS:
            if kw in combined_text:
                full_url = urljoin(root_url, href)
                pretty_label = kw.replace("-", " ").title()
                category_map.setdefault(pretty_label, full_url)

    if not category_map:
        category_map["All"] = root_url

    return category_map


def parse_products_from_html(html: str, category_hint: str | None = None) -> pd.DataFrame:
    """
    Generic HTML parser to find product "cards" and pull basic info.
    This is intentionally loose so it won't crash on weird layouts.
    """
    soup = BeautifulSoup(html, "html.parser")

    cards = soup.find_all(
        ["article", "div", "li"],
        class_=lambda c: any(
            isinstance(c, str)
            and token
            and ("product" in token.lower() or "card" in token.lower())
            for token in (c.split() if isinstance(c, str) else [])
        )
    )

    rows = []

    for card in cards:
        text = card.get_text(separator=" ", strip=True)
        if not text:
            continue

        name = None
        brand = None
        price = None
        thc = None
        weight = None

        # Name: first header tag
        h = card.find(["h1", "h2", "h3", "h4"])
        if h:
            name = h.get_text(strip=True)

        if not name:
            name = text[:40] + "..." if len(text) > 40 else text

        # Price: $xx.xx
        m_price = re.search(r"\$(\d+(?:\.\d{1,2})?)", text)
        if m_price:
            try:
                price = float(m_price.group(1))
            except ValueError:
                price = None

        # THC: "THC 27%" or "27% THC"
        m_thc = re.search(r"(\d{1,2}\.?\d*)\s*%?\s*THC", text, re.IGNORECASE)
        if m_thc:
            try:
                thc = float(m_thc.group(1))
            except ValueError:
                thc = None

        # Weight: "3.5g", "1g", "1/8", etc.
        m_weight = re.search(
            r"(\d+(\.\d+)?\s*g|\b1/8\b|\b1/4\b|\b1/2\b|\b1oz\b|\boz\b)",
            text,
            re.IGNORECASE,
        )
        if m_weight:
            weight = m_weight.group(1)

        # Brand: look for class with 'brand'
        brand_tag = card.find(
            ["span", "p", "div"],
            class_=lambda c: isinstance(c, str) and "brand" in c.lower()
        )
        if brand_tag:
            brand = brand_tag.get_text(strip=True)

        if name:
            rows.append({
                "Brand": brand,
                "Product": name,
                "Category": category_hint,
                "Weight": weight,
                "THC": thc,
                "Price": price,
            })

    if not rows:
        return pd.DataFrame(columns=["Brand", "Product", "Category", "Weight", "THC", "Price"])

    df = pd.DataFrame(rows)
    df["Price"] = pd.to_numeric(df["Price"], errors="coerce")
    df["THC"] = pd.to_numeric(df["THC"], errors="coerce")
    return df


# ------------------ Generic Scraper ------------------ #

def fetch_menu_generic(root_url: str) -> pd.DataFrame:
    session = make_age_verified_session(root_url)
    category_urls = discover_category_links(session, root_url)

    all_frames = []

    for category_label, url in category_urls.items():
        try:
            resp = session.get(url, timeout=25)
        except Exception:
            continue

        if resp.status_code != 200 or not resp.text:
            continue

        df_cat = parse_products_from_html(resp.text, category_hint=category_label)
        if not df_cat.empty:
            all_frames.append(df_cat)

    if not all_frames:
        return pd.DataFrame(columns=["Brand", "Product", "Category", "Weight", "THC", "Price"])

    df = pd.concat(all_frames, ignore_index=True)
    return df


# ------------------ Engine Stubs (can be upgraded later) ------------------ #

def fetch_menu_aiq(root_url: str) -> pd.DataFrame:
    return fetch_menu_generic(root_url)


def fetch_menu_dutchie(root_url: str) -> pd.DataFrame:
    return fetch_menu_generic(root_url)


def fetch_menu_jane(root_url: str) -> pd.DataFrame:
    return fetch_menu_generic(root_url)


def fetch_competitor_menu(root_url: str) -> pd.DataFrame:
    platform = detect_platform(root_url)
    if platform == "dutchie":
        return fetch_menu_dutchie(root_url)
    if platform == "jane":
        return fetch_menu_jane(root_url)
    if platform == "aiq":
        return fetch_menu_aiq(root_url)
    return fetch_menu_generic(root_url)


# ------------------ Streamlit UI ------------------ #

def main():
    st.set_page_config(
        page_title="Competitor Menu Intel",
        layout="wide",
    )

    st.title("Competitor Menu Intelligence (Standalone)")

    st.write(
        "Paste dispensary menu URLs one at a time. "
        "Each scan will be added to a combined table you can export to Excel."
    )

    # Initialize session storage
    if "all_competitors" not in st.session_state:
        st.session_state["all_competitors"] = pd.DataFrame()

    default_url = "https://naturesmedicines.com/menu/ma-fall-river/med"
    url = st.text_input("Competitor menu URL", value=default_url)

    dispo_label = st.text_input(
        "Dispensary label (optional)",
        value="Nature's Medicines - Fall River (Med)",
        help="This will be added as a 'Source' column so you can tell locations apart."
    )

    col_btn1, col_btn2 = st.columns([1, 1])

    scan_clicked = col_btn1.button("Scan & Add to Table")
    clear_clicked = col_btn2.button("Clear All Data")

    if clear_clicked:
        st.session_state["all_competitors"] = pd.DataFrame()
        st.success("Cleared all stored competitor data.")

    if scan_clicked and url:
        with st.spinner("Scanning menu and parsing products…"):
            df = fetch_competitor_menu(url)

        if df.empty:
            st.warning(
                "No products were detected. This site may rely heavily on "
                "JavaScript/API and might need a custom parser."
            )
        else:
            # Tag with source info
            source_label = dispo_label.strip() if dispo_label.strip() else url
            df["Source"] = source_label
            df["Source_URL"] = url

            # Append to global table
            if st.session_state["all_competitors"].empty:
                st.session_state["all_competitors"] = df
            else:
                st.session_state["all_competitors"] = pd.concat(
                    [st.session_state["all_competitors"], df],
                    ignore_index=True
                )

            st.success(f"Added {len(df)} products from: {source_label}")

            # Show summary just for this scan
            st.subheader(f"Latest Scan – {source_label}")

            if "Price" in df.columns:
                df["Price"] = pd.to_numeric(df["Price"], errors="coerce")
            if "THC" in df.columns:
                df["THC"] = pd.to_numeric(df["THC"], errors="coerce")

            total_items = len(df)
            total_brands = df["Brand"].nunique() if "Brand" in df.columns else 0
            avg_price = df["Price"].mean() if "Price" in df.columns else None

            c1, c2, c3 = st.columns(3)
            c1.metric("Total Menu Items (This Dispo)", total_items)
            c2.metric("Unique Brands (This Dispo)", total_brands)
            c3.metric(
                "Average Price (This Dispo)",
                f"${avg_price:,.2f}" if avg_price is not None and not pd.isna(avg_price) else "N/A",
            )

            if "Category" in df.columns:
                cat_group = (
                    df.groupby("Category", dropna=False)
                    .agg(
                        SKU_Count=("Product", "count"),
                        Min_Price=("Price", "min"),
                        Max_Price=("Price", "max"),
                        Avg_Price=("Price", "mean"),
                        Avg_THC=("THC", "mean"),
                    )
                    .reset_index()
                )
                st.markdown("**Category Breakdown (This Dispo)**")
                st.dataframe(cat_group)

            st.markdown("**Detailed Products (This Dispo)**")
            st.dataframe(df)

    # ---------- Combined Table & Excel Export ---------- #

    all_df = st.session_state["all_competitors"]

    st.markdown("---")
    st.subheader("Combined Competitor Table (All Dispensaries This Session)")

    if all_df.empty:
        st.info("No data yet. Scan at least one dispensary menu to build the table.")
    else:
        st.write(f"Total rows across all dispensaries: **{len(all_df)}**")
        st.dataframe(all_df)

        # Excel export
        output = BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            all_df.to_excel(writer, index=False, sheet_name="Competitors")
        output.seek(0)

        st.download_button(
            label="Download Full Excel File",
            data=output,
            file_name="competitor_menus.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


if __name__ == "__main__":
    main()


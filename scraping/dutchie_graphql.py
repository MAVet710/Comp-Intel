"""
Dutchie GraphQL-based menu crawler.

Uses Playwright to capture Dutchie GraphQL API responses across all categories
and paginated pages.  Handles MED and REC menus, in-stock filtering, and
multi-variant products (one row per purchasable size/weight option).

Provides:
- crawl_dutchie(url, menu_type=None, timeout=45000, max_pages=20)
  -> (DataFrame, debug_info)
"""

import json
import re
from urllib.parse import quote

import pandas as pd

try:
    from playwright.sync_api import sync_playwright

    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

# Stock-related field names (lower-cased) used to determine availability
_STOCK_KEYS = frozenset(
    {
        "instock",
        "isavailable",
        "available",
        "stockstatus",
        "inventorystatus",
        "quantityavailable",
        "quantity",
    }
)

# CSS selectors to wait on so we know the menu has rendered
_MENU_SELECTORS = [
    "[class*='product-card']",
    "[class*='ProductCard']",
    "[class*='menu-item']",
    "[class*='MenuItem']",
    "[class*='productGrid']",
    "[class*='product-grid']",
    "[class*='products-grid']",
]

# Age-gate button texts (case-insensitive)
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _detect_menu_type(url: str) -> str | None:
    """Infer med/rec from URL path."""
    lower = url.lower()
    if "/med" in lower or "medical" in lower:
        return "med"
    if "/rec" in lower or "adult" in lower or "recreational" in lower:
        return "rec"
    return None


def _is_in_stock(product: dict) -> bool:
    """
    Return True when a product appears to be in stock.

    Checks common Dutchie stock field names.  When no stock field is found
    the product is included (True) so we never silently drop unknown schemas.
    """
    for k, v in product.items():
        if k.lower() not in _STOCK_KEYS:
            continue
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.lower() not in (
                "false",
                "0",
                "out_of_stock",
                "out of stock",
                "unavailable",
                "no",
            )
        if isinstance(v, (int, float)):
            return v > 0

    # Check variant-level stock
    variants = product.get("variants") or product.get("options") or []
    if variants and isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            for k, v in variant.items():
                if k.lower() not in _STOCK_KEYS:
                    continue
                if isinstance(v, bool):
                    if v:
                        return True
                elif isinstance(v, str):
                    if v.lower() not in (
                        "false",
                        "0",
                        "out_of_stock",
                        "unavailable",
                    ):
                        return True
                elif isinstance(v, (int, float)):
                    if v > 0:
                        return True
        # Variants exist but none had a recognised stock field – include
        return True

    # No stock field found anywhere – include with note
    return True


def _extract_cannabinoid(product: dict, name: str) -> str | None:
    """
    Return a cannabinoid percentage string (e.g. '22.5%') from the
    product.cannabinoids array, or None when not present.
    """
    cannabinoids = product.get("cannabinoids") or []
    for c in cannabinoids:
        if not isinstance(c, dict):
            continue
        cb_obj = c.get("cannabinoid") or {}
        if isinstance(cb_obj, dict):
            cname = cb_obj.get("name", "") or ""
        elif isinstance(cb_obj, str):
            cname = cb_obj
        else:
            cname = c.get("cannabinoidType") or c.get("name") or ""
        if isinstance(cname, str) and cname.upper() == name.upper():
            val = (
                c.get("value")
                or c.get("formattedValue")
                or c.get("percentageValue")
            )
            if val is not None:
                return str(val)
    return None


def _build_rows_from_product(
    product: dict, source_url: str, menu_type: str | None
) -> list[dict]:
    """
    Build one DataFrame row per purchasable variant (size/weight).
    Falls back to a single row at product level if no variants exist.
    """
    name = (
        product.get("name")
        or product.get("title")
        or product.get("displayName")
    )
    if not name or not isinstance(name, str) or not name.strip():
        return []

    name = name.strip()

    category = (
        product.get("category")
        or product.get("type")
        or product.get("productType")
        or product.get("kind")
    )
    if isinstance(category, dict):
        category = category.get("name") or category.get("title")

    brand = None
    brand_obj = product.get("brand") or product.get("brandInfo")
    if isinstance(brand_obj, dict):
        brand = brand_obj.get("name") or brand_obj.get("title")
    elif isinstance(brand_obj, str):
        brand = brand_obj

    thc = _extract_cannabinoid(product, "THC")
    cbd = _extract_cannabinoid(product, "CBD")
    product_id = (
        product.get("id")
        or product.get("productId")
        or product.get("slug")
    )

    variants = product.get("variants") or product.get("options") or []
    rows: list[dict] = []

    if variants and isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            # Per-variant stock check
            variant_in_stock = True
            for k, v in variant.items():
                if k.lower() not in _STOCK_KEYS:
                    continue
                if isinstance(v, bool):
                    variant_in_stock = v
                elif isinstance(v, str):
                    variant_in_stock = v.lower() not in (
                        "false",
                        "0",
                        "out_of_stock",
                        "unavailable",
                    )
                elif isinstance(v, (int, float)):
                    variant_in_stock = v > 0
                break
            if not variant_in_stock:
                continue

            price = (
                variant.get("specialPrice")
                or variant.get("price")
                or variant.get("amount")
                or variant.get("listPrice")
            )
            size = (
                variant.get("size")
                or variant.get("weight")
                or variant.get("option")
            )
            variant_id = variant.get("id") or variant.get("variantId")
            sku = str(variant_id) if variant_id else (
                str(product_id) if product_id else None
            )

            rows.append(
                {
                    "Product": name,
                    "Menu_Type": menu_type,
                    "Category": category,
                    "Brand": brand,
                    "THC": thc,
                    "CBD": cbd,
                    "Price": price,
                    "Size": str(size) if size is not None else None,
                    "SKU": sku,
                    "Source": "Dutchie GraphQL",
                    "Source_URL": source_url,
                }
            )
    else:
        # No variants – use product-level price
        price = product.get("price") or product.get("basePrice") or product.get("amount")
        if isinstance(price, dict):
            price = price.get("amount") or price.get("value")

        rows.append(
            {
                "Product": name,
                "Menu_Type": menu_type,
                "Category": category,
                "Brand": brand,
                "THC": thc,
                "CBD": cbd,
                "Price": price,
                "Size": None,
                "SKU": str(product_id) if product_id else None,
                "Source": "Dutchie GraphQL",
                "Source_URL": source_url,
            }
        )

    return rows


# Known nested paths to the product array in Dutchie GraphQL responses
_PRODUCT_ARRAY_PATHS: list[list[str]] = [
    ["data", "filteredProducts", "products"],
    ["data", "products", "products"],
    ["data", "menuProducts"],
    ["data", "products"],
]


def _extract_products_from_payload(payload: dict) -> list[dict]:
    """Return the product list from a captured GraphQL response dict."""
    body = payload.get("json") or payload.get("data")
    if not body or not isinstance(body, dict):
        return []

    # Try known paths first (fast path)
    for path in _PRODUCT_ARRAY_PATHS:
        obj = body
        for key in path:
            if not isinstance(obj, dict):
                break
            obj = obj.get(key)
        if isinstance(obj, list):
            return obj

    # Fallback: find the largest list of product-like objects
    return _find_product_list(body)


def _find_product_list(obj, depth: int = 0, max_depth: int = 6) -> list:
    """Recursively find the largest list of dicts with a name/title field."""
    if depth > max_depth:
        return []

    best: list = []

    if isinstance(obj, dict):
        for v in obj.values():
            candidate = _find_product_list(v, depth + 1, max_depth)
            if len(candidate) > len(best):
                best = candidate

    elif isinstance(obj, list):
        named = sum(
            1
            for item in obj
            if isinstance(item, dict) and ("name" in item or "title" in item)
        )
        if named >= min(2, len(obj)) and named > 0:
            return obj
        for item in obj:
            candidate = _find_product_list(item, depth + 1, max_depth)
            if len(candidate) > len(best):
                best = candidate

    return best


def _page_url(base_url: str, category: str | None, page: int) -> str:
    """Build a Dutchie menu URL with dtche[category] and dtche[page] params."""
    # Strip existing query string
    base = base_url.split("?")[0]
    params = []
    if category:
        params.append(f"dtche%5Bcategory%5D={quote(category, safe='')}")
    if page > 1:
        params.append(f"dtche%5Bpage%5D={page}")
    if params:
        return base + "?" + "&".join(params)
    return base


def _discover_categories(page) -> list[str]:
    """
    Discover Dutchie category slugs from the rendered page by looking for
    links/buttons that carry a ``dtche[category]`` query parameter.
    """
    categories: list[str] = []
    seen: set[str] = set()

    # 1) Links with dtche in href
    try:
        links = page.query_selector_all("a[href*='dtche']")
        for link in links:
            href = link.get_attribute("href") or ""
            m = re.search(
                r"dtche(?:%5B|\[)category(?:%5D|\])=([^&\"'\s]+)", href, re.I
            )
            if m:
                cat = re.sub(r"%20", " ", m.group(1)).strip()
                if cat and cat not in seen:
                    categories.append(cat)
                    seen.add(cat)
    except Exception:
        pass

    # 2) Scan full page HTML for any dtche[category] occurrences
    try:
        html = page.content()
        for m in re.finditer(
            r"dtche(?:%5B|\[)category(?:%5D|\])=([^&\"'\s<>]+)", html, re.I
        ):
            cat = re.sub(r"%20", " ", m.group(1)).strip()
            if cat and cat not in seen:
                categories.append(cat)
                seen.add(cat)
    except Exception:
        pass

    return categories


def _bypass_age_gate(page) -> None:
    """Best-effort 21+ age-gate dismissal (mirrors playwright_helpers logic)."""
    # 1) CSS selectors
    for selector in _AGE_GATE_SELECTORS:
        try:
            el = page.query_selector(selector)
            if el and el.is_visible():
                el.click(timeout=3000)
                page.wait_for_timeout(1500)
                return
        except Exception:
            pass

    # 2) Text-based matching
    for text in _AGE_GATE_TEXTS:
        for tag in ["button", "a", "[role='button']"]:
            try:
                locator = page.locator(f"{tag}:has-text('{text}')").first
                if locator.is_visible(timeout=1000):
                    locator.click(timeout=3000)
                    page.wait_for_timeout(1500)
                    return
            except Exception:
                pass


def _wait_for_menu(page, timeout_ms: int = 8000) -> None:
    """Wait for any of the known menu container selectors to appear."""
    for selector in _MENU_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=timeout_ms)
            return
        except Exception:
            pass
    # Final fallback: just wait a bit
    page.wait_for_timeout(3000)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def crawl_dutchie(
    url: str,
    menu_type: str | None = None,
    timeout: int = 45000,
    max_pages: int = 20,
) -> tuple[pd.DataFrame, dict]:
    """
    Full Dutchie menu crawler.

    Launches a headless Chromium browser, attaches a GraphQL response
    listener *before* navigation, discovers all category slugs from the
    rendered page, and iterates through each category page-by-page until
    no new products are found or a page signature repeats.

    Args:
        url:        Dutchie (or Dutchie-embedded) menu URL.
        menu_type:  ``'med'`` or ``'rec'``.  Auto-detected from URL when None.
        timeout:    Playwright navigation timeout in milliseconds.
        max_pages:  Maximum pages per category to prevent infinite loops.

    Returns:
        ``(df, debug_info)`` where *df* has columns:
        Product, Menu_Type, Category, Brand, THC, CBD, Price, Size, SKU,
        Source, Source_URL.
    """
    if menu_type is None:
        menu_type = _detect_menu_type(url)

    debug_info: dict = {
        "captured_count": 0,
        "captured_urls": [],
        "categories": [],
        "per_page_counts": {},
        "parse_notes": [],
        "graphql_details": [],
    }

    if not HAS_PLAYWRIGHT:
        debug_info["parse_notes"].append("Playwright not available – install with: playwright install chromium")
        return pd.DataFrame(), debug_info

    all_rows: list[dict] = []
    seen_keys: set[tuple] = set()

    def _dedup_and_add(rows: list[dict]) -> int:
        added = 0
        for row in rows:
            key = (row.get("Product"), row.get("Price"), row.get("Size"))
            if key not in seen_keys:
                seen_keys.add(key)
                all_rows.append(row)
                added += 1
        return added

    # Shared captured-response list – appended to by the listener below
    captured: list[dict] = []

    def _on_response(response) -> None:
        """Capture every GraphQL response, regardless of Content-Type header."""
        resp_url = response.url
        url_lower = resp_url.lower()
        # Only care about GraphQL endpoints
        if "graphql" not in url_lower and "operationname" not in url_lower:
            return
        ctype = (response.headers.get("content-type") or "").lower()
        try:
            # Always use text() → json.loads() so we are not gated on
            # the Content-Type header being exactly 'application/json'
            text = response.text()
            body = json.loads(text)
            if not body:
                return
            entry = {
                "url": resp_url,
                "status": response.status,
                "content_type": ctype,
                "json": body,
                "data": body,
                "text_snippet": text[:200],
            }
            captured.append(entry)
            debug_info["captured_count"] += 1
            debug_info["captured_urls"].append(resp_url)
            debug_info["graphql_details"].append(
                {
                    "url": resp_url,
                    "status": response.status,
                    "body_length": len(text),
                    "json_ok": True,
                }
            )
        except Exception as exc:
            debug_info["graphql_details"].append(
                {
                    "url": resp_url,
                    "status": response.status,
                    "body_length": None,
                    "json_ok": False,
                    "error": str(exc),
                }
            )

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
            # Pre-set localStorage age-gate keys before any page loads
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
            # *** Attach listener BEFORE first navigation ***
            page.on("response", _on_response)

            # --- Initial page load ---
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_timeout(3000)
            _bypass_age_gate(page)
            page.wait_for_timeout(2000)
            _wait_for_menu(page)
            page.wait_for_timeout(1000)

            # --- Discover categories ---
            categories = _discover_categories(page)
            debug_info["categories"] = categories
            debug_info["parse_notes"].append(
                f"Discovered {len(categories)} categories: {categories}"
            )

            if not categories:
                # No dtche category links found — parse whatever was captured
                debug_info["parse_notes"].append(
                    "No categories discovered; parsing initial page responses"
                )
                initial_count = 0
                for payload in captured:
                    products = _extract_products_from_payload(payload)
                    rows: list[dict] = []
                    for prod in products:
                        if not _is_in_stock(prod):
                            continue
                        rows.extend(
                            _build_rows_from_product(prod, url, menu_type)
                        )
                    initial_count += _dedup_and_add(rows)
                debug_info["per_page_counts"]["initial"] = initial_count
            else:
                for cat in categories:
                    cat_total = 0
                    prev_signatures: set = set()

                    for pg in range(1, max_pages + 1):
                        cap_before = len(captured)
                        nav_url = _page_url(url, cat, pg)

                        page.goto(
                            nav_url,
                            wait_until="domcontentloaded",
                            timeout=timeout,
                        )
                        page.wait_for_timeout(2000)
                        _wait_for_menu(page)
                        page.wait_for_timeout(1000)

                        # Only look at responses captured during this navigation
                        new_captures = captured[cap_before:]
                        page_products: list[dict] = []
                        for payload in new_captures:
                            raw_products = _extract_products_from_payload(payload)
                            for prod in raw_products:
                                if not _is_in_stock(prod):
                                    continue
                                page_products.extend(
                                    _build_rows_from_product(
                                        prod, url, menu_type
                                    )
                                )

                        page_key = f"{cat}_page{pg}"

                        if not page_products:
                            debug_info["parse_notes"].append(
                                f"Category '{cat}' page {pg}: 0 products – stopping"
                            )
                            debug_info["per_page_counts"][page_key] = 0
                            break

                        # Stable-signature check: stop if we see the same
                        # set of (name, price, size) tuples again
                        sig = frozenset(
                            (r.get("Product"), r.get("Price"), r.get("Size"))
                            for r in page_products
                        )
                        if sig in prev_signatures:
                            debug_info["parse_notes"].append(
                                f"Category '{cat}' page {pg}: repeated signature – stopping"
                            )
                            debug_info["per_page_counts"][page_key] = 0
                            break
                        prev_signatures.add(sig)

                        added = _dedup_and_add(page_products)
                        cat_total += added
                        debug_info["per_page_counts"][page_key] = added

                    debug_info["parse_notes"].append(
                        f"Category '{cat}': {cat_total} unique rows added"
                    )

            browser.close()

    except Exception as exc:
        debug_info["parse_notes"].append(f"Crawler error: {exc}")

    if not all_rows:
        return pd.DataFrame(), debug_info

    df = pd.DataFrame(all_rows)
    if "Price" in df.columns:
        df["Price"] = pd.to_numeric(df["Price"], errors="coerce")
    return df, debug_info

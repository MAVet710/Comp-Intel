"""
Dutchie (and generic GraphQL) product data parser.

Provides:
- parse_dutchie_responses(payloads) -> pd.DataFrame

The parser recursively searches JSON payloads for objects that look like
products (have a name/title field, optionally a price) and builds a
normalised DataFrame with columns: Product, Category, Price, THC, Source.
"""

import re

import pandas as pd

# Lower-cased key names recognised as product name fields
_NAME_KEYS = frozenset({"name", "title", "productname", "product_name", "displayname"})

# Lower-cased key names recognised as price fields
_PRICE_KEYS = frozenset(
    {
        "price",
        "baseprice",
        "unitprice",
        "amount",
        "cost",
        "discountedprice",
        "saleprice",
        "listprice",
    }
)

# Lower-cased key names recognised as THC fields
_THC_KEYS = frozenset(
    {"thc", "thccontent", "thcpercentage", "thcmax", "thcmin", "thclevel"}
)

# Lower-cased key names recognised as category fields
_CATEGORY_KEYS = frozenset(
    {"category", "type", "producttype", "subcategory", "kind", "menutype"}
)


def _extract_name(obj: dict) -> str | None:
    for k, v in obj.items():
        if k.lower() in _NAME_KEYS and isinstance(v, str) and len(v.strip()) > 1:
            return v.strip()
    return None


def _extract_price(obj: dict) -> float | None:
    for k, v in obj.items():
        if k.lower() not in _PRICE_KEYS:
            continue
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
        if isinstance(v, str):
            m = re.search(r"\d+\.?\d*", v.replace(",", ""))
            if m:
                try:
                    return float(m.group(0))
                except ValueError:
                    pass
    return None


def _extract_thc(obj: dict) -> float | None:
    for k, v in obj.items():
        if k.lower() not in _THC_KEYS:
            continue
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            m = re.search(r"(\d+\.?\d*)", v)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    pass
    return None


def _extract_category(obj: dict) -> str | None:
    for k, v in obj.items():
        if k.lower() not in _CATEGORY_KEYS:
            continue
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            inner = v.get("name") or v.get("title")
            if inner and isinstance(inner, str):
                return inner.strip()
    return None


def _search_for_products(obj, depth: int = 0, max_depth: int = 8) -> list:
    """
    Recursively search a JSON value for product-like dicts — objects that
    contain at least a name/title field.  Price, THC, and category are
    extracted when present.

    Returns a list of raw product dicts with keys:
    Product, Category, Price, THC.
    """
    if depth > max_depth:
        return []

    results: list[dict] = []

    if isinstance(obj, dict):
        name = _extract_name(obj)
        if name:
            results.append(
                {
                    "Product": name,
                    "Category": _extract_category(obj),
                    "Price": _extract_price(obj),
                    "THC": _extract_thc(obj),
                }
            )
        # Always recurse into dict values
        for v in obj.values():
            if isinstance(v, (dict, list)):
                results.extend(_search_for_products(v, depth + 1, max_depth))

    elif isinstance(obj, list):
        for item in obj:
            results.extend(_search_for_products(item, depth + 1, max_depth))

    return results


def parse_dutchie_responses(payloads: list) -> pd.DataFrame:
    """
    Given a list of ``{"url": str, "data": any}`` dicts captured from
    Dutchie/GraphQL network responses, extract products and return a
    normalised DataFrame.

    Args:
        payloads: Network response payloads as returned by
                  ``browser_fetch()``.

    Returns:
        DataFrame with columns: Product, Category, Price, THC, Source.
        Empty DataFrame (with those columns) if no products are found.
    """
    rows: list[dict] = []
    # Use (name, price) composite key to allow same-named products at different price points
    seen_keys: set[tuple] = set()

    for payload in payloads:
        src_url = payload.get("url", "Dutchie API")
        # Truncate long URLs to keep the Source label readable
        if len(src_url) > 60:
            source_label = f"Dutchie API ({src_url[:57]}...)"
        else:
            source_label = f"Dutchie API ({src_url})"

        data = payload.get("data")
        if data is None:
            continue

        for p in _search_for_products(data):
            name = p["Product"]
            # Use (name, price) as the composite dedup key so that same-named
            # products at different price points (e.g. different sizes) are kept
            dedup_key = (name, p["Price"])
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            rows.append(
                {
                    "Product": name,
                    "Category": p["Category"],
                    "Price": p["Price"],
                    "THC": p["THC"],
                    "Source": source_label,
                }
            )

    if not rows:
        return pd.DataFrame(columns=["Product", "Category", "Price", "THC", "Source"])

    df = pd.DataFrame(rows)
    df["Price"] = pd.to_numeric(df["Price"], errors="coerce")
    df["THC"] = pd.to_numeric(df["THC"], errors="coerce")
    return df

"""Sainsbury's grocery price lookup via the public GOL product API.

Resolves a price for a scanned product by querying Sainsbury's groceries
search (keyword built from brand/name/quantity) and matching the results
against the product's barcode (EAN). Public entry point is
``get_price(barcode, name, brand, product_quantity)``, returning the price,
price type and product URL, or ``None`` when there's no confident match.

DISCLAIMER — personal/educational use only.
This module queries an undocumented internal Sainsbury's endpoint (not a public
API) and sends a browser-like User-Agent. It is included for personal,
educational reference only. This project is not affiliated with or endorsed by
Sainsbury's; the product and price data belong to Sainsbury's, and automated
access may be contrary to their website terms. Keep any use low-volume and
personal — not for bulk or commercial data collection — and ensure your use
complies with Sainsbury's terms and applicable law. The GPLv3 licence governs
this code, not your use of any third-party service. See the README section
"Price data and the Sainsbury's lookup" for the full note.
"""

import logging
import time
from typing import Any

import requests

_BASE_URL = "https://www.sainsburys.co.uk/groceries-api/gol-services/product/v1/product"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}
_PAGE_SIZE = 10
_MAX_PAGES = 3
_TIMEOUT = 10
_INTER_PAGE_SLEEP = 0.5


def _build_query(brand: str, name: str, product_quantity: str | None) -> str:
    """Return a search query string from product metadata, e.g. 'Heinz Baked Beans 415g'."""
    if product_quantity is None:
        return f"{brand} {name}"
    return f"{brand} {name} {product_quantity}"


def _ean_matches(barcode: str, eans: list[str]) -> bool:
    """Return True if *barcode* matches any entry in *eans* after stripping leading zeros."""
    stripped = barcode.lstrip("0")
    return any(stripped == ean.lstrip("0") for ean in eans)


def _extract_price(product: dict[str, Any]) -> dict | None:
    """Parse a Sainsbury's product dict and return a normalised price dict, or None on failure."""
    retail_price = product.get("retail_price")
    if retail_price is None:
        logging.warning("Sainsbury's: retail_price missing on matched product")
        return None

    measure = retail_price.get("measure", "")

    if measure in ("unit", "ea", ""):
        return {
            "price_pence": round(retail_price["price"] * 100),
            "price_type": "unit",
        }

    if measure == "kg":
        unit_price = product.get("unit_price")
        if unit_price is None:
            logging.warning("Sainsbury's: unit_price missing on per-kg product")
            return None
        return {
            "price_pence": round(unit_price["price"] * 100),
            "price_type": "per_kg",
        }

    logging.warning(f"Sainsbury's: unrecognised measure '{measure}'")
    return None


def get_price(barcode: str, name: str, brand: str, product_quantity: str | None) -> dict | None:
    """
    Search Sainsbury's for a product matching the given barcode.

    Args:
        barcode:          EAN barcode string (e.g. "5014788110140")
        name:             Product name from OpenFoodFacts (e.g. "Red Kidney Beans in Chilli Sauce")
        brand:            Brand from OpenFoodFacts (e.g. "Sainsbury's")
        product_quantity: Quantity string from OpenFoodFacts (e.g. "400g", "500ml"), or None

    Returns:
        {
            "price_pence": int,        # e.g. 110
            "price_type": str,         # "unit" or "per_kg"
            "product_url": str | None  # e.g. "https://www.sainsburys.co.uk/gol-ui/product/..."
        }
        or None if no match found.
    """
    query = _build_query(brand, name, product_quantity)

    for page in range(1, _MAX_PAGES + 1):
        logging.debug(f"Sainsbury's search: query='{query}' page={page}")

        response = requests.get(
            _BASE_URL,
            headers=_HEADERS,
            params={
                "filter[keyword]": query,
                "page_number": page,
                "page_size": _PAGE_SIZE,
                "sort_order": "FAVOURITES_FIRST",
            },  # type: ignore[arg-type]
            timeout=_TIMEOUT,
        )

        if response.status_code >= 400:
            logging.error(f"Sainsbury's: HTTP {response.status_code} for query '{query}'")
            return None

        data = response.json()

        if "products" not in data:
            logging.warning(f"Sainsbury's: 'products' key missing in response for query '{query}'")
            return None

        for product in data["products"]:
            if "eans" not in product:
                continue
            if _ean_matches(barcode, product["eans"]):
                logging.debug(f"Sainsbury's: found EAN match on page {page} for barcode {barcode}")
                price = _extract_price(product)
                if price is not None:
                    url = product.get("full_url") or None
                    if url and url.startswith("://"):
                        url = "https" + url
                    # Reject any non-http(s) scheme before storing — a hostile
                    # API response must not be able to inject e.g. a javascript:
                    # URL that later reaches an anchor href in the frontend.
                    if url and not url.startswith(("https://", "http://")):
                        url = None
                    price["product_url"] = url
                return price

        if page < _MAX_PAGES:
            time.sleep(_INTER_PAGE_SLEEP)

    logging.warning(f"Sainsbury's: no EAN match after {_MAX_PAGES} pages for barcode {barcode}")
    return None

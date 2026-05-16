import logging
import time

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


def _build_query(brand: str, name: str, weight_g: float) -> str:
    if weight_g == int(weight_g):
        weight_str = f"{int(weight_g)}g"
    else:
        weight_str = f"{weight_g:.1f}g"
    return f"{brand} {name} {weight_str}"


def _ean_matches(barcode: str, eans: list) -> bool:
    stripped = barcode.lstrip("0")
    return any(stripped == ean.lstrip("0") for ean in eans)


def _extract_price(product: dict) -> dict | None:
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


def get_price(barcode: str, name: str, brand: str, weight_g: float) -> dict | None:
    """
    Search Sainsbury's for a product matching the given barcode.

    Args:
        barcode:  EAN barcode string (e.g. "5014788110140")
        name:     Product name from OpenFoodFacts (e.g. "Red Kidney Beans in Chilli Sauce")
        brand:    Brand from OpenFoodFacts (e.g. "Sainsbury's")
        weight_g: Weight in grams from OpenFoodFacts (e.g. 400.0)

    Returns:
        {
            "price_pence": int,
            "price_type": str,  # "unit" or "per_kg"
        }
        or None if no match found.
    """
    query = _build_query(brand, name, weight_g)

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
            },
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
                return _extract_price(product)

        if page < _MAX_PAGES:
            time.sleep(_INTER_PAGE_SLEEP)

    logging.warning(f"Sainsbury's: no EAN match after {_MAX_PAGES} pages for barcode {barcode}")
    return None

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print(get_price("00485081", "Mixed beans in mild chilli sauce", "Sainsbury's", 395))
    
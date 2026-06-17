"""OpenFoodFacts barcode lookup client."""

import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv

_headers: dict | None = None


def _get_headers() -> dict:
    global _headers
    if _headers is None:
        load_dotenv(Path(__file__).parents[2] / "config.local.env")
        email = os.environ.get("OFF_CONTACT_EMAIL")
        if not email:
            raise RuntimeError("OFF_CONTACT_EMAIL not set in config.local.env")
        _headers = {"User-Agent": f"FFInventory/2.0.0 ({email})"}
    return _headers


def _parse_weight_string(s: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(g|kg)\b", s, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    if unit == "kg":
        value *= 1000
    return value


def lookup_barcode(barcode: str) -> dict | None:
    """
    Query OpenFoodFacts for product info.

    Returns:
        {"name": str | None, "brand": str | None, "weight_g": float | None}
        or None if the product was not found (status != 1).

    Raises:
        requests.RequestException  on network errors
    """
    response = requests.get(
        f"https://world.openfoodfacts.org/api/v2/product/{barcode}",
        headers=_get_headers(),
        params={"fields": "status,product_name,product_name_en,brands,product_quantity,product_quantity_unit,quantity"},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("status") != 1:
        return None

    product = data.get("product", {})

    name = product.get("product_name") or product.get("product_name_en") or None
    if name == "":
        name = None

    brands = product.get("brands", "")
    if brands:
        brand = brands.split(",")[0].strip() or None
    else:
        brand = None

    weight_g = None
    if "product_quantity" in product:
        try:
            weight_g = float(product["product_quantity"])
        except (ValueError, TypeError):
            pass
    if weight_g is None:
        raw = product.get("quantity", "")
        if raw:
            weight_g = _parse_weight_string(str(raw))

    return {"name": name, "brand": brand, "weight_g": weight_g}

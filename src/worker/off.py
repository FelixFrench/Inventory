"""OpenFoodFacts barcode lookup client."""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from src.version import __version__

_headers: dict | None = None

# Defensive cap on the externally-sourced product_quantity string before it is
# stored. Real values are short ("400g", "500ml"); this bounds an anomalous OFF
# response from writing an unbounded TEXT blob to the DB.
_MAX_PQ_LEN = 64

# The same defensive intent for name and brand, but deliberately a LOOSER bound. Unlike
# product_quantity these are not just stored and rendered — they are re-emitted outbound as
# the Sainsbury's search keyword (sainsburys._build_query), so truncating them at 64 would
# silently degrade price lookups. 200 clears the longest realistic OFF product_name (~120
# characters) while still bounding a multi-kilobyte anomalous response.
_MAX_TEXT_LEN = 200


def _get_headers() -> dict:
    global _headers
    if _headers is None:
        load_dotenv(Path(__file__).parents[2] / "config.local.env")
        email = os.environ.get("OFF_CONTACT_EMAIL")
        if not email:
            raise RuntimeError("OFF_CONTACT_EMAIL not set in config.local.env")
        _headers = {"User-Agent": f"FFInventory/{__version__} ({email})"}
    return _headers


def lookup_barcode(barcode: str) -> dict | None:
    """
    Query OpenFoodFacts for product info.

    Returns:
        {"name": str | None, "brand": str | None, "product_quantity": str | None}
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
    if name is not None:
        name = str(name)[:_MAX_TEXT_LEN]

    brands = product.get("brands", "")
    if brands:
        brand = brands.split(",")[0].strip() or None
    else:
        brand = None
    if brand is not None:
        brand = brand[:_MAX_TEXT_LEN]

    product_quantity = None
    pq = product.get("product_quantity")
    pqu = product.get("product_quantity_unit")

    if pq is not None and str(pq) != "":
        try:
            qty_str = f"{pq:g}"
        except (TypeError, ValueError):
            qty_str = str(pq)
        if pqu is not None and str(pqu).strip() != "":
            product_quantity = f"{qty_str}{str(pqu).strip().lower()}"
        else:
            product_quantity = qty_str
    else:
        raw = product.get("quantity", "")
        product_quantity = str(raw).strip() or None

    if product_quantity is not None:
        product_quantity = product_quantity[:_MAX_PQ_LEN]

    return {"name": name, "brand": brand, "product_quantity": product_quantity}

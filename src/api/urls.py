"""URL construction helpers for product links."""

_OFF_VIEW = "https://world.openfoodfacts.org/product/{barcode}"
_OFF_ADD  = "https://world.openfoodfacts.org/cgi/product.pl?type=edit&code={barcode}"


def off_url(barcode: str, *, info_status: str | None = None, name: str | None = None) -> str:
    """
    Return the appropriate OpenFoodFacts URL for a product.

    In session contexts pass info_status; in report contexts pass name.
    Falls back to the add/edit URL when the lookup is known to have failed.
    """
    if info_status == "failed" or (info_status is None and name is None):
        return _OFF_ADD.format(barcode=barcode)
    return _OFF_VIEW.format(barcode=barcode)


def product_page_url(barcode: str, retailer_id: int) -> str:
    """Return the internal product-info page link for a variant.

    Same safety reasoning as off_url: barcode is a digit-only in-system value and
    retailer_id an integer, so there is no free-text user input to interpolate.
    """
    return f"/product.html?barcode={barcode}&retailer_id={retailer_id}"


def group_page_url(group_id: int) -> str:
    """Return the internal group-info page link for a group (integer id, no free text)."""
    return f"/group.html?id={group_id}"

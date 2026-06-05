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

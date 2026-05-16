import re


def is_valid_barcode(s: str) -> bool:
    """Returns True if s is a valid barcode: digits only, 8-14 characters."""
    return re.fullmatch(r'\d{8,14}', s) is not None

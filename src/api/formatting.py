"""Shared formatting helpers for API responses."""


def format_weight(weight_g) -> str | None:
    if weight_g is None:
        return None
    if weight_g >= 1000:
        return f"{weight_g / 1000:g}kg"
    return f"{weight_g:g}g"

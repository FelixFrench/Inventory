"""Unit tests for src/api/formatting.py — the shared weight formatter."""

from src.api.formatting import format_weight


def test_format_weight_none():
    assert format_weight(None) is None


def test_format_weight_grams():
    assert format_weight(415) == "415g"
    assert format_weight(415.0) == "415g"


def test_format_weight_below_kg_boundary():
    assert format_weight(999) == "999g"


def test_format_weight_kg_boundary():
    # 1000 g is the threshold and converts to kg, dropping the trailing ".0".
    assert format_weight(1000) == "1kg"


def test_format_weight_kilograms():
    assert format_weight(1500) == "1.5kg"
    assert format_weight(2000) == "2kg"

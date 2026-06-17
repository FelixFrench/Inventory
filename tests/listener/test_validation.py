"""Tests for src/listener/validation.py — barcode format validation."""

import pytest

from src.listener.validation import is_valid_barcode


@pytest.mark.parametrize("s, expected", [
    ("12345678",         True),   # EAN-8, 8 digits
    ("1234567890123",    True),   # EAN-13, 13 digits
    ("12345678901234",   True),   # 14-digit retailer code
    ("1234567",          False),  # 7 digits — too short
    ("123456789012345",  False),  # 15 digits — too long
    ("1234567A",         False),  # contains letter
    ("",                 False),  # empty string
    ("abcdefgh",         False),  # all letters
    ("1234 5678",        False),  # contains space
    ("00447744",         True),   # real scan, 8 digits
    ("5013635312160",    True),   # real scan, 13 digits
])
def test_is_valid_barcode(s, expected):
    assert is_valid_barcode(s) == expected

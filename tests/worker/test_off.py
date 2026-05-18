import os
from unittest.mock import MagicMock, patch

import pytest
import requests

import src.worker.off as off_module
from src.worker.off import _get_headers, _parse_weight_string, lookup_barcode


def _make_response(data: dict) -> MagicMock:
    mock = MagicMock()
    mock.json.return_value = data
    return mock


def _product_response(**kwargs) -> dict:
    return {"status": 1, "product": kwargs}


# ---------------------------------------------------------------------------
# _parse_weight_string
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "s,expected",
    [
        ("395g", 395.0),
        ("400 g", 400.0),
        ("1.5kg", 1500.0),
        ("1 kg", 1000.0),
        ("500ml", None),
        ("not-a-weight", None),
    ],
)
def test_parse_weight_string_variants(s, expected):
    assert _parse_weight_string(s) == expected


# ---------------------------------------------------------------------------
# _get_headers lazy init
# ---------------------------------------------------------------------------

def test_get_headers_raises_if_env_missing():
    off_module._headers = None
    # Patch load_dotenv to a no-op so it can't populate OFF_CONTACT_EMAIL from the .env file.
    with patch("src.worker.off.load_dotenv"), \
         patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="OFF_CONTACT_EMAIL"):
            _get_headers()
    off_module._headers = None  # reset for other tests


def test_get_headers_caches_result():
    off_module._headers = None
    with patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        h1 = _get_headers()
        h2 = _get_headers()
    assert h1 is h2
    off_module._headers = None


# ---------------------------------------------------------------------------
# lookup_barcode
# ---------------------------------------------------------------------------

def test_lookup_returns_name_brand_weight():
    resp = _make_response(_product_response(
        product_name="Red Kidney Beans",
        brands="Sainsbury's",
        product_quantity=400,
    ))
    with patch("requests.get", return_value=resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        result = lookup_barcode("1234567890123")
    assert result == {"name": "Red Kidney Beans", "brand": "Sainsbury's", "weight_g": 400.0}


def test_lookup_returns_none_when_status_zero():
    resp = _make_response({"status": 0, "product": {}})
    with patch("requests.get", return_value=resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        result = lookup_barcode("1234567890123")
    assert result is None


def test_lookup_falls_back_to_product_name_en():
    resp = _make_response(_product_response(
        product_name_en="English Name",
        brands="BrandX",
        product_quantity=200,
    ))
    with patch("requests.get", return_value=resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        result = lookup_barcode("1234567890123")
    assert result["name"] == "English Name"


def test_lookup_brand_comma_separated():
    resp = _make_response(_product_response(
        product_name="Beans",
        brands="Sainsbury's, Sainsbury",
        product_quantity=395,
    ))
    with patch("requests.get", return_value=resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        result = lookup_barcode("1234567890123")
    assert result["brand"] == "Sainsbury's"


def test_lookup_brand_missing():
    resp = _make_response(_product_response(product_name="Beans"))
    with patch("requests.get", return_value=resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        result = lookup_barcode("1234567890123")
    assert result["brand"] is None


def test_lookup_weight_from_quantity_string_fallback():
    resp = _make_response(_product_response(
        product_name="Beans",
        quantity="400 g",
    ))
    with patch("requests.get", return_value=resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        result = lookup_barcode("1234567890123")
    assert result["weight_g"] == 400.0


def test_lookup_weight_kg_conversion():
    resp = _make_response(_product_response(
        product_name="Pasta",
        quantity="1.5kg",
    ))
    with patch("requests.get", return_value=resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        result = lookup_barcode("1234567890123")
    assert result["weight_g"] == 1500.0


def test_lookup_weight_absent_both_fields():
    resp = _make_response(_product_response(product_name="Beans"))
    with patch("requests.get", return_value=resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        result = lookup_barcode("1234567890123")
    assert result["weight_g"] is None


def test_lookup_propagates_request_exception():
    with patch("requests.get", side_effect=requests.RequestException("timeout")), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        with pytest.raises(requests.RequestException):
            lookup_barcode("1234567890123")


def test_lookup_raises_http_error_on_5xx():
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = requests.HTTPError("503 Server Error")
    with patch("requests.get", return_value=mock_resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        with pytest.raises(requests.HTTPError):
            lookup_barcode("1234567890123")
    off_module._headers = None

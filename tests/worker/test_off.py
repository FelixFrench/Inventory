import os
from unittest.mock import MagicMock, patch

import pytest
import requests

import src.worker.off as off_module
from src.worker.off import _get_headers, lookup_barcode


def _make_response(data: dict) -> MagicMock:
    mock = MagicMock()
    mock.json.return_value = data
    return mock


def _product_response(**kwargs) -> dict:
    return {"status": 1, "product": kwargs}


# ---------------------------------------------------------------------------
# _get_headers lazy init
# ---------------------------------------------------------------------------

def test_get_headers_raises_if_env_missing():
    off_module._headers = None
    with patch("src.worker.off.load_dotenv"), \
         patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="OFF_CONTACT_EMAIL"):
            _get_headers()
    off_module._headers = None


def test_get_headers_caches_result():
    off_module._headers = None
    with patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        h1 = _get_headers()
        h2 = _get_headers()
    assert h1 is h2
    off_module._headers = None


def test_get_headers_user_agent_carries_the_single_version_constant():
    """1a: src/version.py is the single source feeding the OFF User-Agent.

    OFF's usage policy requires an identifying UA; a hardcoded or drifted version here would
    only be visible in outbound traffic, never in a response.
    """
    from src.version import __version__

    off_module._headers = None
    with patch("src.worker.off.load_dotenv"), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}, clear=True):
        ua = _get_headers()["User-Agent"]
    off_module._headers = None

    assert ua == f"FFInventory/{__version__} (test@example.com)"


# ---------------------------------------------------------------------------
# lookup_barcode — product_quantity structured fields
# ---------------------------------------------------------------------------

def test_lookup_product_quantity_with_unit():
    resp = _make_response(_product_response(
        product_name="Red Kidney Beans",
        brands="Sainsbury's",
        product_quantity=500,
        product_quantity_unit=" ML",
    ))
    with patch("requests.get", return_value=resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        result = lookup_barcode("1234567890123")
    assert result == {"name": "Red Kidney Beans", "brand": "Sainsbury's", "product_quantity": "500ml"}


def test_lookup_product_quantity_no_unit():
    resp = _make_response(_product_response(
        product_name="Red Kidney Beans",
        brands="Sainsbury's",
        product_quantity=400,
    ))
    with patch("requests.get", return_value=resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        result = lookup_barcode("1234567890123")
    assert result["product_quantity"] == "400"


def test_lookup_product_quantity_float_stripped():
    resp = _make_response(_product_response(
        product_name="Beans",
        brands="Heinz",
        product_quantity=415.0,
        product_quantity_unit="g",
    ))
    with patch("requests.get", return_value=resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        result = lookup_barcode("1234567890123")
    assert result["product_quantity"] == "415g"


def test_lookup_product_quantity_truncated_to_max_len():
    # An anomalous OFF response whose assembled value exceeds the cap must be
    # truncated to exactly _MAX_PQ_LEN chars before it reaches the DB.
    resp = _make_response(_product_response(
        product_name="Beans",
        brands="Heinz",
        product_quantity=500,
        product_quantity_unit="m" * 100,
    ))
    with patch("requests.get", return_value=resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        result = lookup_barcode("1234567890123")
    assert len(result["product_quantity"]) == off_module._MAX_PQ_LEN
    assert result["product_quantity"].startswith("500m")


# ---------------------------------------------------------------------------
# lookup_barcode — raw quantity string fallback
# ---------------------------------------------------------------------------

def test_lookup_quantity_from_raw_string():
    resp = _make_response(_product_response(
        product_name="Beans",
        quantity="400 g",
    ))
    with patch("requests.get", return_value=resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        result = lookup_barcode("1234567890123")
    assert result["product_quantity"] == "400 g"


def test_lookup_quantity_from_raw_kg_string():
    resp = _make_response(_product_response(
        product_name="Pasta",
        quantity="1.5kg",
    ))
    with patch("requests.get", return_value=resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        result = lookup_barcode("1234567890123")
    assert result["product_quantity"] == "1.5kg"


def test_lookup_quantity_null_when_both_absent():
    resp = _make_response(_product_response(product_name="Beans"))
    with patch("requests.get", return_value=resp), \
         patch.dict(os.environ, {"OFF_CONTACT_EMAIL": "test@example.com"}):
        off_module._headers = None
        result = lookup_barcode("1234567890123")
    assert result["product_quantity"] is None


# ---------------------------------------------------------------------------
# lookup_barcode — name / brand / status
# ---------------------------------------------------------------------------

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

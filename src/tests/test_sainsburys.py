import pytest
from unittest.mock import patch, MagicMock, call
from worker.sainsburys import get_price, _build_query, _ean_matches, _extract_price


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_product(eans, retail_price, unit_price=None):
    return {
        "product_uid": "123456",
        "name": "Test Product 400g",
        "eans": eans,
        "retail_price": retail_price,
        "unit_price": unit_price or {"price": 2.75, "measure": "kg", "measure_amount": 1},
        "is_available": True,
    }


def make_response(products):
    mock = MagicMock()
    mock.status_code = 200
    mock.raise_for_status.return_value = None
    mock.json.return_value = {"products": products}
    return mock


def make_error_response(status_code):
    mock = MagicMock()
    mock.status_code = status_code
    return mock


# ---------------------------------------------------------------------------
# TestBuildQuery
# ---------------------------------------------------------------------------

class TestBuildQuery:
    def test_whole_weight_formatted_as_integer(self):
        result = _build_query("Sainsbury's", "Red Kidney Beans", 400.0)
        assert result == "Sainsbury's Red Kidney Beans 400g"

    def test_fractional_weight_formatted_with_decimal(self):
        result = _build_query("Heinz", "Soup", 1500.5)
        assert "1500.5g" in result

    def test_large_whole_weight(self):
        result = _build_query("Heinz", "Soup", 1000.0)
        assert "1000g" in result
        assert "1000.0g" not in result

    def test_brand_and_name_included(self):
        result = _build_query("Heinz", "Baked Beans", 415.0)
        assert result.startswith("Heinz Baked Beans")


# ---------------------------------------------------------------------------
# TestEanMatches
# ---------------------------------------------------------------------------

class TestEanMatches:
    def test_exact_match(self):
        assert _ean_matches("5014788110140", ["5014788110140"]) is True

    def test_zero_padded_ean_matches_short_barcode(self):
        assert _ean_matches("171915", ["0000000171915"]) is True

    def test_short_barcode_matches_zero_padded_ean(self):
        assert _ean_matches("0000000171915", ["171915"]) is True

    def test_no_match(self):
        assert _ean_matches("1234567890123", ["9999999999999"]) is False

    def test_match_in_multiple_eans(self):
        assert _ean_matches("5014788110140", ["9999999999999", "5014788110140"]) is True

    def test_empty_eans_list(self):
        assert _ean_matches("5014788110140", []) is False


# ---------------------------------------------------------------------------
# TestExtractPrice
# ---------------------------------------------------------------------------

class TestExtractPrice:
    def test_unit_price_measure_unit(self):
        product = make_product(
            eans=["1234"],
            retail_price={"price": 1.10, "measure": "unit"},
        )
        assert _extract_price(product) == {"price_pence": 110, "price_type": "unit"}

    def test_unit_price_measure_ea(self):
        product = make_product(
            eans=["1234"],
            retail_price={"price": 1.10, "measure": "ea"},
        )
        assert _extract_price(product) == {"price_pence": 110, "price_type": "unit"}

    def test_unit_price_measure_empty_string(self):
        product = make_product(
            eans=["1234"],
            retail_price={"price": 1.10, "measure": ""},
        )
        assert _extract_price(product) == {"price_pence": 110, "price_type": "unit"}

    def test_per_kg_price(self):
        product = make_product(
            eans=["1234"],
            retail_price={"price": 2.75, "measure": "kg"},
            unit_price={"price": 2.75, "measure": "kg", "measure_amount": 1},
        )
        assert _extract_price(product) == {"price_pence": 275, "price_type": "per_kg"}

    def test_price_rounded_to_pence(self):
        product = make_product(
            eans=["1234"],
            retail_price={"price": 1.005, "measure": "unit"},
        )
        result = _extract_price(product)
        assert isinstance(result["price_pence"], int)

    def test_missing_retail_price_returns_none(self):
        product = {
            "product_uid": "123456",
            "name": "Test",
            "eans": ["1234"],
            "is_available": True,
        }
        assert _extract_price(product) is None

    def test_unrecognised_measure_returns_none(self):
        product = make_product(
            eans=["1234"],
            retail_price={"price": 1.10, "measure": "litre"},
        )
        assert _extract_price(product) is None

    def test_missing_unit_price_on_per_kg_item_returns_none(self):
        product = {
            "product_uid": "123456",
            "name": "Test",
            "eans": ["1234"],
            "retail_price": {"price": 2.75, "measure": "kg"},
            "is_available": True,
        }
        assert _extract_price(product) is None


# ---------------------------------------------------------------------------
# TestGetPrice
# ---------------------------------------------------------------------------

BARCODE = "0000000171915"
NAME = "Red Kidney Beans in Chilli Sauce"
BRAND = "Sainsbury's"
WEIGHT = 400.0

_MATCH = make_product(
    eans=[BARCODE],
    retail_price={"price": 1.10, "measure": "unit"},
)
_NON_MATCH = make_product(
    eans=["9999999999999"],
    retail_price={"price": 0.50, "measure": "unit"},
)
_EMPTY = make_response([])


class TestGetPrice:
    def _call(self):
        return get_price(BARCODE, NAME, BRAND, WEIGHT)

    @patch("worker.sainsburys.time.sleep")
    @patch("worker.sainsburys.requests.get")
    def test_ean_match_on_page_1_returns_price(self, mock_get, mock_sleep):
        mock_get.side_effect = [make_response([_MATCH])]
        result = self._call()
        assert result == {"price_pence": 110, "price_type": "unit"}
        assert mock_get.call_count == 1

    @patch("worker.sainsburys.time.sleep")
    @patch("worker.sainsburys.requests.get")
    def test_ean_match_on_page_2_returns_price(self, mock_get, mock_sleep):
        mock_get.side_effect = [make_response([_NON_MATCH]), make_response([_MATCH])]
        result = self._call()
        assert result == {"price_pence": 110, "price_type": "unit"}
        assert mock_get.call_count == 2
        assert mock_sleep.call_count == 1

    @patch("worker.sainsburys.time.sleep")
    @patch("worker.sainsburys.requests.get")
    def test_ean_match_on_page_3_returns_price(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            make_response([_NON_MATCH]),
            make_response([_NON_MATCH]),
            make_response([_MATCH]),
        ]
        result = self._call()
        assert result == {"price_pence": 110, "price_type": "unit"}
        assert mock_get.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("worker.sainsburys.time.sleep")
    @patch("worker.sainsburys.requests.get")
    def test_no_match_after_max_pages_returns_none(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            make_response([_NON_MATCH]),
            make_response([_NON_MATCH]),
            make_response([_NON_MATCH]),
        ]
        assert self._call() is None
        assert mock_get.call_count == 3

    @patch("worker.sainsburys.time.sleep")
    @patch("worker.sainsburys.requests.get")
    def test_empty_products_list_continues_to_next_page(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            make_response([]),
            make_response([]),
            make_response([_MATCH]),
        ]
        result = self._call()
        assert result == {"price_pence": 110, "price_type": "unit"}
        assert mock_get.call_count == 3

    @patch("worker.sainsburys.time.sleep")
    @patch("worker.sainsburys.requests.get")
    def test_http_4xx_returns_none(self, mock_get, mock_sleep):
        mock_get.return_value = make_error_response(404)
        assert self._call() is None

    @patch("worker.sainsburys.time.sleep")
    @patch("worker.sainsburys.requests.get")
    def test_http_5xx_returns_none(self, mock_get, mock_sleep):
        mock_get.return_value = make_error_response(500)
        assert self._call() is None

    @patch("worker.sainsburys.time.sleep")
    @patch("worker.sainsburys.requests.get")
    def test_missing_products_key_returns_none(self, mock_get, mock_sleep):
        mock = MagicMock()
        mock.status_code = 200
        mock.json.return_value = {}
        mock_get.return_value = mock
        assert self._call() is None

    @patch("worker.sainsburys.time.sleep")
    @patch("worker.sainsburys.requests.get")
    def test_product_missing_eans_is_skipped(self, mock_get, mock_sleep):
        no_eans_product = {
            "product_uid": "x",
            "name": "y",
            "retail_price": {"price": 0.5, "measure": "unit"},
        }
        mock_get.side_effect = [make_response([no_eans_product, _MATCH])]
        result = self._call()
        assert result == {"price_pence": 110, "price_type": "unit"}

    @patch("worker.sainsburys.time.sleep")
    @patch("worker.sainsburys.requests.get")
    def test_per_kg_product_returns_per_kg_price(self, mock_get, mock_sleep):
        per_kg = make_product(
            eans=[BARCODE],
            retail_price={"price": 2.75, "measure": "kg"},
            unit_price={"price": 2.75, "measure": "kg", "measure_amount": 1},
        )
        mock_get.side_effect = [make_response([per_kg])]
        assert self._call() == {"price_pence": 275, "price_type": "per_kg"}

    @patch("worker.sainsburys.time.sleep")
    @patch("worker.sainsburys.requests.get")
    def test_correct_query_params_sent(self, mock_get, mock_sleep):
        mock_get.side_effect = [make_response([_MATCH])]
        self._call()
        _, kwargs = mock_get.call_args
        params = kwargs["params"]
        assert params["filter[keyword]"] == "Sainsbury's Red Kidney Beans in Chilli Sauce 400g"
        assert params["page_number"] == 1
        assert params["page_size"] == 10
        assert params["sort_order"] == "FAVOURITES_FIRST"

    @patch("worker.sainsburys.time.sleep")
    @patch("worker.sainsburys.requests.get")
    def test_correct_headers_sent(self, mock_get, mock_sleep):
        mock_get.side_effect = [make_response([_MATCH])]
        self._call()
        _, kwargs = mock_get.call_args
        assert "FFInventory" not in kwargs["headers"]["User-Agent"]

    @patch("worker.sainsburys.time.sleep")
    @patch("worker.sainsburys.requests.get")
    def test_sleep_not_called_after_final_page(self, mock_get, mock_sleep):
        mock_get.side_effect = [
            make_response([_NON_MATCH]),
            make_response([_NON_MATCH]),
            make_response([_MATCH]),
        ]
        self._call()
        assert mock_sleep.call_count == 2

    @patch("worker.sainsburys.time.sleep")
    @patch("worker.sainsburys.requests.get")
    def test_zero_padded_barcode_matches(self, mock_get, mock_sleep):
        product_with_short_ean = make_product(
            eans=["171915"],
            retail_price={"price": 1.10, "measure": "unit"},
        )
        mock_get.side_effect = [make_response([product_with_short_ean])]
        result = get_price("0000000171915", NAME, BRAND, WEIGHT)
        assert result is not None

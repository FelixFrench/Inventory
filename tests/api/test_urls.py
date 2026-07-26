"""Tests for src/api/urls.py — off_url() and internal page-link construction."""

from src.api.urls import group_page_url, off_url, product_page_url


# ---------------------------------------------------------------------------
# off_url() — session context (info_status=) and report context (name=)
# ---------------------------------------------------------------------------

def test_off_url_view_when_info_status_resolved():
    url = off_url("5014788110140", info_status="resolved")
    assert "world.openfoodfacts.org/product/5014788110140" in url


def test_off_url_add_edit_when_info_status_failed():
    url = off_url("5014788110140", info_status="failed")
    assert "cgi/product.pl" in url
    assert "5014788110140" in url


def test_off_url_add_edit_when_name_none():
    url = off_url("5014788110140", name=None)
    assert "cgi/product.pl" in url


def test_off_url_view_when_name_present():
    url = off_url("5014788110140", name="Baked Beans")
    assert "world.openfoodfacts.org/product/5014788110140" in url


# ---------------------------------------------------------------------------
# product_page_url() / group_page_url() — internal page links
# ---------------------------------------------------------------------------

def test_product_page_url():
    assert (
        product_page_url("5014788110140", 1)
        == "/product.html?barcode=5014788110140&retailer_id=1"
    )


def test_group_page_url():
    assert group_page_url(7) == "/group.html?id=7"

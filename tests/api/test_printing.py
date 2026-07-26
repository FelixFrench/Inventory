"""Tests for src/api/printer.py — ESC/POS formatting and print endpoint."""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from src.api.dependencies import get_db, verify_api_key
from src.api.main import app
from src.api.printer import PrinterUnavailableError, _format_inventory, _format_low_stock, _get_printer

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE barcodes (barcode TEXT PRIMARY KEY);
CREATE TABLE product_variants (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, name TEXT, brand TEXT, minimum_quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit', product_url TEXT NULL, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE sessions (id INTEGER PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('in', 'out')), started_at TEXT NOT NULL, recovered_at TEXT);
CREATE TABLE session_items (session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL, delta INTEGER NOT NULL CHECK(delta >= 0), info_status TEXT NOT NULL DEFAULT 'pending' CHECK(info_status IN ('pending', 'resolved', 'failed')), price_status TEXT NOT NULL DEFAULT 'pending' CHECK(price_status IN ('pending', 'resolved', 'failed', 'not_possible')), first_scanned_at TEXT NOT NULL, PRIMARY KEY (session_id, barcode, retailer_id));
CREATE TABLE worker_state (id INTEGER PRIMARY KEY CHECK(id = 1), off_last_called_at TEXT NOT NULL);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE product_groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, minimum_quantity INTEGER NOT NULL DEFAULT 0 CHECK(minimum_quantity >= 0));
CREATE TABLE group_variant_members (group_id INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE, barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, PRIMARY KEY (group_id, barcode, retailer_id), FOREIGN KEY (barcode, retailer_id) REFERENCES product_variants(barcode, retailer_id) ON DELETE CASCADE);
CREATE TABLE group_group_members (parent_group_id INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE, child_group_id INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE, PRIMARY KEY (parent_group_id, child_group_id), CHECK (parent_group_id != child_group_id));
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
INSERT INTO worker_state (id, off_last_called_at) VALUES (1, '1970-01-01T00:00:00');
"""

_RETAILER_ID = 1


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "test.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


@pytest.fixture
def client(db):
    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_api_key] = lambda: None
    app.state.sainsburys_retailer_id = _RETAILER_ID
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def client_no_auth(db):
    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    app.state.sainsburys_retailer_id = _RETAILER_ID
    with patch("src.api.dependencies._API_KEY", "test-secret"):
        yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Formatter fixtures
# ---------------------------------------------------------------------------

INVENTORY_TWO_ITEMS = {
    "items": [
        {"name": "Baked Beans", "brand": "Heinz", "quantity": 3, "price_pence": 123, "line_total_pence": 369},
        {"name": "Kidney Beans", "brand": None, "quantity": 2, "price_pence": None, "line_total_pence": None},
    ],
    "total_value_pence": 369,
}

INVENTORY_ALL_PRICED = {
    "items": [
        {"name": "Apple", "brand": None, "quantity": 1, "price_pence": 123, "line_total_pence": 123},
        {"name": "Banana", "brand": None, "quantity": 1, "price_pence": 123, "line_total_pence": 123},
    ],
    "total_value_pence": 246,
}

INVENTORY_NO_PRICES = {
    "items": [
        {"name": "Apple", "brand": None, "quantity": 1, "price_pence": None, "line_total_pence": None},
    ],
    "total_value_pence": 0,
}

LOW_STOCK_REPORT = {
    "groups": [
        {"group_id": 1, "name": "Bean Collection", "have": 2, "need": 5, "short": 3},
    ],
    "products": [
        {"barcode": "1", "name": "Baked Beans", "brand": "Heinz", "have": 3, "need": 5, "short": 2},
        {"barcode": "2", "name": "Kidney Beans", "brand": None, "have": 0, "need": 3, "short": 3},
    ],
}

LOW_STOCK_EMPTY = {"groups": [], "products": []}


# ---------------------------------------------------------------------------
# Formatting tests (tests 1–5)
# ---------------------------------------------------------------------------

def test_format_inventory_names_and_prices():
    raw = _format_inventory(INVENTORY_TWO_ITEMS)
    assert b"Baked Beans" in raw
    assert b"Heinz" in raw
    assert b"1.23" in raw
    assert b"?" in raw  # em dash substituted as ? by CP437 Dummy for no-price item


def test_format_inventory_total_value():
    raw = _format_inventory(INVENTORY_ALL_PRICED)
    assert b"2.46" in raw


def test_format_inventory_no_prices():
    raw = _format_inventory(INVENTORY_NO_PRICES)
    assert b"No prices available" in raw


def test_format_low_stock_names_and_quantities():
    raw = _format_low_stock(LOW_STOCK_REPORT)
    assert b"Bean Collection" in raw
    assert b"Baked Beans" in raw
    assert b"Kidney Beans" in raw
    assert b"3" in raw
    assert b"5" in raw


def test_format_low_stock_sections_and_no_footer():
    """Groups section precedes Products section; the old 'n items below minimum' footer is gone."""
    raw = _format_low_stock(LOW_STOCK_REPORT)
    assert b"GROUPS" in raw
    assert b"PRODUCTS" in raw
    assert raw.index(b"GROUPS") < raw.index(b"PRODUCTS")
    # The removed footer read "<n> item(s) below minimum" — assert that phrasing is gone.
    assert b"items below minimum" not in raw
    assert b"item below minimum" not in raw


def test_format_low_stock_empty():
    raw = _format_low_stock(LOW_STOCK_EMPTY)
    assert b"No groups below minimum" in raw
    assert b"No products below minimum" in raw


# ---------------------------------------------------------------------------
# Endpoint tests (tests 6–9, 13)
# ---------------------------------------------------------------------------

def test_post_print_inventory_200(client):
    with patch("src.api.printer.print_inventory", MagicMock(return_value=None)):
        res = client.post("/print/inventory")
    assert res.status_code == 200
    assert res.json() == {"printed": True}


def test_post_print_inventory_503_unavailable(client):
    err = PrinterUnavailableError("tcp fail", code="printer_unavailable")
    with patch("src.api.printer.print_inventory", side_effect=err):
        res = client.post("/print/inventory")
    assert res.status_code == 503
    assert res.json() == {"error": "printer_unavailable"}


def test_post_print_low_stock_200(client):
    with patch("src.api.printer.print_low_stock", MagicMock(return_value=None)):
        res = client.post("/print/low-stock")
    assert res.status_code == 200
    assert res.json() == {"printed": True}


def test_post_print_low_stock_503_unavailable(client):
    err = PrinterUnavailableError("tcp fail", code="printer_unavailable")
    with patch("src.api.printer.print_low_stock", side_effect=err):
        res = client.post("/print/low-stock")
    assert res.status_code == 503
    assert res.json() == {"error": "printer_unavailable"}


def test_post_print_inventory_503_not_configured(client):
    err = PrinterUnavailableError("not set", code="printer_not_configured")
    with patch("src.api.printer.print_inventory", side_effect=err):
        res = client.post("/print/inventory")
    assert res.status_code == 503
    assert res.json() == {"error": "printer_not_configured"}


# ---------------------------------------------------------------------------
# _get_printer raises correct error when PRINTER_IP unset (test 10)
# ---------------------------------------------------------------------------

def test_get_printer_raises_when_ip_unset():
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(PrinterUnavailableError) as exc_info:
            _get_printer()
    assert exc_info.value.code == "printer_not_configured"


# ---------------------------------------------------------------------------
# Auth tests (tests 11–12)
# ---------------------------------------------------------------------------

def test_post_print_inventory_401_no_auth(client_no_auth):
    res = client_no_auth.post("/print/inventory")
    assert res.status_code == 401


def test_post_print_low_stock_401_no_auth(client_no_auth):
    res = client_no_auth.post("/print/low-stock")
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# Untrusted text reaching the printer (4c audit, findings 4c-01 / 4c-02 / 4c-03)
# ---------------------------------------------------------------------------
# Group names are user-supplied free text and product names are OpenFoodFacts-sourced;
# both are written straight into an ESC/POS byte stream built with
# magic_encode_args={"disabled": True}, which does no escaping of its own. There are
# exactly two places where non-literal text enters that stream — the `ident` assignments
# in _format_inventory and _low_stock_section — and every other p.text() argument is a
# module literal, a divider, a datetime or an integer.

# ESC/POS sequences with real physical effects on a TM-T88IV.
_DRAWER_KICK = b"\x1bp"      # ESC p - fire the cash-drawer solenoid
_PAPER_CUT = b"\x1dVA"       # GS V A - cut the paper
_PRINTER_RESET = b"\x1b@"    # ESC @ - reset the printer to defaults
_HOSTILE = "Beans\x1bp\x00\x19\x19 CUT:\x1dVA\x00 RESET:\x1b@"


def _assert_no_escpos_injection(hostile: bytes, benign: bytes, where: str):
    """Assert a hostile name adds no ESC/POS control bytes over an equivalent benign name.

    Absolute byte assertions do not work here: the formatter legitimately emits ESC and GS
    sequences of its own (bold on/off, alignment, feed, and the real p.cut(), whose
    parameter bytes include NUL). The airtight check is differential — a sanitised hostile
    name must not raise the ESC/GS count above the benign baseline — plus absence of the
    specific sequences the formatter provably never emits.
    """
    for label, seq in (
        ("ESC p drawer kick", _DRAWER_KICK),
        ("GS V A paper cut", _PAPER_CUT),
        ("ESC @ printer reset", _PRINTER_RESET),
    ):
        assert seq not in benign, f"baseline invalidated: formatter itself emits {label}"
        assert seq not in hostile, f"{label} from {where} reached the printer stream"

    for label, byte in (("ESC", 0x1B), ("GS", 0x1D)):
        assert hostile.count(byte) <= benign.count(byte), (
            f"{where} raised the {label} byte count from "
            f"{benign.count(byte)} to {hostile.count(byte)}"
        )


def test_low_stock_group_name_cannot_inject_escpos_commands():
    """A hostile GROUP name must not reach the printer as ESC/POS control bytes.

    This is the path that bypasses _identifier entirely: the groups section's ident_of is
    `lambda it: it["name"]`, so sanitising inside _identifier would leave it open.
    """
    def render(name):
        return _format_low_stock({
            "groups": [{"name": name, "have": 0, "need": 5, "short": 5}],
            "products": [],
        })

    _assert_no_escpos_injection(render(_HOSTILE), render("Beans"), "a group name")


def test_low_stock_product_name_cannot_inject_escpos_commands():
    """A hostile OFF-sourced PRODUCT name must not inject, via the low-stock section."""
    def render(name):
        return _format_low_stock({
            "groups": [],
            "products": [{"name": name, "brand": None, "have": 0, "need": 5, "short": 5}],
        })

    _assert_no_escpos_injection(render(_HOSTILE), render("Beans"), "a low-stock product name")


def test_inventory_product_name_cannot_inject_escpos_commands():
    """A hostile OFF-sourced PRODUCT name must not inject, via the inventory report."""
    def render(name, brand):
        return _format_inventory({
            "items": [{"name": name, "brand": brand, "quantity": 1, "price_pence": 100}],
            "total_value_pence": 100,
        })

    _assert_no_escpos_injection(
        render(_HOSTILE, _HOSTILE), render("Beans", "Heinz"),
        "an inventory product name or brand",
    )


def test_inventory_whitespace_only_name_does_not_raise():
    """A whitespace-only name must not crash the inventory receipt.

    textwrap.wrap("   ") returns [], so an unguarded lines[0] raises IndexError and the
    print endpoint 500s. _low_stock_section already guards this with `or [""]`.
    """
    out = _format_inventory({
        "items": [{"name": "   ", "brand": None, "quantity": 1, "price_pence": 100}],
        "total_value_pence": 100,
    })
    assert isinstance(out, bytes) and len(out) > 0


def test_low_stock_whitespace_only_group_name_does_not_raise():
    out = _format_low_stock({
        "groups": [{"name": "   ", "have": 0, "need": 1, "short": 1}],
        "products": [],
    })
    assert isinstance(out, bytes) and len(out) > 0


def test_print_inventory_endpoint_survives_whitespace_only_name(client, db):
    """End-to-end: the endpoint must not 500 on a whitespace-only OFF name."""
    db.execute("INSERT INTO barcodes (barcode) VALUES ('12345678')")
    db.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name, brand) "
        "VALUES ('12345678', ?, '   ', NULL)", (_RETAILER_ID,)
    )
    db.execute(
        "INSERT INTO inventory (barcode, retailer_id, quantity) VALUES ('12345678', ?, 2)",
        (_RETAILER_ID,)
    )
    db.commit()
    with patch("src.api.printer._get_printer", return_value=MagicMock()):
        res = client.post("/print/inventory")
    assert res.status_code == 200, res.text
    assert res.json() == {"printed": True}


def test_over_long_group_name_is_bounded_on_the_receipt():
    """An over-long name must not consume the paper roll.

    The schema cap (max_length=64 on the group-name request models) only constrains new
    creates and renames, so a legacy row can still carry an arbitrarily long name. The
    render bound is what covers those.
    """
    long_out = _format_low_stock({
        "groups": [{"name": "A" * 5000, "have": 0, "need": 1, "short": 1}],
        "products": [],
    })
    short_out = _format_low_stock({
        "groups": [{"name": "A" * 40, "have": 0, "need": 1, "short": 1}],
        "products": [],
    })
    long_lines = long_out.count(b"\n"[0])
    short_lines = short_out.count(b"\n"[0])
    # 5000 chars wrapped at the 21-column name width is 259 lines unbounded; the 120-char
    # display bound holds it to a handful more than the short case.
    assert long_lines <= short_lines + 6, (
        f"5000-char name produced {long_lines} lines vs {short_lines} for a short name"
    )


def test_normal_length_name_is_not_truncated():
    """The display bound must not bite a realistic name."""
    name = "Sainsbury's Organic Free Range Large Eggs, Box of Six"
    out = _format_low_stock({
        "groups": [{"name": name, "have": 0, "need": 1, "short": 1}],
        "products": [],
    })
    # Every word of the name survives (it is wrapped, so match word by word).
    for word in name.replace(",", "").split():
        assert word.encode("cp437") in out, f"{word!r} was lost from the receipt"

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from src.worker.main import compute_startup_sleep, process_row

_SCHEMA_PATH = Path(__file__).parents[2] / "src" / "db" / "schema.sql"

_BARCODE = "5014788110140"
_RETAILER_ID = 1
_QUEUED_AT = "2024-01-01 10:00:00"
_SCAN_AFTER = "2024-01-01 10:01:00"

_GOOD_OFF = {"name": "Red Kidney Beans", "brand": "Sainsbury's", "weight_g": 400.0}
_GOOD_PRICE = {"price_pence": 85, "price_type": "unit"}


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema)
    yield conn
    conn.close()


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO barcodes (barcode, retailer_id, product_variant_id) VALUES (?, ?, NULL)",
        (_BARCODE, _RETAILER_ID),
    )
    conn.execute(
        "INSERT INTO pending_lookups (barcode, retailer_id, queued_at, status) VALUES (?, ?, ?, 'pending')",
        (_BARCODE, _RETAILER_ID, _QUEUED_AT),
    )
    conn.commit()


def _add_scan(conn: sqlite3.Connection, direction: str, ts: str = _SCAN_AFTER) -> None:
    conn.execute(
        "INSERT INTO scan_events (barcode, retailer_id, direction, timestamp) VALUES (?, ?, ?, ?)",
        (_BARCODE, _RETAILER_ID, direction, ts),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Full success path
# ---------------------------------------------------------------------------

def test_full_success_path(db):
    _seed(db)
    with patch("src.worker.main.off.lookup_barcode", return_value=_GOOD_OFF), \
         patch("src.worker.main.sainsburys.get_price", return_value=_GOOD_PRICE):
        process_row(_BARCODE, _RETAILER_ID, _QUEUED_AT, db=db)

    pv = db.execute("SELECT * FROM product_variants").fetchone()
    assert pv is not None
    assert pv["name"] == "Red Kidney Beans"
    assert pv["brand"] == "Sainsbury's"
    assert pv["weight_g"] == 400.0

    bc = db.execute("SELECT product_variant_id FROM barcodes WHERE barcode=?", (_BARCODE,)).fetchone()
    assert bc["product_variant_id"] is not None

    inv = db.execute("SELECT quantity FROM inventory WHERE product_variant_id=?", (pv["id"],)).fetchone()
    assert inv["quantity"] == 0

    pr = db.execute("SELECT price_pence, price_type FROM prices WHERE product_variant_id=?", (pv["id"],)).fetchone()
    assert pr["price_pence"] == 85
    assert pr["price_type"] == "unit"

    pl = db.execute("SELECT status FROM pending_lookups WHERE barcode=?", (_BARCODE,)).fetchone()
    assert pl["status"] == "done"


# ---------------------------------------------------------------------------
# Scan delta tests
# ---------------------------------------------------------------------------

def test_scan_delta_applied_to_inventory(db):
    _seed(db)
    for _ in range(3):
        _add_scan(db, "in")
    _add_scan(db, "out")

    with patch("src.worker.main.off.lookup_barcode", return_value=_GOOD_OFF), \
         patch("src.worker.main.sainsburys.get_price", return_value=_GOOD_PRICE):
        process_row(_BARCODE, _RETAILER_ID, _QUEUED_AT, db=db)

    pv = db.execute("SELECT id FROM product_variants").fetchone()
    inv = db.execute("SELECT quantity FROM inventory WHERE product_variant_id=?", (pv["id"],)).fetchone()
    assert inv["quantity"] == 2


def test_scan_delta_floored_at_zero(db):
    _seed(db)
    _add_scan(db, "out")

    with patch("src.worker.main.off.lookup_barcode", return_value=_GOOD_OFF), \
         patch("src.worker.main.sainsburys.get_price", return_value=_GOOD_PRICE):
        process_row(_BARCODE, _RETAILER_ID, _QUEUED_AT, db=db)

    pv = db.execute("SELECT id FROM product_variants").fetchone()
    inv = db.execute("SELECT quantity FROM inventory WHERE product_variant_id=?", (pv["id"],)).fetchone()
    assert inv["quantity"] == 0


# ---------------------------------------------------------------------------
# OFF failure paths
# ---------------------------------------------------------------------------

def test_off_not_found_marks_failed(db):
    _seed(db)
    with patch("src.worker.main.off.lookup_barcode", return_value=None), \
         patch("src.worker.main.sainsburys.get_price", return_value=_GOOD_PRICE):
        process_row(_BARCODE, _RETAILER_ID, _QUEUED_AT, db=db)

    assert db.execute("SELECT COUNT(*) FROM product_variants").fetchone()[0] == 0
    pl = db.execute("SELECT status FROM pending_lookups WHERE barcode=?", (_BARCODE,)).fetchone()
    assert pl["status"] == "failed"


def test_off_no_name_marks_failed(db):
    _seed(db)
    off_result = {"name": None, "brand": "Sainsbury's", "weight_g": 400.0}
    with patch("src.worker.main.off.lookup_barcode", return_value=off_result), \
         patch("src.worker.main.sainsburys.get_price", return_value=_GOOD_PRICE):
        process_row(_BARCODE, _RETAILER_ID, _QUEUED_AT, db=db)

    assert db.execute("SELECT COUNT(*) FROM product_variants").fetchone()[0] == 0
    pl = db.execute("SELECT status FROM pending_lookups WHERE barcode=?", (_BARCODE,)).fetchone()
    assert pl["status"] == "failed"


def test_off_network_error_marks_failed(db):
    _seed(db)
    with patch("src.worker.main.off.lookup_barcode", side_effect=requests.RequestException("timeout")), \
         patch("src.worker.main.sainsburys.get_price", return_value=_GOOD_PRICE):
        process_row(_BARCODE, _RETAILER_ID, _QUEUED_AT, db=db)

    pl = db.execute("SELECT status FROM pending_lookups WHERE barcode=?", (_BARCODE,)).fetchone()
    assert pl["status"] == "failed"


# ---------------------------------------------------------------------------
# Sainsbury's failure paths
# ---------------------------------------------------------------------------

def test_sainsburys_no_price_still_marks_done(db):
    _seed(db)
    with patch("src.worker.main.off.lookup_barcode", return_value=_GOOD_OFF), \
         patch("src.worker.main.sainsburys.get_price", return_value=None):
        process_row(_BARCODE, _RETAILER_ID, _QUEUED_AT, db=db)

    pl = db.execute("SELECT status FROM pending_lookups WHERE barcode=?", (_BARCODE,)).fetchone()
    assert pl["status"] == "done"

    pv = db.execute("SELECT id FROM product_variants").fetchone()
    pr = db.execute("SELECT price_pence FROM prices WHERE product_variant_id=?", (pv["id"],)).fetchone()
    assert pr is not None
    assert pr["price_pence"] is None


def test_sainsburys_exception_still_marks_done(db):
    _seed(db)
    with patch("src.worker.main.off.lookup_barcode", return_value=_GOOD_OFF), \
         patch("src.worker.main.sainsburys.get_price", side_effect=Exception("scrape failed")):
        process_row(_BARCODE, _RETAILER_ID, _QUEUED_AT, db=db)

    pl = db.execute("SELECT status FROM pending_lookups WHERE barcode=?", (_BARCODE,)).fetchone()
    assert pl["status"] == "done"

    pv = db.execute("SELECT id FROM product_variants").fetchone()
    pr = db.execute("SELECT price_pence FROM prices WHERE product_variant_id=?", (pv["id"],)).fetchone()
    assert pr is not None
    assert pr["price_pence"] is None


# ---------------------------------------------------------------------------
# Startup recovery sleep
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# rowcount / integrity checks
# ---------------------------------------------------------------------------

def test_missing_barcodes_row_marks_failed_and_rolls_back(db):
    """UPDATE barcodes matching 0 rows should roll back the whole transaction
    (no orphaned product_variant) and mark pending_lookups as failed."""
    db.execute(
        "INSERT INTO pending_lookups (barcode, retailer_id, queued_at, status) VALUES (?, ?, ?, 'pending')",
        (_BARCODE, _RETAILER_ID, _QUEUED_AT),
    )
    db.commit()

    with patch("src.worker.main.off.lookup_barcode", return_value=_GOOD_OFF), \
         patch("src.worker.main.sainsburys.get_price", return_value=_GOOD_PRICE):
        process_row(_BARCODE, _RETAILER_ID, _QUEUED_AT, db=db)

    # Transaction rolled back — product_variant INSERT must have been undone
    assert db.execute("SELECT COUNT(*) FROM product_variants").fetchone()[0] == 0

    # pending_lookups should be marked failed by the error handler
    pl = db.execute("SELECT status FROM pending_lookups WHERE barcode=?", (_BARCODE,)).fetchone()
    assert pl["status"] == "failed"


# ---------------------------------------------------------------------------
# Startup recovery sleep
# ---------------------------------------------------------------------------

def test_restart_recovery_sleep():
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)

    assert compute_startup_sleep(None, now) == 0.0

    one_second_ago = now - timedelta(seconds=1)
    result = compute_startup_sleep(one_second_ago, now)
    assert abs(result - 3.0) < 0.01

    five_seconds_ago = now - timedelta(seconds=5)
    assert compute_startup_sleep(five_seconds_ago, now) == 0.0

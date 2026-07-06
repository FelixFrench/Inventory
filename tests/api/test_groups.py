"""Resolver + cycle-prevention tests for src/api/groups.py (Sprint 2, Phase 2a).

Runs over a real in-memory SQLite connection (schema applied, PRAGMA foreign_keys = ON,
sqlite3.Row factory) — the resolver is real SQL, so exercise it against real rows, never
mocks.
"""
import sqlite3

import pytest

from src.api.groups import (
    GroupResolution,
    is_low_stock,
    resolve_all_groups,
    resolve_group,
    shortfall,
    would_create_cycle,
)

_RID = 1  # retailer id (Sainsbury's seed)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE barcodes (barcode TEXT PRIMARY KEY);
CREATE TABLE product_variants (barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL REFERENCES retailers(id), name TEXT, brand TEXT, product_quantity TEXT, minimum_quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL REFERENCES retailers(id), quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE product_groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, minimum_quantity INTEGER NOT NULL DEFAULT 0 CHECK(minimum_quantity >= 0));
CREATE TABLE group_variant_members (group_id INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE, barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, PRIMARY KEY (group_id, barcode, retailer_id), FOREIGN KEY (barcode, retailer_id) REFERENCES product_variants(barcode, retailer_id) ON DELETE CASCADE);
CREATE TABLE group_group_members (parent_group_id INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE, child_group_id INTEGER NOT NULL REFERENCES product_groups(id) ON DELETE CASCADE, PRIMARY KEY (parent_group_id, child_group_id), CHECK (parent_group_id != child_group_id));
INSERT INTO retailers (name, scraper_class) VALUES ('Sainsbury''s', 'SainsburysProvider');
"""


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


# --- seed helpers ----------------------------------------------------------------------

def _variant(conn, barcode, quantity=None):
    """A variant with an optional inventory row (None = no inventory row at all)."""
    conn.execute("INSERT OR IGNORE INTO barcodes (barcode) VALUES (?)", (barcode,))
    conn.execute(
        "INSERT INTO product_variants (barcode, retailer_id, name) VALUES (?, ?, ?)",
        (barcode, _RID, f"V{barcode}"),
    )
    if quantity is not None:
        conn.execute(
            "INSERT INTO inventory (barcode, retailer_id, quantity) VALUES (?, ?, ?)",
            (barcode, _RID, quantity),
        )


def _group(conn, gid, name=None, minimum=0):
    conn.execute(
        "INSERT INTO product_groups (id, name, minimum_quantity) VALUES (?, ?, ?)",
        (gid, name or f"grp{gid}", minimum),
    )


def _add_variant(conn, gid, barcode):
    conn.execute(
        "INSERT INTO group_variant_members (group_id, barcode, retailer_id) VALUES (?, ?, ?)",
        (gid, barcode, _RID),
    )


def _add_child(conn, parent, child):
    conn.execute(
        "INSERT INTO group_group_members (parent_group_id, child_group_id) VALUES (?, ?)",
        (parent, child),
    )


# --- shared low-stock predicate --------------------------------------------------------

def test_is_low_stock_and_shortfall():
    assert is_low_stock(5, 10) is True
    assert is_low_stock(10, 10) is False   # at minimum is not low
    assert is_low_stock(11, 10) is False
    assert is_low_stock(0, 0) is False     # minimum 0 = no minimum, never low
    assert is_low_stock(0, 3) is True
    assert shortfall(5, 10) == 5
    assert shortfall(10, 5) == 0           # clamped at 0
    assert shortfall(3, 3) == 0
    assert shortfall(7, 0) == 0            # no minimum


# --- resolver --------------------------------------------------------------------------

def test_resolver_single_group(db):
    _variant(db, "b1", 4)
    _variant(db, "b2", 3)
    _group(db, 1, minimum=10)
    _add_variant(db, 1, "b1")
    _add_variant(db, 1, "b2")
    db.commit()

    res = resolve_group(db, 1)
    assert isinstance(res, GroupResolution)
    assert res.total_quantity == 7
    assert sorted(res.member_variants) == [("b1", _RID), ("b2", _RID)]
    assert res.low_stock is True        # 7 < 10
    assert res.shortfall == 3


def test_resolver_nested_rollup(db):
    # Parent (g1) holds no variants directly; child (g2) holds them.
    _variant(db, "b1", 4)
    _variant(db, "b2", 6)
    _group(db, 1, minimum=0)
    _group(db, 2, minimum=0)
    _add_child(db, 1, 2)
    _add_variant(db, 2, "b1")
    _add_variant(db, 2, "b2")
    db.commit()

    res = resolve_group(db, 1)
    assert res is not None
    assert res.total_quantity == 10
    assert sorted(res.member_variants) == [("b1", _RID), ("b2", _RID)]


def test_resolver_diamond_dedup(db):
    # root -> a, root -> b, a -> c, b -> c; variant lives in c: counted once via both paths.
    _variant(db, "shared", 5)
    for gid in (1, 2, 3, 4):  # 1=root, 2=a, 3=b, 4=c
        _group(db, gid)
    _add_child(db, 1, 2)
    _add_child(db, 1, 3)
    _add_child(db, 2, 4)
    _add_child(db, 3, 4)
    _add_variant(db, 4, "shared")
    db.commit()

    res = resolve_group(db, 1)
    assert res is not None
    assert res.member_variants == [("shared", _RID)]  # exactly once
    assert res.total_quantity == 5                     # not 10


def test_resolver_own_minimum(db):
    # Parent total 8 with its own minimum 5 (not low); child total 8 with minimum 20 (low).
    # The child's minimum must not influence the parent's evaluation.
    _variant(db, "b1", 8)
    _group(db, 1, minimum=5)    # parent
    _group(db, 2, minimum=20)   # child, high minimum
    _add_child(db, 1, 2)
    _add_variant(db, 2, "b1")
    db.commit()

    parent = resolve_group(db, 1)
    child = resolve_group(db, 2)
    assert parent is not None and child is not None
    assert parent.total_quantity == 8 and child.total_quantity == 8  # same rolled-up variant
    assert parent.low_stock is False   # 8 >= 5, child's min 20 is irrelevant
    assert child.low_stock is True     # 8 < 20


def test_resolver_missing_inventory_contributes_zero(db):
    _variant(db, "b1", 4)
    _variant(db, "b2", None)   # no inventory row
    _group(db, 1, minimum=0)
    _add_variant(db, 1, "b1")
    _add_variant(db, 1, "b2")
    db.commit()

    res = resolve_group(db, 1)
    assert res is not None
    assert res.total_quantity == 4     # b2 contributes 0, not an error
    assert sorted(res.member_variants) == [("b1", _RID), ("b2", _RID)]


def test_resolver_minimum_zero_never_low(db):
    _variant(db, "b1", 0)
    _group(db, 1, minimum=0)
    _add_variant(db, 1, "b1")
    db.commit()

    res = resolve_group(db, 1)
    assert res is not None
    assert res.low_stock is False
    assert res.shortfall == 0


def test_resolver_unknown_group_returns_none(db):
    assert resolve_group(db, 999) is None   # does not raise


def test_resolve_all_groups(db):
    _variant(db, "b1", 2)
    _group(db, 1, minimum=0)
    _group(db, 2, minimum=5)
    _add_variant(db, 1, "b1")
    db.commit()

    results = resolve_all_groups(db)
    assert [r.group_id for r in results] == [1, 2]
    assert all(isinstance(r, GroupResolution) for r in results)
    assert results[0].total_quantity == 2
    assert results[1].total_quantity == 0


def test_resolver_terminates_on_injected_cycle(db):
    # Insert a cycle DIRECTLY, bypassing would_create_cycle. The UNION walk + path guard must
    # terminate rather than loop forever.
    _variant(db, "b1", 3)
    _group(db, 1)
    _group(db, 2)
    _add_child(db, 1, 2)
    _add_child(db, 2, 1)   # cycle: 1 -> 2 -> 1
    _add_variant(db, 2, "b1")
    db.commit()

    res = resolve_group(db, 1)   # returns (does not hang)
    assert res is not None
    assert res.total_quantity == 3
    assert res.member_variants == [("b1", _RID)]


# --- cycle prevention ------------------------------------------------------------------

def test_cycle_self_rejected(db):
    _group(db, 1)
    db.commit()
    assert would_create_cycle(db, 1, 1) is True


def test_cycle_self_edge_rejected_by_db_check(db):
    _group(db, 1)
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO group_group_members (parent_group_id, child_group_id) VALUES (1, 1)"
        )


def test_cycle_direct_detected(db):
    _group(db, 1)  # A
    _group(db, 2)  # B
    _add_child(db, 1, 2)  # A -> B
    db.commit()
    # Adding B -> A would close a 2-cycle.
    assert would_create_cycle(db, 2, 1) is True


def test_cycle_indirect_detected(db):
    _group(db, 1)  # A
    _group(db, 2)  # B
    _group(db, 3)  # C
    _add_child(db, 1, 2)  # A -> B
    _add_child(db, 2, 3)  # B -> C
    db.commit()
    # Adding C -> A would close a 3-cycle via reachability.
    assert would_create_cycle(db, 3, 1) is True


def test_cycle_valid_edge_allowed(db):
    _group(db, 1)  # A
    _group(db, 2)  # B
    _group(db, 3)  # C (separate)
    _add_child(db, 1, 2)  # A -> B
    db.commit()
    # A -> C closes no loop; B -> C closes no loop.
    assert would_create_cycle(db, 1, 3) is False
    assert would_create_cycle(db, 2, 3) is False

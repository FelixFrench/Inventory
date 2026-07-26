"""Resolver + cycle-prevention tests for src/api/groups.py and the
groups HTTP surface in src/api/routers/groups.py.

Runs over a real in-memory SQLite connection (schema applied, PRAGMA foreign_keys = ON,
sqlite3.Row factory) — the resolver and edge logic are real SQL, so exercise them against
real rows, never mocks.
"""
import sqlite3
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from src.api.dependencies import get_db, verify_api_key
from src.api.groups import (
    GroupResolution,
    is_low_stock,
    reachable_group_ids,
    resolve_all_groups,
    resolve_group,
    shortfall,
    would_create_cycle,
)
from src.api.main import app
from src.api.routers.groups import ensure_variant_row, run_membership

_RID = 1  # retailer id (Sainsbury's seed)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE retailers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, scraper_class TEXT NOT NULL);
CREATE TABLE barcodes (barcode TEXT PRIMARY KEY);
CREATE TABLE product_variants (barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL REFERENCES retailers(id), name TEXT, brand TEXT, product_quantity TEXT, minimum_quantity INTEGER NOT NULL DEFAULT 0, lookup_status TEXT NOT NULL DEFAULT 'pending' CHECK(lookup_status IN ('pending', 'resolved', 'failed')), lookup_failure_count INTEGER NOT NULL DEFAULT 0 CHECK(lookup_failure_count >= 0), last_lookup_datetime TEXT, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE inventory (barcode TEXT NOT NULL REFERENCES barcodes(barcode), retailer_id INTEGER NOT NULL REFERENCES retailers(id), quantity INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (barcode, retailer_id));
CREATE TABLE prices (barcode TEXT NOT NULL, retailer_id INTEGER NOT NULL, price_pence INTEGER, price_type TEXT NOT NULL DEFAULT 'unit', product_url TEXT NULL, PRIMARY KEY (barcode, retailer_id), FOREIGN KEY (barcode, retailer_id) REFERENCES product_variants(barcode, retailer_id));
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


# --- reachable_group_ids (the WITH RECURSIVE walk, exercised directly) ------------------
# resolve_group covers it indirectly; these pin the walk itself, so a termination or
# self-inclusion regression is attributed to the right function.

def test_reachable_includes_the_root_even_with_no_edges(db):
    _group(db, 1)
    db.commit()
    assert reachable_group_ids(db, 1) == {1}


def test_reachable_walks_nested_children(db):
    _group(db, 1)
    _group(db, 2)
    _group(db, 3)
    _add_child(db, 1, 2)
    _add_child(db, 2, 3)
    db.commit()
    assert reachable_group_ids(db, 1) == {1, 2, 3}
    # Direction matters: the walk is parent -> child only.
    assert reachable_group_ids(db, 3) == {3}


def test_reachable_deduplicates_a_diamond(db):
    # 1 -> 2, 1 -> 3, and both 2 and 3 -> 4. Node 4 is reachable by two paths.
    for gid in (1, 2, 3, 4):
        _group(db, gid)
    _add_child(db, 1, 2)
    _add_child(db, 1, 3)
    _add_child(db, 2, 4)
    _add_child(db, 3, 4)
    db.commit()
    assert reachable_group_ids(db, 1) == {1, 2, 3, 4}


def test_reachable_terminates_on_an_injected_cycle(db):
    # Cycles inserted directly, bypassing would_create_cycle: the path guard must stop the walk.
    _group(db, 1)
    _group(db, 2)
    _group(db, 3)
    _add_child(db, 1, 2)
    _add_child(db, 2, 3)
    _add_child(db, 3, 1)   # 1 -> 2 -> 3 -> 1
    db.commit()
    assert reachable_group_ids(db, 1) == {1, 2, 3}   # returns, does not hang
    assert reachable_group_ids(db, 2) == {1, 2, 3}


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


# =======================================================================================
# HTTP surface — group CRUD, membership (group side), read endpoints
# =======================================================================================


@pytest.fixture
def client(db):
    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_api_key] = lambda: None
    app.state.sainsburys_retailer_id = _RID
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
    app.state.sainsburys_retailer_id = _RID
    with patch("src.api.dependencies._API_KEY", "test-secret"):
        yield TestClient(app)
    app.dependency_overrides.clear()


# --- Group CRUD ------------------------------------------------------------------------

def test_create_group(client, db):
    resp = client.post("/groups", json={"name": "Beans", "minimum_quantity": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Beans"
    assert body["minimum_quantity"] == 5
    gid = body["id"]
    row = db.execute("SELECT name, minimum_quantity FROM product_groups WHERE id=?", (gid,)).fetchone()
    assert row["name"] == "Beans" and row["minimum_quantity"] == 5


def test_create_group_default_minimum(client):
    resp = client.post("/groups", json={"name": "Organisational"})
    assert resp.status_code == 200
    assert resp.json()["minimum_quantity"] == 0


def test_create_group_duplicate_name_409(client):
    client.post("/groups", json={"name": "Beans"})
    resp = client.post("/groups", json={"name": "Beans"})
    assert resp.status_code == 409
    assert resp.json() == {"error": "group_name_conflict"}


def test_rename_group(client, db):
    gid = client.post("/groups", json={"name": "Old"}).json()["id"]
    resp = client.patch(f"/groups/{gid}", json={"name": "New"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "New"
    assert db.execute("SELECT name FROM product_groups WHERE id=?", (gid,)).fetchone()["name"] == "New"


def test_rename_group_conflict_409(client):
    client.post("/groups", json={"name": "A"})
    gid_b = client.post("/groups", json={"name": "B"}).json()["id"]
    resp = client.patch(f"/groups/{gid_b}", json={"name": "A"})
    assert resp.status_code == 409
    assert resp.json() == {"error": "group_name_conflict"}


def test_update_group_unknown_404(client):
    resp = client.patch("/groups/999", json={"name": "X"})
    assert resp.status_code == 404
    assert resp.json() == {"error": "group_not_found"}


def test_set_minimum(client):
    gid = client.post("/groups", json={"name": "G", "minimum_quantity": 0}).json()["id"]
    resp = client.patch(f"/groups/{gid}", json={"minimum_quantity": 4})
    assert resp.status_code == 200
    assert resp.json()["minimum_quantity"] == 4


def test_clear_minimum_makes_never_low(client, db):
    """Setting minimum to 0 clears it: the group is organisational-only and never low."""
    _variant(db, "b1", 1)
    gid = client.post("/groups", json={"name": "G", "minimum_quantity": 5}).json()["id"]
    client.post(f"/groups/{gid}/variants", json={"barcode": "b1"})
    # Below minimum now:
    assert client.get(f"/groups/{gid}").json()["low_stock"] is True
    # Clear it:
    client.patch(f"/groups/{gid}", json={"minimum_quantity": 0})
    detail = client.get(f"/groups/{gid}").json()
    assert detail["minimum_quantity"] == 0
    assert detail["low_stock"] is False


def test_delete_group_edges_only(client, db):
    """Deleting a group removes only its edges (as parent and as child); its members,
    sub-groups, and any other group it belonged to survive."""
    _variant(db, "b1")
    # g_parent -> g_mid -> (variant b1); g_mid also a member of g_other.
    gp = client.post("/groups", json={"name": "parent"}).json()["id"]
    gm = client.post("/groups", json={"name": "mid"}).json()["id"]
    go = client.post("/groups", json={"name": "other"}).json()["id"]
    client.post(f"/groups/{gp}/subgroups", json={"child_group_id": gm})
    client.post(f"/groups/{go}/subgroups", json={"child_group_id": gm})
    client.post(f"/groups/{gm}/variants", json={"barcode": "b1"})

    resp = client.delete(f"/groups/{gm}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": gm}

    # gm gone; its former parent gp and go survive; variant b1 survives; the b1 edge (child of gm)
    # is gone with gm, but the variant row itself remains.
    assert db.execute("SELECT 1 FROM product_groups WHERE id=?", (gm,)).fetchone() is None
    assert db.execute("SELECT 1 FROM product_groups WHERE id=?", (gp,)).fetchone() is not None
    assert db.execute("SELECT 1 FROM product_groups WHERE id=?", (go,)).fetchone() is not None
    assert db.execute("SELECT 1 FROM product_variants WHERE barcode='b1'").fetchone() is not None
    # No dangling edges referencing gm:
    assert db.execute(
        "SELECT 1 FROM group_group_members WHERE parent_group_id=? OR child_group_id=?", (gm, gm)
    ).fetchone() is None
    assert db.execute("SELECT 1 FROM group_variant_members WHERE group_id=?", (gm,)).fetchone() is None


def test_delete_group_unknown_404(client):
    resp = client.delete("/groups/999")
    assert resp.status_code == 404
    assert resp.json() == {"error": "group_not_found"}


# --- Membership: variants --------------------------------------------------------------

def test_add_variant_upserts_null_row(client, db):
    """A barcode with a barcodes row but no product_variants row: add upserts a null-data row."""
    db.execute("INSERT INTO barcodes (barcode) VALUES ('b1')")
    db.commit()
    gid = client.post("/groups", json={"name": "G"}).json()["id"]
    resp = client.post(f"/groups/{gid}/variants", json={"barcode": "b1"})
    assert resp.status_code == 200
    pv = db.execute(
        "SELECT name, lookup_status FROM product_variants WHERE barcode='b1' AND retailer_id=?", (_RID,)
    ).fetchone()
    assert pv is not None and pv["name"] is None
    # The upsert (ON CONFLICT DO NOTHING) must not name the durable columns, leaving the default.
    assert pv["lookup_status"] == "pending"
    edge = db.execute(
        "SELECT 1 FROM group_variant_members WHERE group_id=? AND barcode='b1'", (gid,)
    ).fetchone()
    assert edge is not None


def test_add_variant_unknown_barcode_404(client):
    gid = client.post("/groups", json={"name": "G"}).json()["id"]
    resp = client.post(f"/groups/{gid}/variants", json={"barcode": "99999999"})
    assert resp.status_code == 404
    assert resp.json() == {"error": "barcode_not_found"}


def test_add_variant_unknown_group_404(client, db):
    db.execute("INSERT INTO barcodes (barcode) VALUES ('b1')")
    db.commit()
    resp = client.post("/groups/999/variants", json={"barcode": "b1"})
    assert resp.status_code == 404
    assert resp.json() == {"error": "group_not_found"}


def test_add_variant_idempotent(client, db):
    _variant(db, "b1")
    gid = client.post("/groups", json={"name": "G"}).json()["id"]
    assert client.post(f"/groups/{gid}/variants", json={"barcode": "b1"}).status_code == 200
    assert client.post(f"/groups/{gid}/variants", json={"barcode": "b1"}).status_code == 200
    count = db.execute(
        "SELECT COUNT(*) c FROM group_variant_members WHERE group_id=? AND barcode='b1'", (gid,)
    ).fetchone()["c"]
    assert count == 1


def test_remove_variant_edge_only(client, db):
    _variant(db, "b1")
    gid = client.post("/groups", json={"name": "G"}).json()["id"]
    client.post(f"/groups/{gid}/variants", json={"barcode": "b1"})
    resp = client.delete(f"/groups/{gid}/variants/b1")
    assert resp.status_code == 200
    assert db.execute("SELECT 1 FROM group_variant_members WHERE group_id=?", (gid,)).fetchone() is None
    assert db.execute("SELECT 1 FROM product_variants WHERE barcode='b1'").fetchone() is not None
    assert db.execute("SELECT 1 FROM product_groups WHERE id=?", (gid,)).fetchone() is not None


def test_remove_variant_unknown_group_404(client, db):
    _variant(db, "b1")
    resp = client.delete("/groups/999/variants/b1")
    assert resp.status_code == 404
    assert resp.json() == {"error": "group_not_found"}


def test_remove_variant_valid_nonmember_noop(client, db):
    _variant(db, "b1")
    gid = client.post("/groups", json={"name": "G"}).json()["id"]
    resp = client.delete(f"/groups/{gid}/variants/b1")
    assert resp.status_code == 200


def test_remove_variant_unknown_barcode_404(client, db):
    """The membership existence rule's remaining arm: only the EDGE is idempotent.

    A valid group + an unknown barcode is a client mistake (404), not a silent zero-row delete
    — the contrast with test_remove_variant_valid_nonmember_noop above is the whole point.
    """
    gid = client.post("/groups", json={"name": "G"}).json()["id"]
    resp = client.delete(f"/groups/{gid}/variants/99999999")
    assert resp.status_code == 404
    assert resp.json() == {"error": "barcode_not_found"}


# --- Membership: sub-groups ------------------------------------------------------------

def test_add_subgroup(client, db):
    a = client.post("/groups", json={"name": "A"}).json()["id"]
    b = client.post("/groups", json={"name": "B"}).json()["id"]
    resp = client.post(f"/groups/{a}/subgroups", json={"child_group_id": b})
    assert resp.status_code == 200
    assert db.execute(
        "SELECT 1 FROM group_group_members WHERE parent_group_id=? AND child_group_id=?", (a, b)
    ).fetchone() is not None


def test_add_subgroup_self_rejected(client, db):
    a = client.post("/groups", json={"name": "A"}).json()["id"]
    resp = client.post(f"/groups/{a}/subgroups", json={"child_group_id": a})
    assert resp.status_code == 409
    assert resp.json() == {"error": "cycle_detected"}
    # No partial write.
    assert db.execute("SELECT COUNT(*) c FROM group_group_members").fetchone()["c"] == 0


def test_add_subgroup_cycle_rejected(client, db):
    a = client.post("/groups", json={"name": "A"}).json()["id"]
    b = client.post("/groups", json={"name": "B"}).json()["id"]
    c = client.post("/groups", json={"name": "C"}).json()["id"]
    client.post(f"/groups/{a}/subgroups", json={"child_group_id": b})  # A -> B
    client.post(f"/groups/{b}/subgroups", json={"child_group_id": c})  # B -> C
    resp = client.post(f"/groups/{c}/subgroups", json={"child_group_id": a})  # C -> A closes loop
    assert resp.status_code == 409
    assert resp.json() == {"error": "cycle_detected"}
    assert db.execute(
        "SELECT 1 FROM group_group_members WHERE parent_group_id=? AND child_group_id=?", (c, a)
    ).fetchone() is None


def test_add_subgroup_unknown_group_404(client):
    a = client.post("/groups", json={"name": "A"}).json()["id"]
    resp = client.post(f"/groups/{a}/subgroups", json={"child_group_id": 999})
    assert resp.status_code == 404
    assert resp.json() == {"error": "group_not_found"}


def test_remove_subgroup(client, db):
    a = client.post("/groups", json={"name": "A"}).json()["id"]
    b = client.post("/groups", json={"name": "B"}).json()["id"]
    client.post(f"/groups/{a}/subgroups", json={"child_group_id": b})
    resp = client.delete(f"/groups/{a}/subgroups/{b}")
    assert resp.status_code == 200
    assert db.execute("SELECT 1 FROM group_group_members").fetchone() is None
    # Both groups survive.
    assert db.execute("SELECT 1 FROM product_groups WHERE id=?", (a,)).fetchone() is not None
    assert db.execute("SELECT 1 FROM product_groups WHERE id=?", (b,)).fetchone() is not None


def test_remove_subgroup_unknown_group_404(client):
    a = client.post("/groups", json={"name": "A"}).json()["id"]
    resp = client.delete(f"/groups/{a}/subgroups/999")
    assert resp.status_code == 404
    assert resp.json() == {"error": "group_not_found"}


def test_membership_both_directions(client, db):
    """Add from the group side; read the membership back from the product side (same edge table)."""
    _variant(db, "b1")
    gid = client.post("/groups", json={"name": "Beans"}).json()["id"]
    client.post(f"/groups/{gid}/variants", json={"barcode": "b1"})
    detail = client.get("/products/b1/1").json()
    assert {
        "id": gid,
        "name": "Beans",
        "group_page_url": f"/group.html?id={gid}",
    } in detail["groups"]


# --- Read endpoints --------------------------------------------------------------------

def test_list_groups(client, db):
    _variant(db, "b1", 2)
    gid = client.post("/groups", json={"name": "G", "minimum_quantity": 5}).json()["id"]
    client.post(f"/groups/{gid}/variants", json={"barcode": "b1"})
    resp = client.get("/groups")
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert len(groups) == 1
    g = groups[0]
    assert g["group_id"] == gid
    assert g["total_quantity"] == 2
    assert g["minimum_quantity"] == 5
    assert g["low_stock"] is True
    assert g["shortfall"] == 3
    assert g["group_page_url"] == f"/group.html?id={gid}"


def test_group_detail(client, db):
    _variant(db, "b1", 4)
    parent = client.post("/groups", json={"name": "Parent", "minimum_quantity": 10}).json()["id"]
    child = client.post("/groups", json={"name": "Child"}).json()["id"]
    client.post(f"/groups/{parent}/subgroups", json={"child_group_id": child})
    client.post(f"/groups/{parent}/variants", json={"barcode": "b1"})

    resp = client.get(f"/groups/{parent}")
    assert resp.status_code == 200
    d = resp.json()
    assert d["group_id"] == parent
    assert d["total_quantity"] == 4
    assert d["minimum_quantity"] == 10
    assert d["low_stock"] is True
    assert [v["barcode"] for v in d["variants"]] == ["b1"]
    variant = d["variants"][0]
    assert variant["current_quantity"] == 4
    assert variant["product_page_url"] == "/product.html?barcode=b1&retailer_id=1"
    assert d["subgroups"] == [
        {"id": child, "name": "Child", "group_page_url": f"/group.html?id={child}"}
    ]


def test_group_detail_unknown_404(client):
    resp = client.get("/groups/999")
    assert resp.status_code == 404
    assert resp.json() == {"error": "group_not_found"}


def test_group_detail_diamond_dedup(client, db):
    """resolve_group dedups a diamond-shaped nesting: shared variant counted once."""
    _variant(db, "shared", 5)
    root = client.post("/groups", json={"name": "root"}).json()["id"]
    a = client.post("/groups", json={"name": "a"}).json()["id"]
    b = client.post("/groups", json={"name": "b"}).json()["id"]
    leaf = client.post("/groups", json={"name": "leaf"}).json()["id"]
    client.post(f"/groups/{root}/subgroups", json={"child_group_id": a})
    client.post(f"/groups/{root}/subgroups", json={"child_group_id": b})
    client.post(f"/groups/{a}/subgroups", json={"child_group_id": leaf})
    client.post(f"/groups/{b}/subgroups", json={"child_group_id": leaf})
    client.post(f"/groups/{leaf}/variants", json={"barcode": "shared"})

    assert client.get(f"/groups/{root}").json()["total_quantity"] == 5  # not 10


def test_group_detail_null_variant_renders(client, db):
    """A null-data variant member (no name/brand) still renders in group detail."""
    db.execute("INSERT INTO barcodes (barcode) VALUES ('b1')")
    db.commit()
    gid = client.post("/groups", json={"name": "G"}).json()["id"]
    client.post(f"/groups/{gid}/variants", json={"barcode": "b1"})
    resp = client.get(f"/groups/{gid}")
    assert resp.status_code == 200
    v = resp.json()["variants"][0]
    assert v["barcode"] == "b1"
    assert v["name"] is None and v["brand"] is None


# --- run_membership: the shared transactional wrapper ----------------------------------
# The typed-error arms (404/404/409) are covered by the endpoint tests above. These pin the
# unexpected-failure arms, where an un-rolled-back raise would leave a phantom variant row
# behind and hold the writer lock open.

def _failing_action(db, exc):
    """An action that performs a REAL partial write, then fails."""
    def action():
        ensure_variant_row(db, "b1", _RID)
        raise exc
    return action


def test_run_membership_rolls_back_a_partial_write_on_unexpected_error(db):
    import json as _json

    db.execute("INSERT INTO barcodes (barcode) VALUES ('b1')")
    db.commit()

    resp = run_membership(db, _failing_action(db, RuntimeError("boom")))

    assert resp.status_code == 500
    assert _json.loads(resp.body) == {"error": "internal_error"}
    # The ensure_variant_row write is gone — rollback actually happened, not merely returned.
    assert db.execute("SELECT 1 FROM product_variants WHERE barcode = 'b1'").fetchone() is None
    # ...and the connection is immediately writable again (no lingering transaction/lock).
    db.execute("INSERT INTO barcodes (barcode) VALUES ('b2')")
    db.commit()
    assert db.execute("SELECT COUNT(*) c FROM barcodes").fetchone()["c"] == 2


def test_run_membership_rolls_back_and_raises_503_on_operational_error(db):
    from fastapi import HTTPException

    db.execute("INSERT INTO barcodes (barcode) VALUES ('b1')")
    db.commit()

    with pytest.raises(HTTPException) as exc:
        run_membership(db, _failing_action(db, sqlite3.OperationalError("database is locked")))

    assert exc.value.status_code == 503
    assert db.execute("SELECT 1 FROM product_variants WHERE barcode = 'b1'").fetchone() is None
    db.execute("INSERT INTO barcodes (barcode) VALUES ('b2')")
    db.commit()
    assert db.execute("SELECT COUNT(*) c FROM barcodes").fetchone()["c"] == 2


def test_run_membership_commits_on_success(db):
    """The positive control: the wrapper must actually persist a successful action."""
    db.execute("INSERT INTO barcodes (barcode) VALUES ('b1')")
    db.commit()

    result = run_membership(db, lambda: ensure_variant_row(db, "b1", _RID))

    assert result == {"ok": True}
    db.rollback()   # would discard the write if run_membership had not committed
    assert db.execute("SELECT 1 FROM product_variants WHERE barcode = 'b1'").fetchone() is not None


# --- Auth coverage ---------------------------------------------------------------------

def test_all_new_routes_require_auth(client_no_auth):
    """Every new route returns 401 without an X-API-Key header."""
    calls = [
        ("post", "/groups", {"json": {"name": "X"}}),
        ("patch", "/groups/1", {"json": {"name": "Y"}}),
        ("delete", "/groups/1", {}),
        ("post", "/groups/1/variants", {"json": {"barcode": "12345678"}}),
        ("delete", "/groups/1/variants/12345678", {}),
        ("post", "/groups/1/subgroups", {"json": {"child_group_id": 2}}),
        ("delete", "/groups/1/subgroups/2", {}),
        ("get", "/groups", {}),
        ("get", "/groups/1", {}),
    ]
    for method, url, kwargs in calls:
        resp = getattr(client_no_auth, method)(url, **kwargs)
        assert resp.status_code == 401, f"{method} {url} -> {resp.status_code}"


# --- Whole-app auth coverage, derived from the route table ------------------------------
# The test above pins the groups routes by hand. These two derive the check from
# ``app.routes`` instead, so a route added later cannot escape it by not being listed:
# one asserts every key-gated route 401s without a header, the other pins the
# intentionally-unauthenticated set so widening it has to be a deliberate edit here.

def _route_dependency_names(route) -> set[str]:
    """Every dependency name in effect on a route, router-level and endpoint-level.

    Endpoint-level ``Depends(...)`` (``verify_docs_access`` on ``/docs``) lives in the
    dependant tree rather than ``route.dependencies``, so both are walked — otherwise
    ``/docs`` reads as unauthenticated when it is in fact gated.
    """
    names = {
        getattr(d.dependency, "__name__", "")
        for d in (getattr(route, "dependencies", None) or [])
    }
    dependant = getattr(route, "dependant", None)
    if dependant is not None:
        stack = list(dependant.dependencies)
        while stack:
            d = stack.pop()
            names.add(getattr(d.call, "__name__", ""))
            stack.extend(d.dependencies)
    return names


# Placeholder values for path parameters; any syntactically valid value will do, because
# the 401 must be raised by the dependency before the handler ever sees them.
_PATH_PARAM_STUBS = {
    "barcode": "12345678",
    "retailer_id": "1",
    "group_id": "1",
    "child_group_id": "2",
}

# The routes that intentionally serve without an X-API-Key. Justification per entry lives
# in the 4c audit record; the point of pinning it here is that adding to this set is a
# visible, reviewable change rather than a silent consequence of registering a route.
_EXPECTED_UNAUTHENTICATED = {
    ("GET", "/"),                 # redirect to /feed.html, no data
    ("POST", "/docs-login"),      # the key-exchange endpoint itself; compare_digest'd inside
    ("GET", "/openapi.json"),     # API structure only, no inventory data
    ("WEBSOCKET", "/ws"),         # browser WebSocket API cannot send custom headers
    ("MOUNT", ""),                # StaticFiles mount at "/" serving frontend/
}


def _iter_http_routes():
    """(methods, path, dependency-names) for every plain HTTP route in the app."""
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if not methods or not hasattr(route, "dependant"):
            continue
        yield methods, route.path, _route_dependency_names(route)


def test_every_key_gated_route_401s_without_header(client_no_auth):
    """Every route carrying verify_api_key returns 401 with no X-API-Key header.

    Derived from app.routes rather than a hand-maintained list, so this covers routes
    added after it was written.
    """
    checked = 0
    for methods, path, deps in _iter_http_routes():
        if "verify_api_key" not in deps:
            continue
        url = path
        for name, stub in _PATH_PARAM_STUBS.items():
            url = url.replace("{" + name + "}", stub)
        assert "{" not in url, f"unsubstituted path parameter in {path}"
        for method in methods:
            resp = client_no_auth.request(method, url, json={})
            assert resp.status_code == 401, (
                f"{method} {url} -> {resp.status_code} (expected 401 with no API key)"
            )
            checked += 1
    assert checked >= 25, f"only {checked} key-gated route/method pairs found — sweep too narrow"


def test_unauthenticated_route_set_is_exactly_as_expected():
    """The set of routes served without verify_api_key is pinned.

    A new unauthenticated route, or auth removed from an existing one, fails here.
    ``/docs`` must NOT appear: it is gated by verify_docs_access, not left open.
    """
    actual = set()
    for route in app.routes:
        deps = _route_dependency_names(route)
        if "verify_api_key" in deps or "verify_docs_access" in deps:
            continue
        methods = getattr(route, "methods", None)
        if methods:
            for method in methods:
                actual.add((method, route.path))
        elif hasattr(route, "dependant"):          # APIWebSocketRoute
            actual.add(("WEBSOCKET", route.path))
        else:                                      # Mount
            actual.add(("MOUNT", route.path))

    assert actual == _EXPECTED_UNAUTHENTICATED, (
        f"unauthenticated route set changed\n"
        f"  unexpectedly open: {sorted(actual - _EXPECTED_UNAUTHENTICATED)}\n"
        f"  no longer open:    {sorted(_EXPECTED_UNAUTHENTICATED - actual)}"
    )


def test_docs_is_gated_by_docs_access_not_left_open():
    """/docs carries verify_docs_access — it is authenticated, just by a different means."""
    docs = [r for r in app.routes if getattr(r, "path", None) == "/docs"]
    assert len(docs) == 1
    assert "verify_docs_access" in _route_dependency_names(docs[0])
    assert "verify_api_key" not in _route_dependency_names(docs[0])


# --- Group-name length bound (4c audit, finding 4c-03) ---------------------------------
# A group name is user-typed free text that is stored, rendered on three pages and printed
# on a receipt, and it had no upper bound. The project bounds user-typed API input at the
# schema layer with a Field constraint returning 422 (as ge=0 already does on
# DeltaUpdateRequest.delta and SetMinimumQuantityRequest.minimum_quantity), so the cap
# belongs there. 64 matches the _MAX_PQ_LEN precedent in src/worker/off.py.
#
# This constrains create and rename only; it does NOT retro-validate stored rows, which is
# why src/api/printer.py also bounds the string at the render boundary.

_MAX_GROUP_NAME = 64


def test_create_group_accepts_name_at_the_length_limit(client):
    resp = client.post("/groups", json={"name": "N" * _MAX_GROUP_NAME})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["name"]) == _MAX_GROUP_NAME


def test_create_group_rejects_over_long_name(client):
    resp = client.post("/groups", json={"name": "N" * (_MAX_GROUP_NAME + 1)})
    assert resp.status_code == 422, resp.text


def test_create_group_rejects_grossly_over_long_name(client):
    resp = client.post("/groups", json={"name": "N" * 100_000})
    assert resp.status_code == 422, resp.text


def test_rename_group_accepts_name_at_the_length_limit(client, db):
    gid = client.post("/groups", json={"name": "Original"}).json()["id"]
    resp = client.patch(f"/groups/{gid}", json={"name": "R" * _MAX_GROUP_NAME})
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["name"]) == _MAX_GROUP_NAME


def test_rename_group_rejects_over_long_name(client, db):
    gid = client.post("/groups", json={"name": "Original"}).json()["id"]
    resp = client.patch(f"/groups/{gid}", json={"name": "R" * (_MAX_GROUP_NAME + 1)})
    assert resp.status_code == 422, resp.text
    # The rejected rename must not have partially applied.
    assert db.execute(
        "SELECT name FROM product_groups WHERE id = ?", (gid,)
    ).fetchone()["name"] == "Original"


def test_over_long_name_is_rejected_before_it_reaches_the_database(client, db):
    client.post("/groups", json={"name": "N" * (_MAX_GROUP_NAME + 1)})
    assert db.execute("SELECT COUNT(*) FROM product_groups").fetchone()[0] == 0

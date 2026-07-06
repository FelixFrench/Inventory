"""Product-group HTTP surface.

Group CRUD, group-side membership editing, and the group read endpoints. The membership
write helpers here (``ensure_variant_row``, ``add_variant_to_group`` / ``remove_...``,
``add_subgroup`` / ``remove_...``) are the *single* implementation of the edge-mutation logic:
the product-facing membership endpoints in ``routers/products.py`` import and call these, so the
upsert, cycle check, and existence guards live in one place. Assembly/resolution stays in
``src/api/groups.py`` (imported here as ``group_resolver``); this module is the router layer.
"""
import logging
import sqlite3

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.api import groups as group_resolver
from src.api.dependencies import get_db, get_retailer_id
from src.api.errors import SERVICE_UNAVAILABLE_503 as _503
from src.api.models import (
    AddSubgroupRequest,
    AddVariantMemberRequest,
    CreateGroupRequest,
    UpdateGroupRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/groups", tags=["Groups"])


# --- Typed errors for the shared edge logic -------------------------------------------
# Raised by the membership helpers below and translated to JSON error responses by the
# routes (here and in routers/products.py). Keeps the two membership directions on one
# code path without either side re-deriving the upsert / cycle / existence rules.

class GroupNotFound(Exception):
    """A referenced product_groups row does not exist -> 404 group_not_found."""


class BarcodeNotFound(Exception):
    """A referenced barcodes row does not exist -> 404 barcode_not_found."""


class CycleError(Exception):
    """A sub-group edge would create a cycle (incl. self-edge) -> 409 cycle_detected."""


# --- Existence guards & the shared variant upsert -------------------------------------

def _assert_group_exists(conn: sqlite3.Connection, group_id: int) -> None:
    if conn.execute(
        "SELECT 1 FROM product_groups WHERE id = ?", (group_id,)
    ).fetchone() is None:
        raise GroupNotFound


def _assert_barcode_exists(conn: sqlite3.Connection, barcode: str) -> None:
    if conn.execute(
        "SELECT 1 FROM barcodes WHERE barcode = ?", (barcode,)
    ).fetchone() is None:
        raise BarcodeNotFound


def ensure_variant_row(conn: sqlite3.Connection, barcode: str, retailer_id: int) -> None:
    """Ensure a ``product_variants`` row exists for ``(barcode, retailer_id)``.

    The 1c-style upsert, shared by both membership directions: a variant edge FKs
    ``product_variants(barcode, retailer_id)``, so a scan-only barcode (no variant row yet)
    would fail the edge insert. ``ON CONFLICT DO NOTHING`` creates a null-data row when absent
    and leaves an existing row (with its OFF data / minimum) untouched. A barcode with no
    ``barcodes`` row violates the FK -> ``BarcodeNotFound`` (the existing 1c 404 case).
    """
    try:
        conn.execute(
            "INSERT INTO product_variants (barcode, retailer_id) VALUES (?, ?) "
            "ON CONFLICT(barcode, retailer_id) DO NOTHING",
            (barcode, retailer_id),
        )
    except sqlite3.IntegrityError as e:
        raise BarcodeNotFound from e


# --- Shared membership mutations ------------------------------------------------------
# Callers commit (and roll back on error). Adds are idempotent (ON CONFLICT DO NOTHING);
# removes are idempotent no-ops once both named entities are confirmed to exist.

def add_variant_to_group(
    conn: sqlite3.Connection, group_id: int, barcode: str, retailer_id: int
) -> None:
    _assert_group_exists(conn, group_id)
    ensure_variant_row(conn, barcode, retailer_id)
    conn.execute(
        "INSERT INTO group_variant_members (group_id, barcode, retailer_id) "
        "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
        (group_id, barcode, retailer_id),
    )


def remove_variant_from_group(
    conn: sqlite3.Connection, group_id: int, barcode: str, retailer_id: int
) -> None:
    # Both named entities must exist first: a bad group id or barcode is a client mistake to
    # surface (404), not a silent zero-row delete. A valid non-member is an idempotent no-op.
    _assert_group_exists(conn, group_id)
    _assert_barcode_exists(conn, barcode)
    conn.execute(
        "DELETE FROM group_variant_members "
        "WHERE group_id = ? AND barcode = ? AND retailer_id = ?",
        (group_id, barcode, retailer_id),
    )


def add_subgroup(conn: sqlite3.Connection, parent_group_id: int, child_group_id: int) -> None:
    _assert_group_exists(conn, parent_group_id)
    _assert_group_exists(conn, child_group_id)
    # would_create_cycle covers self-edge (parent == child) and deeper cycles in one check.
    if group_resolver.would_create_cycle(conn, parent_group_id, child_group_id):
        raise CycleError
    conn.execute(
        "INSERT INTO group_group_members (parent_group_id, child_group_id) "
        "VALUES (?, ?) ON CONFLICT DO NOTHING",
        (parent_group_id, child_group_id),
    )


def remove_subgroup(
    conn: sqlite3.Connection, parent_group_id: int, child_group_id: int
) -> None:
    _assert_group_exists(conn, parent_group_id)
    _assert_group_exists(conn, child_group_id)
    conn.execute(
        "DELETE FROM group_group_members "
        "WHERE parent_group_id = ? AND child_group_id = ?",
        (parent_group_id, child_group_id),
    )


def run_membership(db: sqlite3.Connection, action) -> dict | JSONResponse:
    """Execute a membership mutation, commit on success, translate typed errors to JSON.

    Shared by every add/remove route (both directions) so the error mapping lives once.
    """
    try:
        action()
        db.commit()
        return {"ok": True}
    except GroupNotFound:
        db.rollback()
        return JSONResponse(status_code=404, content={"error": "group_not_found"})
    except BarcodeNotFound:
        db.rollback()
        return JSONResponse(status_code=404, content={"error": "barcode_not_found"})
    except CycleError:
        db.rollback()
        return JSONResponse(status_code=409, content={"error": "cycle_detected"})
    except sqlite3.OperationalError:
        db.rollback()
        raise _503
    except Exception as e:
        db.rollback()
        logger.error("DB error during membership mutation: %s", e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})


# --- Group CRUD -----------------------------------------------------------------------

@router.post("", response_model=None)
def create_group(
    body: CreateGroupRequest, db: sqlite3.Connection = Depends(get_db)
) -> dict | JSONResponse:
    """Create a product group. Duplicate name -> 409 group_name_conflict."""
    try:
        cur = db.execute(
            "INSERT INTO product_groups (name, minimum_quantity) VALUES (?, ?)",
            (body.name, body.minimum_quantity),
        )
        db.commit()
        return {"id": cur.lastrowid, "name": body.name, "minimum_quantity": body.minimum_quantity}
    except sqlite3.IntegrityError:
        db.rollback()
        return JSONResponse(status_code=409, content={"error": "group_name_conflict"})
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error creating group: %s", e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})


@router.patch("/{group_id}", response_model=None)
def update_group(
    group_id: int, body: UpdateGroupRequest, db: sqlite3.Connection = Depends(get_db)
) -> dict | JSONResponse:
    """Rename and/or set the minimum. ``minimum_quantity=0`` clears it (organisational-only).
    Unknown id -> 404 group_not_found; rename collision -> 409 group_name_conflict."""
    try:
        row = db.execute(
            "SELECT id, name, minimum_quantity FROM product_groups WHERE id = ?", (group_id,)
        ).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "group_not_found"})
        new_name = body.name if body.name is not None else row["name"]
        new_min = (
            body.minimum_quantity if body.minimum_quantity is not None
            else row["minimum_quantity"]
        )
        db.execute(
            "UPDATE product_groups SET name = ?, minimum_quantity = ? WHERE id = ?",
            (new_name, new_min, group_id),
        )
        db.commit()
        return {"id": group_id, "name": new_name, "minimum_quantity": new_min}
    except sqlite3.IntegrityError:
        db.rollback()
        return JSONResponse(status_code=409, content={"error": "group_name_conflict"})
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error updating group %s: %s", group_id, e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})


@router.delete("/{group_id}", response_model=None)
def delete_group(
    group_id: int, db: sqlite3.Connection = Depends(get_db)
) -> dict | JSONResponse:
    """Delete a group. The 2a ON DELETE CASCADE FKs drop only its edges (as parent and as
    child); member variants, sub-groups, and other groups it belonged to survive."""
    try:
        row = db.execute(
            "SELECT 1 FROM product_groups WHERE id = ?", (group_id,)
        ).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "group_not_found"})
        db.execute("DELETE FROM product_groups WHERE id = ?", (group_id,))
        db.commit()
        return {"deleted": group_id}
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error deleting group %s: %s", group_id, e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})


# --- Membership (group side) ----------------------------------------------------------

@router.post("/{group_id}/variants", response_model=None)
def add_group_variant(
    group_id: int,
    body: AddVariantMemberRequest,
    db: sqlite3.Connection = Depends(get_db),
    retailer_id: int = Depends(get_retailer_id),
) -> dict | JSONResponse:
    """Add a variant (barcode, retailer) to a group; upserts the variant row first."""
    return run_membership(
        db, lambda: add_variant_to_group(db, group_id, body.barcode, retailer_id)
    )


@router.delete("/{group_id}/variants/{barcode}", response_model=None)
def remove_group_variant(
    group_id: int,
    barcode: str,
    db: sqlite3.Connection = Depends(get_db),
    retailer_id: int = Depends(get_retailer_id),
) -> dict | JSONResponse:
    """Remove a variant edge (idempotent); bad group id or barcode -> 404."""
    return run_membership(
        db, lambda: remove_variant_from_group(db, group_id, barcode, retailer_id)
    )


@router.post("/{group_id}/subgroups", response_model=None)
def add_group_subgroup(
    group_id: int, body: AddSubgroupRequest, db: sqlite3.Connection = Depends(get_db)
) -> dict | JSONResponse:
    """Nest a sub-group under a group; rejects self-edges and cycles -> 409 cycle_detected."""
    return run_membership(db, lambda: add_subgroup(db, group_id, body.child_group_id))


@router.delete("/{group_id}/subgroups/{child_group_id}", response_model=None)
def remove_group_subgroup(
    group_id: int, child_group_id: int, db: sqlite3.Connection = Depends(get_db)
) -> dict | JSONResponse:
    """Remove a sub-group edge (idempotent); bad parent or child id -> 404."""
    return run_membership(db, lambda: remove_subgroup(db, group_id, child_group_id))


# --- Read endpoints -------------------------------------------------------------------

@router.get("", response_model=None)
def list_groups(db: sqlite3.Connection = Depends(get_db)) -> dict | JSONResponse:
    """Every group with its resolved state (via 2a's resolver)."""
    try:
        groups = [
            {
                "group_id": g.group_id,
                "name": g.name,
                "minimum_quantity": g.minimum_quantity,
                "total_quantity": g.total_quantity,
                "low_stock": g.low_stock,
                "shortfall": g.shortfall,
            }
            for g in group_resolver.resolve_all_groups(db)
        ]
        return {"groups": groups}
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error listing groups: %s", e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})


@router.get("/{group_id}", response_model=None)
def get_group(
    group_id: int, db: sqlite3.Connection = Depends(get_db)
) -> dict | JSONResponse:
    """One group's full resolution plus its direct variant and sub-group members.

    Totals come from ``resolve_group`` (which dedups the diamond case across nesting); the
    member lists are the group's *direct* edges only, LEFT-JOINed for display fields so a
    null-data variant still renders.
    """
    try:
        res = group_resolver.resolve_group(db, group_id)
        if res is None:
            return JSONResponse(status_code=404, content={"error": "group_not_found"})
        variant_rows = db.execute(
            "SELECT gvm.barcode, gvm.retailer_id, pv.name, pv.brand, pv.product_quantity "
            "FROM group_variant_members gvm "
            "LEFT JOIN product_variants pv "
            "       ON pv.barcode = gvm.barcode AND pv.retailer_id = gvm.retailer_id "
            "WHERE gvm.group_id = ? "
            "ORDER BY pv.name ASC NULLS LAST, gvm.barcode ASC",
            (group_id,),
        ).fetchall()
        subgroup_rows = db.execute(
            "SELECT pg.id, pg.name FROM group_group_members ggm "
            "JOIN product_groups pg ON pg.id = ggm.child_group_id "
            "WHERE ggm.parent_group_id = ? ORDER BY pg.name ASC",
            (group_id,),
        ).fetchall()
        return {
            "group_id": res.group_id,
            "name": res.name,
            "minimum_quantity": res.minimum_quantity,
            "total_quantity": res.total_quantity,
            "low_stock": res.low_stock,
            "shortfall": res.shortfall,
            "variants": [
                {
                    "barcode": r["barcode"],
                    "retailer_id": r["retailer_id"],
                    "name": r["name"],
                    "brand": r["brand"],
                    "product_quantity": r["product_quantity"],
                }
                for r in variant_rows
            ],
            "subgroups": [{"id": r["id"], "name": r["name"]} for r in subgroup_rows],
        }
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error fetching group %s: %s", group_id, e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})

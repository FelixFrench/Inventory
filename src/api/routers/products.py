import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from src.api.dependencies import get_db
from src.api.errors import SERVICE_UNAVAILABLE_503 as _503
from src.api.models import AddGroupMembershipRequest, SetMinimumQuantityRequest
from src.api.routers.groups import (
    add_variant_to_group,
    remove_variant_from_group,
    run_membership,
)
from src.api.urls import group_page_url, off_url

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Products"])


def _retailer_missing_response(
    db: sqlite3.Connection, retailer_id: int
) -> JSONResponse | None:
    """Return a 404 retailer_not_found response if retailer_id has no retailers row, else None.

    The retailer-scoped product routes take retailer_id from the path, so a syntactically valid
    but nonexistent id must be surfaced explicitly: otherwise GET returns a misleading all-null
    200 (the LEFT JOINs match nothing) and a membership add fails the product_variants ->
    retailers FK, which would otherwise be misattributed to barcode_not_found. Raises
    sqlite3.OperationalError on contention (callers map it to 503)."""
    if db.execute("SELECT 1 FROM retailers WHERE id = ?", (retailer_id,)).fetchone() is None:
        return JSONResponse(status_code=404, content={"error": "retailer_not_found"})
    return None


@router.put("/products/{barcode}/{retailer_id}/minimum_quantity", response_model=None)
def set_minimum_quantity(
    barcode: str,
    retailer_id: int,
    body: SetMinimumQuantityRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> dict | JSONResponse:
    """
    Set the minimum quantity threshold for a product variant.

    The variant is identified by the composite (barcode, retailer_id), both taken from the
    path — the same {barcode}/{retailer_id} convention as the product-detail/membership routes.
    Upserts the restock alert level onto product_variants: updates the row if it exists,
    otherwise creates a null-data variant row carrying only the minimum. A negative
    minimum_quantity is rejected at the schema layer (422, SetMinimumQuantityRequest.
    minimum_quantity has ge=0). An unknown retailer_id -> 404 retailer_not_found (checked
    before the upsert, so a FK failure isn't misattributed to barcode_not_found). The minimum
    is settable for any in-system barcode (one with a barcodes row); a barcode with no barcodes
    row violates the FK and returns 404 barcode_not_found.
    """
    # Defence in depth: schema validation (Field(ge=0)) already rejects negatives
    # with 422 before this handler runs, so this branch is not reachable via HTTP.
    if body.minimum_quantity < 0:
        return JSONResponse(status_code=400, content={"error": "invalid_minimum_quantity"})
    try:
        guard = _retailer_missing_response(db, retailer_id)
        if guard is not None:
            return guard
        db.execute(
            "INSERT INTO product_variants (barcode, retailer_id, minimum_quantity) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(barcode, retailer_id) DO UPDATE SET minimum_quantity = excluded.minimum_quantity",
            (barcode, retailer_id, body.minimum_quantity),
        )
        db.commit()
        return {"barcode": barcode, "minimum_quantity": body.minimum_quantity}
    except sqlite3.IntegrityError:
        db.rollback()  # release the implicit transaction from the failed FK insert
        return JSONResponse(status_code=404, content={"error": "barcode_not_found"})
    except HTTPException:
        raise
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error updating minimum_quantity for %s: %s", barcode, e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})


@router.post("/products/{barcode}/{retailer_id}/refresh", response_model=None)
def request_manual_refresh(
    barcode: str,
    retailer_id: int,
    db: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    """
    Queue an on-demand OFF + price refresh for a product variant (Sprint 2, Phase 3c).

    This handler performs NO OpenFoodFacts or Sainsbury's call — it only records a request marker
    in the DB, preserving the single-OFF-caller invariant (3a/3b): the single worker process picks
    the marked row up on its next poll, performs the paced OFF call + chained price lookup, and
    clears the marker. So FastAPI never calls OFF; the worker does.

    Marks the variant by upserting the row for (barcode, retailer_id): a fresh INSERT creates a
    null-data pending variant row carrying only manual_refresh_requested = 1 (all other columns at
    their defaults), so a never-resolved barcode is refreshable (mirrors the 1c set-minimum upsert).
    On CONFLICT it sets ONLY manual_refresh_requested = 1 — name/brand/product_quantity/
    minimum_quantity/lookup_status/lookup_failure_count/last_lookup_datetime are left untouched.
    Idempotent: a second call before the worker processes leaves the marker at 1.

    An unknown retailer_id -> 404 retailer_not_found (checked before the upsert). A barcode with no
    barcodes row violates the FK and returns 404 barcode_not_found. Returns 202 Accepted.
    """
    try:
        guard = _retailer_missing_response(db, retailer_id)
        if guard is not None:
            return guard
        db.execute(
            "INSERT INTO product_variants (barcode, retailer_id, manual_refresh_requested) "
            "VALUES (?, ?, 1) "
            "ON CONFLICT(barcode, retailer_id) DO UPDATE SET manual_refresh_requested = 1",
            (barcode, retailer_id),
        )
        db.commit()
        return JSONResponse(
            status_code=202,
            content={"status": "queued", "barcode": barcode, "retailer_id": retailer_id},
        )
    except sqlite3.IntegrityError:
        db.rollback()  # release the implicit transaction from the failed FK insert
        return JSONResponse(status_code=404, content={"error": "barcode_not_found"})
    except HTTPException:
        raise
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error queueing manual refresh for %s: %s", barcode, e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})


@router.get("/products/{barcode}/{retailer_id}", response_model=None)
def get_product_detail(
    barcode: str,
    retailer_id: int,
    db: sqlite3.Connection = Depends(get_db),
) -> dict | JSONResponse:
    """
    Return one variant's own fields, current inventory quantity, minimum, price fields,
    and the groups it is directly a member of.

    The variant is identified by the composite (barcode, retailer_id), both taken from the path.
    LEFT JOINs throughout so a null-data / unresolved variant still renders (null name/brand/
    quantity, no price row). An unknown retailer_id -> 404 retailer_not_found; a barcode absent
    from `barcodes` entirely -> 404 barcode_not_found. Returns raw nullable fields; the frontend
    decides display text. No lookup-status field: the durable columns exist
    (`product_variants.lookup_status`) but are deliberately not exposed here -- they are read only
    by the refresh/retry worker. Exposing them so the product page can distinguish "OFF failed"
    from "never looked up" is unclaimed follow-up work.
    """
    try:
        guard = _retailer_missing_response(db, retailer_id)
        if guard is not None:
            return guard
        row = db.execute(
            """
            SELECT
                b.barcode,
                pv.name,
                pv.brand,
                pv.product_quantity,
                COALESCE(pv.minimum_quantity, 0) AS minimum_quantity,
                COALESCE(inv.quantity, 0)        AS current_quantity,
                pr.price_pence,
                pr.price_type,
                pr.product_url
            FROM barcodes b
            LEFT JOIN product_variants pv
                   ON pv.barcode = b.barcode AND pv.retailer_id = ?
            LEFT JOIN inventory inv
                   ON inv.barcode = b.barcode AND inv.retailer_id = ?
            LEFT JOIN prices pr
                   ON pr.barcode = b.barcode AND pr.retailer_id = ?
            WHERE b.barcode = ?
            """,
            (retailer_id, retailer_id, retailer_id, barcode),
        ).fetchone()
        if row is None:
            return JSONResponse(status_code=404, content={"error": "barcode_not_found"})

        group_rows = db.execute(
            "SELECT pg.id, pg.name FROM group_variant_members gvm "
            "JOIN product_groups pg ON pg.id = gvm.group_id "
            "WHERE gvm.barcode = ? AND gvm.retailer_id = ? ORDER BY pg.name ASC",
            (barcode, retailer_id),
        ).fetchall()

        return {
            "barcode": row["barcode"],
            "name": row["name"],
            "brand": row["brand"],
            "product_quantity": row["product_quantity"],
            "minimum_quantity": row["minimum_quantity"],
            "current_quantity": row["current_quantity"],
            "price_pence": row["price_pence"],
            "price_type": row["price_type"],
            "product_url": row["product_url"],
            # OFF link built via the single urls.py builder (view vs add/edit chosen by name)
            # so the branching lives in one place; the frontend consumes off_url directly.
            "off_url": off_url(barcode, name=row["name"]),
            "groups": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "group_page_url": group_page_url(r["id"]),
                }
                for r in group_rows
            ],
        }
    except sqlite3.OperationalError:
        raise _503
    except Exception as e:
        logger.error("DB error fetching product detail for %s: %s", barcode, e)
        return JSONResponse(status_code=500, content={"error": "internal_error"})


@router.post("/products/{barcode}/{retailer_id}/groups", response_model=None)
def add_product_to_group(
    barcode: str,
    retailer_id: int,
    body: AddGroupMembershipRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> dict | JSONResponse:
    """Product-facing membership add: put this variant into a group. The variant is identified
    by the composite (barcode, retailer_id) from the path. Drives the same edge logic as the
    group-side endpoint (upsert-then-insert, idempotent). An unknown retailer_id -> 404
    retailer_not_found (checked first so the FK failure isn't misattributed to barcode_not_found)."""
    try:
        guard = _retailer_missing_response(db, retailer_id)
    except sqlite3.OperationalError:
        raise _503
    if guard is not None:
        return guard
    return run_membership(
        db, lambda: add_variant_to_group(db, body.group_id, barcode, retailer_id)
    )


@router.delete("/products/{barcode}/{retailer_id}/groups/{group_id}", response_model=None)
def remove_product_from_group(
    barcode: str,
    retailer_id: int,
    group_id: int,
    db: sqlite3.Connection = Depends(get_db),
) -> dict | JSONResponse:
    """Product-facing membership remove. The variant is identified by the composite
    (barcode, retailer_id) from the path. Validates the retailer, barcode, and group exist;
    a valid non-membership is an idempotent no-op."""
    try:
        guard = _retailer_missing_response(db, retailer_id)
    except sqlite3.OperationalError:
        raise _503
    if guard is not None:
        return guard
    return run_membership(
        db, lambda: remove_variant_from_group(db, group_id, barcode, retailer_id)
    )

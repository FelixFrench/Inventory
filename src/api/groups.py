"""Product-group resolver -- assembly, not rendering (Sprint 2, Phase 2a).

Mirrors ``reports.py``'s role: pure data assembly over the group edge tables. Every function
takes a ``sqlite3.Connection`` and returns plain data structures (a ``NamedTuple`` or plain
Python values) -- no HTML, no display strings, no HTTP/Pydantic concerns. The group CRUD /
read endpoints (2b) and the group/product pages (2c) call into this module; 2a ships no HTTP
surface of its own.

The membership graph (Option B, two edge tables):
  * ``group_variant_members(group_id, barcode, retailer_id)`` -- variant leaves.
  * ``group_group_members(parent_group_id, child_group_id)`` -- group -> group nesting.

A variant or group may belong to several parents; a variant reachable via several paths
counts exactly once in a group's total (dedup before summing). Each group is evaluated for
low-stock against *its own* ``minimum_quantity`` only -- nesting changes which variants roll
into a total, never which minimum applies.
"""
import sqlite3
from typing import NamedTuple, Optional


class GroupResolution(NamedTuple):
    """The rolled-up state of one product group (assembly output, not for display)."""

    group_id: int
    name: str
    minimum_quantity: int
    total_quantity: int
    low_stock: bool
    shortfall: int
    member_variants: list[tuple[str, int]]  # distinct (barcode, retailer_id), sorted


# --- Shared low-stock predicate --------------------------------------------------------
# Defined once here so 2b can reuse it for the product low-stock path. ``minimum == 0`` means
# "no minimum" and is never low. (2a does NOT rewire reports.py to consume these -- that is
# 2b's restructure; here we only introduce the reusable definition and use it in the resolver.)

def is_low_stock(total: int, minimum: int) -> bool:
    """True when a minimum is set and the total falls below it."""
    return minimum > 0 and total < minimum


def shortfall(total: int, minimum: int) -> int:
    """How far below the minimum the total is (0 when at/above minimum or no minimum)."""
    return max(minimum - total, 0)


# --- Recursive reachability (cycle-safe) -----------------------------------------------
# WITH RECURSIVE over the group->group edges. UNION (not UNION ALL) discards already-seen
# rows, so the walk terminates even if the edge data contains a cycle -- the primary safety
# net. Belt-and-braces: a '/'-delimited path column plus a NOT LIKE guard excludes any group
# already on the current path, so termination holds regardless of the set operator. Group ids
# are integers, so the path can contain no LIKE wildcards ('%'/'_'); the '/...//' delimiting
# rules out substring false matches (e.g. '/5/' vs '/15/').
_REACHABLE_SQL = """
WITH RECURSIVE reachable(gid, path) AS (
    SELECT :root, '/' || :root || '/'
    UNION
    SELECT ggm.child_group_id, reachable.path || ggm.child_group_id || '/'
    FROM group_group_members ggm
    JOIN reachable ON ggm.parent_group_id = reachable.gid
    WHERE reachable.path NOT LIKE '%/' || ggm.child_group_id || '/%'
)
SELECT DISTINCT gid FROM reachable
"""


def reachable_group_ids(conn: sqlite3.Connection, start: int) -> set[int]:
    """Every group reachable from ``start`` following group->group edges, including ``start``.

    Shared by ``resolve_group`` (which variants roll up) and ``would_create_cycle``
    (reachability check). Terminates on cyclic edge data (see ``_REACHABLE_SQL``).
    """
    rows = conn.execute(_REACHABLE_SQL, {"root": start}).fetchall()
    return {row[0] for row in rows}


def resolve_group(conn: sqlite3.Connection, group_id: int) -> Optional[GroupResolution]:
    """Resolve one group to its rolled-up state, or ``None`` if the id does not exist.

    ``total_quantity`` sums ``inventory.quantity`` once per *distinct* member variant across
    the group and every group nested beneath it; a member variant with no inventory row
    contributes 0 (COALESCE). Low-stock is evaluated against this group's own minimum only.
    """
    group = conn.execute(
        "SELECT id, name, minimum_quantity FROM product_groups WHERE id = ?",
        (group_id,),
    ).fetchone()
    if group is None:
        return None

    gids = reachable_group_ids(conn, group_id)  # non-empty: always contains group_id
    placeholders = ",".join("?" * len(gids))
    rows = conn.execute(
        "SELECT mv.barcode, mv.retailer_id, COALESCE(inv.quantity, 0) AS quantity "
        "FROM (SELECT DISTINCT barcode, retailer_id FROM group_variant_members "
        f"      WHERE group_id IN ({placeholders})) mv "
        "LEFT JOIN inventory inv "
        "       ON inv.barcode = mv.barcode AND inv.retailer_id = mv.retailer_id",
        tuple(gids),
    ).fetchall()

    member_variants = sorted((row["barcode"], row["retailer_id"]) for row in rows)
    total = sum(row["quantity"] for row in rows)
    minimum = group["minimum_quantity"]
    return GroupResolution(
        group_id=group["id"],
        name=group["name"],
        minimum_quantity=minimum,
        total_quantity=total,
        low_stock=is_low_stock(total, minimum),
        shortfall=shortfall(total, minimum),
        member_variants=member_variants,
    )


def resolve_all_groups(conn: sqlite3.Connection) -> list[GroupResolution]:
    """Resolve every group (assembly only; 2b renders). Thin map over the group ids."""
    ids = [row[0] for row in conn.execute("SELECT id FROM product_groups ORDER BY id").fetchall()]
    return [res for gid in ids if (res := resolve_group(conn, gid)) is not None]


def would_create_cycle(
    conn: sqlite3.Connection, parent_group_id: int, child_group_id: int
) -> bool:
    """True when adding the group->group edge (parent, child) would create a cycle.

    That happens when it is a self edge, or when the prospective child can already reach the
    prospective parent (so the new edge would close a loop). Only meaningful for group->group
    edges; variant members are leaves and can never create a cycle. 2b wires this into the
    add-member endpoint.
    """
    if parent_group_id == child_group_id:
        return True
    return parent_group_id in reachable_group_ids(conn, child_group_id)

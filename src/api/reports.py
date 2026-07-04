from src.api.urls import off_url as build_off_url


def _label_for_info_field(info_status, has_pv_row, field_value) -> str:
    if info_status == 'pending':
        return 'pending'
    if info_status == 'failed':
        return 'failed'
    if not has_pv_row:
        return 'no_data'
    if field_value is None:
        return 'missing'
    return 'resolved'


def _label_for_price(price_status, has_pr_row, price_pence) -> str:
    if price_status == 'pending':
        return 'pending'
    if price_status == 'not_possible':
        return 'not_attempted'
    if price_status == 'failed':
        return 'failed'
    if not has_pr_row:
        return 'missing'
    if price_pence is None:
        return 'per_kg'
    return 'resolved'


def get_unresolved_report(db, retailer_id: int) -> dict:
    session_row = db.execute("SELECT id FROM sessions LIMIT 1").fetchone()
    session_id = session_row['id'] if session_row else -1

    rows = db.execute(
        """
        SELECT
            b.barcode,
            COALESCE(inv.quantity, 0)   AS inventory_quantity,
            pv.barcode                  AS pv_barcode,
            pv.name,
            pv.brand,
            pv.product_quantity,
            pr.barcode                  AS pr_barcode,
            pr.price_pence,
            pr.price_type,
            pr.product_url,
            si.info_status,
            si.price_status
        FROM barcodes b
        LEFT JOIN product_variants pv
               ON pv.barcode = b.barcode AND pv.retailer_id = ?
        LEFT JOIN prices pr
               ON pr.barcode = b.barcode AND pr.retailer_id = ?
        LEFT JOIN inventory inv
               ON inv.barcode = b.barcode AND inv.retailer_id = ?
        LEFT JOIN session_items si
               ON si.barcode = b.barcode AND si.session_id = ? AND si.retailer_id = ?
        WHERE (
            pv.barcode     IS NULL
            OR pv.name     IS NULL
            OR pv.brand    IS NULL
            OR pv.product_quantity IS NULL
            OR pr.barcode  IS NULL
            OR pr.price_pence IS NULL
            OR si.info_status  = 'pending'
            OR si.price_status = 'pending'
        )
        ORDER BY
            CASE WHEN si.barcode IS NOT NULL THEN 0 ELSE 1 END,
            pv.name ASC NULLS LAST,
            b.barcode ASC
        """,
        (retailer_id, retailer_id, retailer_id, session_id, retailer_id)
    ).fetchall()

    items = []
    for row in rows:
        has_pv = row['pv_barcode'] is not None
        has_pr = row['pr_barcode'] is not None

        name_label     = _label_for_info_field(row['info_status'], has_pv, row['name'])
        brand_label    = _label_for_info_field(row['info_status'], has_pv, row['brand'])
        quantity_label = _label_for_info_field(row['info_status'], has_pv, row['product_quantity'])
        price_label    = _label_for_price(row['price_status'], has_pr, row['price_pence'])

        if all(label == 'resolved' for label in [name_label, brand_label, quantity_label, price_label]):
            continue

        price_value = round(row['price_pence'] / 100, 2) if row['price_pence'] is not None else None

        items.append({
            "barcode":            row['barcode'],
            "inventory_quantity": row['inventory_quantity'],
            "name":     {"value": row['name'],             "label": name_label},
            "brand":    {"value": row['brand'],            "label": brand_label},
            "quantity": {"value": row['product_quantity'], "label": quantity_label},
            "price":    {"value": price_value,             "label": price_label},
            "off_url":   build_off_url(row['barcode'], name=row['name']),
            "price_url": row['product_url'],
        })

    return {"items": items, "total_count": len(items)}


def get_inventory_report(db, retailer_id: int) -> dict:
    rows = db.execute(
        """
        SELECT i.barcode, COALESCE(pv.name, i.barcode) AS name, pv.brand, i.quantity,
               p.price_pence, p.product_url
        FROM inventory i
        LEFT JOIN product_variants pv ON pv.barcode = i.barcode AND pv.retailer_id = i.retailer_id
        LEFT JOIN prices p ON p.barcode = i.barcode AND p.retailer_id = i.retailer_id
        WHERE i.retailer_id = ?
        ORDER BY (i.quantity = 0), LOWER(COALESCE(pv.name, i.barcode))
        """,
        (retailer_id,)
    ).fetchall()

    items = []
    total = 0
    for r in rows:
        line = r["price_pence"] * r["quantity"] if r["price_pence"] is not None else None
        if line is not None:
            total += line
        items.append({
            "name": r["name"],
            "brand": r["brand"],
            "quantity": r["quantity"],
            "price_pence": r["price_pence"],
            "line_total_pence": line,
            "off_url":   build_off_url(r["barcode"], name=r["name"]),
            "price_url": r["product_url"],
        })
    return {"items": items, "total_value_pence": total}


def get_low_stock_report(db, retailer_id: int) -> dict:
    rows = db.execute(
        """
        SELECT COALESCE(pv.name, i.barcode) AS name, pv.brand, i.quantity, i.minimum_quantity,
               (i.minimum_quantity - i.quantity) AS shortfall
        FROM inventory i
        LEFT JOIN product_variants pv ON pv.barcode = i.barcode AND pv.retailer_id = i.retailer_id
        WHERE i.quantity < i.minimum_quantity AND i.retailer_id = ?
        ORDER BY shortfall DESC, LOWER(COALESCE(pv.name, i.barcode))
        """,
        (retailer_id,)
    ).fetchall()

    items = [dict(r) for r in rows]
    return {"items": items}

def get_inventory_report(db) -> dict:
    rows = db.execute("""
        SELECT pv.name, pv.brand, i.quantity, p.price_pence
        FROM inventory i
        JOIN product_variants pv ON pv.id = i.product_variant_id
        LEFT JOIN prices p ON p.product_variant_id = pv.id
        ORDER BY (i.quantity = 0), LOWER(pv.name)
    """).fetchall()

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
        })
    return {"items": items, "total_value_pence": total}


def get_low_stock_report(db) -> dict:
    rows = db.execute("""
        SELECT pv.name, pv.brand, i.quantity, i.minimum_quantity,
               (i.minimum_quantity - i.quantity) AS shortfall
        FROM inventory i
        JOIN product_variants pv ON pv.id = i.product_variant_id
        WHERE i.quantity < i.minimum_quantity
        ORDER BY shortfall DESC, LOWER(pv.name)
    """).fetchall()

    items = [dict(r) for r in rows]
    return {"items": items}

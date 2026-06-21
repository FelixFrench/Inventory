#!/bin/bash
# DEV USE ONLY — clears cached product data not referenced by current inventory

DB="$(dirname "$0")/../inventory.db"

sudo systemctl stop fastapi worker listener

sqlite3 "$DB" <<'SQL'
PRAGMA foreign_keys = ON;
DELETE FROM inventory
  WHERE quantity = 0;
DELETE FROM prices
  WHERE barcode NOT IN (SELECT barcode FROM inventory);
DELETE FROM product_variants
  WHERE barcode NOT IN (SELECT barcode FROM inventory);
DELETE FROM barcodes
  WHERE barcode NOT IN (SELECT barcode FROM inventory);
SQL

sudo systemctl start fastapi worker listener
echo "Done. Unreferenced product data cleared."

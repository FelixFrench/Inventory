# src/db.py
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "inventory.db"

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("INVENTORY_DB", Path(__file__).parents[2] / "inventory.db"))


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

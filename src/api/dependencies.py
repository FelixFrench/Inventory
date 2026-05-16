from src.db.db import get_connection


def get_db():
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()

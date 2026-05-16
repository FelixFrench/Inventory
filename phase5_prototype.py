import sqlite3
import time
from datetime import datetime, timezone

DB = "dev.db"
MIN_INTERVAL = 4.0

def seconds_since_last_lookup(conn):
    row = conn.execute(
        "SELECT queued_at FROM pending_lookups WHERE status='done' ORDER BY queued_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return float('inf')
    last = datetime.fromisoformat(row[0])
    return (datetime.now(timezone.utc) - last.replace(tzinfo=timezone.utc)).total_seconds()

def get_pending_job(conn):
    return conn.execute(
        "SELECT barcode, retailer_id FROM pending_lookups WHERE status='pending' ORDER BY queued_at LIMIT 1"
    ).fetchone()

def process_job(conn, barcode, retailer_id):
    print(f"[worker] Processing barcode {barcode}")
    print(f"[worker] → GET https://world.openfoodfacts.org/api/v2/product/{barcode}.json")
    print(f"[worker] → Sainsbury's price lookup for {barcode}")
    conn.execute(
        "UPDATE pending_lookups SET status='done' WHERE barcode=? AND retailer_id=?",
        (barcode, retailer_id)
    )
    conn.commit()
    print(f"[worker] Marked {barcode} as done")

def main():
    conn = sqlite3.connect(DB, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")

    gap = seconds_since_last_lookup(conn)
    if gap < MIN_INTERVAL:
        wait = MIN_INTERVAL - gap
        print(f"[worker] Restarted recently — waiting {wait:.1f}s before first request")
        time.sleep(wait)

    print("[worker] Started. Polling pending_lookups...")
    while True:
        job = get_pending_job(conn)
        if job:
            process_job(conn, *job)
            time.sleep(MIN_INTERVAL)
        else:
            print("[worker] No pending jobs. Sleeping 5s...")
            time.sleep(5)

if __name__ == "__main__":
    main()

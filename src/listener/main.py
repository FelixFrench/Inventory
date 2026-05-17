import logging
import os
import time

import httpx

from src.listener.scanner import find_scanner, read_barcodes
from src.listener.validation import is_valid_barcode

FASTAPI_URL              = os.environ.get("INVENTORY_API_URL", "http://127.0.0.1:8000/scan")
RECONNECT_INITIAL_DELAY  = 2
RECONNECT_MAX_DELAY      = 30
RECONNECT_BACKOFF_FACTOR = 2


def post_scan(barcode: str) -> None:
    try:
        response = httpx.post(FASTAPI_URL, json={"barcode": barcode}, timeout=5)
        if not (200 <= response.status_code < 300):
            logging.warning("POST %s returned HTTP %s", FASTAPI_URL, response.status_code)
    except httpx.HTTPError as e:
        logging.warning("POST %s failed: %s", FASTAPI_URL, e)


def _reconnect():
    delay = RECONNECT_INITIAL_DELAY
    while True:
        time.sleep(delay)
        device = find_scanner()
        if device is not None:
            logging.info("Scanner reconnected")
            return device
        delay = min(delay * RECONNECT_BACKOFF_FACTOR, RECONNECT_MAX_DELAY)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("Inventory listener starting")

    device = find_scanner()
    if device is None:
        logging.warning("Scanner not found at startup, entering reconnect loop")
        device = _reconnect()

    while True:
        try:
            for barcode in read_barcodes(device):
                if is_valid_barcode(barcode):
                    logging.info("Scanned: %s", barcode)
                    post_scan(barcode)
        except OSError as e:
            if e.errno == 19:
                logging.warning("Scanner disconnected")
                device = _reconnect()
            else:
                raise


if __name__ == "__main__":
    main()

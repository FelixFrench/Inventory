import logging
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from src.listener.scanner import find_scanner, read_barcodes
from src.listener.validation import is_valid_barcode

logger = logging.getLogger(__name__)

FASTAPI_URL              = os.environ.get("INVENTORY_API_URL", "http://127.0.0.1:8000/scan")
RECONNECT_INITIAL_DELAY  = 2
RECONNECT_MAX_DELAY      = 30
RECONNECT_BACKOFF_FACTOR = 2

load_dotenv(Path(__file__).parents[2] / "config.local.env")
INVENTORY_API_KEY = os.environ.get("INVENTORY_API_KEY")

def post_scan(barcode: str) -> None:
    try:
        headers = {"X-API-Key": INVENTORY_API_KEY} if INVENTORY_API_KEY is not None else {}
        response = requests.post(FASTAPI_URL, json={"barcode": barcode}, headers=headers, timeout=5)
        if not (200 <= response.status_code < 300):
            logger.warning("POST %s returned HTTP %s", FASTAPI_URL, response.status_code)
    except requests.exceptions.RequestException as e:
        logger.warning("POST %s failed: %s", FASTAPI_URL, e)


def _reconnect():
    delay = RECONNECT_INITIAL_DELAY
    while True:
        time.sleep(delay)
        device = find_scanner()
        if device is not None:
            logger.info("Scanner reconnected")
            return device
        delay = min(delay * RECONNECT_BACKOFF_FACTOR, RECONNECT_MAX_DELAY)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger.info("Inventory listener starting")

    device = find_scanner()
    if device is None:
        logger.warning("Scanner not found at startup, entering reconnect loop")
        device = _reconnect()

    while True:
        try:
            for barcode in read_barcodes(device):
                if is_valid_barcode(barcode):
                    logger.info("Scanned: %s", barcode)
                    post_scan(barcode)
        except OSError as e:
            if e.errno == 19:
                logger.warning("Scanner disconnected")
                device = _reconnect()
            else:
                raise


if __name__ == "__main__":
    main()

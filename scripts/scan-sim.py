#!/usr/bin/env python3
"""
scan_sim.py — Dev tool for simulating barcode scans.

Reads API key from config.local.env in the repo root (or INVENTORY_API_KEY env var).
URL defaults to http://localhost:8000/scan but can be overridden with --url.

Usage:
    python scan_sim.py
    python scan_sim.py --url http://raspberrypi:8000/scan
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_api_key() -> str:
    """Load INVENTORY_API_KEY from config.local.env or the environment."""
    # Try config.local.env relative to this script's location
    env_file = Path(__file__).parent.parent / "config.local.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("INVENTORY_API_KEY="):
                return line.split("=", 1)[1].strip()
    else:
        print("Couldn't find config.local.env")

    # Fall back to environment variable
    key = os.environ.get("INVENTORY_API_KEY", "")
    if key:
        return key

    print("Error: INVENTORY_API_KEY not found in config.local.env or environment.")
    sys.exit(1)


def send_scan(url: str, barcode: str, api_key: str) -> None:
    payload = json.dumps({"barcode": barcode}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
            delta = body.get("session_delta", "?")
            print(f"  ✓  session_delta={delta}")
    except urllib.error.HTTPError as e:
        body = {}
        try:
            body = json.loads(e.read())
        except Exception:
            pass
        error = body.get("error") or body.get("detail") or e.reason
        print(f"  ✗  HTTP {e.code}: {error}")
    except urllib.error.URLError as e:
        print(f"  ✗  Connection error: {e.reason}")
    except TimeoutError:
        print("  ✗  Request timed out")


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate barcode scans against the inventory API.")
    parser.add_argument(
        "--url",
        default="http://localhost:8000/scan",
        help="Scan endpoint URL (default: http://localhost:8000/scan)",
    )
    args = parser.parse_args()

    api_key = load_api_key()
    print(f"Scan simulator → {args.url}")
    print("Enter a barcode and press Enter. Ctrl+C to quit.\n")

    while True:
        try:
            barcode = input("barcode: ").strip()
        except KeyboardInterrupt:
            print("\nBye.")
            break
        except EOFError:
            break

        if not barcode:
            continue

        send_scan(args.url, barcode, api_key)


if __name__ == "__main__":
    main()
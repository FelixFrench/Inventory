import logging
import os
import textwrap
from datetime import datetime

from escpos.printer import Dummy, Network

logger = logging.getLogger(__name__)

QTY_WIDTH = 5
PRICE_WIDTH = 9
HAVE_WIDTH = 5
NEED_WIDTH = 5
SHORT_WIDTH = 5


class PrinterUnavailableError(Exception):
    def __init__(self, message: str, code: str = "printer_unavailable"):
        super().__init__(message)
        self.code = code


def _identifier(name: str | None, brand: str | None, barcode: str) -> str:
    base = name if name else barcode
    return f"{base} ({brand})" if brand else base



def _row(left: str, right: str, cols: int) -> str:
    name_width = cols - len(right)
    return left[:name_width].ljust(name_width) + right


def _get_printer() -> Network:
    host = os.environ.get("PRINTER_IP")
    if not host:
        raise PrinterUnavailableError("Printer IP not configured", code="printer_not_configured")
    try:
        return Network(host, port=9100, timeout=5, profile="TM-T88IV")
    except Exception as e:
        raise PrinterUnavailableError(f"Cannot connect to printer: {e}") from e


def _format_inventory(report_data: dict) -> bytes:
    p = Dummy(magic_encode_args={"disabled": True, "encoding": "CP437"})
    COLS = p.profile.get_columns(font="a")
    divider = "-" * COLS

    now = datetime.now().strftime("%d %b %Y  %H:%M")

    p.set(align="center", bold=True)
    p.text("INVENTORY REPORT\n")
    p.set(align="center", bold=False)
    p.text(f"{now}\n")
    p.text(divider + "\n")

    header_right = f"  {'Qty':>{QTY_WIDTH}}  {'Price':>{PRICE_WIDTH}}"
    p.set(align="left")
    p.text(_row("Product", header_right, COLS) + "\n")
    p.text(divider + "\n")

    items = [item for item in report_data["items"] if item["quantity"] > 0]

    items = sorted(
        items,
        key=lambda it: _identifier(it["name"], it["brand"], "").lower()
    )

    for item in items:
        ident = _identifier(item["name"], item["brand"], "")
        price = f"£{item['price_pence'] / 100:.2f}" if item["price_pence"] is not None else "—"
        right = f"  {item['quantity']:>{QTY_WIDTH}}  {price:>{PRICE_WIDTH}}"
        id_width = COLS - len(right)
        lines = textwrap.wrap(ident, id_width, subsequent_indent=' ')
        p.text(_row(lines[0], right, COLS) + "\n")
        for cont in lines[1:]:
            p.text(cont + "\n")

    p.text(divider + "\n")

    total = report_data["total_value_pence"]
    if total:
        total_str = f"£{total / 100:.2f}"
        p.set(bold=True)
        p.text(_row("TOTAL VALUE", f"  {total_str:>{QTY_WIDTH + PRICE_WIDTH + 2}}", COLS) + "\n")
        p.set(bold=False)
    else:
        p.text("No prices available\n")

    count = len(items)
    p.text(f"{count} item{'s' if count != 1 else ''}\n")
    p.cut()
    return p.output


def _format_low_stock(report_data: dict) -> bytes:
    p = Dummy(magic_encode_args={"disabled": True, "encoding": "CP437"})
    COLS = p.profile.get_columns(font="a")
    divider = "-" * COLS

    now = datetime.now().strftime("%d %b %Y  %H:%M")

    p.set(align="center", bold=True)
    p.text("LOW STOCK REPORT\n")
    p.set(align="center", bold=False)
    p.text(f"{now}\n")
    p.text(divider + "\n")

    header_right = f"  {'Have':>{HAVE_WIDTH}}  {'Need':>{NEED_WIDTH}}  {'Short':>{SHORT_WIDTH}}"
    p.set(align="left")
    p.text(_row("Product", header_right, COLS) + "\n")
    p.text(divider + "\n")

    items = report_data["items"]

    if not items:
        p.set(align="center")
        p.text("All items in stock\n")
        p.cut()
        return p.output

    items = sorted(items, key=lambda it: _identifier(it["name"], it["brand"], "").lower())

    for item in items:
        ident = _identifier(item["name"], item["brand"], "")
        right = (
            f"  {item['quantity']:>{HAVE_WIDTH}}"
            f"  {item['minimum_quantity']:>{NEED_WIDTH}}"
            f"  {item['shortfall']:>{SHORT_WIDTH}}"
        )
        id_width = COLS - len(right)
        lines = textwrap.wrap(ident, id_width, subsequent_indent=' ')
        p.set(align="left")
        p.text(_row(lines[0], right, COLS) + "\n")
        for cont in lines[1:]:
            p.text(cont + "\n")

    p.text(divider + "\n")
    count = len(items)
    p.text(f"{count} item{'s' if count != 1 else ''} below minimum\n")
    p.cut()
    return p.output


def print_inventory(report_data: dict) -> None:
    raw = _format_inventory(report_data)
    p = _get_printer()
    try:
        p._raw(raw)
    except Exception as e:
        raise PrinterUnavailableError(f"Print failed: {e}") from e
    finally:
        try:
            p.close()
        except Exception:
            pass


def print_low_stock(report_data: dict) -> None:
    raw = _format_low_stock(report_data)
    p = _get_printer()
    try:
        p._raw(raw)
    except Exception as e:
        raise PrinterUnavailableError(f"Print failed: {e}") from e
    finally:
        try:
            p.close()
        except Exception:
            pass

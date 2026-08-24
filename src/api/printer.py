import logging
import os
import textwrap
from datetime import datetime

from escpos.exceptions import BarcodeCodeError, BarcodeTypeError
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


# Display bound for any untrusted string reaching the printer. 120 characters is ~6 wrapped
# lines at the 21-column name width, against 259 lines for an unbounded 5000-character name.
# It clears the longest realistic value (an OFF product_name runs to ~120 characters), so it
# only bites values that are already anomalous.
_MAX_PRINT_IDENT_LEN = 120

# C0 controls plus DEL. In an ESC/POS stream these are commands, not text: ESC (0x1B) and
# GS (0x1D) introduce sequences that fire the cash drawer, cut the paper or reset the
# device, and the Dummy/Network printers are built with magic_encode_args={"disabled": True},
# which does no escaping of its own.
_CONTROL_CHARS = dict.fromkeys(list(range(0x00, 0x20)) + [0x7F], " ")


def _safe_ident(text: str) -> str:
    """Make an untrusted display string safe to write into the ESC/POS stream.

    Applied at the only two points where non-literal text enters the stream (the ``ident``
    assignments in ``_format_inventory`` and ``_low_stock_section``) rather than inside
    ``_identifier``: the low-stock groups section passes ``lambda it: it["name"]``, which
    does not go through ``_identifier`` at all, so sanitising there would leave the
    user-supplied group name — the one genuinely user-controlled value on this path —
    unprotected.

    Control characters become spaces (so a name containing a newline still reads as one
    line) and the result is truncated to ``_MAX_PRINT_IDENT_LEN``.
    """
    return str(text).translate(_CONTROL_CHARS)[:_MAX_PRINT_IDENT_LEN]


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
        ident = _safe_ident(_identifier(item["name"], item["brand"], ""))
        price = f"£{item['price_pence'] / 100:.2f}" if item["price_pence"] is not None else "—"
        right = f"  {item['quantity']:>{QTY_WIDTH}}  {price:>{PRICE_WIDTH}}"
        id_width = COLS - len(right)
        # `or [""]` matches _low_stock_section: textwrap.wrap returns [] for a blank or
        # whitespace-only name, and an unguarded lines[0] would raise IndexError.
        lines = textwrap.wrap(ident, id_width, subsequent_indent=' ') or [""]
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


def _low_stock_section(p, title: str, items: list, empty_msg: str, ident_of, COLS: int) -> None:
    """Render one low-stock section (Groups or Products): a title, a have/need/short header,
    one row per entry (name wrapped), or an empty-state line. ``ident_of(item)`` yields the
    display name for the leftmost column."""
    divider = "-" * COLS
    p.set(align="left", bold=True)
    p.text(title + "\n")
    p.set(align="left", bold=False)

    if not items:
        p.text(empty_msg + "\n")
        p.text(divider + "\n")
        return

    header_right = f"  {'Have':>{HAVE_WIDTH}}  {'Need':>{NEED_WIDTH}}  {'Short':>{SHORT_WIDTH}}"
    p.text(_row("Name", header_right, COLS) + "\n")
    for item in items:
        ident = _safe_ident(ident_of(item))
        right = (
            f"  {item['have']:>{HAVE_WIDTH}}"
            f"  {item['need']:>{NEED_WIDTH}}"
            f"  {item['short']:>{SHORT_WIDTH}}"
        )
        id_width = COLS - len(right)
        lines = textwrap.wrap(ident, id_width, subsequent_indent=' ') or [""]
        p.text(_row(lines[0], right, COLS) + "\n")
        for cont in lines[1:]:
            p.text(cont + "\n")
    p.text(divider + "\n")


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

    _low_stock_section(
        p, "GROUPS", report_data["groups"], "No groups below minimum",
        lambda it: it["name"], COLS,
    )
    _low_stock_section(
        p, "PRODUCTS", report_data["products"], "No products below minimum",
        lambda it: _identifier(it["name"], it["brand"], ""), COLS,
    )

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


def _gs1_check_digit_valid(digits: str) -> bool:
    """Standard GS1 mod-10 check digit, used by EAN-8, UPC-A and EAN-13 alike.

    Sum the payload digits (all but the last) from the right, alternating weights
    3 and 1 starting with 3 on the rightmost payload digit; the check digit is
    ``(10 - total % 10) % 10``.
    """
    payload, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(payload)))
    return (10 - total % 10) % 10 == check


def choose_barcode_symbology(barcode: str) -> str:
    """Pick the shortest native symbology whose length and GS1 check digit both match.

    Falls back to CODE128 for any other length, and for a length-matching code
    whose check digit is invalid (e.g. a synthetic stand-in) — CODE128 is always
    scannable regardless of the digit string, which is the property this feature
    depends on.
    """
    length_to_symbology = {8: "EAN8", 12: "UPCA", 13: "EAN13"}
    symbology = length_to_symbology.get(len(barcode))
    if symbology is not None and _gs1_check_digit_valid(barcode):
        return symbology
    return "CODE128"


def _format_product_print(product: dict) -> bytes:
    """Render one product's name/brand/quantity plus a scannable barcode, uncut.

    Meant to be printed alongside other products' fragments onto one uncut strip
    (see ``_format_cut``), so alignment is reset to left up front and a trailing
    blank line separates this fragment from the next one on the same strip.
    """
    p = Dummy(magic_encode_args={"disabled": True, "encoding": "CP437"})
    p.set(align="left", bold=False)

    for field in ("name", "brand", "quantity"):
        value = product.get(field)
        if value:
            p.text(_safe_ident(str(value)) + "\n")

    barcode = product["barcode"]
    symbology = choose_barcode_symbology(barcode)
    payload = f"{{B{barcode}" if symbology == "CODE128" else barcode
    try:
        p.barcode(payload, symbology, height=64, width=3, pos="OFF", align_ct=True)
    except (BarcodeCodeError, BarcodeTypeError) as e:
        logger.warning("Barcode render failed for %s (%s): %s", barcode, symbology, e)

    # The barcode's own HRI is disabled (pos="OFF") so the legible number below is always
    # this explicit, sanitised text line — deterministic regardless of symbology or
    # whether the barcode render above succeeded.
    p.set(align="left", bold=False)
    p.text(_safe_ident(barcode) + "\n")
    p.text("\n")
    return p.output


def _format_cut() -> bytes:
    p = Dummy(magic_encode_args={"disabled": True, "encoding": "CP437"})
    p.cut()
    return p.output


def print_product(product: dict) -> None:
    raw = _format_product_print(product)
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


def print_cut() -> None:
    raw = _format_cut()
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

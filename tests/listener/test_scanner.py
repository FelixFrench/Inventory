"""Tests for src/listener/scanner.py and src/listener/main.py — device and HTTP posting."""

import errno
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import EV_KEY

# Import at module level so @patch can resolve src.listener.main.httpx
import src.listener.main  # noqa: F401
from src.listener.main import post_scan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeEvent:
    def __init__(self, type_, code, value):
        self.type  = type_
        self.code  = code
        self.value = value


def make_events(digits: str) -> list[FakeEvent]:
    char_to_code = {
        '1': 2, '2': 3, '3': 4, '4': 5, '5': 6,
        '6': 7, '7': 8, '8': 9, '9': 10, '0': 11,
    }
    events = [FakeEvent(EV_KEY, char_to_code[ch], 1) for ch in digits]
    events.append(FakeEvent(EV_KEY, 28, 1))  # KEY_ENTER
    return events


# ---------------------------------------------------------------------------
# find_scanner
# ---------------------------------------------------------------------------

def _make_device(vendor, product):
    d = MagicMock()
    d.info.vendor  = vendor
    d.info.product = product
    return d


@patch("src.listener.scanner.evdev.InputDevice")
@patch("src.listener.scanner.evdev.list_devices")
def test_find_scanner_present(mock_list, mock_device_cls):
    dev_a = _make_device(0x9999, 0x9999)
    dev_b = _make_device(0x05e0, 0x1200)
    mock_list.return_value   = ["/dev/input/event0", "/dev/input/event1"]
    mock_device_cls.side_effect = [dev_a, dev_b]

    from src.listener.scanner import find_scanner
    result = find_scanner()

    assert result is dev_b


@patch("src.listener.scanner.evdev.InputDevice")
@patch("src.listener.scanner.evdev.list_devices")
def test_find_scanner_absent(mock_list, mock_device_cls):
    dev_a = _make_device(0x9999, 0x9999)
    mock_list.return_value      = ["/dev/input/event0"]
    mock_device_cls.return_value = dev_a

    from src.listener.scanner import find_scanner
    result = find_scanner()

    assert result is None


@patch("src.listener.scanner.evdev.InputDevice")
@patch("src.listener.scanner.evdev.list_devices")
def test_find_scanner_empty(mock_list, mock_device_cls):
    mock_list.return_value = []

    from src.listener.scanner import find_scanner
    result = find_scanner()

    assert result is None
    mock_device_cls.assert_not_called()


# ---------------------------------------------------------------------------
# read_barcodes
# ---------------------------------------------------------------------------

def test_read_barcodes_single():
    from src.listener.scanner import read_barcodes

    mock_device = MagicMock()
    mock_device.read_loop.return_value = iter(make_events("12345678"))

    result = list(read_barcodes(mock_device))

    assert result == ["12345678"]


def test_read_barcodes_multiple():
    from src.listener.scanner import read_barcodes

    mock_device = MagicMock()
    events = make_events("11111111") + make_events("22222222")
    mock_device.read_loop.return_value = iter(events)

    result = list(read_barcodes(mock_device))

    assert result == ["11111111", "22222222"]


def test_read_barcodes_key_up_ignored():
    from src.listener.scanner import read_barcodes

    mock_device = MagicMock()
    # key-up for '1' (value=0), then key-down for '1' (value=1), then Enter
    events = [
        FakeEvent(EV_KEY, 2, 0),  # key-up — ignored
        FakeEvent(EV_KEY, 2, 1),  # key-down for '1'
        FakeEvent(EV_KEY, 28, 1), # KEY_ENTER
    ]
    mock_device.read_loop.return_value = iter(events)

    result = list(read_barcodes(mock_device))

    assert result == ["1"]


def test_read_barcodes_unknown_scancode_ignored():
    from src.listener.scanner import read_barcodes

    mock_device = MagicMock()
    events = [
        FakeEvent(EV_KEY, 999, 1),  # unknown — ignored
        FakeEvent(EV_KEY, 4,   1),  # '3'
        FakeEvent(EV_KEY, 3,   1),  # '2'
        FakeEvent(EV_KEY, 2,   1),  # '1'
        FakeEvent(EV_KEY, 28,  1),  # KEY_ENTER
    ]
    mock_device.read_loop.return_value = iter(events)

    result = list(read_barcodes(mock_device))

    assert result == ["321"]


def test_read_barcodes_oserror_propagates():
    from src.listener.scanner import read_barcodes

    def _raise_mid():
        yield make_events("12345678")[0]  # one event before disconnect
        raise OSError(errno.ENODEV, "No such device")

    mock_device = MagicMock()
    mock_device.read_loop.return_value = _raise_mid()

    gen = read_barcodes(mock_device)
    with pytest.raises(OSError):
        list(gen)


# ---------------------------------------------------------------------------
# post_scan (imported from main)
# ---------------------------------------------------------------------------

@patch("src.listener.main.INVENTORY_API_KEY", "test-api-key")
@patch("src.listener.main.httpx.post")
def test_post_scan_success(mock_post):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response

    post_scan("12345678")

    mock_post.assert_called_once_with(
        "http://127.0.0.1:8000/scan",
        json={"barcode": "12345678"},
        headers={"X-API-Key": "test-api-key"},
        timeout=5,
    )


@patch("src.listener.main.INVENTORY_API_KEY", None)
@patch("src.listener.main.httpx.post")
def test_post_scan_with_no_api_key(mock_post):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_post.return_value = mock_response

    post_scan("12345678")

    mock_post.assert_called_once_with(
        "http://127.0.0.1:8000/scan",
        json={"barcode": "12345678"},
        headers={},
        timeout=5,
    )


@patch("src.listener.main.httpx.post")
def test_post_scan_network_error(mock_post):
    import httpx
    mock_post.side_effect = httpx.RequestError("connection refused")

    post_scan("12345678")  # must not raise


@patch("src.listener.main.httpx.post")
def test_post_scan_non_2xx(mock_post):
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_post.return_value = mock_response
    with patch("src.listener.main.logger") as mock_logger:
        post_scan("12345678")
    mock_logger.warning.assert_called_once()

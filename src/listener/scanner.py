from __future__ import annotations

import evdev
from evdev import ecodes
from collections.abc import Iterator


SCANCODE_MAP: dict[int, str] = {
    # Top-row digit keys
    2: '1', 3: '2', 4: '3', 5: '4', 6: '5',
    7: '6', 8: '7', 9: '8', 10: '9', 11: '0',
    # Numpad digits
    79: '1', 80: '2', 81: '3', 75: '4', 76: '5',
    77: '6', 71: '7', 72: '8', 73: '9', 82: '0',
}
KEY_ENTER = 28


def find_scanner() -> evdev.InputDevice | None:
    for path in evdev.list_devices():
        device = evdev.InputDevice(path)
        if device.info.vendor == 0x05e0 and device.info.product == 0x1200:
            return device
    return None


def read_barcodes(device: evdev.InputDevice) -> Iterator[str]:
    buf: list[str] = []
    for event in device.read_loop():
        if event.type != ecodes.EV_KEY or event.value != 1:
            continue
        if event.code == KEY_ENTER:
            yield ''.join(buf)
            buf.clear()
        elif event.code in SCANCODE_MAP:
            buf.append(SCANCODE_MAP[event.code])

import sys
from unittest.mock import MagicMock

EV_KEY = 1  # ecodes.EV_KEY on Linux; exported so test files use the integer, not a mock attribute

evdev_mock = MagicMock()
evdev_mock.ecodes.EV_KEY = EV_KEY
sys.modules.setdefault("evdev", evdev_mock)
sys.modules.setdefault("evdev.ecodes", evdev_mock.ecodes)

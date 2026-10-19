"""不发送真实事件，验证 Windows ABI、扫描码与失焦时的输入行为。"""
import ctypes
import sys
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows ABI")

from ai.output.windows_input import (DEFAULT_BINDINGS, INPUT, WindowsInput, keyboard_event)


def fake_backend(focused=True):
    backend = WindowsInput.__new__(WindowsInput)
    backend.bindings = dict(DEFAULT_BINDINGS)
    backend._down = set()
    backend.user32 = Mock()
    backend.user32.SendInput.return_value = 1
    backend.is_game_foreground = Mock(return_value=focused)
    return backend


def test_input_size_matches_python_bitness():
    assert ctypes.sizeof(INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def test_extended_scan_codes_and_keyup_flags():
    down = keyboard_event("right_ctrl", True)
    up = keyboard_event("right_ctrl", False)
    assert down.type == 1 and down.ki.wVk == 0
    assert down.ki.wScan == 0x1D and down.ki.dwFlags == 0x0009
    assert up.ki.dwFlags == 0x000B
    assert keyboard_event("right_shift", True).ki.dwFlags == 0x0008
    assert keyboard_event("right_alt", True).ki.wScan == 0x38
    assert keyboard_event("right_alt", True).ki.dwFlags == 0x0009


def test_defaults_match_local_game_split_keyboard_bindings():
    # 同时解析默认键表与 LogicalToAmbiButton，不能把两个枚举的编号等同：
    # Pickup=A=RightShift，WorkstationInteract=X=RightControl，Dash=B=RightAlt。
    assert DEFAULT_BINDINGS["pickup"] == "right_shift"
    assert DEFAULT_BINDINGS["chop"] == "right_ctrl"


def test_unfocused_does_not_send_keydown_or_unknown_keyup():
    backend = fake_backend(False)
    backend.send("up", True)
    backend.send("up", False)
    backend.user32.SendInput.assert_not_called()
    assert not backend._down


def test_release_our_key_even_after_game_loses_focus():
    backend = fake_backend()
    backend.send("pickup", True)
    backend.is_game_foreground.return_value = False
    backend.send("pickup", False)
    assert backend.user32.SendInput.call_count == 2
    assert not backend._down


def test_failed_send_is_reported_and_not_marked_pressed():
    backend = fake_backend()
    backend.user32.SendInput.return_value = 0
    with pytest.raises(OSError, match="SendInput"):
        backend.send("up", True)
    assert not backend._down

# -*- coding: utf-8 -*-
"""CGEvent controller 的接口契约（不注入真实按键）。

真正的按键注入需要游戏前台 + 辅助功能权限，无法在单测里跑；这里只钉住
"键表覆盖了原语会用到的键"与"接口与 ai/hands.Hands 的调用面一致"。
"""
import pytest

pytest.importorskip("Quartz", reason="CGEvent controller 需要 pyobjc（仅 macOS）")

from ai import controller


def test_key_table_covers_primitive_keys():
    """原语会按的键：方向（移动/转向）、pickup（拾取）、chop（切/洗/喷火）。"""
    for k in ("up", "down", "left", "right", "pickup", "chop"):
        assert k in controller.KEY, "KEY 表缺少 %s" % k


def test_controller_interface_matches_hands():
    """Hands 通过 ctrl 调 tap / press / release / release_all。"""
    for m in ("tap", "press", "release", "release_all"):
        assert callable(getattr(controller.Controller, m, None))

# -*- coding: utf-8 -*-
"""ai.exec.commands 的协议 schema 测试。"""
import pytest

from ai.exec.commands import CMD_NAMES, build_command, parse_result


def test_cmd_names_covers_six():
    assert set(CMD_NAMES) == {"move", "tap", "hold", "wait", "throw", "halt"}


def test_build_move_roundtrip_multi_grid():
    c = build_command(
        "move",
        goal=[15.6, -10.8],
        path=[[14.4, -9.6], [15.6, -10.8]],
        target={"grid": "BalloonBasket", "gx": 2, "gz": -1,
                "n": "ingredient_crate (3)"},
        kind="pickup", dash=True, timeout_ms=8000)
    assert "cmd_id" not in c
    assert c["cmd"] == "move"
    assert c["goal"] == [15.6, -10.8]
    assert c["path"][-1] == [15.6, -10.8]
    assert c["target"]["grid"] == "BalloonBasket"
    assert c["target"]["gx"] == 2 and c["target"]["gz"] == -1
    assert c["target"]["n"] == "ingredient_crate (3)"
    assert c["kind"] == "pickup"
    assert c["dash"] is True
    assert c["timeout_ms"] == 8000


def test_target_without_grid_serializes():
    c = build_command("move", goal=[0.0, 0.0], path=[[0.0, 0.0]],
                      target={"grid": None, "gx": None, "gz": None, "n": "Rice"},
                      kind=None, dash=False, timeout_ms=5000)
    assert "grid" in c["target"] and c["target"]["grid"] is None
    assert c["target"]["n"] == "Rice"
    assert c["kind"] is None


def test_build_rejects_unknown_cmd():
    with pytest.raises(ValueError):
        build_command("teleport")


def test_parse_result_full():
    r = parse_result('{"cmd_id": 7, "ok": true, "why": "stuck", '
                     '"held_after": null, "blocked_cell": [1.2, 3.4]}')
    assert r == {"cmd_id": 7, "ok": True, "why": "stuck",
                 "held_after": None, "blocked_cell": [1.2, 3.4]}


def test_parse_result_missing_keys_default_none():
    r = parse_result('{"cmd_id": 7, "ok": true}')
    assert r["cmd_id"] == 7 and r["ok"] is True
    assert r["why"] is None and r["held_after"] is None and r["blocked_cell"] is None


def test_parse_result_bad_json_raises():
    with pytest.raises(ValueError):
        parse_result("not json")

# -*- coding: utf8 -*-
"""分工表：写入、清空、planner 偏好与动态签名。"""
from types import SimpleNamespace

from ai.dialog import division as D


def test_set_and_preferences_action_level():
    state = D.DivisionState()
    assert state.set("切", D.OWNER_YOU)
    prefs = state.preferences()
    assert len(prefs) == 1
    assert prefs[0].kind == "direct"
    assert prefs[0].weight == D.WEIGHT_DO
    assert prefs[0].scope == frozenset({("Chop", None)})


def test_set_item_level_and_claim():
    state = D.DivisionState()
    assert state.set("切", D.OWNER_PLAYER, "Meat")
    pref = state.preferences()[0]
    assert pref.kind == "claim"
    assert pref.weight == D.WEIGHT_CLAIM
    assert pref.scope == frozenset({("Chop", "meat")})


def test_both_has_no_preference():
    state = D.DivisionState()
    state.set("装盘", D.OWNER_BOTH)
    assert state.preferences() == []
    assert state.snapshot()["both"] == ["plate"]


def test_overwrite_and_clear():
    state = D.DivisionState()
    state.set("切", D.OWNER_PLAYER, "Meat")
    assert state.set("切", D.OWNER_YOU, "Meat") is True
    assert len(state.entries) == 1
    assert state.preferences()[0].kind == "direct"
    assert state.clear("切", "Meat") is True
    assert state.entries == []


def test_clear_action_removes_item_entries():
    state = D.DivisionState()
    state.set("切", D.OWNER_PLAYER, "Meat")
    state.set("切", D.OWNER_YOU, "Lettuce")
    state.set("下锅", D.OWNER_PLAYER)
    assert state.clear("切") is True
    assert [e.action for e in state.entries] == ["下锅"]
    assert state.clear_all() is True
    assert state.entries == []


def test_diff_entries():
    old = D.DivisionState()
    old.set("下锅", D.OWNER_YOU)
    new = D.DivisionState()
    new.set("下锅", D.OWNER_PLAYER)
    new.set("装盘", D.OWNER_YOU)
    diff = D.diff_entries(old, new)
    assert {(row["action"], row["from"], row["to"]) for row in diff} == {
        ("cook", "you", "player"),
        ("plate", None, "you"),
    }


def test_tool_actions_excludes_deliver_wash_and_missing_stations():
    stations = [
        {"t": "chopping"}, {"t": "cooking"}, {"t": "plate_station"},
        {"t": "washing"},
    ]
    assert D.tool_actions(stations) == ["chop", "cook", "plate"]


def test_station_signature_changes_only_when_stations_move():
    a = SimpleNamespace(stations=[
        {"t": "chopping", "grid": "G", "gx": 1, "gz": 2, "x": 1.2, "z": 2.4},
        {"t": "cooking", "grid": "G", "gx": 3, "gz": 4, "x": 3.6, "z": 4.8},
    ])
    b = SimpleNamespace(stations=[
        {"t": "chopping", "grid": "G", "gx": 1, "gz": 2, "x": 1.2, "z": 2.4},
        {"t": "cooking", "grid": "G", "gx": 3, "gz": 4, "x": 3.6, "z": 4.8},
    ])
    c = SimpleNamespace(stations=[
        {"t": "chopping", "grid": "G", "gx": 1, "gz": 2, "x": 1.2, "z": 2.4},
        {"t": "cooking", "grid": "G", "gx": 9, "gz": 4, "x": 10.8, "z": 4.8},
    ])
    assert D.station_signature(a) == D.station_signature(b)
    assert D.station_signature(a) != D.station_signature(c)


def test_validate_entry_rejects_unknown_action_and_owner():
    world = SimpleNamespace(stations=[{"t": "chopping"}])
    ok, reason, _ = D.validate_entry(world, None, None, None,
                                     "deliver", "you")
    assert not ok and "不开放" in reason
    ok, reason, _ = D.validate_entry(world, None, None, None,
                                     "chop", "P1")
    assert not ok and "owner" in reason


def test_validate_entry_rejects_non_choppable_item(monkeypatch):
    monkeypatch.setattr(D, "_reachable", lambda *a, **k: True)
    world = SimpleNamespace(stations=[{"t": "chopping"}])
    kitchen = SimpleNamespace(materials={"Bun": SimpleNamespace(items={"Bun"})})

    class Book(object):
        @staticmethod
        def needs_chopping(item):
            return item.lower() != "bun"

    ok, reason, _ = D.validate_entry(world, None, kitchen, Book(),
                                     "chop", "you", "Bun")
    assert not ok and "不需要切" in reason


def test_validate_entry_warns_when_temporarily_unreachable(monkeypatch):
    monkeypatch.setattr(D, "_reachable", lambda *a, **k: False)
    world = SimpleNamespace(stations=[{"t": "cooking"}])
    ok, reason, warning = D.validate_entry(world, None, None, None,
                                           "cook", "you")
    assert ok and not reason and "够不着" in warning


def test_reach_signature_uses_cost_to(monkeypatch):
    import ai.dialog.help as help_mod
    calls = []

    def fake_cost_to(world, bb, name, station):
        calls.append((name, station["t"]))
        return 0 if name == "Player 2" else None

    monkeypatch.setattr(help_mod, "cost_to", fake_cost_to)
    world = SimpleNamespace(stations=[{"t": "chopping"}, {"t": "cooking"}])
    assert D.reach_signature(world, None) == (
        ("下锅", False, True),
        ("切", False, True),
    )
    assert calls

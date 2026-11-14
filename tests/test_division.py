# -*- coding: utf8 -*-
"""分工表：写入、清空、planner 偏好与动态签名。"""
from types import SimpleNamespace

from ai.dialog import division as D


def test_you_gets_no_bonus():
    """分给你的动作不加分：只靠「别抢玩家的活」来分工。

    加分会让自己的活压过更值钱但没分工的事（组装/送餐/救锅）——真机
    trace-20261109-124818 里 55% 的时间都在切菜，披萨组装不起来。
    """
    state = D.DivisionState()
    assert state.set("切", D.OWNER_YOU)
    assert state.preferences() == []


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
    assert state.preferences() == []
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
    assert D.tool_actions(stations) == ["chop", "cook"]


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


def test_station_signature_ignores_world_coords_on_moving_grid():
    """木筏巡航会让 cooking 站点的 x/z 每帧变化；局部 gx/gz 没变就不该复核。"""
    raw = {"grids": [{"id": "MovingRaft", "moving": True}]}
    a = SimpleNamespace(raw=raw, stations=[
        {"t": "cooking", "grid": "MovingRaft", "gx": 1, "gz": 2,
         "x": -8.0, "z": 2.4},
    ])
    b = SimpleNamespace(raw=raw, stations=[
        {"t": "cooking", "grid": "MovingRaft", "gx": 1, "gz": 2,
         "x": -6.0, "z": 2.4},
    ])
    assert D.station_signature(a) == D.station_signature(b)


def test_station_signature_detects_local_move_on_moving_grid():
    """木筏上的站点真的换了局部格，仍要触发复核。"""
    raw = {"grids": [{"id": "MovingRaft", "moving": True}]}
    a = SimpleNamespace(raw=raw, stations=[
        {"t": "cooking", "grid": "MovingRaft", "gx": 1, "gz": 2,
         "x": -8.0, "z": 2.4},
    ])
    b = SimpleNamespace(raw=raw, stations=[
        {"t": "cooking", "grid": "MovingRaft", "gx": 2, "gz": 2,
         "x": -6.8, "z": 2.4},
    ])
    assert D.station_signature(a) != D.station_signature(b)


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


def test_station_signature_ignores_moving_serving_hatch():
    """3-5 的出餐口挂在移动出餐船上，grid=null；它不该参与分工复核签名。"""
    raw = {"grids": [{"id": "MovingRaft", "moving": True}]}
    a = SimpleNamespace(raw=raw, stations=[
        {"t": "plate_station", "grid": None, "gx": -1, "gz": 8,
         "x": 8.0, "z": 9.0},
    ])
    b = SimpleNamespace(raw=raw, stations=[
        {"t": "plate_station", "grid": None, "gx": -3, "gz": 7,
         "x": 5.0, "z": 8.0},
    ])
    assert D.station_signature(a) == D.station_signature(b)


def test_rapids35_fixture_station_signature_is_stable():
    """真实 3-5 三帧：木筏和出餐船都在动，分工签名不应变。"""
    import gzip
    import json
    import os
    from ai.state.world import World

    path = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                        "rapids35-20261101-180609-deliver.ndjson.gz")
    with gzip.open(path, "rt", encoding="utf8") as fh:
        frames = [json.loads(line) for line in fh if line.strip()]
    sigs = {D.station_signature(World(f)) for f in frames[:3]}
    assert len(sigs) == 1


def test_validate_entry_knows_crates_outside_current_orders(monkeypatch):
    """订单只刷到沙拉时，玩家那侧的土豆/鸡块料箱仍要能认领。

    voice-20261110-221304：ChickenOnly + Salad 开局，模型写 Potato/Chicken，
    校验只看当前订单 DAG，把土豆也报成「本关没有物料」。
    """
    monkeypatch.setattr(D, "_reachable", lambda *a, **k: True)
    world = SimpleNamespace(stations=[
        {"t": "chopping"},
        {"t": "crate", "ing": "Potato"},
        {"t": "crate", "ing": "NuggetChicken"},
        {"t": "crate", "ing": "Lettuce"},
    ])
    # 当前订单只有沙拉：DAG 里没有土豆/鸡块
    kitchen = SimpleNamespace(materials={"Lettuce": SimpleNamespace(items={"Lettuce"})})

    class Book(object):
        @staticmethod
        def needs_chopping(item):
            return item.lower() in ("potato", "nuggetchicken")

    for item in ("Potato", "NuggetChicken"):
        ok, reason, _ = D.validate_entry(world, None, kitchen, Book(),
                                         "chop", "player", item)
        assert ok, (item, reason)

    # 模型写「鸡块」→ Chicken：料箱里唯一子串匹配是 NuggetChicken，归一再校验。
    assert D.resolve_material(world, kitchen, "Chicken") == "NuggetChicken"
    ok, reason, _ = D.validate_entry(
        world, None, kitchen, Book(), "chop", "player",
        D.resolve_material(world, kitchen, "Chicken"))
    assert ok, reason

    # 归不到唯一料箱的名字仍要拒绝，并把可用物料列给模型。
    ok, reason, _ = D.validate_entry(world, None, kitchen, Book(),
                                     "chop", "player", "Chips")
    assert not ok and "NuggetChicken" in reason

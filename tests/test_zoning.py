# -*- coding: utf-8 -*-
"""区域分工：连通块、功能区、目标区与安全区。

3-3 的实测（trace-20261109-011932）：料箱在「竖列 / 横排」之间每 34 秒
换一次，把厨房切成左右或上下两半；站点本身不动，分工也不用改，但**每个人
该待的半场**每 34 秒翻一次。本模块只回答「这一帧每个角色该在哪一半」。
"""
from ai.dialog.division import DIVISION_ACTIONS, DivisionState
from ai.space import zoning
from ai.state.world import World


def _world(walk, stations=(), plates=(), p2=(1, 1), p1=(8, 1)):
    # type: (object, object, object, tuple, tuple) -> World
    """构造一个只用显式可走格集合的 World（不走 terrain/nodeMap 解析）。"""
    state = {
        "phase": "level", "level": "test", "paused": False,
        "chefs": [
            {"name": "Player 2", "x": p2[0], "z": p2[1]},
            {"name": "Player 1", "x": p1[0], "z": p1[1]},
        ],
        "stations": list(stations),
        "plates": list(plates),
        "grid": {"size": 1.2, "origin": {"x": 0, "z": 0}},
        "terrain": [], "occupancy": [], "cookers": [], "items": [],
        "extinguishers": [], "orders": [], "recipes": [], "workstations": [],
    }
    world = World(state)
    cells = set(walk)
    world.walkable = lambda gx, gz: (gx, gz) in cells
    world.to_grid = lambda x, z: (int(x), int(z))
    return world


def _room_split_by_column():
    """10x6 的房间，x=5 整列是墙：左半 / 右半。"""
    walk = set()
    for gx in range(11):
        for gz in range(6):
            if gx == 5:
                continue
            walk.add((gx, gz))
    stations = [
        {"t": "chopping", "n": "boardL", "gx": 1, "gz": -1},
        {"t": "chopping", "n": "boardR", "gx": 7, "gz": -1},
        {"t": "cooking", "n": "ovenL", "gx": 0, "gz": -1},
        {"t": "cooking", "n": "ovenR", "gx": 10, "gz": -1},
        {"t": "washing", "n": "sink", "gx": -1, "gz": 1},
    ]
    plates = [
        {"n": "pL", "x": -1, "z": 3, "count": 0},
        {"n": "pR", "x": 11, "z": 3, "count": 0},
    ]
    return walk, stations, plates


def _room_split_by_row():
    """10x6 的房间，z=3 整行是墙：上 / 下。"""
    walk = set()
    for gx in range(11):
        for gz in range(6):
            if gz == 3:
                continue
            walk.add((gx, gz))
    stations = [
        {"t": "chopping", "n": "boardU", "gx": 1, "gz": 6},
        {"t": "chopping", "n": "boardU2", "gx": 8, "gz": 6},
        {"t": "cooking", "n": "ovenD", "gx": 1, "gz": -1},
        {"t": "washing", "n": "sink", "gx": -1, "gz": 1},
    ]
    plates = [
        {"n": "pD", "x": -1, "z": 2, "count": 0},
        {"n": "pU", "x": 11, "z": 5, "count": 0},
    ]
    return walk, stations, plates


def _regions(**kwargs):
    walk, stations, plates = _room_split_by_column()
    world = _world(walk, stations, plates, **kwargs)
    return zoning.regions(world)


def _observe(tracker, world, division=None, times=None):
    # type: (object, object, object, int) -> object
    """连续观察几次（主循环每秒一次，布局要连续两次才记进历史）。"""
    report = None
    for _ in range(times or zoning.CONFIRM_OBSERVATIONS):
        report = tracker.observe(world, division)
    return report


def _division(pairs):
    # type: (object) -> DivisionState
    state = DivisionState()
    for action, owner in pairs:
        state.set(action, owner)
    return state


def test_regions_split_left_right():
    regions = _regions()
    assert len(regions) == 2
    assert sorted(len(r.cells) for r in regions) == [30, 30]


def test_region_labels_follow_split_axis():
    assert [r.label for r in _regions()] == ["左", "右"]


def test_region_labels_vertical_split():
    walk, stations, plates = _room_split_by_row()
    world = _world(walk, stations, plates)
    regions = zoning.regions(world)
    assert [r.label for r in regions] == ["下", "上"]


def test_region_kinds_use_station_adjacency():
    regions = _regions()
    left, right = regions
    assert left.kinds == frozenset(["board", "oven", "sink", "plate"])
    assert right.kinds == frozenset(["board", "oven", "plate"])


def test_role_kits_default_when_division_empty():
    p2, p1 = zoning.role_kits(None)
    assert p2 == frozenset(["board"])
    assert p1 == frozenset(["oven", "sink"])


def test_role_kits_follow_division():
    p2, p1 = zoning.role_kits(_division([("切", "P2"), ("下锅", "P1")]))
    assert p2 == frozenset(["board"])
    assert p1 == frozenset(["oven", "sink"])


def test_role_kits_only_cover_division_actions():
    assert set(zoning.KIND_BY_ACTION) <= set(DIVISION_ACTIONS)


def test_pick_avoids_the_other_role_region():
    regions = _regions()
    p2_kit, p1_kit = zoning.role_kits(None)
    p1 = zoning.pick(regions, p1_kit)
    p2 = zoning.pick(regions, p2_kit, avoid=p1)
    assert (regions[p1].label, regions[p2].label) == ("左", "右")


def test_pick_returns_none_when_kit_impossible():
    walk = {(gx, gz) for gx in range(3) for gz in range(3)}
    world = _world(walk, [{"t": "chopping", "n": "b", "gx": 2, "gz": 0}])
    regions = zoning.regions(world)
    assert len(regions) == 1
    assert regions[0].kinds == frozenset(["board"])
    assert zoning.pick(regions, frozenset(["oven", "sink"])) is None


def test_tracker_flags_wrong_side():
    walk, stations, plates = _room_split_by_column()
    world = _world(walk, stations, plates, p2=(7, 1), p1=(7, 1))
    tracker = zoning.ZoneTracker()
    report = _observe(tracker, world)
    assert report.district == "LR"
    assert report.p2_label == "右" and report.p1_label == "左"
    assert report.p2_wrong is False
    assert report.p1_wrong is True


def test_tracker_open_window_keeps_safe_cells():
    walk, stations, plates = _room_split_by_column()
    left = set(walk)
    open_walk = set(walk) | {(5, gz) for gz in range(6)}
    tracker = zoning.ZoneTracker()
    world = _world(left, stations, plates, p2=(7, 1), p1=(1, 1))
    first = _observe(tracker, world)
    assert first.p2_wrong is False
    world = _world(open_walk, stations, plates, p2=(5, 1), p1=(5, 1))
    report = _observe(tracker, world)
    assert report.open is True
    assert report.p2_cells == frozenset()
    assert report.safe_p2 == first.p2_cells


def test_safe_cells_are_intersection_of_seen_layouts():
    walk_lr, stations, plates = _room_split_by_column()
    walk_ud, stations_ud, plates_ud = _room_split_by_row()
    tracker = zoning.ZoneTracker()
    lr = _observe(tracker, _world(walk_lr, stations, plates, p2=(7, 1), p1=(1, 1)))
    ud = _observe(tracker, _world(walk_ud, stations_ud, plates_ud, p2=(1, 5), p1=(1, 1)))
    assert lr.district == "LR" and ud.district == "UD"
    assert lr.safe_p2 == lr.p2_cells
    assert ud.safe_p2 == lr.p2_cells & ud.p2_cells
    assert ud.safe_p1 == lr.p1_cells & ud.p1_cells


def test_chef_cell_outside_every_region_is_wrong():
    walk, stations, plates = _room_split_by_column()
    world = _world(walk, stations, plates, p2=(99, 99), p1=(0, 0))
    report = _observe(zoning.ZoneTracker(), world)
    assert report.p2_wrong is True


# ---------- 对话层接线 ----------

def test_layout_review_prompt_tells_model_to_speak():
    """换布局事件必须让模型开口，而不是像 division_review 那样默认闭嘴。"""
    from ai.dialog import prompts
    text = prompts.render_event("layout_review", "玩家在右半边，应该去左半边。")
    assert "speak" in text
    assert "左半边" in text
    assert "set_division" in text


def test_facts_skip_role_already_in_place():
    report = zoning.ZoneReport(
        p2_label="上", p2_current="上", p2_wrong=False,
        p1_label="下", p1_current="上", p1_wrong=True)
    facts = report.facts()
    assert "你在" not in facts
    assert "请让玩家去下半边" in facts


def test_facts_player_line_is_imperative_and_self_line_is_statement():
    """两人都站错时，只有玩家那句是祈使句，避免模型把方向说反。"""
    report = zoning.ZoneReport(
        p2_label="上", p2_current="下", p2_wrong=True,
        p1_label="下", p1_current="上", p1_wrong=True)
    facts = report.facts()
    assert "请让玩家去下半边" in facts
    assert "只说「去下半边」" in facts
    assert "你被隔开了" in facts


def test_layout_review_event_detail_carries_facts():
    from ai.dialog.worker import _event_detail
    detail = _event_detail("layout_review", {"facts": "你在这边，应该去右上。"})
    assert "右上" in detail


def test_tracker_keeps_assignment_when_both_halves_fit():
    """两边都满足装备时不要跟着格数抖动换边（真机目标每秒翻一次）。"""
    walk, stations, plates = _room_split_by_column()
    # 两侧都补上水槽：默认装备（砧板 / 灶+水槽）两边都满足
    stations = list(stations) + [{"t": "washing", "n": "sinkR", "gx": 10, "gz": 1}]
    tracker = zoning.ZoneTracker()
    first = _observe(tracker, _world(walk, stations, plates, p2=(7, 1), p1=(1, 1)))
    # 只少一格（真机里料箱转场时边界就在抖）
    jittered = set(walk) - {(0, 5)}
    second = _observe(tracker, _world(jittered, stations, plates, p2=(7, 1), p1=(1, 1)))
    assert (second.p2_label, second.p1_label) == (first.p2_label, first.p1_label)


def test_no_advice_when_division_cannot_separate_halves():
    """两边只有半边能干活时不给建议：把两个人都赶去同一半没有意义。

    真机 trace-20261109-124818：分工 `下锅=you`、`装盘=player`（装盘不加功能
    区）+ `切=both`，两个人的装备都只剩灶，UD 布局下都只能去下半边。
    """
    walk, stations, plates = _room_split_by_column()
    stations = [
        {"t": "chopping", "n": "boardL", "gx": 1, "gz": -1},
        {"t": "chopping", "n": "boardR", "gx": 7, "gz": -1},
        {"t": "cooking", "n": "ovenL", "gx": 0, "gz": -1},
        {"t": "washing", "n": "sink", "gx": -1, "gz": 1},
    ]
    division = _division([("下锅", "P2"), ("装盘", "P1"), ("切", "BOTH")])
    world = _world(walk, stations, plates, p2=(7, 1), p1=(1, 1))
    report = _observe(zoning.ZoneTracker(), world, division)
    assert report.p2_label == "" and report.p1_label == ""
    assert report.p2_wrong is False and report.p1_wrong is False


def test_facts_always_state_both_sides():
    report = zoning.ZoneReport(
        district="LR", labels=("左", "右"),
        p2_label="右", p2_current="左", p1_label="左", p1_current="左")
    facts = report.facts()
    assert "你在右半边" in facts
    assert "玩家在左半边" in facts

# -*- coding: utf-8 -*-
"""跨地板缝隙识别：缝隙不能走，只能 dash 跳。"""
from ai.space import gaps
from ai.space.navigation import distance_field

CELL = 1.2


class FakeWorld(object):
    def __init__(self, ground, blocking=(), walk=True):
        self.terrain = {9: list(ground)}
        if blocking:
            self.terrain[10] = list(blocking)
        self._walk = walk
        self.raw = {}
        self.edge_blocked = None

    def walkable(self, gx, gz):
        return self._walk(gx, gz) if callable(self._walk) else self._walk

    def to_grid(self, x, z):
        return (int(round(x / CELL)), int(round(z / CELL)))

    def to_world(self, gx, gz):
        return (gx * CELL, gz * CELL)


def _nav(w):
    return gaps.wrap(
        {"is_walkable": w.walkable, "edge_blocked": None,
         "jumps": {}, "jump_cost": {}}, w)


def test_gap_pair_found_between_adjacent_floors():
    # 厨房 x∈[0,2.4]，木筏 x∈[3.0,5.4]，格心 (2,1) 在厨房边缘、(3,1) 在木筏上
    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (3.0, 5.4, 0.0, 2.4)])
    pairs = gaps.gap_pairs(w)
    assert ((2, 1), (3, 1)) in pairs
    assert ((3, 1), (2, 1)) in pairs
    assert 0.5 < pairs[((2, 1), (3, 1))] < 0.7


def test_two_cell_gap_has_dash_jump():
    # 厨房格心 (2,1)=2.4，木筏格心 (4,1)=4.8；中间 (3,1)=3.6 是水面
    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (3.6, 6.0, 0.0, 2.4)],
                  walk=lambda gx, gz: (gx, gz) != (3, 1))
    pairs = gaps.gap_pairs(w)
    assert ((2, 1), (4, 1)) in pairs
    assert 0.9 < pairs[((2, 1), (4, 1))] < 1.5
    nav = _nav(w)
    field = distance_field((2, 1), belt_dir={}, **nav)
    assert (4, 1) in field


def test_wrap_blocks_normal_edge_and_adds_dash_jump():
    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (3.0, 5.4, 0.0, 2.4)])
    nav = _nav(w)
    assert nav["edge_blocked"]((2, 1), (3, 1)) is True
    assert (3, 1) in nav["jumps"].get((2, 1), ())
    field = distance_field((2, 1), belt_dir={}, **nav)
    assert (3, 1) in field


def test_gap_too_wide_is_blocked_without_jump():
    # 两块地板边缘相距 2.3m：隔一格的 (2,1)→(4,1) 也超过 dash 上限
    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (4.7, 7.1, 0.0, 2.4)],
                  walk=lambda gx, gz: gx != 3)
    nav = _nav(w)
    assert nav["edge_blocked"]((2, 1), (4, 1)) is True
    assert (4, 1) not in nav["jumps"].get((2, 1), ())
    field = distance_field((2, 1), belt_dir={}, **nav)
    assert (4, 1) not in field


def test_gap_through_wall_has_no_jump():
    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (3.0, 5.4, 0.0, 2.4)],
                  blocking=[(2.7, 3.3, 0.0, 2.4)])
    nav = _nav(w)
    assert nav["edge_blocked"]((2, 1), (3, 1)) is True
    assert (3, 1) not in nav["jumps"].get((2, 1), ())


def test_path_has_gap():
    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (3.0, 5.4, 0.0, 2.4)])
    assert gaps.path_has_gap(w, [(2, 1), (3, 1)]) is True
    assert gaps.path_has_gap(w, [(1, 1), (2, 1)]) is False


def test_portals_nav_args_include_gap_jump():
    from ai.space import portals

    # 2 格缝：中间 (3,1) 不可走，只有 gaps 的跳边能到 (4,1)
    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (3.6, 6.0, 0.0, 2.4)],
                  walk=lambda gx, gz: gx != 3)
    w.seam_gates = None
    w.portal_map = None
    w.lane_map = None
    w.belt_dir = {}
    nav = portals.nav_args(w)
    field = distance_field((2, 1), belt_dir={}, **nav)
    assert (4, 1) in field


def test_sub_character_crack_is_not_a_gap():
    """0.36m 的停靠缝（Balloon_1_5）人能直接走过，不能当 dash 跳边。"""
    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (2.76, 5.16, 0.0, 2.4)],
                  walk=lambda gx, gz: 0.0 <= gx * CELL <= 5.16
                  and 0.0 <= gz * CELL <= 2.4)
    assert gaps.gap_pairs(w) == {}


def test_no_gap_returns_nav_unchanged():
    """没有缝隙的关卡不能改变寻路参数（门洞/传送门/走廊原样保留）。"""
    w = FakeWorld([(0.0, 5.0, 0.0, 5.0)],
                  walk=lambda gx, gz: 0 <= gx <= 4 and 0 <= gz <= 4)
    base = {"is_walkable": w.walkable, "edge_blocked": None,
            "jumps": {}, "jump_cost": {}}
    assert gaps.wrap(base, w) is base


def test_wrap_preserves_existing_jumps_and_costs():
    """gaps.wrap 要能叠在 lanes._Jumps/_JumpCost 之上，不丢已有跳边。"""
    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (3.0, 5.4, 0.0, 2.4)])
    base_jumps = {(0, 0): [(5, 5)]}
    base_cost = {((0, 0), (5, 5)): 7}
    nav = gaps.wrap({"is_walkable": w.walkable, "edge_blocked": None,
                     "jumps": base_jumps, "jump_cost": base_cost}, w)
    assert (5, 5) in nav["jumps"].get((0, 0), ())
    assert (3, 1) in nav["jumps"].get((2, 1), ())
    assert nav["jump_cost"].get(((0, 0), (5, 5))) == 7

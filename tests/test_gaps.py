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

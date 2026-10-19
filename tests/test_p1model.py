# -*- coding: utf-8 -*-
"""P1Model 单测：只保留实机可用的两层（holding + 站位软代价）。"""
from ai.state import p1model
from ai.state.blackboard import Blackboard


def _make_world(chefs):
    # type: (list) -> object
    """构造测试用 World（只需 chefs 与 mapping.to_grid）。"""
    class FakeMapping:
        def to_grid(self, x, z):
            # type: (float, float) -> tuple
            return (int(x), int(z))

    class FakeWorld:
        def __init__(self, chefs):
            self.chefs = chefs
            self.mapping = FakeMapping()

    return FakeWorld(chefs)


def test_p1model_facts_holding():
    """事实层：直接读 P1 手持。"""
    world = _make_world([
        {"name": "Player 1", "held": "Cucumber", "x": 5.0, "z": 5.0, "yaw": 0.0},
        {"name": "Player 2", "held": None, "x": 10.0, "z": 10.0, "yaw": 0.0}
    ])

    bb = Blackboard()
    p1model.update(world, bb)

    assert bb.p1_model.holding == "Cucumber"


def test_p1_soft_cost_map():
    """P1 周边格子加通行代价。"""
    world = _make_world([
        {"name": "Player 1", "held": "Cucumber", "x": 5.0, "z": 5.0, "yaw": 0.0},
        {"name": "Player 2", "held": None, "x": 10.0, "z": 10.0, "yaw": 0.0}
    ])

    bb = Blackboard()
    p1model.update(world, bb)

    soft_cost = p1model.p1_soft_cost_map(world, bb.p1_model)

    # 周边格子（曼哈顿距离 ≤2）有额外代价
    assert (4, 5) in soft_cost  # 左
    assert (6, 5) in soft_cost  # 右
    assert (5, 4) in soft_cost  # 上
    assert (5, 6) in soft_cost  # 下
    assert (4, 4) in soft_cost  # 左上
    assert (3, 5) in soft_cost  # 左左

    # 所有周边格子代价都是 P1_SOFT_COST
    for cell in soft_cost:
        assert soft_cost[cell] == p1model.P1_SOFT_COST

    # 距离 >2 的格子不在软代价图里
    assert (2, 5) not in soft_cost
    assert (8, 5) not in soft_cost


def test_p1_soft_cost_map_empty_when_p1_not_holding():
    """P1 空手 → 软代价图为空（不影响 AI）。"""
    world = _make_world([
        {"name": "Player 1", "held": None, "x": 5.0, "z": 5.0, "yaw": 0.0},
        {"name": "Player 2", "held": None, "x": 10.0, "z": 10.0, "yaw": 0.0}
    ])

    bb = Blackboard()
    p1model.update(world, bb)

    soft_cost = p1model.p1_soft_cost_map(world, bb.p1_model)

    assert soft_cost == {}

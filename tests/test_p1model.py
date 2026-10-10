# -*- coding: utf-8 -*-
"""P1Model 人类意图推断单测。

重点验证：
  1. 事实层：holding 直接读取，置信度 1.0。
  2. 短程意图：手持 + 移动 → 推断 task，置信度 0.7。
  3. confidence 衰减：无新证据则递减，低于阈值清空。
  4. 站位软代价图：P1 周边格子加通行代价。
"""
import pytest
from ai import p1model
from ai.blackboard import Blackboard


def _make_world(chefs, tick=0):
    # type: (list, int) -> object
    """构造测试用 World。"""
    class FakeWorld:
        def __init__(self, chefs, tick):
            self.chefs = chefs
            self.tick = tick
            self.stations = []
            self.cookers = []

            class FakeMapping:
                def to_grid(self, x, z):
                    # type: (float, float) -> tuple
                    return (int(x), int(z))
            self.mapping = FakeMapping()

    return FakeWorld(chefs, tick)


def test_p1model_facts_holding():
    """事实层：直接读 P1 手持，置信度 1.0。"""
    # 构造：P1 手持 Cucumber
    world = _make_world([
        {"name": "Player 1", "held": "Cucumber", "x": 5.0, "z": 5.0, "yaw": 0.0},
        {"name": "Player 2", "held": None, "x": 10.0, "z": 10.0, "yaw": 0.0}
    ], tick=1)

    bb = Blackboard()
    p1model.update(world, bb)

    # 断言：bb.p1_model.holding == "Cucumber"
    assert bb.p1_model.holding == "Cucumber"


def test_p1model_short_term_intent_interacting():
    """短程意图：P1 正在交互站点 → 推断动作，置信度 1.0。"""
    # 构造：P1 持 Cucumber，正在与 board#1 交互
    world = _make_world([
        {
            "name": "Player 1",
            "held": "Cucumber",
            "x": 5.0,
            "z": 5.0,
            "yaw": 0.0,
            "interactions": {"station": "board#1"}
        },
        {"name": "Player 2", "held": None, "x": 10.0, "z": 10.0, "yaw": 0.0}
    ], tick=1)

    bb = Blackboard()
    p1model.update(world, bb)

    # 断言：confidence == 1.0（正在交互）
    assert bb.p1_model.confidence == 1.0
    # 短程意图应被推断
    assert bb.p1_model.p1_active_task is not None


def test_p1model_short_term_intent_moving():
    """短程意图：P1 手持物且移动 → 推断 task，置信度 0.7。"""
    # 构造：P1 持 Cucumber，第一拍在 (5,5)
    world1 = _make_world([
        {"name": "Player 1", "held": "Cucumber", "x": 5.0, "z": 5.0, "yaw": 0.0},
        {"name": "Player 2", "held": None, "x": 10.0, "z": 10.0, "yaw": 0.0}
    ], tick=1)

    bb = Blackboard()
    p1model.update(world1, bb)

    # 第二拍：P1 移动到 (6,5)
    world2 = _make_world([
        {"name": "Player 1", "held": "Cucumber", "x": 6.0, "z": 5.0, "yaw": 0.0},
        {"name": "Player 2", "held": None, "x": 10.0, "z": 10.0, "yaw": 0.0}
    ], tick=2)

    p1model.update(world2, bb)

    # 断言：confidence == 0.7（移动中）
    assert bb.p1_model.confidence == 0.7
    # 短程意图应被推断
    assert bb.p1_model.p1_active_task is not None


def test_p1model_no_intent_when_stationary():
    """P1 静止 → 不推断动作，只登记事实。"""
    # 构造：P1 持 Cucumber，位置不变，无交互
    world = _make_world([
        {"name": "Player 1", "held": "Cucumber", "x": 5.0, "z": 5.0, "yaw": 0.0},
        {"name": "Player 2", "held": None, "x": 10.0, "z": 10.0, "yaw": 0.0}
    ], tick=1)

    bb = Blackboard()
    p1model.update(world, bb)

    # 断言：只登记事实，不推断意图
    assert bb.p1_model.holding == "Cucumber"
    assert bb.p1_model.p1_active_task is None
    assert bb.p1_model.confidence == 0.0


def test_p1model_confidence_decay():
    """无新证据 → confidence 衰减，低于阈值清空。"""
    # 构造：初始有交互，confidence = 1.0
    world1 = _make_world([
        {
            "name": "Player 1",
            "held": "Cucumber",
            "x": 5.0,
            "z": 5.0,
            "yaw": 0.0,
            "interactions": {"station": "board#1"}
        },
        {"name": "Player 2", "held": None, "x": 10.0, "z": 10.0, "yaw": 0.0}
    ], tick=1)

    bb = Blackboard()
    p1model.update(world1, bb)
    assert bb.p1_model.confidence == 1.0
    assert bb.p1_model.p1_active_task is not None

    # 连续多拍无新证据（静止，无交互）
    # 0.95^25 ≈ 0.277 < 0.3，需要至少 25 次迭代
    for i in range(30):
        world_i = _make_world([
            {"name": "Player 1", "held": "Cucumber", "x": 5.0, "z": 5.0, "yaw": 0.0},
            {"name": "Player 2", "held": None, "x": 10.0, "z": 10.0, "yaw": 0.0}
        ], tick=i+2)
        p1model.update(world_i, bb)

    # 断言：confidence 低于阈值，p1_active_task 被清空
    assert bb.p1_model.confidence < p1model.CONFIDENCE_THRESHOLD
    assert bb.p1_model.p1_active_task is None


def test_p1_soft_cost_map():
    """P1 周边格子加通行代价。"""
    # 构造：P1 在 (5, 5)
    world = _make_world([
        {"name": "Player 1", "held": "Cucumber", "x": 5.0, "z": 5.0, "yaw": 0.0},
        {"name": "Player 2", "held": None, "x": 10.0, "z": 10.0, "yaw": 0.0}
    ], tick=1)

    bb = Blackboard()
    p1model.update(world, bb)

    # 生成软代价图
    soft_cost = p1model.p1_soft_cost_map(world, bb.p1_model)

    # 断言：周边格子（曼哈顿距离 ≤2）有额外代价
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
    # 构造：P1 空手
    world = _make_world([
        {"name": "Player 1", "held": None, "x": 5.0, "z": 5.0, "yaw": 0.0},
        {"name": "Player 2", "held": None, "x": 10.0, "z": 10.0, "yaw": 0.0}
    ], tick=1)

    bb = Blackboard()
    p1model.update(world, bb)

    # 生成软代价图
    soft_cost = p1model.p1_soft_cost_map(world, bb.p1_model)

    # 断言：软代价图为空
    assert soft_cost == {}

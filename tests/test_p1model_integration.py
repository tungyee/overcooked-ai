# -*- coding: utf-8 -*-
"""P1Model 集成测试（Task 2）。

验证 p1model.update() 在主循环中正确调用，并且结果写入黑板。
"""
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

        def to_grid(self, pos):
            # type: (tuple) -> tuple
            return self.mapping.to_grid(pos[0], pos[2])

    return FakeWorld(chefs, tick)


def test_p1model_integration_update_writes_to_blackboard():
    """p1model.update() 将推断结果写入黑板的 p1_model 字段。"""
    w = _make_world(chefs=[
        {"x": 1.0, "z": 2.0, "name": "Player 1", "held": "Tomato"},
        {"x": 3.0, "z": 4.0, "name": "Player 2", "held": None},
    ])
    bb = Blackboard(tick=100)

    # 调用 update
    p1model.update(w, bb)

    # 验证黑板中的 p1_model
    assert hasattr(bb, "p1_model")
    model = bb.p1_model
    assert model.holding == "Tomato"
    # 注意：仅有 holding 不足以推断意图（还需要移动或交互），所以 confidence 为 0
    assert model.confidence == 0.0
    assert model.p1_active_task is None


def test_p1model_integration_confidence_decay_on_no_evidence():
    """无新证据时置信度衰减。"""
    w = _make_world(chefs=[
        {"x": 1.0, "z": 2.0, "name": "Player 1", "held": None},
        {"x": 3.0, "z": 4.0, "name": "Player 2", "held": None},
    ])
    bb = Blackboard(tick=100)

    # 初次推断：无证据
    p1model.update(w, bb)
    assert bb.p1_model.holding is None
    assert bb.p1_model.p1_active_task is None
    assert bb.p1_model.confidence == 0.0

    # 给一些初始置信度（模拟之前有证据）
    bb.p1_model.confidence = 1.0
    bb.p1_model.last_evidence_tick = 100

    # 1 拍后仍无证据，调用一次 update
    bb.tick = 101
    p1model.update(w, bb)

    # 置信度应该衰减一次（1 * 0.95 = 0.95）
    assert abs(bb.p1_model.confidence - 0.95) < 0.01


def test_p1model_integration_soft_cost_computed():
    """soft cost map 可以从 p1_model 计算。"""
    w = _make_world(
        chefs=[
            {"x": 5.0, "z": 3.0, "name": "Player 1", "held": "Tomato", "gx": 5, "gz": 3},
            {"x": 1.0, "z": 1.0, "name": "Player 2", "held": None, "gx": 1, "gz": 1},
        ],
        tick=100
    )
    bb = Blackboard(tick=100)

    # 更新模型
    p1model.update(w, bb)

    # 计算 soft cost
    soft_cost = p1model.p1_soft_cost_map(w, bb.p1_model)

    # P1 在格子 (5, 3)，周围应该有成本
    assert soft_cost[(5, 3)] == p1model.P1_SOFT_COST  # P1 所在格
    assert soft_cost[(4, 3)] == p1model.P1_SOFT_COST  # 相邻格
    assert soft_cost[(5, 2)] == p1model.P1_SOFT_COST

    # 远处格子无惩罚
    assert (1, 1) not in soft_cost
    assert (10, 10) not in soft_cost

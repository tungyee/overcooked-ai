# -*- coding: utf-8 -*-
"""协作过滤单测。

重点验证：
  1. 接力模式：p1_active_task 做分数惩罚，置信度越高惩罚越重。
  2. 分单模式：只留 order_assignment == "P2" 的 task。
  3. 分单模式归属为空 → 退回接力。
  4. 物理冲突检测：P1 正在交互的站点 AI 不可用。
"""
import pytest
from ai import planner
from ai.blackboard import Blackboard, P1Model, Intents
from ai.world import World
from ai import tasks as T
from ai.recipes import RecipeBook


def _make_simple_world():
    # type: () -> World
    """构造简单测试世界。"""
    state = {
        "chefs": [
            {"name": "Player 1", "x": 5.0, "z": 5.0, "held": None},
            {"name": "Player 2", "x": 10.0, "z": 10.0, "held": None}
        ],
        "orders": [
            {"id": "order1", "name": "Salad", "composition": ["Cucumber", "Tomato"], "remaining": 60},
            {"id": "order2", "name": "Soup", "composition": ["Tomato"], "remaining": 50}
        ],
        "stations": [
            {"n": "board1", "t": "board", "gx": 3, "gz": 3, "item": None},
            {"n": "crate_cucumber", "t": "crate", "ing": "Cucumber", "gx": 1, "gz": 1},
            {"n": "crate_tomato", "t": "crate", "ing": "Tomato", "gx": 1, "gz": 2}
        ],
        "cookers": [],
        "plates": [],
        "recipes": [],
        "workstations": [],
        "grid": {"nodeMap": None}
    }
    world = World(state)
    world.tick = 0  # Add tick as attribute
    return world


def test_relay_mode_penalizes_p1_active_task():
    """接力模式：p1_active_task 做分数惩罚，置信度越高惩罚越重。"""
    world = _make_simple_world()
    bb = Blackboard()
    bb.tick = 100

    # 设置 P1 模型：P1 正在切黄瓜（高置信度）
    bb.p1_model.holding = "Cucumber"
    bb.p1_model.p1_active_task = "interacting_with_board1"
    bb.p1_model.confidence = 0.8

    # 设置协作模式为接力
    bb.intents.collab_mode = "relay"

    # 添加第二个砧板
    world.stations.append({"n": "board2", "t": "board", "gx": 5, "gz": 5, "item": None})

    # 创建两个候选任务
    task1 = T.FetchToBoard("order1", ("board1",), "Cucumber")  # 与 P1 冲突（使用 board1）
    task2 = T.FetchToBoard("order2", ("board2",), "Tomato")    # 不冲突（使用 board2）

    candidates = [
        planner.Candidate(task1, [], 10),
        planner.Candidate(task2, [], 10)
    ]

    # 打分
    scored = planner._score_tasks([task1, task2], candidates, world, bb)

    # 验证：task1 应该有 p1_conflict 惩罚
    task1_score, task1_terms = None, None
    task2_score, task2_terms = None, None

    for task, score, terms in scored:
        if task == task1:
            task1_score, task1_terms = score, terms
        elif task == task2:
            task2_score, task2_terms = score, terms

    # task1 应该有冲突惩罚项
    assert "p1_conflict" in task1_terms, "task1 应该有 p1_conflict 项"
    assert task1_terms["p1_conflict"] < 0, "p1_conflict 应该是负数（惩罚）"

    # task2 不应该有冲突惩罚
    assert "p1_conflict" not in task2_terms or task2_terms["p1_conflict"] == 0

    # task1 总分应该低于 task2
    assert task1_score < task2_score, "冲突任务分数应该更低"


def test_relay_mode_no_penalty_when_no_p1_task():
    """接力模式：P1 无 active_task 时不惩罚。"""
    world = _make_simple_world()
    bb = Blackboard()
    bb.tick = 100

    # P1 模型：无 active_task
    bb.p1_model.holding = None
    bb.p1_model.p1_active_task = None
    bb.p1_model.confidence = 0.0

    bb.intents.collab_mode = "relay"

    task1 = T.FetchToBoard("order1", ("board1",), "Cucumber")
    candidates = [planner.Candidate(task1, [], 10)]

    scored = planner._score_tasks([task1], candidates, world, bb)

    _, _, terms = scored[0]

    # 不应该有 p1_conflict 惩罚
    assert "p1_conflict" not in terms or terms["p1_conflict"] == 0


def test_split_mode_filters_by_assignment():
    """分单模式：只留 order_assignment == "P2" 的 task。"""
    world = _make_simple_world()
    bb = Blackboard()
    bb.tick = 100

    # 订单分配：order1 → P1, order2 → P2
    bb.order_assignment["order1"] = "P1"
    bb.order_assignment["order2"] = "P2"

    bb.intents.collab_mode = "split"

    # 创建两个订单的任务
    task1 = T.FetchToBoard("order1", ("board1",), "Cucumber")  # P1 的订单
    task2 = T.FetchToBoard("order2", ("board1",), "Tomato")    # P2 的订单

    # 过滤协作
    feasible = [task1, task2]
    filtered = planner._filter_collaboration(feasible, world, bb)

    # 只应该保留 task2（P2 的订单）
    assert len(filtered) == 1, "应该只保留 P2 的任务"
    assert filtered[0] == task2, "应该是 order2 的任务"


def test_split_mode_fallback_to_relay_when_no_assignment():
    """分单模式归属为空 → 退回接力模式。"""
    world = _make_simple_world()
    bb = Blackboard()
    bb.tick = 100

    # 所有订单归属为空
    bb.order_assignment["order1"] = None
    bb.order_assignment["order2"] = None

    # 或者 order_assignment 为空字典
    bb.order_assignment.clear()

    bb.intents.collab_mode = "split"

    # P1 正在做某事
    bb.p1_model.p1_active_task = "interacting_with_board1"
    bb.p1_model.confidence = 0.8

    task1 = T.FetchToBoard("order1", ("board1",), "Cucumber")  # 与 P1 冲突
    task2 = T.FetchToBoard("order2", ("board1",), "Tomato")

    feasible = [task1, task2]
    filtered = planner._filter_collaboration(feasible, world, bb)

    # 应该退回接力模式，两个任务都保留（协作过滤在打分阶段处理）
    assert len(filtered) == 2, "退回接力模式应该保留所有任务"


def test_relay_mode_conflict_detection_by_resource():
    """接力模式：冲突检测基于资源匹配。"""
    world = _make_simple_world()
    bb = Blackboard()
    bb.tick = 100

    # P1 正在使用 board1
    bb.p1_model.p1_active_task = "interacting_with_board1"
    bb.p1_model.confidence = 1.0

    bb.intents.collab_mode = "relay"

    # 任务使用不同的 board
    world.stations.append({"n": "board2", "t": "board", "gx": 5, "gz": 5, "item": None})

    task1 = T.FetchToBoard("order1", ("board1",), "Cucumber")  # 使用 board1，冲突
    task2 = T.FetchToBoard("order1", ("board2",), "Cucumber")  # 使用 board2，不冲突

    candidates = [
        planner.Candidate(task1, [], 10),
        planner.Candidate(task2, [], 10)
    ]

    scored = planner._score_tasks([task1, task2], candidates, world, bb)

    scores_dict = {task: (score, terms) for task, score, terms in scored}

    # task1 应该有冲突惩罚
    assert "p1_conflict" in scores_dict[task1][1]
    assert scores_dict[task1][1]["p1_conflict"] < 0

    # task2 不应该有冲突惩罚
    assert "p1_conflict" not in scores_dict[task2][1] or scores_dict[task2][1]["p1_conflict"] == 0


def test_confidence_scales_penalty():
    """置信度影响惩罚大小。"""
    world = _make_simple_world()
    bb = Blackboard()
    bb.tick = 100

    bb.p1_model.p1_active_task = "interacting_with_board1"
    bb.intents.collab_mode = "relay"

    task = T.FetchToBoard("order1", ("board1",), "Cucumber")
    candidates = [planner.Candidate(task, [], 10)]

    # 高置信度
    bb.p1_model.confidence = 0.9
    scored_high = planner._score_tasks([task], candidates, world, bb)
    penalty_high = scored_high[0][2].get("p1_conflict", 0)

    # 低置信度
    bb.p1_model.confidence = 0.3
    scored_low = planner._score_tasks([task], candidates, world, bb)
    penalty_low = scored_low[0][2].get("p1_conflict", 0)

    # 高置信度惩罚应该更重
    assert penalty_high < penalty_low, "高置信度应该有更重的惩罚"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

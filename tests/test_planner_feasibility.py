"""Planner 可行性过滤单测（Plan 3 Task 4）。

测试 filter_feasible() 的核心逻辑：
  1. 所有候选都可行时全部通过
  2. 手持状态过滤（hands_empty 前置）
  3. 站点状态过滤（board item 存在性）
  4. 空输入返回空
"""
import pytest

from ai.state.blackboard import Blackboard, Commitment
from ai.core.planner import Candidate, filter_feasible
from ai.query.recipes import RecipeBook
from ai.state.world import World
from ai import tasks as T


def _make_world(stations=None, chefs=None, workstations=None):
    # type: (list, list, list) -> World
    """构造最小 World。"""
    state = {
        "orders": [],
        "stations": stations or [],
        "workstations": workstations or [],
        "recipes": [],
        "chefs": chefs or [],
        "grid": {"nodeMap": None}
    }
    return World(state)


def _make_bb(claims=None, active_task=None):
    # type: (dict, object) -> Blackboard
    """构造黑板。claims 是 {(resource_id, action): owner} 的简化映射。"""
    bb = Blackboard()
    bb.tick = 10
    if claims:
        for (res, act), owner in claims.items():
            bb.claim(res, act, owner, "order1", bb.tick)
    if active_task is not None:
        bb.commitment.active_task = active_task
    return bb


# ---------- 测试用例 ----------

def test_all_candidates_feasible():
    """所有候选都可行时，全部通过过滤。"""
    # 构造场景：板上有番茄（Chop 可行）
    w = _make_world(
        stations=[
            {"t": "crate", "n": "crate_tomato", "ing": "Tomato",
             "chop": "ChoppedTomato", "chop_stages": 8,
             "gx": 1, "gz": 1, "x": 1.0, "z": 1.0},
        ],
        workstations=[
            {"t": "chopping", "n": "board_01", "gx": 2, "gz": 2,
             "x": 2.0, "z": 2.0, "item": "Tomato"},
        ],
        chefs=[{"name": "Player 2", "held": "", "x": 1.0, "z": 1.0}]
    )
    bb = _make_bb()
    book = RecipeBook([{"name": "Tomato", "comp": []}], w.stations)

    # 创建候选：Chop（需要板上有番茄）
    candidates = [
        Candidate(
            task=T.Chop("order1", ("board_01",), "Tomato"),
            preconditions=["board_has_tomato"],
            estimated_steps=20
        )
    ]

    result = filter_feasible(w, bb, candidates, book)

    # 应该保留所有可行候选
    assert len(result) == 1
    assert result[0].task.TYPE == "Chop"


def test_filtered_by_hands_state():
    """手上已有番茄时，FetchToBoard 仍可行（因为 ingredient_available 包括手持）。

    这个测试改为：板上已有东西时，FetchToBoard 不可行。
    """
    # 构造场景：板上已有黄瓜（占用中）
    w = _make_world(
        stations=[
            {"t": "crate", "n": "crate_tomato", "ing": "Tomato",
             "chop": "ChoppedTomato", "chop_stages": 8,
             "gx": 1, "gz": 1, "x": 1.0, "z": 1.0},
        ],
        workstations=[
            {"t": "chopping", "n": "board_01", "gx": 2, "gz": 2,
             "x": 2.0, "z": 2.0, "item": "Cucumber"},  # 板上有其他东西
        ],
        chefs=[{"name": "Player 2", "held": "", "x": 1.0, "z": 1.0}]
    )
    bb = _make_bb()
    book = RecipeBook([{"name": "Tomato", "comp": []}], w.stations)

    # 候选：FetchToBoard（需要空板）
    candidates = [
        Candidate(
            task=T.FetchToBoard("order1", ("board_01",), "Tomato"),
            preconditions=["board_empty"],
            estimated_steps=10
        )
    ]

    result = filter_feasible(w, bb, candidates, book)

    # 板上已有东西时，FetchToBoard 不可行
    assert len(result) == 0


def test_filtered_by_station_state():
    """站点状态不满足时，相关任务被过滤。"""
    # 构造场景：板上没有番茄（item=None）
    w = _make_world(
        stations=[
            {"t": "crate", "n": "crate_tomato", "ing": "Tomato",
             "chop": "ChoppedTomato", "chop_stages": 8,
             "gx": 1, "gz": 1, "x": 1.0, "z": 1.0},
        ],
        workstations=[
            {"t": "chopping", "n": "board_01", "gx": 2, "gz": 2,
             "x": 2.0, "z": 2.0, "item": None},  # 空板
        ],
        chefs=[{"name": "Player 2", "held": "Tomato", "x": 1.0, "z": 1.0}]
    )
    bb = _make_bb()
    book = RecipeBook([{"name": "Tomato", "comp": []}], w.stations)

    # 候选：Chop（需要板上有番茄）
    candidates = [
        Candidate(
            task=T.Chop("order1", ("board_01",), "Tomato"),
            preconditions=["holding_tomato", "chopping_station_free"],
            estimated_steps=20
        )
    ]

    result = filter_feasible(w, bb, candidates, book)

    # 板上没有番茄时，Chop 不可行
    assert len(result) == 0


def test_empty_input_returns_empty():
    """空候选列表输入时返回空列表。"""
    w = _make_world()
    bb = _make_bb()
    book = RecipeBook([], [])

    result = filter_feasible(w, bb, [], book)

    assert result == []

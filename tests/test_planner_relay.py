# -*- coding: utf-8 -*-
"""1-3 关卡 relay 加分的 planner 测试。

验证：relay task 只在 Sushi_1_3_2P 加分，且加分低于端熟锅/送餐主线。
"""
import pytest

from ai import planner
from ai import tasks as T
from ai.blackboard import Blackboard
from ai.world import World


def _world(level):
    # type: (str) -> World
    state = {
        "level": level,
        "chefs": [
            {"name": "Player 1", "x": 5.0, "z": 5.0, "held": None},
            {"name": "Player 2", "x": 10.0, "z": 10.0, "held": None},
        ],
        "orders": [],
        "stations": [],
        "cookers": [],
        "plates": [],
        "recipes": [],
        "workstations": [],
        "items": [],
        "extinguishers": [],
        "grid": {"nodeMap": None},
    }
    return World(state)


def _cand(task):
    return planner.Candidate(task=task, preconditions=[], estimated_steps=10)


def test_relay_1_3_bonus_only_in_1_3():
    world = _world("Sushi_1_3_2P")
    bb = Blackboard()
    cand = _cand(T.PlaceToRelay(item="p1"))
    score, terms = planner._score_candidate(world, bb, cand)
    assert terms["relay_1_3_bonus"] == planner._RELAY_BONUS_1_3["PlaceToRelay"]

    world_other = _world("Sushi_1_4_2P")
    _, terms2 = planner._score_candidate(world_other, bb, cand)
    assert terms2["relay_1_3_bonus"] == 0.0


def test_relay_bonus_below_deliver_and_pressure():
    """relay 是支线：加分必须低于送餐和端熟锅的硬分，主线优先。"""
    for bonus in planner._RELAY_BONUS_1_3.values():
        assert bonus < planner.W_DELIVER_BONUS
        assert bonus < planner.W_PRESSURE

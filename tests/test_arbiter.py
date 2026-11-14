# -*- coding: utf-8 -*-
"""卡住检测单测（原 Arbiter 的火情/模式仲裁已下沉 planner，此处只测 `_is_stuck`）。"""
from ai.core import arbiter
from ai.state.blackboard import Blackboard, StuckScratch
from ai.state.world import World


def _make_world(chefs=None):
    # type: (list) -> World
    state = {
        "phase": "InLevel", "level": "test_level", "paused": False,
        "orders": [], "chefs": chefs or [], "stations": [],
        "recipes": [], "plates": [], "workstations": [],
        "cookers": [], "extinguishers": [], "items": [],
        "grid": {"size": 0.72, "nodeMap": []},
    }
    return World(state)


def _make_chef(name, x, z):
    # type: (str, float, float) -> dict
    return {"name": name, "x": x, "z": z, "held": None}


def test_not_stuck_when_moving():
    """AI 在移动（格子变化）→ 不判定卡住，计数重置为 1。"""
    world = _make_world(chefs=[_make_chef("Player 2", 3.0, 3.0)])
    scratch = StuckScratch()
    scratch.last_cell = (4, 4)   # 上一拍在别的格
    scratch.same_count = 2
    bb = Blackboard()
    bb.scratch = scratch

    assert arbiter._is_stuck(world, bb) is False
    assert scratch.same_count == 1

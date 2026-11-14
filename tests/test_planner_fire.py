# -*- coding: utf-8 -*-
"""火情候选进 planner 正常路径（L_FIRE 顶层）的回归钉。

覆盖：
  1. 着火 → plan() 选 Extinguish（L_FIRE=0 顶层）。
  2. P1 正在灭火 → 不发 Extinguish（不双人抢灭火器）。
  3. 仅糊锅（burning）≠ 着火（onFire）→ 不发 Extinguish（仍走 RescueCooked/L1）。
  4. 火情要压过当前承诺惯性（_apply_commitment_inertia）。
  5. 火情要压过玩家命令 scope。
"""
from ai import tasks as T
from ai.core import planner
from ai.query.recipes import RecipeBook
from ai.state.blackboard import Blackboard
from ai.state.world import World


def _world(cookers=None, extinguishers=None, chefs=None, stations=None):
    # type: (list, list, list, list) -> World
    return World({
        "orders": [],
        "stations": stations or [],
        "recipes": [],
        "chefs": chefs or [
            {"name": "Player 2", "x": 5.0, "z": 5.0, "held": None},
            {"name": "Player 1", "x": 9.0, "z": 9.0, "held": None},
        ],
        "plates": [],
        "workstations": [],
        "cookers": cookers or [],
        "items": [],
        "extinguishers": extinguishers or [],
        "grid": {"nodeMap": None},
    })


def _cooker(name="cooker_01", on_fire=False, burning=False, x=1.0, z=1.0):
    # type: (str, bool, bool, float, float) -> dict
    return {"n": name, "x": x, "z": z, "cooked": False, "isCooking": True,
            "burning": burning, "onFire": on_fire}


def _ext(n="ext_01", x=3.0, z=3.0):
    # type: (str, float, float) -> dict
    return {"n": n, "x": x, "z": z}


_FIRE_STATIONS = [{"t": "cooking", "n": "cooker_01", "gx": 1, "gz": 1}]


def test_plan_chooses_extinguish_when_fire():
    """着火且灭火器可得 → plan() 选 Extinguish，落在 L_FIRE（0）顶层。"""
    w = _world(cookers=[_cooker(on_fire=True, burning=True)],
               extinguishers=[_ext()], stations=_FIRE_STATIONS)
    bb = Blackboard()
    bb.tick = 10
    book = RecipeBook([], _FIRE_STATIONS)

    chosen, record = planner.plan(w, bb, None, book)

    assert chosen is not None and chosen.TYPE == "Extinguish"
    assert record["top_layer"] == 0


def test_no_extinguish_when_p1_near_fire():
    """P1 已经站在着火灶旁 → 不发 Extinguish（别跟 P1 抢）。"""
    w = _world(cookers=[_cooker(on_fire=True, burning=True)],
               extinguishers=[_ext()], stations=_FIRE_STATIONS,
               chefs=[{"name": "Player 2", "x": 5.0, "z": 5.0, "held": None},
                      {"name": "Player 1", "x": 1.5, "z": 1.0, "held": None}])
    bb = Blackboard()
    bb.tick = 10
    book = RecipeBook([], _FIRE_STATIONS)

    _, record = planner.plan(w, bb, None, book)

    assert not any(c[0].TYPE == "Extinguish" for c in record["candidates"])


def test_no_extinguish_for_burning_only():
    """糊锅（burning）但灶没着火（onFire=False）→ 不发 Extinguish。"""
    w = _world(cookers=[_cooker(on_fire=False, burning=True)],
               extinguishers=[_ext()], stations=_FIRE_STATIONS)
    bb = Blackboard()
    bb.tick = 10
    book = RecipeBook([], _FIRE_STATIONS)

    _, record = planner.plan(w, bb, None, book)

    assert not any(c[0].TYPE == "Extinguish" for c in record["candidates"])


def test_extinguish_preempts_command_scope():
    """命令 scope 激活时，着火仍选 Extinguish（抢占命令）。"""
    w = _world(cookers=[_cooker(on_fire=True, burning=True)],
               extinguishers=[_ext()], stations=_FIRE_STATIONS)
    bb = Blackboard()
    bb.tick = 10
    bb.command_scope = frozenset({("WashPlate", None)})
    book = RecipeBook([], _FIRE_STATIONS)

    chosen, record = planner.plan(w, bb, None, book)

    assert chosen is not None and chosen.TYPE == "Extinguish"
    assert not record.get("command_scope_empty")


def test_extinguish_preempts_commitment():
    """火情无条件抢占当前承诺：不因「当前任务不在 L0 候选」而被惯性摁住。"""
    bb = Blackboard()
    bb.commitment.active_task = T.ReturnPot(resources=("cooker_01",))
    scored = [(T.Extinguish(), 0.0, {})]

    chosen = planner._apply_commitment_inertia(scored, _world(), bb, None)

    assert chosen is not None and chosen.TYPE == "Extinguish"

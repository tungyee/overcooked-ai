# -*- coding: utf-8 -*-
"""planner `now` 硬指令闸（Task 2）。

`now` 是「马上去」的硬指令：可行性过滤后、分层打分前加一道闸，命中未过期
now 偏好直接选中对应候选，绕过层选择 / 打分 / 承诺惯性。但 L0/L1/L2 救援
（火 / 快糊 / 刚熟救锅）永远优先——闸让位，由正常分层路径接手。
"""
import pytest

from ai import tasks as T
from ai.core import planner
from ai.core import planner_layers as PL
from ai.core.planner import Candidate
from ai.dialog.schema import Preference
from ai.query.recipes import RecipeBook
from ai.state.blackboard import Blackboard, Commitment
from ai.state.world import World


def _now_pref(*pairs, **kw):
    # type: (tuple, dict) -> Preference
    return Preference(kind="now", owner="P2", weight=20.0,
                      scope=frozenset(pairs), **kw)


def _world(cookers=(), plates=()):
    # type: (tuple, tuple) -> Any
    """最小 world：层判定只读 cookers / plates / orders。"""
    return type("W", (), {
        "cookers": list(cookers),
        "plates": list(plates),
        "orders": [],
        "stations": [],
        "items": [],
        "chefs": [],
        "level": "Sushi_1_4_2P",
    })()


def _cand(task):
    # type: (Any) -> Candidate
    return Candidate(task, [], 15, branch=None)


# ---------- 闸的单元行为 ----------

def test_now_selects_match_over_higher_layer():
    """feasible 里 L3 送餐（不匹配）+ L6 切黄瓜（匹配 now）→ 返回切黄瓜。"""
    deliver = T.DeliverPlate("order1", ("plate1",))
    chop = T.Chop("order1", ("board1",), "Cucumber")
    bb = Blackboard()
    bb.tick = 10
    bb.preferences = [_now_pref((None, "cucumber"))]
    w = _world()
    assert PL.layer_of_task(deliver, w) == PL.L_REDEEM
    assert PL.layer_of_task(chop, w) == PL.L_PREP

    chosen = planner._select_imperative([_cand(deliver), _cand(chop)], w, bb)

    assert chosen is not None
    assert chosen.task == chop


def test_now_yields_to_fire():
    """feasible 里有灭火（L0）→ 让位返回 None，正常分层路径接手。"""
    bb = Blackboard()
    bb.tick = 10
    bb.preferences = [_now_pref((None, "cucumber"))]
    w = _world()
    ext = T.Extinguish()
    assert PL.layer_of_task(ext, w) == PL.L_FIRE

    assert planner._select_imperative([_cand(ext)], w, bb) is None


def test_now_yields_to_rescue():
    """feasible 里有 `_time_pressure >= 1.0` 的盛盘（L2 救锅）→ 让位返回 None。"""
    take = T.TakeCookedToPlate("order1", ("c1", "plate1"), "BoiledSushiRice")
    w = _world(cookers=[{"n": "c1", "cooking_time": 12.0,
                         "cooking_progress": 10.0, "cooked": True}])
    assert planner._time_pressure(take, w) >= 1.0
    assert PL.layer_of_task(take, w) == PL.L_RESCUE

    bb = Blackboard()
    bb.tick = 10
    bb.preferences = [_now_pref((None, "cucumber"))]

    assert planner._select_imperative([_cand(take)], w, bb) is None


def test_now_no_match_returns_none():
    """feasible 无候选命中 now scope → 返回 None。"""
    deliver = T.DeliverPlate("order1", ("plate1",))
    bb = Blackboard()
    bb.tick = 10
    bb.preferences = [_now_pref((None, "sushifish"))]
    w = _world()

    assert planner._select_imperative([_cand(deliver)], w, bb) is None


def test_now_expired_returns_none():
    """now 偏好已到期 → 闸不触发。"""
    chop = T.Chop("order1", ("board1",), "Cucumber")
    bb = Blackboard()
    bb.tick = 101
    bb.preferences = [_now_pref((None, "cucumber"), expires_tick=100)]
    w = _world()

    assert planner._select_imperative([_cand(chop)], w, bb) is None


def test_now_pins_current_commitment_when_it_matches():
    """当前承诺已命中 now 作用域 → 闸把它钉住返回（别让送餐等更高分抢走）。

    只返回 None 的话，普通打分会把 now 任务顶掉（送餐分更高），now 任务永远
    做不成——trace-20261021-145504 的 1-6 段正是 FetchToBoard(Cucumber)↔
    DeliverPlate 逐拍交替、角色来回卡。
    """
    chop = T.Chop("order1", ("board1",), "Cucumber")
    bb = Blackboard()
    bb.tick = 10
    bb.preferences = [_now_pref((None, "cucumber"))]
    bb.commitment = Commitment(chop, 5, 0, 0, 0)  # 已经在做切黄瓜
    w = _world()

    chosen = planner._select_imperative([_cand(chop)], w, bb)
    assert chosen is not None
    assert chosen.task == chop


def test_now_yields_when_committed_task_is_infeasible():
    """当前承诺命中 now 但已不在 feasible → 返回 None，交正常流程。"""
    chop = T.Chop("order1", ("board1",), "Cucumber")
    deliver = T.DeliverPlate("order1", ("plate1",))
    bb = Blackboard()
    bb.tick = 10
    bb.preferences = [_now_pref((None, "cucumber"))]
    bb.commitment = Commitment(chop, 5, 0, 0, 0)  # 承诺在切黄瓜
    w = _world()

    # 黄瓜已不可行（feasible 里只有送餐）→ 闸让位
    assert planner._select_imperative([_cand(deliver)], w, bb) is None


def test_now_still_fires_when_commitment_is_different():
    """当前承诺不命中 now（在做别的料）→ 闸照常抢选匹配候选。"""
    chop = T.Chop("order1", ("board1",), "Cucumber")
    deliver = T.DeliverPlate("order1", ("plate1",))
    bb = Blackboard()
    bb.tick = 10
    bb.preferences = [_now_pref((None, "cucumber"))]
    bb.commitment = Commitment(deliver, 5, 0, 0, 0)  # 在送餐，不在切黄瓜
    w = _world()

    chosen = planner._select_imperative([_cand(chop)], w, bb)
    assert chosen is not None
    assert chosen.task == chop


# ---------- 闸接入 plan() 的端到端行为 ----------

def test_plan_applies_imperative():
    """构造 bb + world 走 plan()：chosen 是匹配候选，record 带 imperative 标记。"""
    state = {
        "orders": [
            {"dish": "Sushi_Cucumber", "composition": ["Cucumber"],
             "id": "order1", "remaining": 60, "lifetime": 130},
        ],
        "stations": [
            {"t": "crate", "n": "crate_cucumber", "ing": "Cucumber",
             "chop": "ChoppedCucumber", "chop_stages": 8, "gx": 1, "gz": 1},
        ],
        "workstations": [{"n": "board_full", "item": "Cucumber"}],
        "recipes": [{"name": "Cucumber", "comp": []}],
        "chefs": [{"name": "Player 2", "held": None, "x": 0.0, "z": 0.0}],
        "plates": [],
        "cookers": [],
        "items": [],
        "extinguishers": [],
        "grid": {"nodeMap": None},
    }
    w = World(state)
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    bb.tick = 10
    bb.preferences = [_now_pref((None, "cucumber"))]

    chosen, record = planner.plan(w, bb, None, book)

    assert chosen is not None
    assert chosen.TYPE == "Chop"
    assert chosen.item == "Cucumber"
    # 闸写入的 trace 标记：证明走的是硬指令闸，而非分层打分路径。
    assert record.get("imperative") == chosen.key
    # 跳过打分：被选中的候选分数恒 0、terms 为空。
    for cand in record["candidates"]:
        if cand[0].key == chosen.key:
            assert cand[1] == 0.0 and cand[2] == {}
            assert cand[5] is True       # chosen 标记

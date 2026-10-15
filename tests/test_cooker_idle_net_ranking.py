# -*- coding: utf-8 -*-
"""填灶权重的净值边界（Ruling 12）。

`W_COOKER_IDLE = 5.6` 字面上大于 `W_PRESSURE * 1.0 = 5.0`，看起来违反了
「填灶不能压过端刚熟的锅」。但 cooker_idle 要扣 eta（净 4.1），而端熟锅的
pressure 分不扣同量 eta —— 所以必须比**算出来的净分**，不是比常量。

比常量查不出这类问题，正是 cf2fc64（手写加法）与 W_DEADLINE_LOOKAHEAD
（从未被引用）那两个 bug 的形状。
"""
import pytest

from ai import planner, tasks as T
from ai.blackboard import Blackboard
from ai.recipes import RecipeBook
from tests.test_fill_cooker_priority import _cooker, _world


def _score(w, tasks):
    bb = Blackboard()
    book = RecipeBook(w.recipes, w.stations)
    cands = [planner.Candidate(t, [], 15) for t in tasks]
    plate_match = planner._match_plates_to_orders(w, bb, book)
    return {t.TYPE: s
            for t, s, _ in planner._score_tasks(tasks, cands, w, bb, book,
                                                plate_match)}


def test_hot_pot_still_beats_fill_on_net_score():
    """端刚熟的锅 > 填空灶，按净分比较。

    W_COOKER_IDLE(5.6) > W_PRESSURE*1.0(5.0) 是故意的：前者扣 eta、后者不扣。
    """
    hot = _cooker("c1", cooked=True, cooking=True, contents=["sushirice"])
    hot["cooking_time"] = 12.0
    hot["cooking_progress"] = 12.0          # 刚熟
    plate = {"n": "p1", "name": "plate", "x": 3.0, "z": 3.0,
             "contents": [], "detail": []}
    w = _world([hot, _cooker("c2")], plates=[plate])
    by = _score(w, [
        T.TakeCookedToPlate("1", ("c1", "p1"), "BoiledSushiRice"),
        T.FillCooker(None, ("c2", ("SushiRice",)), "BoiledSushiRice"),
    ])
    assert by["TakeCookedToPlate"] > by["FillCooker"], by


def test_fill_beats_prep_with_margin():
    """填灶 > 备菜，且余量要够——0.17 分的胜负是噪声级，不算钉住。"""
    w = _world([_cooker("c1"), _cooker("c2")])
    by = _score(w, [
        T.FillCooker(None, ("c1", ("SushiRice",)), "BoiledSushiRice"),
        T.FetchToPlate("1", ("p1",), "Seaweed"),
    ])
    assert by["FillCooker"] - by["FetchToPlate"] > 0.5, by

# -*- coding: utf-8 -*-
"""锅烧糊前瞻走 cooker_match，而不是不存在的 bb.cooker_assignments。

评审发现：bb.cooker_assignments 在 Blackboard 上不存在、全项目无人写入，
所以 _find_cooker_for_order 在生产路径恒返回 None，_deadline_forecast 恒
返回 0.0 —— A1 接上的那个权重乘在一个永远为 0 的系数上。
"""
import pytest

from ai.core import planner
from ai.state.blackboard import Blackboard


def test_blackboard_has_no_cooker_assignments():
    """钉住这个事实本身：这是上面那条 bug 的根因。"""
    assert not hasattr(Blackboard(), "cooker_assignments")


def test_burn_lookahead_fires_via_cooker_match():
    """锅快糊 + 这一趟来不及 → 惩罚为满额。"""
    task = type("T", (), {"TYPE": "TakeCookedToPlate", "order_id": "1"})()
    cand = planner.Candidate(task, [], 200)      # 200 拍，赶不上
    cooker = {"n": "c1", "cooking_time": 12.0, "cooking_progress": 23.0}
    w = type("W", (), {"cookers": [cooker], "orders": [], "stations": [],
                       "plates": [], "items": [], "chefs": []})()
    bb = Blackboard()
    # cooker_match 的形状是 {source_id: order_id}
    pen = planner._deadline_forecast(task, cand, w, bb, None, {"c1": "1"})
    assert pen == 1.0


def test_burn_lookahead_safe_when_plenty_of_time():
    """同一口锅、这一趟来得及 → 不惩罚。"""
    task = type("T", (), {"TYPE": "TakeCookedToPlate", "order_id": "1"})()
    cand = planner.Candidate(task, [], 5)        # 5 拍，来得及
    cooker = {"n": "c1", "cooking_time": 12.0, "cooking_progress": 13.0}
    w = type("W", (), {"cookers": [cooker], "orders": [], "stations": [],
                       "plates": [], "items": [], "chefs": []})()
    assert planner._deadline_forecast(task, cand, w, Blackboard(), None,
                                      {"c1": "1"}) == 0.0

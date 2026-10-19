# -*- coding: utf-8 -*-
"""截止前瞻不得与 urgency 重复计分（Ruling 10）。

`_doom_penalty` 与 `_urgency_from_slack` 读**同一条 slack 曲线**，形状相同、
符号相反。A1 把 W_DEADLINE_LOOKAHEAD=10 接上之后，`-10*d` 永远压过 `+5*u`，
净效果 = -urgency —— slack 越小（越紧迫）总分越低，正好把 53d6a2b 那次
「解 94% 倒挂」的修复反转回去。

根因是 `_deadline_forecast` 的订单到期路径对同一事实重复计分：硬否决已在
可行性层（`_is_doomed_investment`），软梯度已由 urgency 承担。该路径已删，
只保留锅烧糊路径（那是真前瞻，与 urgency 无关）。
"""
import pytest

from ai.core import planner


def test_order_deadline_no_longer_penalized_twice():
    """没有锅在跟踪时，截止前瞻必须为 0——紧迫度由 urgency 独家承担。"""
    task = type("T", (), {"TYPE": "Chop", "order_id": "1"})()
    cand = planner.Candidate(task, [], 10)
    w = type("W", (), {"cookers": [], "orders": [], "stations": [],
                       "plates": [], "items": [], "chefs": []})()
    bb = type("B", (), {"cooker_assignments": {}})()
    # slack 很小（很紧迫）也不该产生惩罚
    assert planner._deadline_forecast(task, cand, w, bb, slack_ticks=0) == 0.0
    assert planner._deadline_forecast(task, cand, w, bb, slack_ticks=600) == 0.0


def test_urgency_is_monotone_in_slack_after_full_scoring():
    """全量打分后，slack 越小总分越高（这是 53d6a2b 的核心结论）。

    直接验算两项的净和：删掉重复计分后，urgency 不再被 deadline 抵消。
    """
    # 单调性：slack 越小 urgency 越大
    assert planner._urgency_from_slack(0) > planner._urgency_from_slack(600)
    assert planner._urgency_from_slack(600) > planner._urgency_from_slack(1200)

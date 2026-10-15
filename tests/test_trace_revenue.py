# -*- coding: utf-8 -*-
"""Trace 收益视图回归（第 0 组 0.1）。

复盘「为什么 miss / 为什么 tips 这么低」时，旧 trace 要自己从 state 里
diff 订单与 score：combo 归零的那一拍没有任何显式标记，`remaining` 跳回
lifetime 才是过期（唯一信号），翻 4577 行才能看出来。本模块钉住一个
`revenue` 摘要字段，让这些事实一眼可读。
"""
import os
import pytest

from ai import trace


@pytest.fixture(autouse=True)
def clean_log():
    trace.clear_log()
    yield
    if os.path.exists(trace.TRACE_PATH):
        os.remove(trace.TRACE_PATH)


def _state(deliveries=0, base=0, tips=0, combo=0, mult=0, left=None, orders=()):
    return {
        "score": {"deliveries": deliveries, "base": base, "tips": tips,
                  "combo": combo, "multiplier": mult},
        "timer": {"server": {"left": left, "limit": 210},
                  "client": {"left": left, "limit": 210}},
        "orders": [dict(o) for o in orders],
    }


RECORD = {"mode_reason": "", "p1": {}, "candidates": []}


def test_revenue_summarises_score_and_timer():
    st = _state(deliveries=2, base=120, tips=10, combo=2, mult=2, left=85)
    trace.emit(RECORD, "Normal", {}, {}, [], 1, st)

    rev = trace.read_log()[0]["revenue"]
    assert rev["deliveries"] == 2
    assert rev["base"] == 120
    assert rev["tips"] == 10
    assert rev["combo"] == 2
    assert rev["multiplier"] == 2
    assert rev["time_left"] == 85


def test_revenue_lists_orders_compactly():
    """每单只留决策/复盘要用的三项，别把整个 composition 再抄一遍。"""
    st = _state(orders=[
        {"id": "3", "dish": "Sushi_Cucumber", "remaining": 31.5, "lifetime": 130,
         "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]},
        {"id": "4", "dish": "Sushi_Fish", "remaining": 70.1, "lifetime": 130,
         "composition": ["Seaweed", "BoiledSushiRice", "SushiFish"]},
    ])
    trace.emit(RECORD, "Normal", {}, {}, [], 1, st)

    orders = trace.read_log()[0]["revenue"]["orders"]
    assert orders == [
        {"id": "3", "remaining": 31.5, "lifetime": 130},
        {"id": "4", "remaining": 70.1, "lifetime": 130},
    ]


def test_revenue_marks_combo_break():
    """combo 归零要有显式事件：这是超时的唯一代价，也是 tips 低的根因。"""
    trace.emit(RECORD, "Normal", {}, {}, [], 1, _state(combo=2, mult=2))
    trace.emit(RECORD, "Normal", {}, {}, [], 2, _state(combo=0, mult=0))

    rows = trace.read_log()
    assert rows[0]["revenue"].get("combo_broke") is not True
    assert rows[1]["revenue"]["combo_broke"] is True
    assert rows[1]["revenue"]["combo_was"] == 2


def test_revenue_does_not_mark_break_on_combo_gain():
    trace.emit(RECORD, "Normal", {}, {}, [], 1, _state(combo=1, mult=1))
    trace.emit(RECORD, "Normal", {}, {}, [], 2, _state(combo=2, mult=2))

    assert trace.read_log()[1]["revenue"].get("combo_broke") is not True


def test_revenue_marks_delivery_with_earned_split():
    """送达那一拍记下 base/tip 增量，省得复盘时自己 diff 相邻帧。"""
    trace.emit(RECORD, "Normal", {}, {}, [], 1,
               _state(deliveries=1, base=60, tips=5, combo=1, mult=1))
    trace.emit(RECORD, "Normal", {}, {}, [], 2,
               _state(deliveries=2, base=120, tips=10, combo=2, mult=2))

    rev = trace.read_log()[1]["revenue"]
    assert rev["delivered"] is True
    assert rev["base_gain"] == 60
    assert rev["tip_gain"] == 5


def test_revenue_marks_expiry_by_remaining_rebound():
    """订单过期的唯一信号是 remaining 跳回 lifetime（id 会被复用）。"""
    o_live = {"id": "3", "dish": "Sushi_Cucumber", "remaining": 0.1, "lifetime": 130}
    o_new = {"id": "3", "dish": "Sushi_Cucumber", "remaining": 129.9, "lifetime": 130}
    trace.emit(RECORD, "Normal", {}, {}, [], 1, _state(orders=[o_live]))
    trace.emit(RECORD, "Normal", {}, {}, [], 2, _state(orders=[o_new]))

    rows = trace.read_log()
    assert rows[0]["revenue"].get("expired") in (None, [])
    assert rows[1]["revenue"]["expired"] == ["3"]


def test_revenue_absent_without_state():
    """没有 state（暂停/非关卡那条）不要凭空造 revenue 字段。"""
    trace.emit(RECORD, "Normal", {}, {"status": "skipped"}, [], 1)
    assert trace.read_log()[0].get("revenue") in (None, {})


def test_revenue_survives_partial_frames():
    """残帧缺 score/timer/orders 时不炸，只给能给的。"""
    trace.emit(RECORD, "Normal", {}, {}, [], 1, {"orders": []})
    rev = trace.read_log()[0].get("revenue") or {}
    assert rev.get("deliveries") in (None, 0)

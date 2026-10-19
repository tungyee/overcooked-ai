# -*- coding: utf-8 -*-
"""截止前瞻的权重（spec §10）。

W_DEADLINE_LOOKAHEAD = 10.0 定义后从未被引用：_score_candidate ⑩ 直接用
-_deadline_forecast(...)，而后者返回 0~1，于是这一项的实际量级是 1.0 而非
10.0，比 W_COOKER_IDLE=4.8 小一个数量级。

和 cf2fc64 修掉的手写加法同一个病根：trace 里有这一项但它不影响选择，而单测
断言的是 terms、一个都不会报警。

**Ruling 14（评审发现）**：原来这里的两个测试都不会注意到「这一项在生产路径
恒为 0」——一个是纯常量算术（`W * _doom_penalty(0)`，没有调用 `_score_candidate`，
不会碰到 `_find_cooker_for_order` 恒返回 None 这件事），另一个是 grep 源码字符串
（只要字面出现 `W_DEADLINE_LOOKAHEAD` 就通过，不管它乘的系数是不是恒为 0）。
正是它们自己 docstring 引用的 `cf2fc64` 同款失效：断言的是「样子对不对」而不是
「算出来的值对不对」。改成走 `_score_candidate` 实算。
"""
from ai.core import planner
from ai.state.blackboard import Blackboard


def test_deadline_term_is_scaled_by_its_weight_when_scored():
    """走 _score_candidate 实算：锅快糊 + 这一趟赶不上，该项必须是 -10.0。

    与 tests/test_deadline_burn_path.py 同款局面（评审验证过它能真正命中
    cooker_match）：不比常量算术，比 _score_candidate 吐出来的 terms。
    """
    task = type("T", (), {"TYPE": "TakeCookedToPlate", "order_id": "1"})()
    cand = planner.Candidate(task, [], 200)      # 200 拍，赶不上
    cooker = {"n": "c1", "cooking_time": 12.0, "cooking_progress": 23.0}
    w = type("W", (), {"cookers": [cooker], "orders": [], "stations": [],
                       "plates": [], "items": [], "chefs": [],
                       "level": "Sushi_1_4_2P", "recipes": {}})()
    bb = Blackboard()
    score, terms = planner._score_candidate(w, bb, cand, None, None, None,
                                             {"c1": "1"})
    assert terms["deadline_lookahead"] == -10.0, terms

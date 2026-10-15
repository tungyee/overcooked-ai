# -*- coding: utf-8 -*-
"""送餐按 credited order 求值（spec §10）。

游戏把一盘菜记给「同 dish 存活单里 remaining 最小」的那个（实测 55/0 无反例），
与 AI 把 task 绑给哪个 order_id 无关。所以同一盘菜绑给不同 order_id 必须算出
**相同**的分数——否则 trace 上的 order_id 变好看了而实机行为一字不差，纯属自欺。

_score_candidate 的注释点名「紧迫度 / 新鲜度 / 完成度」三项都该按 credited
order 求值，但完成度那一项曾经漏了（它调的 _order_completion_ratio 内部读
task.order_id）。

**Ruling 14（评审发现）**：原来的测试直接调
`_order_completion_ratio(order_id="2")`，只验证「新参数能传得通」，没有覆盖
A2 真正要修的 bug（第 ⑦ 项调用点要不要传 `effective_order_id`）。把
`ai/planner.py:1070` 的 `effective_order_id = task.order_id` 临时改成绕过
credited-order 逻辑后，旧测试仍然通过（因为它根本没走到 `_score_candidate`）。
改成走 `_score_candidate` 整条路径，对两个分别绑 order#1 / order#2 的
`DeliverPlate` 打分，断言两者的 `terms["completion_ratio"]` 相等——我已验证：
临时把第 ⑦ 项的 `effective_order_id` 换回 `task.order_id`（禁用 credited
override）后，这个新测试会看到 0.0 vs 0.75 并翻红；改回来后恢复绿。
"""
from ai import planner
from ai import tasks as T
from ai.blackboard import Blackboard


def test_completion_ratio_uses_credited_order():
    """两单同 dish：#1 remaining 大、#2 remaining 小 → 游戏记给 #2。

    两个 DeliverPlate 分别绑给 #1 和 #2，走完整的 _score_candidate，
    completion_ratio 这一项必须相同——因为实机送出去那一盘菜的归属与 task
    绑谁无关。
    """
    orders = [
        {"id": "1", "dish": "Sushi", "remaining": 100.0, "lifetime": 130.0,
         "composition": ["BoiledSushiRice", "Seaweed"]},
        {"id": "2", "dish": "Sushi", "remaining": 20.0, "lifetime": 130.0,
         "composition": ["BoiledSushiRice", "Seaweed"]},
    ]
    # 游戏会记给 #2（remaining 最小）
    assert planner._credited_order_id(orders, "Sushi") == "2"

    # 盘里已有一份海苔：空盘两种口径算出的完成度都是 0，不会暴露 bug。
    plate = {"n": "plate1", "x": 1.0, "z": 1.0,
             "contents": ["seaweed"], "detail": ["ing:Seaweed"]}
    w = type("W", (), {
        "orders": orders, "plates": [plate],
        "cookers": [], "stations": [], "items": [], "chefs": [],
        "recipes": {}, "level": "Sushi_1_4_2P", "raw": {},
    })()
    bb = Blackboard()
    plate_match = {"plate1": "2"}

    terms_by_oid = {}
    for oid in ("1", "2"):
        task = T.DeliverPlate(oid, ("plate1",))
        cand = planner.Candidate(task, [], 5)
        _, terms = planner._score_candidate(w, bb, cand, None, plate_match)
        terms_by_oid[oid] = terms["completion_ratio"]

    assert terms_by_oid["1"] == terms_by_oid["2"], terms_by_oid

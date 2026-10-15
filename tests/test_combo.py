# -*- coding: utf-8 -*-
"""Combo 建模回归（第 3 组 3.3/3.4）。

旧 `_is_combo_continuation` 判的是「连续送同类菜」（比较 order["name"]），
和游戏机制无关——本局 1→2 顺序对、combo 爬到 2，然后 3/4/5 过期、跳到 6、7，
两次送达 combo 都是 0，16 的 tips 里一分连击红利都没有。

真实机制（[Overcooked Wiki: Combos] + 57 次送达实测）：**按屏幕队首依次送出**
才累加，跳单或让单过期归零。

队首的判据是**订单 id 的数值最小**，而不是 remaining 最小。两个假设在全部
9 局 57 次送达上对比：
    A) remaining 最小（存活最久）→ 43 一致 / 14 不一致
    B) id 数值最小（UI 槽位序）  → 57 一致 /  0 不一致
B 是唯一无反例的解释。两者会分歧是因为订单过期后 id 会被复用到新单上，此时
「id 小」的那单反而 remaining 很大（例：trace-20261014-120406 t=3000 的
存活单是 #3 rem=117.9、#4 rem=127.9、#5 rem=7.9——队首是 #3 而非 #5）。
"""
import pytest

from ai import economy as ECON
from ai import planner
from ai.blackboard import Blackboard


def _orders(*spec):
    """spec: (id, remaining) ..."""
    return [{"id": i, "dish": "Sushi_Cucumber", "remaining": r, "lifetime": 130,
             "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]}
            for i, r in spec]


# ---------- 队首判定 ----------

def test_queue_head_is_lowest_numeric_id():
    assert planner._combo_head_id(_orders(("3", 51.5), ("4", 61.5),
                                         ("5", 71.5))) == "3"


def test_queue_head_ignores_remaining():
    """真实帧 t=3000：#5 只剩 7.9s 但队首是 #3（id 复用后 remaining 会反序）。"""
    assert planner._combo_head_id(_orders(("3", 117.9), ("4", 127.9),
                                         ("5", 7.9))) == "3"


def test_queue_head_handles_id_reuse_wrap():
    """真实帧 t=3300：#7 存活、#8 是新单，队首仍是 id 最小的 #3。"""
    assert planner._combo_head_id(_orders(("3", 101.9), ("4", 111.9),
                                         ("5", 121.9), ("7", 44.9),
                                         ("8", 128.3))) == "3"


def test_queue_head_none_without_orders():
    assert planner._combo_head_id([]) is None


def test_queue_head_tolerates_non_numeric_ids():
    """单测/异常帧里 id 可能是 "order1" 这种，不能抛异常。"""
    odd = [{"id": "order1", "remaining": 50}, {"id": "order2", "remaining": 20}]
    assert planner._combo_head_id(odd) is not None


# ---------- 打分：队首加分 ----------

def _score_deliver(world, bb, order_id, book=None):
    from ai import tasks as T
    cand = planner.Candidate(T.DeliverPlate(order_id, ("p1",)), [], 5)
    return planner._score_candidate(world, bb, cand, book)


class FakeWorld(object):
    def __init__(self, orders, combo=0):
        self.orders = orders
        self.level = "Sushi_1_4_2P"
        self.chefs = []
        self.plates = []
        self.cookers = []
        self.items = []
        self.stations = []
        self.recipes = []
        self.workstations = []
        self.raw = {"score": {"combo": combo}}
        self.score = {"combo": combo}


def _mixed(*spec):
    """spec: (id, remaining, dish) —— 需要区分菜品的场景。"""
    comp = {"Sushi_Cucumber": ["Seaweed", "BoiledSushiRice", "Cucumber"],
            "Sushi_Fish": ["Seaweed", "BoiledSushiRice", "SushiFish"]}
    return [{"id": i, "dish": dsh, "remaining": r, "lifetime": 130,
             "composition": comp[dsh]} for i, r, dsh in spec]


def test_credit_goes_to_lowest_remaining_of_that_dish():
    """送一盘菜，游戏记给「同 dish 存活单里 remaining 最小」的那个。

    **这是实测事实，不是我们能选的**（57 次送达里 55 个同 dish 多单的样本，
    55/0 无反例）。它决定了 combo 只能靠"选菜"去影响，不能靠"选订单"。
    """
    orders = _mixed(("3", 100.0, "Sushi_Cucumber"),
                    ("4", 20.0, "Sushi_Cucumber"),
                    ("5", 50.0, "Sushi_Fish"))
    assert planner._credited_order_id(orders, "Sushi_Cucumber") == "4"
    assert planner._credited_order_id(orders, "Sushi_Fish") == "5"


def test_combo_bonus_when_the_dish_credits_the_head():
    """送的菜会记在队首头上 → 连击接得上，给加分。"""
    # 队首 #3 是黄瓜，且它就是黄瓜里 remaining 最小的 → 送黄瓜记给 #3
    w = FakeWorld(_mixed(("3", 30.0, "Sushi_Cucumber"),
                         ("4", 60.0, "Sushi_Fish")))
    bb = Blackboard()
    _, cuc = _score_deliver(w, bb, "3")
    assert cuc["combo_head"] > 0
    assert cuc["combo_break"] == 0


def test_combo_break_when_the_dish_credits_someone_else():
    """送的菜会记给别人 → 连击断，罚。

    本局 #6、#7 都是这种情况，各只拿 3 分小费；接上连击本可以是 6、9。
    """
    w = FakeWorld(_mixed(("3", 30.0, "Sushi_Cucumber"),
                         ("4", 60.0, "Sushi_Fish")))
    bb = Blackboard()
    _, fish = _score_deliver(w, bb, "4")      # 送鱼 → 记给 #4，不是队首 #3
    assert fish["combo_head"] == 0
    assert fish["combo_break"] < 0


def test_order_id_of_the_task_does_not_change_combo():
    """同一种菜绑给哪个 order_id 都一样——combo 只看 dish。

    这条钉住上一版的错误：曾按 task.order_id == head 给加分，离线复验
    "t=3226 改选 DeliverPlate(order#3)" 看似修好了，但盘里装的是黄瓜，游戏
    照样记给同 dish 里 remaining 最小的单，实际行为与旧版一字不差——只是
    trace 上的 order_id 变好看了，自欺欺人。
    """
    w = FakeWorld(_mixed(("3", 30.0, "Sushi_Cucumber"),
                         ("6", 80.0, "Sushi_Cucumber")))
    bb = Blackboard()
    _, as3 = _score_deliver(w, bb, "3")
    _, as6 = _score_deliver(w, bb, "6")
    assert as3["combo_head"] == as6["combo_head"]
    assert as3["combo_break"] == as6["combo_break"]


def test_no_break_penalty_when_head_is_doomed():
    """队首已判死（救不回来）时，跳过它不该再罚——combo 注定要断。"""
    w = FakeWorld(_orders(("3", 51.5), ("4", 61.5)))
    bb = Blackboard()
    bb.tick = 0
    bb.doomed_orders["3"] = 100
    _, tail = _score_deliver(w, bb, "4")
    assert tail["combo_break"] == 0


def test_combo_bonus_grows_with_current_combo():
    """连击越高，保住它越值钱：combo 2→3 系数翻倍，远超一次备菜。"""
    orders = _orders(("3", 51.5), ("4", 61.5))
    bb = Blackboard()
    low = _score_deliver(FakeWorld(orders, combo=0), bb, "3")[1]["combo_head"]
    high = _score_deliver(FakeWorld(orders, combo=2), bb, "3")[1]["combo_head"]
    assert high > low


def test_whole_delivery_score_is_invariant_to_bound_order_id():
    """同一盘菜绑给哪个同 dish 的 order_id，**总分**都必须一样。

    不只是 combo 两项——紧迫度/新鲜度/完成度都该按游戏实际记账的那一单求值。
    否则 planner 会在一堆等价候选里挑出"看起来分高"的那个 order_id，trace 上
    像是做了决策，实机行为却完全相同（离线复验曾出现 t=1365 改选 order#5、
    t=3518 改选 order#8，全是这种假决策）。
    """
    w = FakeWorld(_mixed(("3", 30.0, "Sushi_Cucumber"),
                         ("6", 80.0, "Sushi_Cucumber"),
                         ("7", 120.0, "Sushi_Cucumber")))
    bb = Blackboard()
    scores = [_score_deliver(w, bb, oid)[0] for oid in ("3", "6", "7")]
    assert max(scores) - min(scores) < 1e-9, scores


def test_combo_signal_reaches_prep_tasks():
    """combo 必须影响**备菜**，否则这套加分几乎是空转。

    离线复验发现：本局 4577 拍里「料齐的盘」数量分布是 0 盘 4279 拍、1 盘
    298 拍，**从没有两盘同时料齐**。也就是说送餐那一刻根本没有选择——
    `DeliverPlate` 上的 combo 加分在实机里永远碰不到分叉。

    真正的分叉在上游：现在该给哪一单备菜。所以 combo 偏好要传导到备菜类
    task（权重打折，见 COMBO_PREP_DISCOUNT），让 P2 优先把队首那道菜做出来。
    """
    from ai import tasks as T
    w = FakeWorld(_mixed(("3", 30.0, "Sushi_Cucumber"),
                         ("4", 60.0, "Sushi_Fish")))
    bb = Blackboard()
    cuc = planner.Candidate(T.FetchToPlate("3", ("p1",), "Cucumber"), [], 10)
    fish = planner.Candidate(T.FetchToPlate("4", ("p2",), "SushiFish"), [], 10)
    _, t_cuc = planner._score_candidate(w, bb, cuc)
    _, t_fish = planner._score_candidate(w, bb, fish)
    assert t_cuc["combo_head"] > t_fish["combo_head"]


def test_prep_combo_bonus_is_discounted_vs_delivery():
    """备菜的 combo 偏好要比送餐弱：备菜离兑现还远，不该盖过救锅/填灶。"""
    from ai import tasks as T
    w = FakeWorld(_mixed(("3", 30.0, "Sushi_Cucumber"),
                         ("4", 60.0, "Sushi_Fish")))
    bb = Blackboard()
    _, deliver = _score_deliver(w, bb, "3")
    prep = planner.Candidate(T.FetchToPlate("3", ("p1",), "Cucumber"), [], 10)
    _, prep_terms = planner._score_candidate(w, bb, prep)
    assert 0 < prep_terms["combo_head"] < deliver["combo_head"]
    assert 0 < planner.COMBO_PREP_DISCOUNT < 1.0


def test_prep_combo_not_applied_to_order_independent_tasks():
    from ai import tasks as T
    w = FakeWorld(_mixed(("3", 30.0, "Sushi_Cucumber")))
    bb = Blackboard()
    fill = planner.Candidate(
        T.FillCooker(None, ("c1", ("SushiRice",)), "BoiledSushiRice"), [], 15)
    _, terms = planner._score_candidate(w, bb, fill)
    assert terms["combo_head"] == 0.0
    assert terms["combo_break"] == 0.0


def test_combo_term_dominates_freshness_term():
    """量级关系：combo 必须显著高于新鲜度，不能在同一量级上互相抵消。

    依据（ai/economy.py 实测）：新鲜度最多差 5 分（3→8，相对 base 60 仅 8%），
    而 combo 到 3 就系数翻倍。队首不新鲜、后面某单很新鲜时，**combo 赢**。
    """
    assert planner.W_COMBO_HEAD > planner.W_FRESHNESS * 2


# ---------- 打分：新鲜度 ----------

def test_freshness_bonus_tracks_tier():
    w = FakeWorld(_orders(("3", 120.0)))      # 高档（>2/3 lifetime）
    bb = Blackboard()
    _, fresh = _score_deliver(w, bb, "3")
    w2 = FakeWorld(_orders(("3", 20.0)))      # 低档（<1/3）
    _, late = _score_deliver(w2, bb, "3")
    assert fresh["freshness"] > late["freshness"]


def test_freshness_only_applies_to_delivery():
    """只有送餐才谈新鲜度：备菜快慢不直接决定小费档位。"""
    from ai import tasks as T
    w = FakeWorld(_orders(("3", 120.0)))
    bb = Blackboard()
    cand = planner.Candidate(T.FetchToPlate("3", ("p1",), "Seaweed"), [], 10)
    _, terms = planner._score_candidate(w, bb, cand)
    assert terms["freshness"] == 0.0


def test_freshness_is_small_relative_to_delivery_bonus():
    """新鲜度是顺带收，不是目标：不能让它盖过"把盘送出去"本身。"""
    assert planner.W_FRESHNESS < planner.W_DELIVER_BONUS / 2


def test_old_same_dish_combo_heuristic_is_gone():
    """旧的「连续送同类菜」判据必须删除，否则两套语义并存互相干扰。"""
    assert not hasattr(planner, "_is_combo_continuation")

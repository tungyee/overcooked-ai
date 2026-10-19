# -*- coding: utf-8 -*-
"""填灶权重回归（第 1 组 1.4）。

灶空转的机会成本是 12 秒不可压缩的时钟，补不回来——所以填灶必须压过备菜
类候选（FetchToPlate / FetchToBoard），但不能压过送餐和端熟锅（锅会烧糊、
盘不会）。实测 trace-20261014-120406 的 1700~3270 窗口三口灶 59% 空转，
而同期 planner 一直在摆海苔、切鱼。
"""
from ai.core import planner
from ai.core.planner import W_COOKER_IDLE, W_DELIVER_BONUS, W_PRESSURE


def test_weight_beats_prep_but_loses_to_delivery_and_hot_pot():
    """量级关系是这条权重的全部意义，单独钉住，免得后续调参破坏。

    **备菜上限的口径已修正（Ruling 13）**：原先写「备菜上限 =
    async_resource(2.0) + completion_ratio(1.0) = 3.0」，漏掉了 urgency。
    实测备菜合计是 **4.97**（urgency 3.72 + ingredient_reuse 1.0 +
    combo_head 0.25，灶全空 3 单要米）。那个 3.0 的模型在 53d6a2b 把 urgency
    从「中段恒 ~0.1」改成「中段 3~4 分」时就失效了，只是当时 urgency 又被
    deadline_lookahead 以同形曲线反向抵消（见 _deadline_forecast 已移除的 ②），
    净效果接近 0，所以这条断言一直没翻红。

    「填灶不能压过端刚熟的锅」这条**改为比净分**，见
    tests/test_cooker_idle_net_ranking.py::test_hot_pot_still_beats_fill_on_net_score
    （实测 7.47 vs 2.23）。常量比较在这里不成立且会误导：W_COOKER_IDLE=5.6 >
    W_PRESSURE=5.0，但 cooker_idle 要扣 eta 而 pressure 不扣。
    """
    prep_ceiling = planner.W_ASYNC_RESOURCE + planner.W_COMPLETION_RATIO
    assert W_COOKER_IDLE > prep_ceiling
    assert W_COOKER_IDLE < W_DELIVER_BONUS + prep_ceiling


def test_idle_bonus_scales_with_shortfall():
    """缺口越大越该填：缺 1 份（可以等）和缺 3 份（产能崩了）不该同分。"""
    assert planner._cooker_idle_bonus(0) == 0.0
    one = planner._cooker_idle_bonus(1)
    two = planner._cooker_idle_bonus(2)
    three = planner._cooker_idle_bonus(3)
    assert 0 < one < two < three


def test_idle_bonus_saturates_beyond_threshold():
    """封顶：开局 5 单全要米时填灶不能盖过送餐与灭火。"""
    assert planner._cooker_idle_bonus(99) == W_COOKER_IDLE
    assert planner._cooker_idle_bonus(5) == planner._cooker_idle_bonus(3)


def test_idle_bonus_rejects_negative_shortfall():
    """负缺口会反向变成惩罚或加分，必须夹住（cooked_shortfall 已夹，双保险）。"""
    assert planner._cooker_idle_bonus(-3) == 0.0

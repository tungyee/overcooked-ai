# -*- coding: utf-8 -*-
"""收益函数回归：钉住从真实送达回归出的档位/连击系数。

样本来自 trace-20261013-* / trace-20261014-*（52 次送达），每条
`(combo, remaining, lifetime) -> tip` 都是实测值，不是推演。
"""
from ai.revenue import economy as E


# (combo, remaining, lifetime, 实测 tip) —— 全部 52 个样本
SAMPLES = [
    (0, 6.7, 130, 3), (0, 8.3, 130, 3), (0, 8.8, 130, 3), (0, 9.3, 130, 3),
    (0, 12.5, 130, 3), (0, 12.7, 130, 3), (0, 17.7, 130, 3), (0, 29.7, 130, 3),
    (0, 29.9, 130, 3), (0, 30.6, 130, 3), (0, 31.5, 130, 3),
    (0, 46.5, 130, 5), (0, 48.8, 130, 5), (0, 62.9, 130, 5), (0, 71.5, 130, 5),
    (0, 79.1, 130, 5), (0, 80.6, 130, 5),
    (0, 89.9, 130, 8), (0, 100.4, 130, 8), (0, 104.1, 130, 8), (0, 112.1, 130, 8),
    (1, 2.5, 130, 3), (1, 8.5, 130, 3), (1, 19.5, 130, 3), (1, 28.6, 130, 3),
    (1, 30.1, 130, 3), (1, 32.3, 130, 3), (1, 36.6, 130, 3),
    (1, 48.0, 130, 5), (1, 58.7, 130, 5), (1, 59.1, 130, 5), (1, 62.3, 130, 5),
    (1, 64.4, 130, 5), (1, 68.6, 130, 5), (1, 71.0, 130, 5), (1, 72.7, 130, 5),
    (1, 82.7, 130, 5), (1, 83.7, 130, 5), (1, 83.9, 130, 5),
    (1, 97.5, 130, 8), (1, 108.0, 130, 8), (1, 114.2, 130, 8),
    (2, 5.8, 130, 3), (2, 21.6, 130, 3), (2, 38.9, 130, 3),
    (2, 45.2, 130, 5), (2, 47.1, 130, 5), (2, 59.9, 130, 5),
    (3, 10.0, 130, 6), (3, 20.8, 130, 6),
    (4, 5.2, 130, 9),
    (5, 7.9, 130, 12),
]


def test_tip_matches_every_observed_sample():
    """实测样本必须逐条吻合——这是整套收益建模的事实依据。"""
    bad = [(c, r, t, E.tip_for(r, lf, c))
           for c, r, lf, t in SAMPLES if E.tip_for(r, lf, c) != t]
    assert bad == []


def test_tip_tier_boundaries():
    """档位边界：2/3 与 1/3 的 lifetime。"""
    assert E.tip_tier(130, 130) == E.TIP_FRESH
    assert E.tip_tier(89.9, 130) == E.TIP_FRESH     # 实测最低的 8 分样本
    assert E.tip_tier(83.9, 130) == E.TIP_MID       # 实测最高的 5 分样本
    assert E.tip_tier(45.2, 130) == E.TIP_MID       # 实测最低的 5 分样本
    assert E.tip_tier(38.9, 130) == E.TIP_LATE      # 实测最高的 3 分样本
    assert E.tip_tier(0.1, 130) == E.TIP_LATE


def test_tip_tier_degrades_safely_without_lifetime():
    """缺 lifetime（残帧）时按最低档保守估计，不要凭空给高分。"""
    assert E.tip_tier(100, 0) == E.TIP_LATE
    assert E.tip_tier(100, None) == E.TIP_LATE
    assert E.tip_tier(None, 130) == E.TIP_LATE


def test_combo_multiplier_only_kicks_in_at_three():
    """combo 0/1/2 系数都是 1——本局爬到 2 却一分连击红利都没拿到。"""
    assert E.combo_multiplier(0) == 1
    assert E.combo_multiplier(1) == 1
    assert E.combo_multiplier(2) == 1
    assert E.combo_multiplier(3) == 2
    assert E.combo_multiplier(4) == 3
    assert E.combo_multiplier(5) == 4


def test_combo_multiplier_extrapolates_monotonically():
    """>5 无实测样本，按 combo-1 外推；必须单调不减。"""
    vals = [E.combo_multiplier(c) for c in range(0, 12)]
    assert vals == sorted(vals)
    assert E.combo_multiplier(8) == 7


def test_combo_multiplier_clamps_negative():
    assert E.combo_multiplier(-1) == 1
    assert E.combo_multiplier(None) == 1


def test_expiry_costs_no_points_only_combo():
    """超时不扣分：trace-20261014-120406 t=2771 order#3 过期时 base/tips
    均未变，只有 combo 2→0。这一条推翻了社区攻略所说的 -10 罚金。"""
    assert E.EXPIRY_POINT_PENALTY == 0


def test_tip_value_of_combo_dominates_freshness():
    """新鲜度最多差 5 分（3→8），而 combo 3 起步就是 2 倍——权重量级依据。"""
    freshness_gain = E.TIP_FRESH - E.TIP_LATE
    combo_gain = E.tip_for(10, 130, 3) - E.tip_for(10, 130, 2)
    assert freshness_gain == 5
    assert combo_gain == 3
    # 高 combo 下连击的边际收益远超一个档位
    assert E.tip_for(10, 130, 5) - E.tip_for(10, 130, 4) >= freshness_gain // 2

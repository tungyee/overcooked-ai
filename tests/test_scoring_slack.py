# -*- coding: utf-8 -*-
"""urgency 改读 slack（第 3 组 3.1）+ 截止前瞻改口径（第 2 组 2.4）。

旧曲线 `W_URGENCY / max(remaining,1)` 的形态是错的：remaining=60s 时只有
0.08、100s 时 0.05（分不出先后），只有跌到 1~2s 才冲到 2.5~5.0。实机后果是
整局 2000 多拍里 deadline 对决策的影响≈0，直到最后 20 拍突然把人拽过去——
trace-20261014-120406 t=2755 为只剩 0.9s 的 order#3 放黄瓜（必死），而
order#4/#5 全程饿死。

新曲线读 slack（remaining - ETA）：slack 小才叫紧迫，remaining 小但工序也
少的单其实不急。
"""
import pytest

from ai.core import planner
from ai.core.planner import W_URGENCY


def test_urgency_discriminates_in_the_mid_range():
    """60s 和 100s 必须分出先后——旧曲线在这里是 0.08 vs 0.05，形同无差别。"""
    mid = planner._urgency_from_slack(60 * 20)
    far = planner._urgency_from_slack(100 * 20)
    assert mid > far
    assert mid - far > 0.3, (mid, far)


def test_urgency_rises_as_slack_shrinks():
    vals = [planner._urgency_from_slack(s * 20)
            for s in (120, 90, 60, 40, 20, 10, 5)]
    assert vals == sorted(vals), vals


def test_urgency_saturates_at_zero_slack():
    """slack 耗尽就是最紧迫，不需要等到 remaining 只剩 1 秒。"""
    assert planner._urgency_from_slack(0) == pytest.approx(W_URGENCY)
    assert planner._urgency_from_slack(-500) == pytest.approx(W_URGENCY)


def test_urgency_is_zero_when_slack_is_plentiful():
    """余量充足时不该贡献紧迫分，免得盖过别的项。"""
    assert planner._urgency_from_slack(10000) == 0.0


def test_urgency_curve_is_not_the_old_reciprocal():
    """钉住形态改变本身：旧曲线在 60s 处 ≈0.08，新曲线必须显著更高。

    否则「改成线性」只是口号，实际仍是最后一秒才生效。
    """
    old_at_60s = W_URGENCY / 60.0
    assert planner._urgency_from_slack(60 * 20) > old_at_60s * 5


def test_deadline_forecast_uses_slack_not_fixed_estimate():
    """截止前瞻不再比较「单个 task 的常数估计 vs remaining*0.8」。

    旧口径下 est=10 拍（0.5s）对 130s 寿命永不触发：整局 4577 拍只触发 33 拍。

    `_doom_penalty` 已删除（Ruling 15）：它与 `_urgency_from_slack` 读同一条
    曲线、只是换算系数不同——`_doom_penalty(t) == _urgency_from_slack(t) /
    W_URGENCY`（已用 python3 -c 核对此等式在整条曲线上成立）。原断言改成对
    这个换算后的量求值。
    """
    assert planner._urgency_from_slack(-100) / W_URGENCY > 0   # slack 已负 → 惩罚
    assert planner._urgency_from_slack(2000) / W_URGENCY == 0.0  # 余量充足 → 不罚


def test_doom_penalty_ramps_before_slack_runs_out():
    """余量将尽就要开始罚，不要等到负数才突变。

    换算关系同上：`_doom_penalty` 已删除，断言改成对 `_urgency_from_slack`
    求值。
    """
    tight = planner._urgency_from_slack(100) / W_URGENCY
    loose = planner._urgency_from_slack(1000) / W_URGENCY
    assert tight > loose >= 0

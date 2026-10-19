# -*- coding: utf-8 -*-
"""巡航空转的可观测性（第 0 组 0.3）。

trace-20261014-120406 里 2326/4577 拍（51%）按着方向键但格子没变，其中
`phase=cruise` 占 2234；12 个窗口连续 ≥30 拍（1.5s）坐标完全不动。这是独立
的性能债，但它会系统性抬高一切 ETA 估计，所以必须先能被量化——否则第 2 组
的可完成性估计只会把实现缺陷当成「这单本来就做不完」。

MotionControl 内部早有 `_progress_since/_progress_pos`（用于 no_progress
失败判定），这里只是把它暴露进 `ctx["motion"]`，让 trace 逐拍可读。
"""
from ai.exec.bt import Status
from ai.primitives.motion import MotionControl, PROGRESS_MAX_SECONDS
from tests.test_motion_control import observe
from tests.test_primitives import FakeWorld, make_ctx


def _cruise_ctx():
    """一条直走廊，目标在 4 格外——足够长才会走巡航档而非精确档。"""
    w = FakeWorld(walkable=lambda x, z: x == 6 and 0 <= z <= 6)
    ctx, ctrl = make_ctx(w=w, start=(6, 6))
    observe(ctx, 0.0, 1)
    return ctx, ctrl


def _tick(mc, ctx, now, seq, x=None, z=None):
    observe(ctx, now, seq, x=x, z=z)
    return mc.tick(ctx, "down", None, near=False, arrived=False, final=False)


def test_motion_reports_zero_stall_while_making_progress():
    ctx, _ = _cruise_ctx()
    mc = MotionControl()
    _tick(mc, ctx, 0.0, 1)
    _tick(mc, ctx, 0.2, 2, z=5.0)
    assert ctx["motion"]["stall_seconds"] == 0.0
    assert ctx["motion"].get("stalled") is not True


def test_motion_accumulates_stall_when_position_frozen():
    """新帧到了、键按着、坐标不动——这正是实机那 2234 拍的形态。"""
    ctx, _ = _cruise_ctx()
    mc = MotionControl()
    _tick(mc, ctx, 0.0, 1)
    for i in range(1, 8):
        status = _tick(mc, ctx, 0.1 * i, 1 + i)
        if status == Status.FAILURE:
            break
    assert ctx["motion"]["stall_seconds"] > 0.3
    assert ctx["motion"]["stalled"] is True


def test_stall_threshold_is_below_no_progress_failure():
    """告警必须早于 no_progress 的 FAILURE，否则等到失败才记就没有预警价值。"""
    from ai.primitives.motion import STALL_WARN_SECONDS
    assert 0 < STALL_WARN_SECONDS < PROGRESS_MAX_SECONDS


def test_stall_resets_after_movement_resumes():
    ctx, _ = _cruise_ctx()
    mc = MotionControl()
    _tick(mc, ctx, 0.0, 1)
    _tick(mc, ctx, 0.3, 2)                 # 卡住
    assert ctx["motion"]["stall_seconds"] > 0
    _tick(mc, ctx, 0.5, 3, z=5.0)          # 动了
    assert ctx["motion"]["stall_seconds"] == 0.0
    assert ctx["motion"].get("stalled") is not True

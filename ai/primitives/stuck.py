"""脱困三原语 + Timeout 装饰（spec §7.2）。

L1 / L2 / L3 都要保留（spec §7.2 原话"三级都要保留，初版只覆盖了 L1/L2"）。
L3 不能用 Timeout 替代：Timeout 的触发条件是"耗时过久"，L3 的是"同一格反复
卡"（last_cell / same_count 计数）。一个 task 可能反复卡在同一格但每次都没
超时，Timeout 永远不响。两个机制都要有。

TTL 单位从秒改为拍：旧实现用 time.monotonic() + BAD_CELL_TTL=4.0
（oc/ai/agent.py:977），而 spec §3.4 规定"拍统一指 Python 循环拍（50 ms）"。
混用两套时基会让测试必须 sleep。4.0 秒 → 80 拍。
"""
from typing import Any, Callable, Dict, Optional

from ai.exec.bt import Node, Status
from ai.primitives.base import Primitive

# 脱困 TTL，单位**拍**。旧实现用 time.monotonic() + 秒数
# （oc/ai/agent.py:977 的 BAD_CELL_TTL = 4.0），而 spec §3.4 规定
# "本文档中'拍'统一指 Python 循环拍（50 ms）"。混用两套时基会让测试
# 必须 sleep。4.0 秒 → 20 Hz 下 80 拍。
BAD_CELL_TTL_TICKS = 80
ABANDON_TTL_TICKS = 200


def _ensure_stuck_scratch(ctx):
    # type: (Dict) -> Any
    """取（必要时新建）StuckScratch。

    卡住守卫是跨模式前置（spec §5.1），可能先于任何模式进入，此时
    scratch 还是 None。
    """
    from ai.state.blackboard import StuckScratch
    bb = ctx["bb"]
    if not isinstance(getattr(bb, "scratch", None), StuckScratch):
        bb.scratch = StuckScratch()
    return bb.scratch


def _resolve_value(v, ctx):
    # type: (Any, Dict) -> Any
    """把可调用或静态值解析到本拍的值。"""
    return v(ctx) if callable(v) else v


def note_motion_failure(bb, task, tick, failure, world=None, chef=None):
    # type: (Any, Any, int, Dict, Any, Any) -> None
    """按位置 / 资源避让已证实的无进展；订单切换不能重置同一处故障。"""
    if failure.get("reason") not in ("no_progress", "observation_timeout") or bb.mode != "Normal":
        return
    scratch = _ensure_stuck_scratch({"bb": bb})
    cell = failure.get("blocked_cell")
    if cell is not None:
        scratch.bad_cells[tuple(cell)] = tick + BAD_CELL_TTL_TICKS
    # 风区里被吹得动不了：不是 AI 的错，只写 bad_cells 让寻路绕开这一格，
    # **不拉黑资源**——否则一阵风就把端料/盛饭的整个资源判死刑（实机：
    # 端番茄时被风推、位置无法移动，observation_timeout 把料箱拉黑 10 秒）。
    if world is not None:
        check = getattr(world, "chef_in_wind", None)
        if callable(check) and check(chef):
            return
    # 端锅/盛饭/灭火这类任务不能拉黑资源：拉黑期(200拍)超过烧糊倒计时，
    # 等于判那口锅死刑（实机：盛饭失败把端锅一起拉黑 → 熟锅烧糊起火）。
    # bad_cells 仍照写——寻路绕开被堵的那一格，只废格、不废整个资源。
    if getattr(task, "NO_ABANDON", False):
        return
    from ai.core.recovery import abandon_actions
    abandon_actions(bb, task, tick + ABANDON_TTL_TICKS)


class MarkBlocked(Primitive):
    """L1：把格子拉黑写入 scratch.bad_cells（spec §7.2）。

    Approach 读它并重算距离场，于是下次寻路绕开堵死的方向。执行层对
    scratch 有写权（spec §4.5），对 claims 没有。
    """

    def __init__(self, cell, ttl=BAD_CELL_TTL_TICKS):
        # type: (Any, int) -> None
        Primitive.__init__(self)
        self.cell = cell
        self.ttl = ttl

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        cell = _resolve_value(self.cell, ctx)
        if cell is None:
            return Status.FAILURE
        scratch = _ensure_stuck_scratch(ctx)
        scratch.bad_cells[tuple(cell)] = (ctx.get("tick") or 0) + self.ttl
        return Status.SUCCESS


class AbandonTarget(Primitive):
    """L3：放弃该目标并拉黑一段时间，触发重规划（spec §7.2）。

    **L3 不能用 Timeout 替代**（spec §7.2 原话）：Timeout 的触发条件是
    "耗时过久"，L3 的是"同一格反复卡"（last_cell / same_count 计数）。
    一个 task 可能反复卡在同一格但每次都没超时，Timeout 永远不响。

    写 scratch.abandoned 而不直接清 bb.commitment：spec §4.5 规定执行层
    对 commitment 只有 steps_done / retries 的写权。Planner 在可行性过滤
    时读这里（Plan 3 接）。
    """

    def __init__(self, target, ttl=ABANDON_TTL_TICKS):
        # type: (Any, int) -> None
        Primitive.__init__(self)
        self.target = target
        self.ttl = ttl

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        t = _resolve_value(self.target, ctx)
        if isinstance(t, dict):
            t = t.get("n")
        if not t:
            return Status.FAILURE
        scratch = _ensure_stuck_scratch(ctx)
        scratch.abandoned[t] = (ctx.get("tick") or 0) + self.ttl
        return Status.SUCCESS


class Timeout(Node):
    """整个 task 级超时装饰（spec §7.2 结构性安全网）。

    今天只有单步重试，没有"这个 task 卡了 30 拍"的兜底。任何 task 异常
    久 → FAILURE → Planner 重算。防死循环的最后一道闸。

    超时转 FAILURE 时 halt 子树：否则 HoldUntil 按住的键会留着。
    子树 SUCCESS / FAILURE 后预算重置——树是跨拍复用的对象，不重置会
    越跑越紧。
    """

    def __init__(self, child, max_ticks):
        # type: (Node, int) -> None
        self.child = child
        self.max_ticks = max_ticks
        self._ticks = 0

    def reset(self):
        # type: () -> None
        self._ticks = 0
        reset = getattr(self.child, "reset", None)
        if callable(reset):
            reset()

    def tick(self, ctx):
        # type: (Dict) -> str
        if self._ticks >= self.max_ticks:
            self.child.halt()
            self._ticks = 0
            return Status.FAILURE
        self._ticks += 1
        s = self.child.tick(ctx)
        if s != Status.RUNNING:
            self._ticks = 0
        return s

    def halt(self):
        # type: () -> None
        self.child.halt()
        self._ticks = 0

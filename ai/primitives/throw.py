# -*- coding: utf-8 -*-
"""ThrowTo 原语：把手里可投掷物投掷到目标方向。

投掷键 = chop（`/` = WorkstationInteract），持可投掷物（食材）时按它就是
投掷（spec §7.9）。锅/盘不可投掷，走 EnsureHandsFree 走近放下，不进这里。

只校验「手空」（离手成功）：投掷物有飞行时间，落地要到下一帧 items[] 才
体现。落点偏差 / 落地结果由 Planner 无状态重算接管，不在原语里猜。

转向为什么不用 Face：facing_cell（interactions.gx/gz）指向「面向方向上的
第一个**交互物**」，不是相邻格。Face 精确匹配「目标相邻格」会一路短按磨到
柜台前（实机复现）。投掷只需面向大致方向，所以这里朝目标主轴方向**长按
转向**（边走边转），facing 朝该方向就停、直接投掷。
"""
from typing import Any, Callable, Dict, Optional

from ai import predicates as P
from ai.bt import Status
from ai.hands import DIR2D
from ai.primitives.base import Primitive, resolve, target_cell
from ai.primitives.tap import Confirm, Tap

# 转向最多 12 拍（0.6s）；转不动也直接投，别卡死。
TURN_MAX_TICKS = 12


def _turn_dir(ctx, target):
    # type: (Dict, Any) -> Optional[str]
    """目标相对 P2 的主轴方向键名（right/left/up/down）。"""
    st = resolve(target, ctx)
    if st is None:
        return None
    cell = target_cell(st, ctx)
    if cell is None:
        return None
    cur = P.chef_cell(ctx["chef"], ctx["w"].to_grid)
    if cur is None:
        return None
    dx = cell[0] - cur[0]
    dz = cell[1] - cur[1]
    if abs(dx) >= abs(dz):
        return "right" if dx > 0 else "left"
    return "up" if dz > 0 else "down"


def _facing_towards(ctx, d):
    # type: (Dict, str) -> bool
    """facing 是否已朝 d 方向（主轴分量同号）。"""
    chef = ctx["chef"]
    cur = P.chef_cell(chef, ctx["w"].to_grid)
    facing = P.facing_cell(chef)
    if cur is None or facing is None:
        return False
    vec = DIR2D[d]
    fd = (facing[0] - cur[0]) * vec[0] + (facing[1] - cur[1]) * vec[1]
    return fd > 0


class ThrowTo(Primitive):
    """= 长按转向（朝目标主轴方向）→ Tap("chop") → Confirm(手空)。

    target 给转向方向用（队友位置 / 目标台面格），不是给按键校验用——
    投掷是「面向方向盲投」，没有 interact_ready 信号可查。target 优先传
    可调用（P1 会动、锅会被端走），每拍重算。
    """

    def __init__(self, target, retries=5):
        # type: (Any, int) -> None
        Primitive.__init__(self)
        self.target = target
        self._tap = Tap("chop")
        self._confirm = Confirm(lambda c: P.hands_free(c["chef"]), retries)
        self._phase = 0        # 0=转向, 1=投掷, 2=确认
        self._turn_dir = None  # type: Optional[str]
        self._turn_ticks = 0

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._phase = 0
        self._turn_dir = None
        self._turn_ticks = 0
        self._tap.reset()
        self._confirm.reset()

    def halt(self):
        # type: () -> None
        self._tap.halt()
        self._confirm.halt()
        Primitive.halt(self)

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        # 手已空 → 无需投掷（可能上一拍已经投出 / 被 P1 换走）
        if P.hands_free(ctx["chef"]):
            self.reset()
            return Status.SUCCESS

        # ① 转向：长按目标主轴方向，facing 朝该方向（或超时）就停
        if self._phase == 0:
            if self._turn_dir is None:
                self._turn_dir = _turn_dir(ctx, self.target)
                if self._turn_dir is None:
                    ctx["hands"].set_move(None)
                    return Status.FAILURE
            if _facing_towards(ctx, self._turn_dir) or self._turn_ticks >= TURN_MAX_TICKS:
                ctx["hands"].set_move(None)
                self._phase = 1
            else:
                ctx["hands"].set_move(self._turn_dir)
                self._turn_ticks += 1
                return Status.RUNNING

        # ② 投掷
        if self._phase == 1:
            s = self._tap.tick(ctx)
            if s == Status.RUNNING:
                return Status.RUNNING
            if s == Status.FAILURE:
                self.halt()
                return Status.FAILURE
            self._phase = 2

        # ③ 确认离手
        s = self._confirm.tick(ctx)
        if s == Status.RUNNING:
            return Status.RUNNING
        if s == Status.FAILURE:
            self.halt()
            return Status.FAILURE
        self.reset()
        return Status.SUCCESS

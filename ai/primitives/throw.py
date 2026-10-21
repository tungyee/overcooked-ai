"""ThrowTo 原语：把手里可投掷物投掷到目标方向。

投掷键 = chop，持可投掷物（食材）时按它就是投掷（spec §7.9）。锅/盘不可投掷，
走 EnsureHandsFree 走近放下，不进这里。

只校验「手空」（离手成功）：投掷物有飞行时间，落地要到下一帧 items[] 才
体现。落点偏差 / 落地结果由 Planner 无状态重算接管，不在原语里猜。

委托后端下 throw 命令由插件面朝目标 + 单击投掷，本地 Confirm 确认离手。
"""
from typing import Any, Callable, Dict, Optional

from ai.query import predicates as P
from ai.exec.bt import Status
from ai.exec.commands import build_command
from ai.primitives.base import Primitive, resolve, target_world
from ai.primitives.tap import Confirm

# throw 命令超时（毫秒）。
THROW_TIMEOUT_MS = 1500


class ThrowTo(Primitive):
    """= throw 命令（插件面朝 + 单击投掷）→ Confirm(手空)。"""

    def __init__(self, target, retries=5):
        # type: (Any, int) -> None
        Primitive.__init__(self)
        self.target = target
        self._confirm = Confirm(lambda c: P.hands_free(c["chef"]), retries)
        self._thrown = False   # throw 命令是否已发出并回 ok

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._thrown = False
        self._confirm.reset()

    def halt(self):
        # type: () -> None
        self._confirm.halt()
        Primitive.halt(self)

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        # 手已空 → 无需投掷（可能上一拍已经投出 / 被 P1 换走）
        if P.hands_free(ctx["chef"]):
            self.reset()
            return Status.SUCCESS
        return self._tick_delegated(ctx, ctx["exec"])

    def _tick_delegated(self, ctx, ex):
        # type: (Dict, Any) -> str
        """throw 命令（插件面朝 + 单击投掷）→ 本地 Confirm 确认离手。"""
        if not self._thrown:
            if self._pending_cmd is None:
                st = resolve(self.target, ctx)
                cmd = build_command(
                    "throw",
                    target_world=target_world(st),
                    timeout_ms=THROW_TIMEOUT_MS,
                )
                return self._begin_exec(ctx, ex, cmd)
            s = self._poll_exec(ctx, ex)
            if s == Status.RUNNING:
                return Status.RUNNING
            if s == Status.FAILURE:
                return Status.FAILURE
            self._thrown = True
        s = self._confirm.tick(ctx)
        if s != Status.SUCCESS:
            return s
        self.reset()
        return Status.SUCCESS

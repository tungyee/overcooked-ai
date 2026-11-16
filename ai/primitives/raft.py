# -*- coding: utf-8 -*-
"""Raft 原语：触发舢板（5-1）移动到对岸，等到岸才 SUCCESS。

按键控制、语义到岸由 Python 判（docked_side），与 HoldUntil 的语义早停同一
思路：插件只负责「按交互键 + 按住方向」触发移动，船从触发到靠岸的等待由
pred 判定。target 可选——有开关站就朝向它再按，没有（触发点是舢板本身）就
原地按。
"""
from typing import Any, Callable, Dict, Optional

from ai.exec.bt import Status
from ai.exec.commands import build_command
from ai.primitives.base import (Primitive, handle_desc, resolve,
                                target_grid_callbacks, target_world)


class Raft(Primitive):
    """触发舢板移动到 target 侧，等到岸（pred 成立）才 SUCCESS。

    命令只发一次（trigger 是边沿语义，连按会把船来回开），之后每拍查 pred。
    timeout 无默认值，必须显式传（覆盖「触发 + 等靠岸」的整段时间）。
    """

    def __init__(self, direction, pred, timeout, target=None):
        # type: (str, Callable[[Dict], bool], int, Any) -> None
        Primitive.__init__(self)
        self.direction = direction
        self.pred = pred
        self.timeout = timeout
        self.target = target
        self._fired = False

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._fired = False

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        if self._ticks > self.timeout:
            if self._pending_cmd is not None:
                self._exec_halt()
            return Status.FAILURE
        if self.pred(ctx):
            if self._pending_cmd is not None:
                self._exec_halt()
            return Status.SUCCESS
        if not self._fired:
            st = resolve(self.target, ctx) if self.target is not None else None
            if self.target is not None and st is None:
                return Status.FAILURE
            cmd = build_command(
                "raft",
                direction=self.direction,
                target=handle_desc(st, target_grid_callbacks(ctx["w"], st)[0]),
                target_world=target_world(st),
                timeout_ms=self.timeout * 50,
            )
            self._fired = True
            return self._begin_exec(ctx, ctx["exec"], cmd)
        if self._pending_cmd is not None:
            s = self._poll_exec(ctx, ctx["exec"])
            if s == Status.RUNNING:
                return s
            if s == Status.FAILURE:
                return s
            # 命令 ok：船还在走，继续等 pred（下一拍回到 pred 检查）
        return Status.RUNNING

# -*- coding: utf-8 -*-
"""换边：布局要变了，先走进「两种布局都算对」的格。

3-3（UI 3-3，帧里报 `Wizard_3_2_2P`）的料箱每 34 秒在竖列/横排之间换一次，
把厨房切成左右或上下两半；全图只连通 2.0–3.4 秒。实测（trace-20261109-
011932）等布局稳定再走就已经被关在另一侧，要等 31 秒才有下一次机会。

目标由 `ai/space/zoning.ZoneTracker` 算好放进 `bb.reposition`：

- `goal`：安全格里离角色最近的一格（进快照时定格，段内不追）；
- `safe`：见过的所有布局里都属于该角色的格，走进去不管最后落在哪种布局都对；
- `until_tick`：兜底过期拍号，防止拓扑检测失灵时一直霸占。

本 task 不占资源、不认领；它只是「把腿走到位」，不做任何交互。
"""
from typing import Any, Dict, Optional, Tuple

from ai import primitives as PR
from ai.exec.bt import Sequence
from ai.query import predicates as P
from ai.tasks.base import Task, _register


@_register
class Reposition(Task):
    """走到安全区（换布局窗口里唯一值得做的事）。"""

    TYPE = "Reposition"
    DESC = "换布局前先走到两种布局都对的中间区"
    LABEL = "换边"
    # 抢不到窗口的代价是 31 秒够不着自己的站点，别让失败拉黑把它压掉。
    NO_ABANDON = True

    def _intent(self, ctx):
        # type: (Dict) -> Optional[Any]
        return getattr(ctx.get("bb"), "reposition", None)

    def claims(self):
        # type: () -> Tuple
        return ()

    def done(self, ctx):
        # type: (Dict) -> bool
        intent = self._intent(ctx)
        if intent is None:
            return True
        cell = P.chef_cell(ctx.get("chef"), ctx["w"].to_grid)
        return cell in intent.safe

    def feasible(self, ctx):
        # type: (Dict) -> bool
        intent = self._intent(ctx)
        if intent is None:
            return False
        tick = ctx.get("tick") or 0
        if intent.until_tick and tick > intent.until_tick:
            return False
        if self.done(ctx):
            return False
        # 目标格不可达（还没被切开 / 已经错过渡口）就不走：硬走只会把角色
        # 卡在走廊里，被两边同时切断。
        return intent.goal in (ctx.get("field") or {})

    def build(self):
        # type: () -> Any
        task = self

        def goal_cell(ctx):
            # type: (Dict) -> Optional[Tuple[int, int]]
            intent = task._intent(ctx)
            return intent.goal if intent is not None else None

        def arrived(ctx):
            # type: (Dict) -> bool
            intent = task._intent(ctx)
            if intent is None:
                return True
            return P.chef_cell(ctx.get("chef"), ctx["w"].to_grid) in intent.safe

        return Sequence([PR.GoToCell(goal_cell, arrived=arrived)])

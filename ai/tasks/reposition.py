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

# 占住窗口的上限（拍）。实测窗口 2.0~3.4 秒，留一倍余量；真正的退出条件是
# 主循环把 bb.reposition 清掉。
WAIT_WINDOW_TICKS = 160


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
        """窗口关掉才算完成。

        **不是**「走进安全区就算完」：实测（trace-20261109-123544 t=118 与
        t=220）它到了安全区就回去干活，两秒后被普通任务拽出来，关窗那一刻
        正好压在分界线上，于是又被隔 31 秒。窗口只有 2~3 秒，占住比多干一趟
        值钱。
        """
        return self._intent(ctx) is None

    def feasible(self, ctx):
        # type: (Dict) -> bool
        intent = self._intent(ctx)
        if intent is None:
            return False
        tick = ctx.get("tick") or 0
        if intent.until_tick and tick > intent.until_tick:
            return False
        cell = P.chef_cell(ctx.get("chef"), ctx["w"].to_grid)
        if cell is not None and cell in intent.safe:
            return True          # 已经在安全区：原地占住窗口
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

        def window_closed(ctx):
            # type: (Dict) -> bool
            return task._intent(ctx) is None

        return Sequence([PR.GoToCell(goal_cell, arrived=arrived),
                         PR.WaitUntil(window_closed, WAIT_WINDOW_TICKS)])

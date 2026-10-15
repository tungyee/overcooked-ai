# -*- coding: utf-8 -*-
"""中转交接 task：PlaceToRelay。

1-3 关卡的「分工 + 传递」落地：不可投掷的锅/盘放中间中转台。这个 task 只在
Sushi_1_3_2P 由 Planner 生成并加分，其他关卡零变化。可投掷食材的批量取+投
已并入通用 FetchToBoard（见 chop.py），不再单独设 relay task。

外部不直接 import 本模块，一律 `from ai import tasks as T`。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai import placement
from ai import predicates as P
from ai import primitives as PR
from ai import stations as S
from ai.bt import Node, Sequence
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register,
                           plate_by_id, plate_cell)


def _relay_drop(c):
    # type: (Dict) -> Optional[Dict]
    """中转台落点：遍历 relay_cells 找第一个空且可达的格，返回合成站点 dict。"""
    for cell in S.relay_cells_for(c["w"]):
        st = placement.find_drop_at(c["w"], c["field"], cell)
        if st is not None:
            return st
    return None


@_register
class PlaceToRelay(Task):
    """把不可投掷物（干净盘子）放到中间中转台交接（1-3 递盘子）。

    item 是盘实例名（plate_id）。盘子不可投掷，走近中转台放下。落点用
    find_drop_at 找第一个空的中转台格，满了（pick 返回 None）就 FAILURE
    交还 Planner，不 fallback 到别处乱放。
    """

    TYPE = "PlaceToRelay"
    HANDS = "empty"

    def claims(self):
        return ()

    def done(self, ctx):
        # type: (Dict) -> bool
        cell = plate_cell(ctx, plate_by_id(ctx, self.item))
        if cell is None:
            return False
        return cell in set(S.relay_cells_for(ctx["w"]))

    def feasible(self, ctx):
        # type: (Dict) -> bool
        relay_cells = S.relay_cells_for(ctx["w"])
        if not relay_cells:
            return False
        p = plate_by_id(ctx, self.item)
        if p is None or P.plate_count(p) != 0:
            return False            # 盘消失 / 不是干净盘
        if self.done(ctx):
            return False
        # 中转台至少要有一个空格可放
        occupied = placement.occupied_cells(ctx["w"])
        return any(c not in occupied for c in relay_cells)

    def build(self):
        # type: () -> Node
        plate_id = self.item
        return PR.Timeout(Sequence([
            PR.EnsureHandsFree(keep=lambda c: P.held_is_plate(c["chef"])),
            PR.InteractWith(
                lambda c: plate_by_id(c, plate_id), "pickup",
                lambda c: P.held_is_plate(c["chef"])),
            PR.EnsureHandsFree(pick=_relay_drop, strict=True),
        ]), TASK_TIMEOUT_TICKS)

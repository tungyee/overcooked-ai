"""移动与转向原语。

Approach：走到能跟 target 交互的位置。
Face：转向 target（委托后端下为 no-op，转向已并入 move/tap 的重瞄）。
"""
from typing import Any, Dict, Optional

from ai.space import navigation
from ai.space import occupancy as O
from ai.query import predicates as P
from ai.query import placement
from ai.exec.bt import Status
from ai.exec.commands import build_command
from ai.query.stations import PLATE_STATION, sink_place_cells
from ai.primitives.base import (Primitive, resolve, target_cell, handle_desc,
                                _blocked_cells, target_grid_callbacks)

# 转向预算（委托后端下 Face 是 no-op，保留该常量仅用于接口兼容）。
FACE_MAX_TICKS = 40

# move 命令超时（毫秒）：委托后端下一趟走位最多 8 秒。
MOVE_TIMEOUT_MS = 8000


def _station_interact_cells(st, cell, w, kind, include_belts=False):
    # type: (Dict, Tuple[int, int], object, Optional[str], bool) -> List[Tuple[int, int]]
    """站点交互位：出餐口只取厨房内侧（前侧），其余站点用四邻并集。

    出餐口是嵌在外墙里的一段墙，墙后那一面是死路，站过去送不了餐；
    见 navigation.serving_hatch_interact_cells 的判据（沿墙法向的深度）。
    """
    if st.get("t") == PLATE_STATION:
        fp = O.station_block_cells(st, w.terrain, w.mapping)
        return navigation.serving_hatch_interact_cells(
            fp, w.walkable, w.belt_cells)
    if st.get("t") == "washing" and kind == "placement":
        cells = sink_place_cells(st, w.stations, w.to_grid)
    else:
        cells = {cell}
    return navigation.interact_cells_multi(
        cells, w.walkable, w.belt_cells, include_belts=include_belts)


class Approach(Primitive):
    """走到能跟 target 交互的位置（委托后端：把走位交给插件 move 命令）。

    本地只负责 goal/path 计算（复用寻路），实际移动由插件逐帧跟随 +
    连续到达 + stuck 检测完成。到位判据是**位置**：`chef_cell ∈
    interact_cells(target)`；朝向交给 move/tap 的重瞄，不在原语里处理。
    """

    def __init__(self, target, kind=None, allow_target=False):
        # type: (Any, Optional[str], bool) -> None
        Primitive.__init__(self)
        self.target = target
        self.kind = kind
        self.allow_target = allow_target

    def reset(self):
        # type: () -> None
        Primitive.reset(self)

    def needs_reapproach(self, ctx):
        # type: (Dict) -> bool
        """若被推离交互区域，交给移动阶段重新接近当前目标。"""
        w, chef = ctx['w'], ctx['chef']
        st = resolve(self.target, ctx)
        cell = target_cell(st, ctx)
        cur = P.chef_cell(chef, w.to_grid)
        if cell is None or cur is None:
            return True
        if cur == cell and (w.walkable(*cell) or P.interact_ready(chef, st, self.kind, w.to_grid)):
            return False
        footprint = {cell}
        if st.get('t') == PLATE_STATION:
            footprint = O.station_block_cells(st, getattr(w, 'terrain', {}),
                                               getattr(w, 'mapping', None)) or footprint
        elif st.get('t') == 'washing' and self.kind == 'placement':
            footprint = sink_place_cells(st, w.stations, w.to_grid)
        goals = navigation.interact_cells_multi(footprint, w.walkable,
                                                w.belt_cells, include_belts=True)
        return cur not in goals

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        return self._tick_delegated(ctx, ctx["exec"])

    def _avoid_occupied(self, st, w, goals):
        # type: (Dict, object, List[Tuple[int, int]]) -> List[Tuple[int, int]]
        """站点目标做 pickup/placement 时，避开地上有散落物的交互格。

        散料/盘落在交互格上时，游戏的 pickup/placement 句柄（距离扫描）会指向
        脚下的散料而非站点，交互必然失败（实机：散米挡灶前，RescueCooked 端锅、
        TakeCookedToPlate 盛饭都卡）。有干净交互格就优先走干净格，全被占才退回。
        """
        if self.kind not in ("pickup", "placement"):
            return goals
        if not (isinstance(st, dict) and st.get("t")):
            return goals          # 目标是散料/盘（无站点类型）→ 不避让
        occ = placement.occupied_cells(w)
        unocc = [g for g in goals if g not in occ]
        return unocc if unocc else goals

    def _tick_delegated(self, ctx, ex):
        # type: (Dict, Any) -> str
        """复用本地 goal/path 计算，把走位交给插件 move 命令。"""
        w, chef = ctx["w"], ctx["chef"]
        st = resolve(self.target, ctx)
        if st is None:
            return Status.FAILURE
        cell = target_cell(st, ctx)
        if cell is None:
            return Status.FAILURE

        cur = P.chef_cell(chef, w.to_grid)
        if cur is None:
            return Status.FAILURE
        # 目标就在脚下（散料/掉落物落自己站的那格）：无需走。
        if cur == cell and w.walkable(cell[0], cell[1]):
            return Status.SUCCESS

        blocked = _blocked_cells(ctx, P.resource_id(st))
        goals = _station_interact_cells(st, cell, w, self.kind)
        if self.allow_target:
            goals = [cell] + goals
        if not goals:
            goals = [cell]
        goals = self._avoid_occupied(st, w, goals)
        field = ctx["field"]
        got = navigation.nearest_cost(field, [g for g in goals if g not in blocked])
        if got is None:
            goals = _station_interact_cells(st, cell, w, self.kind,
                                            include_belts=True)
            got = navigation.nearest_cost(field, [g for g in goals if g not in blocked])
        if got is None:
            return Status.FAILURE
        goal = got[0]
        path = navigation.trace_back(field, goal, w.belt_dir, ctx.get("soft_cost"))
        if not path:
            return Status.FAILURE

        if self._pending_cmd is None:
            cmd = build_command(
                "move",
                goal=list(w.to_world(*goal)),
                path=[list(w.to_world(*c)) for c in path],
                target=handle_desc(st, target_grid_callbacks(w, st)[0]),
                kind=self.kind,
                dash=False,
                timeout_ms=MOVE_TIMEOUT_MS,
            )
            return self._begin_exec(ctx, ex, cmd)
        return self._poll_exec(ctx, ex)


class Face(Primitive):
    """转向 target。委托后端（exec）下转向已并入 move/tap 的重瞄，Face 是 no-op。"""

    def __init__(self, target, max_ticks=FACE_MAX_TICKS):
        # type: (Any, int) -> None
        Primitive.__init__(self)
        self.target = target
        self.max_ticks = max_ticks

    def reset(self):
        # type: () -> None
        Primitive.reset(self)

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        return Status.SUCCESS

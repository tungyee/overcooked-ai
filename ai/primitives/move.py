"""移动与转向原语。

Approach：走到能跟 target 交互的位置。
Face：转向 target（委托后端下为 no-op，转向已并入 move/tap 的重瞄）。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai.space import navigation, seams
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
        # 与规划层 reachability.can_interact 同一口径（见 interaction_footprint）。
        cells = O.interaction_footprint(st, cell, getattr(w, "stations", ()),
                                        w.to_grid, w.to_world, w.walkable)
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
        else:
            footprint = O.interaction_footprint(st, cell, getattr(w, 'stations', ()),
                                                w.to_grid, w.to_world, w.walkable)
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
        path = navigation.trace_back(field, goal, w.belt_dir, ctx.get("soft_cost"),
                                     seams.nav_predicates(w, ctx.get("bb"))[1])
        if not path:
            return Status.FAILURE

        if self._pending_cmd is None:
            points, gates = seams.path_points(w, path, field, ctx.get("bb"))
            cmd = build_command(
                "move",
                goal=list(w.to_world(*goal)),
                path=points,
                target=handle_desc(st, target_grid_callbacks(w, st)[0]),
                kind=self.kind,
                dash=False,
                # 路上要在门洞口等门时放宽超时（插件按 gates[].max_wait 等）
                timeout_ms=MOVE_TIMEOUT_MS + int(1000 * seams.gates_wait_seconds(gates)),
            )
            if gates:
                cmd["gates"] = gates
            return self._begin_exec(ctx, ex, cmd)
        return self._poll_exec(ctx, ex)


# GoToCell 的单段 move 超时（毫秒）：比 Approach 短。漂流平台上格心会被带走，
# 一段追不上就换新格重发，不在一条过期的 move 上耗满 8 秒。
GOTO_MOVE_TIMEOUT_MS = 2500
# move 失败后重算目标格重试的次数。
GOTO_RETRIES = 3


class GoToCell(Primitive):
    """走到指定的格（不是「目标旁的交互位」）：投掷出手位用。

    cell_fn(ctx) -> (gx, gz)。每段 move 发出时定格，段内不追新格（每拍换格
    move 命令永远在被替换）；段失败（超时/stuck）就按当前帧**重算**目标格再
    发，最多 GOTO_RETRIES 次。

    arrived(ctx) -> bool：可选的提前到达判据，每拍先看。投掷传它「此刻位置
    投得出去」：站在漂流木筏上，固定格心被木筏带走、追不上，但只要人当下的
    位置能投就该投（trace-20261029-122831 t=351~798 拿着料干耗 12s）。
    """

    def __init__(self, cell_fn, arrived=None):
        # type: (Any, Any) -> None
        Primitive.__init__(self)
        self.cell_fn = cell_fn
        self.arrived = arrived
        self._goal = None   # type: Optional[Tuple[int, int]]
        self._fails = 0

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._goal = None
        self._fails = 0

    def _done(self):
        # type: () -> str
        self.reset()
        return Status.SUCCESS

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        w, chef = ctx["w"], ctx["chef"]
        cur = P.chef_cell(chef, w.to_grid)
        if cur is None:
            return Status.FAILURE
        if self.arrived is not None and self.arrived(ctx):
            self._exec_halt()
            return self._done()
        if self._pending_cmd is not None:
            s = self._poll_exec(ctx, ctx["exec"])
            if s == Status.RUNNING:
                return s
            if s == Status.SUCCESS:
                return self._done()
            self._goal = None
            self._fails += 1
            if self._fails > GOTO_RETRIES:
                self.reset()
                return Status.FAILURE
            return Status.RUNNING     # 下一拍按新帧重算目标格
        goal = self._goal or self.cell_fn(ctx)
        if goal is None:
            return Status.FAILURE
        if cur == tuple(goal):
            return self._done()
        field = ctx["field"] or {}
        path = navigation.trace_back(field, tuple(goal), w.belt_dir,
                                     ctx.get("soft_cost"), seams.nav_predicates(w, ctx.get("bb"))[1])
        if not path:
            return Status.FAILURE
        self._goal = tuple(goal)
        points, gates = seams.path_points(w, path, field, ctx.get("bb"))
        cmd = build_command(
            "move",
            goal=list(w.to_world(*self._goal)),
            path=points,
            target=None,
            kind=None,
            dash=False,
            timeout_ms=GOTO_MOVE_TIMEOUT_MS + int(1000 * seams.gates_wait_seconds(gates)),
        )
        if gates:
            cmd["gates"] = gates
        return self._begin_exec(ctx, ctx["exec"], cmd)


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

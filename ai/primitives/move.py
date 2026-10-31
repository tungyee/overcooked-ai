"""移动与转向原语。

Approach：走到能跟 target 交互的位置。
Face：转向 target（委托后端下为 no-op，转向已并入 move/tap 的重瞄）。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai.space import navigation, seams, shuttle
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
        self._staging = False          # 渡运：在途的是去候车格的 move
        self._shuttle_wait_ticks = 0

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._staging = False
        self._shuttle_wait_ticks = 0

    def needs_reapproach(self, ctx):
        # type: (Dict) -> bool
        """若被推离交互区域，交给移动阶段重新接近当前目标。"""
        w, chef = ctx['w'], ctx['chef']
        st = resolve(self.target, ctx)
        cell = target_cell(st, ctx)
        cur = P.chef_cell(chef, w.to_grid)
        if cell is None or cur is None:
            return True
        shuttle_st = shuttle.station_of(w, st)
        if shuttle_st is not None:
            # 渡运台面：站位是格外的世界点，不按格判
            return not shuttle.at_stance(chef, shuttle.stances(w, shuttle_st))
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
        shuttle_st = shuttle.station_of(w, st)
        if shuttle_st is not None:
            return self._tick_shuttle(ctx, ex, st, shuttle_st)
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
        # 与 reachability._interact_goals 同一口径：兜底任务在站位全被
        # 「目标-站位」拉黑时忽略该拉黑（脱困拉黑的格子仍然避开）。
        if ctx.get("stance_last_resort") and all(g in blocked for g in goals):
            blocked = _blocked_cells(ctx)
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

    def _tick_shuttle(self, ctx, ex, st, shuttle_st):
        # type: (Dict, Any, Dict, Dict) -> str
        """渡运台面（ai/space/shuttle.py）：走到格外站位；篮子不在口上就等它。

        寻路到站位挂的落脚格，路径末尾追加站位点（朝台面推一点）作 move 终点；
        插件按交互句柄判到达。

        站位此刻不在可达场里（篮子在路上 / 在对面）：时间表能预测就原地等，快到时
        先走到上一轮的落脚格（_await_shuttle）。等待在原语层做，所有 task 拿篮子上的
        东西都一样，不用各自写等篮子分支。预测不了 → FAILURE 交还 Planner。
        """
        w, chef = ctx["w"], ctx["chef"]
        field = ctx["field"]
        if self._pending_cmd is not None:
            s = self._poll_exec(ctx, ex)
            if self._staging:
                ctx["external_wait"] = ctx.get("external_wait", 0) + 1
                if s != Status.RUNNING:
                    self._staging = False   # 候车格走完（成败都行）：回到等篮子
                    return Status.RUNNING
            return s
        options = shuttle.reachable_stances(w, shuttle_st, field)
        if not options:
            return self._await_shuttle(ctx, ex, st)
        self._shuttle_wait_ticks = 0
        if shuttle.at_stance(chef, options):
            return Status.SUCCESS
        if field is None:
            best = options[0]
        else:
            best = min(options, key=lambda s: field[s["cell"]])
        path = navigation.trace_back(field or {}, best["cell"], w.belt_dir,
                                     ctx.get("soft_cost"),
                                     seams.nav_predicates(w, ctx.get("bb"))[1])
        if not path:
            return Status.FAILURE
        points, gates = seams.path_points(w, path, field, ctx.get("bb"))
        goal = shuttle.goal_point(best)
        points = list(points) + [goal]
        cmd = build_command(
            "move",
            goal=goal,
            path=points,
            target=handle_desc(st, target_grid_callbacks(w, st)[0]),
            kind=self.kind,
            dash=False,
            timeout_ms=MOVE_TIMEOUT_MS + int(1000 * seams.gates_wait_seconds(gates)),
        )
        if gates:
            cmd["gates"] = gates
        return self._begin_exec(ctx, ex, cmd)

    def _await_shuttle(self, ctx, ex, st):
        # type: (Dict, Any, Dict) -> str
        """篮子不在口上：等（RUNNING），离到口 ≤ SHUTTLE_STAGE_S 时先去候车格。

        等待期间每拍 ctx["external_wait"] += 1：外层 Timeout 不把等篮子算进超时预算
        （一圈 ≈36s，远超 task 的 200 拍），等待本身由 SHUTTLE_WAIT_TICKS 封顶。
        """
        from ai.space import reachability
        wait = reachability.shuttle_wait(ctx, st)
        self._shuttle_wait_ticks += 1
        if wait is None or self._shuttle_wait_ticks > SHUTTLE_WAIT_TICKS:
            self._shuttle_wait_ticks = 0
            return Status.FAILURE
        ctx["external_wait"] = ctx.get("external_wait", 0) + 1
        ctx["shuttle_wait"] = round(wait, 1)
        if wait > SHUTTLE_STAGE_S:
            return Status.RUNNING
        w, chef, field = ctx["w"], ctx["chef"], ctx["field"]
        cell = shuttle.staging_cell(w, st, field, reachability.shuttle_clock(ctx))
        if cell is None or P.chef_cell(chef, w.to_grid) == cell:
            return Status.RUNNING
        path = navigation.trace_back(field or {}, cell, w.belt_dir, ctx.get("soft_cost"),
                                     seams.nav_predicates(w, ctx.get("bb"))[1])
        if not path:
            return Status.RUNNING
        points, gates = seams.path_points(w, path, field, ctx.get("bb"))
        cmd = build_command("move", goal=list(w.to_world(*cell)), path=points,
                            target=None, kind=None, dash=False,
                            timeout_ms=MOVE_TIMEOUT_MS + int(1000 * seams.gates_wait_seconds(gates)))
        if gates:
            cmd["gates"] = gates
        s = self._begin_exec(ctx, ex, cmd)
        if s == Status.RUNNING:
            self._staging = True
            return s
        return Status.RUNNING     # 发不出去就原地等


# 等篮子的上限（拍）：一圈 ≈36s（Balloon_2_2），留一圈余量。
SHUTTLE_WAIT_TICKS = 800
# 篮子到口前多少秒开始往候车格走（秒）。
SHUTTLE_STAGE_S = 4.0


# GoToCell 的单段 move 超时（毫秒）：比 Approach 短。漂流平台上格心会被带走，
# 一段追不上就换新格重发，不在一条过期的 move 上耗满 8 秒。
GOTO_MOVE_TIMEOUT_MS = 2500
# move 失败后重算目标格重试的次数。
GOTO_RETRIES = 3
# 精确模式（投掷出手位）：人已在目标格、离格心超过这么远（m）且 arrived 不成立，
# 再朝格心挪一次。trace-20261031-201659 t=37~93：出手格 (-5,0) 格心 x=7.2，P2 停在
# x=6.72（同格、离格心 0.48m，插件 move 0.5m 内判到达），从这里投被门洞框挡住、
# x≥6.8 就能投；GoToCell 按「同格」判到位，P2 拿着蘑菇原地重试 56 秒。
GOTO_PRECISE_RADIUS = 0.2
# 纠偏 move 的终点越过格心多远（m）：插件离终点 0.5m 内停，越过 0.45m 停在格心前 ≈0.05m。
GOTO_NUDGE_OVERSHOOT = 0.45


class GoToCell(Primitive):
    """走到指定的格（不是「目标旁的交互位」）：投掷出手位用。

    cell_fn(ctx) -> (gx, gz)。每段 move 发出时定格，段内不追新格（每拍换格
    move 命令永远在被替换）；段失败（超时/stuck）就按当前帧**重算**目标格再
    发，最多 GOTO_RETRIES 次。

    arrived(ctx) -> bool：可选的提前到达判据，每拍先看。投掷传它「此刻位置
    投得出去」：站在漂流木筏上，固定格心被木筏带走、追不上，但只要人当下的
    位置能投就该投（trace-20261029-122831 t=351~798 拿着料干耗 12s）。

    precise=True：「到了目标格」不够，离格心 > GOTO_PRECISE_RADIUS 且 arrived 不成立
    时再朝格心挪**一次**（终点越过格心，抵消插件 0.5m 到达容差）；挪完不管准不准都
    SUCCESS，交给后续节点按真实位置判，不在这里死循环。
    """

    def __init__(self, cell_fn, arrived=None, precise=False):
        # type: (Any, Any, bool) -> None
        Primitive.__init__(self)
        self.cell_fn = cell_fn
        self.arrived = arrived
        self.precise = precise
        self._goal = None   # type: Optional[Tuple[int, int]]
        self._fails = 0
        self._nudged = False

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._goal = None
        self._fails = 0
        self._nudged = False

    def _done(self):
        # type: () -> str
        self.reset()
        return Status.SUCCESS

    def _nudge_point(self, ctx, goal):
        # type: (Dict, Tuple[int, int]) -> Optional[List[float]]
        """精确模式下要不要再挪一次：要就返回纠偏 move 的终点，不要返回 None。"""
        if not self.precise or self._nudged:
            return None
        chef = ctx["chef"]
        x, z = chef.get("x"), chef.get("z")
        if x is None or z is None:
            return None
        cx, cz = ctx["w"].to_world(*goal)
        dx, dz = cx - x, cz - z
        d = (dx * dx + dz * dz) ** 0.5
        if d <= GOTO_PRECISE_RADIUS:
            return None
        return [cx + dx / d * GOTO_NUDGE_OVERSHOOT, cz + dz / d * GOTO_NUDGE_OVERSHOOT]

    def _arrive(self, ctx, goal):
        # type: (Dict, Tuple[int, int]) -> str
        """到了目标格：精确模式先纠偏一次，否则 SUCCESS。"""
        point = self._nudge_point(ctx, goal)
        if point is None:
            return self._done()
        self._nudged = True
        self._goal = tuple(goal)
        chef = ctx["chef"]
        cmd = build_command(
            "move",
            goal=point,
            # path[0] 是起点（插件跳过），直接朝纠偏终点走
            path=[[chef["x"], chef["z"]], point],
            target=None,
            kind=None,
            dash=False,
            timeout_ms=GOTO_MOVE_TIMEOUT_MS,
        )
        s = self._begin_exec(ctx, ctx["exec"], cmd)
        # 纠偏发不出去（exec 未连接）不算走位失败：已在目标格，照常往下
        return self._done() if s == Status.FAILURE else s

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
            if self._nudged:
                return self._done()       # 纠偏只挪一次：成败都交给后续节点
            if s == Status.SUCCESS:
                return self._arrive(ctx, self._goal)
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
            return self._arrive(ctx, tuple(goal))
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

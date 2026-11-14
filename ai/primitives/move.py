"""移动与转向原语。

Approach：走到能跟 target 交互的位置。
Face：转向 target（委托后端下为 no-op，转向已并入 move/tap 的重瞄）。
"""
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from ai.space import gaps, navigation, portals, seams, shuttle, traffic
from ai.space import occupancy as O
from ai.query import predicates as P
from ai.query import placement
from ai.exec.bt import Status
from ai.exec.commands import build_command
from ai.query.stations import PLATE_STATION, sink_place_cells
from ai.primitives.base import (Primitive, resolve, target_cell, handle_desc,
                                _blocked_cells, target_grid_callbacks)
from ai.primitives.stuck import BLOCKED_BOX_TTL_TICKS, _ensure_stuck_scratch

# 转向预算（委托后端下 Face 是 no-op，保留该常量仅用于接口兼容）。
FACE_MAX_TICKS = 40

# move 命令超时（毫秒）：委托后端下一趟走位最多 8 秒。
MOVE_TIMEOUT_MS = 8000
# 路沿等灯/车的上限（拍）：实机红绿周期约 5 秒，留 40 秒余量。
TRAFFIC_WAIT_TICKS = 800
# 跑动 dash 的最短路径（格）：短距离不跑，免得在台面边/交互位附近冲过头。
RUN_MIN_CELLS = 5
# 碰撞回传 blocked（硬障碍）后，Approach 局部重绕的重试上限；超过才升格整树 replan。
BLOCK_RETRIES = 3


def _nav_debug(msg):
    # type: (str) -> None
    """走位诊断日志（OC2_NAV_DEBUG=1 时打印到 stderr）。

    过街/取盘这类问题需要同时看：目标身份、目标格、交互位、路径、以及
    有没有拿到路径。默认关，避免刷屏。
    """
    flag = os.environ.get("OC2_NAV_DEBUG", "").strip().lower()
    if flag not in ("", "0", "false", "no", "off"):
        print("[NAV] " + msg, file=sys.stderr, flush=True)


def _nav_time(ctx):
    # type: (Dict) -> Any
    """用插件关卡时钟给日志打时基，和 trace 的 level_time 对得上。"""
    w = ctx.get("w")
    raw = getattr(w, "raw", None) or {}
    return raw.get("level_time")


def _nav_target(target):
    # type: (Any) -> str
    if not isinstance(target, dict):
        return str(target)
    return "%s:%s@(%s,%s)" % (
        target.get("grid") or "", target.get("n") or "?",
        target.get("x"), target.get("z"))


def _flow_dir(w):
    # type: (Any) -> Dict
    """寻路用流场方向（传送带 ∪ 风）；轻量测试 World 无 flow_dir 时退回 belt_dir。"""
    return getattr(w, "flow_dir", None) or getattr(w, "belt_dir", None) or {}


def _flow_speed(w):
    # type: (Any) -> Optional[Dict]
    """寻路用流场速度（传送带 ∪ 风）；无 flow_speed 时退回 belt_speed（旧帧/测试）。"""
    return getattr(w, "flow_speed", None) or getattr(w, "belt_speed", None)


def _nav_crossing(w, path):
    # type: (Any, Optional[List[Tuple[int, int]]]) -> str
    """4-1 过街标记：路径是否穿过马路条带（gx -3..6）。"""
    if not path:
        return ""
    level = str(getattr(w, "level", "") or "")
    if "4_1" not in level:
        return ""
    road = [tuple(c) for c in path if -3 <= c[0] <= 6]
    if not road:
        return ""
    return " cross_road=%s..%s n=%d" % (road[0], road[-1], len(road))


def _nav_path(path):
    # type: (Optional[List[Tuple[int, int]]]) -> str
    if not path:
        return "[]"
    if len(path) <= 24:
        return str([tuple(c) for c in path])
    return str([tuple(c) for c in path[:12]])[:-1] + ", ... , " + str(
        [tuple(c) for c in path[-8:]])[1:]


def _move_cmd(w, path, field, bb, goal, target, kind, timeout_ms, tail=None):
    # type: (object, List[Tuple[int, int]], Any, Any, List[float], Any, Optional[str], int, Optional[List[float]]) -> Dict
    """格子路径 → 插件 move 命令（门洞 gates + 传送门 portals 段 + 放宽超时）。

    `dash_edges` 是**落点下标**：只有这些段才是缝隙跳边，插件只在这几段
    按 Dash。整条路径只要有一段缝就给 dash=True 会让插件在任意平台边缘
    提前起跳（trace-20261110-003648 的 5 次落水有 4 次是这种误跳）。

    `run`：长距离普通地面路径授权插件按 Dash 提速（不是跳边）。插件自己再判
    直段/离终点还远/2m 前方有地板，短路径和跨缝、门洞、传送门段一律不开。

    tail：路点末尾追加的世界点（渡运站位朝台面推一点）。
    """
    points, gates, hops, extra_s = portals.route(w, path, field, bb)
    if tail is not None:
        points = list(points) + [tail]
    edges = gaps.command_gap_edges(w, points)
    # 跑动 dash：只给干净的普通地面长路径；跨缝/等门/过传送门时插件要精细控制。
    run = not edges and not gates and not hops and len(path) >= RUN_MIN_CELLS
    cmd = build_command("move", goal=goal, path=points, target=target, kind=kind,
                        dash=bool(edges), dash_edges=edges, run=run,
                        # 门洞口等门 / 过传送门时放宽超时（插件按 max_wait 等）
                        timeout_ms=timeout_ms + int(1000 * extra_s))
    if gates:
        cmd["gates"] = gates
    if hops:
        cmd["portals"] = hops
    return cmd


def _station_interact_cells(st, cell, w, kind, include_belts=False, field=None):
    # type: (Dict, Tuple[int, int], object, Optional[str], bool, Optional[Dict]) -> List[Tuple[int, int]]
    """站点交互位：出餐口只取厨房内侧（前侧），其余站点用四邻并集。

    出餐口是嵌在外墙里的一段墙，墙后那一面是死路，站过去送不了餐；
    见 navigation.serving_hatch_interact_cells 的判据（沿墙法向的深度）。
    `field` 传 P2 的距离场：两侧都算，优先选实际可达的一侧（4-1 墙外死区
    比厨房侧更深但不可达）。
    """
    if st.get("t") == PLATE_STATION:
        fp = O.station_block_cells(st, w.terrain, w.mapping)
        return navigation.serving_hatch_interact_cells(
            fp, w.walkable, w.belt_cells, field=field,
            include_belts=include_belts)
    if st.get("t") == "conveyor":
        from ai.space import channels as CH
        moving = CH.conveyor_interact_cells(w, st)
        if moving is not None:
            # 已经是可站位的竹筏地板格，直接返回，不再取四邻。
            return list(moving)
        cells = O.interaction_footprint(st, cell, getattr(w, "stations", ()),
                                        w.to_grid, w.to_world, w.walkable)
    elif st.get("t") == "washing" and kind == "placement":
        cells = sink_place_cells(st, w.stations, w.to_grid)
    else:
        # 与规划层 reachability.can_interact 同一口径（见 interaction_footprint）。
        cells = O.interaction_footprint(st, cell, getattr(w, "stations", ()),
                                        w.to_grid, w.to_world, w.walkable)
    return navigation.interact_cells_multi(
        cells, w.walkable, w.belt_cells, include_belts=include_belts)


def _wait_for_light(prim, ctx, label, kind, cur, info):
    # type: (Any, Dict, str, Optional[str], Any, str) -> str
    """在路沿/中线等灯；等外部（灯/车）不计入 task 的 200 拍超时预算。"""
    prim._traffic_wait_ticks += 1
    if prim._traffic_wait_ticks > TRAFFIC_WAIT_TICKS:
        prim._traffic_wait_ticks = 0
        _nav_debug("traffic wait timeout t=%s target=%s %s"
                   % (_nav_time(ctx), label, info))
        return Status.FAILURE
    ctx["external_wait"] = ctx.get("external_wait", 0) + 1
    ctx["traffic_wait"] = True
    _nav_debug("traffic wait t=%s target=%s kind=%s cur=%s %s"
               % (_nav_time(ctx), label, kind, cur, info))
    return Status.RUNNING


def _issue_median(prim, ctx, ex, w, cur, field, spot, target, kind, timeout_ms,
                  label, info):
    # type: (Any, Dict, Any, Any, Any, Dict, Any, Any, Optional[str], int, str, str) -> Optional[str]
    """发一条只走到中线等待点的小段 move；失败返回 None（调用方退回等待）。"""
    mpath = navigation.trace_back(
        field, spot[0], _flow_dir(w), ctx.get("soft_cost"),
        seams.nav_predicates(w, ctx.get("bb"))[1])
    if not mpath:
        return None
    _nav_debug("traffic median t=%s target=%s kind=%s cur=%s median=%s %s"
               % (_nav_time(ctx), label, kind, cur, spot, info))
    prim._median_cmd = True
    cmd = _move_cmd(w, path=mpath, field=field, bb=ctx.get("bb"),
                    goal=list(spot[1]), target=target, kind=kind,
                    timeout_ms=timeout_ms)
    return prim._begin_exec(ctx, ex, cmd)


def _crossing_guard(prim, ctx, ex, w, cur, field, path, label, kind,
                    timeout_ms, target=None):
    # type: (Any, Dict, Any, Any, Any, Dict, Any, str, Optional[str], int, Any) -> Optional[str]
    """4-1 过街门控（Approach / GoToCell 共用）。

    返回 None = 不涉及过街、或可以继续走；否则返回本拍状态。规则：

    1. 门控在**离开路沿/中线那一拍**判，不在规划整段路时判一次。
    2. 路径要过街但人还没到路沿：先只走到路沿。
    3. 按**车道腿**放行：4-1 每个信号周期只有一辆车跑（蓝/绿交替），另一条
       车道停着。近侧腿的安全就先去中线、远侧腿的安全再走完；整段都安全
       （红灯，两车都停）就一次走完。正在过的这一腿来车、又还没过中线时，
       改道到两条车道中间等。
    4. 中线位置用两车缝隙中点算，**不依赖路径里有没有中线格**——人过了中线
       之后路径里就没有那一格了，若退回「已在路上就继续」会在远侧车道有车时
       放行（trace-20261110-211743 L166 被 Blue 车压）。
    """
    if not traffic.path_crosses_road(w, path):
        return None
    bb = ctx.get("bb")
    chef = ctx.get("chef")
    info = traffic.state_text(w, bb)
    spot = traffic.median_spot(w, path)
    road = traffic.path_road_cells(w, path)
    near, far = traffic.split_at_median(w, path, spot)
    rest = near + far if spot is not None else road
    gap = traffic.median_gap(w)
    in_road = traffic.chef_in_road(w, chef)
    past = traffic.past_median(w, chef, gap, path)
    if prim._pending_cmd is not None:
        if prim._curb_cmd or prim._median_cmd:
            return None
        # 正在过街：剩余这一整段还不安全、人又没过中线 → 去中线等。
        if spot is not None and in_road and not past \
                and not traffic.leg_safe(w, rest):
            got = _issue_median(prim, ctx, ex, w, cur, field, spot, target,
                                kind, timeout_ms, label, info)
            if got is not None:
                return got
        return None
    curb = traffic.curb_index(w, path)
    if curb is not None and cur != path[curb]:
        wait_goal = path[curb]
        _nav_debug("traffic curb t=%s target=%s kind=%s cur=%s curb=%s %s"
                   % (_nav_time(ctx), label, kind, cur, wait_goal, info))
        prim._traffic_wait_ticks = 0
        prim._curb_cmd = True
        cmd = _move_cmd(w, path[:curb + 1], field, ctx.get("bb"),
                        list(w.to_world(*wait_goal)), target, kind, timeout_ms)
        return prim._begin_exec(ctx, ex, cmd)
    # 中线等待：锁存状态管「改道过去」这一趟（5Hz 位置在移动中会差 1 格，
    # 不能用来反复判到没到）；at_median 管「站定在中线」的其它情况（换任务后
    # 新树从车道中间出发）。两者都看剩余这段路（rest）的车。
    if prim._median_wait or traffic.at_median(w, chef, spot):
        if not traffic.leg_safe(w, rest):
            return _wait_for_light(prim, ctx, label, kind, cur, info)
        prim._median_wait = False
        return None
    if spot is None:
        # 路径里没有中线格：已经过了中线，或本来就没有可站的缝。
        if in_road:
            # 过了中线再回头更危险：把剩下这一小段走完（车起步到路口 ≥1s）。
            return None
        if not traffic.safe_to_cross(w, bb, path, cur, chef):
            return _wait_for_light(prim, ctx, label, kind, cur, info)
        return None
    # 在路沿、还没到中线：整段安全就一次走完；只有近侧腿安全就先到中线。
    if traffic.leg_safe(w, rest):
        return None
    if traffic.leg_safe(w, near):
        got = _issue_median(prim, ctx, ex, w, cur, field, spot, target, kind,
                            timeout_ms, label, info)
        if got is not None:
            return got
    return _wait_for_light(prim, ctx, label, kind, cur, info)


def _abort_on_belt_flip(prim, ctx):
    # type: (Any, Dict) -> Optional[str]
    """4-3 快带 / 4-4 风区方向翻转：冻结路径上出现快带（或逆风）逆行 → 提前中止在途 move。

    方向一变，发令时算好的路径就走不动了（快带/逆风走不过去）。与其等插件
    8s stuck 超时，不如每拍看一次，命中就 halt 交回 Planner 按新方向重算。
    只在主 move 在途时生效（curb/median 小段 move 不设 _belt_path，且 4-1
    没有快带）。flow_dir/flow_speed = 传送带 ∪ 风，所以风的翻转也走这条兜底。
    """
    if prim._pending_cmd is None:
        return None
    path = getattr(prim, "_belt_path", None)
    if not path:
        return None
    w = ctx.get("w")
    if w is None or not navigation.path_blocked_by_fast_belt(
            path, _flow_dir(w), _flow_speed(w)):
        return None
    prim._exec_halt()
    prim._belt_path = None
    _nav_debug("belt-flip t=%s path=%s" % (_nav_time(ctx), _nav_path(path)))
    return Status.FAILURE


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
        self._traffic_wait_ticks = 0   # 4-1 路沿/中线等灯/车
        self._curb_cmd = False         # 在途的是「先走到路沿」的小段 move
        self._median_cmd = False       # 在途的是「改道到车道中间」的小段 move
        self._median_wait = False      # 已到中线，锁存等下一轮红灯
        self._belt_path = None         # 发令时冻结的路径（方向翻转检测用）
        self._block_retries = 0        # 碰撞回传 blocked 后的局部重绕计数

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._staging = False
        self._shuttle_wait_ticks = 0
        self._traffic_wait_ticks = 0
        self._curb_cmd = False
        self._median_cmd = False
        self._median_wait = False
        self._belt_path = None
        self._block_retries = 0

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
        hatch = shuttle.hatch_fallback(w, st)
        if hatch:
            # 漂移出餐口此刻没有格内交互位：在格外站位旁（或其落脚格）即算到位
            return not (shuttle.at_stance(chef, hatch)
                        or cur in {s["cell"] for s in hatch})
        goals = _station_interact_cells(st, cell, w, self.kind,
                                        include_belts=True,
                                        field=ctx.get('field'))
        wash = (shuttle.wash_stances(w, st)
                if isinstance(st, dict) and st.get("t") == "washing" and not goals
                else [])
        if wash:
            # 竹筏边缘洗碗池没有格内交互位：站在格外站位旁即算到位
            return not (shuttle.at_stance(chef, wash)
                        or cur in {s["cell"] for s in wash})
        if cur == cell and (w.walkable(*cell) or P.interact_ready(chef, st, self.kind, w.to_grid)):
            return False
        if cur not in goals:
            _nav_debug("reapproach t=%s target=%s kind=%s cur=%s cell=%s goals=%s"
                       % (_nav_time(ctx), _nav_target(st), self.kind,
                          cur, cell, goals))
            return True
        return False

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
        ctx["traffic_wait"] = False
        w, chef = ctx["w"], ctx["chef"]
        st = resolve(self.target, ctx)
        if st is None:
            return Status.FAILURE
        cell = target_cell(st, ctx)
        if cell is None:
            return Status.FAILURE

        cur = P.chef_cell(chef, w.to_grid)
        if cur is None:
            _nav_debug("no-chef-cell t=%s target=%s" % (_nav_time(ctx), _nav_target(st)))
            return Status.FAILURE
        if self._pending_cmd is None:
            _nav_debug("plan t=%s target=%s kind=%s cur=%s cell=%s"
                       % (_nav_time(ctx), _nav_target(st), self.kind, cur, cell))
        shuttle_st = shuttle.station_of(w, st)
        if shuttle_st is not None:
            return self._tick_shuttle(ctx, ex, st, shuttle_st)
        # 目标就在脚下（散料/掉落物落自己站的那格）：无需走。
        if cur == cell and w.walkable(cell[0], cell[1]):
            return Status.SUCCESS

        hatch = shuttle.hatch_fallback(w, st)
        if hatch:
            return self._tick_hatch(ctx, ex, st, hatch)

        goals = _station_interact_cells(st, cell, w, self.kind,
                                        field=ctx.get("field"))
        wash = (shuttle.wash_stances(w, st)
                if isinstance(st, dict) and st.get("t") == "washing" and not goals
                else [])
        if wash:
            return self._tick_hatch(ctx, ex, st, wash)

        blocked = _blocked_cells(ctx, P.resource_id(st))
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
                                            include_belts=True,
                                            field=ctx.get("field"))
            got = navigation.nearest_cost(field, [g for g in goals if g not in blocked])
        if got is None:
            _nav_debug("no-goal t=%s target=%s kind=%s cur=%s cell=%s goals=%s "
                       "blocked=%s field_has=%s"
                       % (_nav_time(ctx), _nav_target(st), self.kind, cur, cell,
                          goals, sorted(blocked), len(field or {})))
            return Status.FAILURE
        goal = got[0]
        path = navigation.trace_back(field, goal, _flow_dir(w), ctx.get("soft_cost"),
                                     seams.nav_predicates(w, ctx.get("bb"))[1])
        if not path:
            _nav_debug("no-path t=%s target=%s kind=%s cur=%s goal=%s goals=%s"
                       % (_nav_time(ctx), _nav_target(st), self.kind, cur, goal, goals))
            return Status.FAILURE

        # 4-1 过街门控：在路沿判灯、途中灯变了去中线等（见 _crossing_guard）。
        guard = _crossing_guard(
            self, ctx, ex, w, cur, field, path, _nav_target(st), self.kind,
            MOVE_TIMEOUT_MS,
            handle_desc(st, target_grid_callbacks(w, st)[0]))
        if guard is not None:
            return guard

        if self._pending_cmd is None:
            self._traffic_wait_ticks = 0
            self._curb_cmd = False
            self._median_cmd = False
            _nav_debug("move t=%s target=%s kind=%s cur=%s goal=%s goals=%s%s path=%s"
                       % (_nav_time(ctx), _nav_target(st), self.kind, cur, goal,
                          goals, _nav_crossing(w, path), _nav_path(path)))
            cmd = _move_cmd(w, path, field, ctx.get("bb"), list(w.to_world(*goal)),
                            handle_desc(st, target_grid_callbacks(w, st)[0]), self.kind,
                            MOVE_TIMEOUT_MS)
            self._belt_path = path
            return self._begin_exec(ctx, ex, cmd)
        aborted = _abort_on_belt_flip(self, ctx)
        if aborted is not None:
            return aborted
        status = self._poll_exec(ctx, ex)
        if self._curb_cmd or self._median_cmd:
            if status != Status.RUNNING:
                if self._median_cmd and status == Status.SUCCESS:
                    self._median_wait = True
                self._curb_cmd = False
                self._median_cmd = False
                # 到路沿/中线不等于到目标：回去重新判灯，不能报 SUCCESS。
                return Status.RUNNING if status == Status.SUCCESS else status
        if status == Status.SUCCESS:
            self._block_retries = 0
            return status
        # 碰撞回传 blocked（硬障碍）：不在 Approach 里整树 FAILURE，先局部重绕——
        # 拉黑障碍整块，下一拍用新 field 重 trace（同 GoToCell 的失败重算惯用法）。
        mf = ctx.get("motion_failure") or {}
        if status == Status.FAILURE and mf.get("reason") == "blocked" \
                and mf.get("blocked_kind") == "obstacle" and mf.get("blocked_cells"):
            tick = ctx.get("tick") or 0
            scratch = _ensure_stuck_scratch(ctx)
            for cell in mf["blocked_cells"]:
                scratch.bad_cells[tuple(cell)] = tick + BLOCKED_BOX_TTL_TICKS
            self._belt_path = None
            self._block_retries += 1
            if self._block_retries <= BLOCK_RETRIES:
                _nav_debug("blocked-reroute t=%s target=%s kind=%s cells=%s retry=%d"
                           % (_nav_time(ctx), _nav_target(self.target),
                              mf.get("blocked_kind"), mf.get("blocked_cells"),
                              self._block_retries))
                return Status.RUNNING
            self._block_retries = 0
            return Status.FAILURE
        return status

    def _tick_shuttle(self, ctx, ex, st, shuttle_st):
        # type: (Dict, Any, Dict, Dict) -> str
        """渡运台面（ai/space/shuttle.py）：走到格外站位；篮子不在口上就等它。

        寻路到站位挂的落脚格，路径末尾追加站位点（朝台面推一点）作 move 终点；
        插件按交互句柄判到达。

        站位此刻不在可达场里（篮子在路上 / 在对面）：时间表能预测就原地等，快到时
        先走到上一轮的落脚格（_await_shuttle）。等待在原语层做，所有 task 拿篮子上的
        东西都一样，不用各自写等篮子分支。预测不了 → FAILURE 交还 Planner。
        """
        w = ctx["w"]
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
        return self._go_stance(ctx, ex, st, options)

    def _tick_hatch(self, ctx, ex, st, stances):
        # type: (Dict, Any, Dict, List[Dict]) -> str
        """漂移出餐口格内交互位为空（shuttle.hatch_fallback）：走到格外站位。"""
        if self._pending_cmd is not None:
            return self._poll_exec(ctx, ex)
        field = ctx["field"]
        options = stances if field is None else [s for s in stances if s["cell"] in field]
        if not options:
            return Status.FAILURE
        return self._go_stance(ctx, ex, st, options)

    def _go_stance(self, ctx, ex, st, options):
        # type: (Dict, Any, Dict, List[Dict]) -> str
        """走到最近的格外站位：寻路到落脚格，路径末尾追加站位点作 move 终点。"""
        w, chef = ctx["w"], ctx["chef"]
        field = ctx["field"]
        if shuttle.at_stance(chef, options):
            return Status.SUCCESS
        if field is None:
            best = options[0]
        else:
            best = min(options, key=lambda s: field[s["cell"]])
        path = navigation.trace_back(field or {}, best["cell"], _flow_dir(w),
                                     ctx.get("soft_cost"),
                                     seams.nav_predicates(w, ctx.get("bb"))[1])
        if not path:
            return Status.FAILURE
        goal = shuttle.goal_point(best)
        cmd = _move_cmd(w, path, field, ctx.get("bb"), goal,
                        handle_desc(st, target_grid_callbacks(w, st)[0]), self.kind,
                        MOVE_TIMEOUT_MS, tail=goal)
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
        path = navigation.trace_back(field or {}, cell, _flow_dir(w), ctx.get("soft_cost"),
                                     seams.nav_predicates(w, ctx.get("bb"))[1])
        if not path:
            return Status.RUNNING
        cmd = _move_cmd(w, path, field, ctx.get("bb"), list(w.to_world(*cell)),
                        None, None, MOVE_TIMEOUT_MS)
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
        self._curb_cmd = False     # 在途的是「先走到路沿」的小段 move
        self._median_cmd = False   # 在途的是「改道到车道中间」的小段 move
        self._median_wait = False  # 已到中线，锁存等下一轮红灯
        self._traffic_wait_ticks = 0
        self._goal = None   # type: Optional[Tuple[int, int]]
        self._fails = 0
        self._nudged = False
        self._belt_path = None   # 发令时冻结的路径（方向翻转检测用）

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._curb_cmd = False
        self._median_cmd = False
        self._median_wait = False
        self._traffic_wait_ticks = 0
        self._goal = None
        self._fails = 0
        self._nudged = False
        self._belt_path = None

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
        field = ctx["field"] or {}
        goal = self._goal or self.cell_fn(ctx)
        path = None
        if goal is not None:
            path = navigation.trace_back(
                field, tuple(goal), _flow_dir(w), ctx.get("soft_cost"),
                seams.nav_predicates(w, ctx.get("bb"))[1])
        # 4-1 过街：和 Approach 共用同一套门控（路沿判灯 / 途中去中线等）。
        if path:
            guard = _crossing_guard(
                self, ctx, ctx["exec"], w, cur, field, path,
                str(tuple(goal)), None, GOTO_MOVE_TIMEOUT_MS)
            if guard is not None:
                if self._pending_cmd is not None:
                    self._goal = tuple(goal)      # 段内定格目标格
                return guard
        if self._pending_cmd is not None:
            aborted = _abort_on_belt_flip(self, ctx)
            if aborted is not None:
                return aborted
            s = self._poll_exec(ctx, ctx["exec"])
            if s == Status.RUNNING:
                return s
            if self._curb_cmd or self._median_cmd:
                # 到路沿/中线不等于到目标：回去按当前灯态重新判，别报 SUCCESS。
                if self._median_cmd and s == Status.SUCCESS:
                    self._median_wait = True
                self._curb_cmd = False
                self._median_cmd = False
                if s == Status.FAILURE:
                    self._goal = None
                    self._fails += 1
                    if self._fails > GOTO_RETRIES:
                        self.reset()
                        return Status.FAILURE
                return Status.RUNNING
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
        if goal is None or not path:
            return Status.FAILURE
        if cur == tuple(goal):
            return self._arrive(ctx, tuple(goal))
        self._goal = tuple(goal)
        cmd = _move_cmd(w, path, field, ctx.get("bb"), list(w.to_world(*self._goal)),
                        None, None, GOTO_MOVE_TIMEOUT_MS)
        self._belt_path = path
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

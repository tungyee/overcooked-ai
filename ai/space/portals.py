"""传送门（Teleportal）：寻路跳边 + 误踩避让 + move 命令的过门段。

机制（反编译 Assembly-CSharp，ServerTeleportal / *TeleportalPlayer*）：
  - 人进门的触发碰撞体、且位置落在门正面 arc_dir ± arc_deg 的扇区里，就被
    强制传走（OnTriggerStay，不需要按键、不能取消）。约 1.2 s 不受控，落在出口门
    player_receiver 的点上、朝向该点 forward；手持物随人走。
  - 刚传过的对象在出口门上记 recentlyTeleported，离开触发区或面朝门（±45°）才清，
    所以落地后不会被立刻弹回，但转身往门里走会再传。
插件 0.24.3+ 在帧里报 portals[]（Oc2Portals.cs），旧帧没有 → 本模块全部空操作。

寻路怎么用它：
  - 触发格（格中心 + 厨师半径碰得到触发盒）对寻路**不可走**：普通路线不会再
    路过门口被误传（trace-20261101-152446 t=19341/19490：去回盘口的路贴着门走）。
  - 入口邻格 → 出口邻格 加一条跳边，代价 = 进门 1 步 + 传送 TELEPORT_STEPS +
    出门 1 步。distance_field 的 jumps/jump_cost 承载，ETA / 可达性自动受益。
  - 路径里出现跳边时，move 命令在两格之间插「推门点 / 落地点」两个路点，并带
    portals[] 段描述；插件 MoveCommand 走到推门点后朝门推、等传送完成再续走。

只读 World 解析好的 PortalMap / mapping，无副作用。
"""
import math
from typing import Callable, Dict, List, Optional, Set, Tuple

from ai.space import lanes, seams
from ai.space.occupancy import CHEF_CLEARANCE

Cell = Tuple[int, int]

# 传送耗时：实测进门到落地恢复控制 1.2 s（trace-20261101-160703 [TELEPORT]
# start→end 1.20/1.20/1.20/1.20 s），按 seams.SEC_PER_STEP（0.25 s/步）折 5 步，
# 再加 1 步余量给吸入前的减速。
TELEPORT_STEPS = 6
# 插件在推门点等门空闲的上限（秒）：门正在传别人（P1）时要等它传完，一次 ≈1.2 s。
PORTAL_WAIT_S = 3.0
# move 超时按每次过门放宽的秒数：传送 1.2 s + 等门。
PORTAL_HOP_S = 1.5 + PORTAL_WAIT_S

_NEIGHBORS8 = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1))


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class Portal(object):
    """一扇门（插件 portals[] 一条）。坐标都是世界 xz。"""

    __slots__ = ("id", "exit", "active", "center", "arc_dir", "arc_deg",
                 "box", "land", "land_fwd", "player_sender", "player_receiver")

    def __init__(self, d):
        # type: (Dict) -> None
        self.id = d.get("id")
        self.exit = d.get("exit")
        self.active = bool(d.get("active", True)) and d.get("server_enabled") is not False
        c = d.get("center") or [None, None]
        self.center = (_f(c[0]), _f(c[1]))
        ad = d.get("arc_dir") or [0.0, -1.0]
        self.arc_dir = (_f(ad[0], 0.0), _f(ad[1], -1.0))
        self.arc_deg = _f(d.get("arc_deg"), 90.0)
        t = d.get("trigger") or {}
        box = tuple(_f(t.get(k)) for k in ("minx", "maxx", "minz", "maxz"))
        self.box = box if None not in box and t.get("enabled", True) is not False else None
        pr = d.get("player_receiver")
        pt = (pr or {}).get("point") or d.get("point")
        self.land = (_f(pt[0]), _f(pt[1])) if pt and len(pt) >= 2 else None
        fw = (pr or {}).get("fwd") or d.get("point_fwd") or [0.0, -1.0]
        self.land_fwd = (_f(fw[0], 0.0), _f(fw[1], -1.0))
        self.player_sender = bool(d.get("player_sender"))
        self.player_receiver = pr is not None

    def touches(self, x, z, pad=CHEF_CLEARANCE):
        # type: (float, float, float) -> bool
        """站在 (x, z) 的厨师胶囊碰不碰得到触发盒（盒外扩 pad）。"""
        if self.box is None:
            return False
        minx, maxx, minz, maxz = self.box
        return minx - pad <= x <= maxx + pad and minz - pad <= z <= maxz + pad

    def in_arc(self, x, z):
        # type: (float, float) -> bool
        """(x, z) 在不在门正面扇区里（ServerTeleportal.IsInTeleportArc，看位置不看朝向）。"""
        cx, cz = self.center
        if cx is None or cz is None:
            return False
        vx, vz = x - cx, z - cz
        n = math.hypot(vx, vz)
        if n <= 1e-6:
            return False
        ax, az = self.arc_dir
        an = math.hypot(ax, az) or 1.0
        cos = (vx * ax + vz * az) / (n * an)
        return math.degrees(math.acos(max(-1.0, min(1.0, cos)))) < self.arc_deg


class PortalMap(object):
    """一帧的门：触发格 + 跳边。walkable 是 World.walkable（站位口径）。"""

    def __init__(self, portals, mapping, walkable):
        # type: (List[Portal], object, Callable[[int, int], bool]) -> None
        self.portals = {p.id: p for p in portals if p.id}
        self.mapping = mapping
        self.trigger_cells = set()   # type: Set[Cell]
        self._trigger_of = {}        # type: Dict[Cell, str]
        # (入口邻格, 出口邻格) → 过门段描述；jumps 双向不对称，按门分别登记
        self.hops = {}               # type: Dict[Tuple[Cell, Cell], Dict]
        self._entry_trigger = {}     # type: Dict[Tuple[Cell, Cell], Set[Cell]]
        for p in self.portals.values():
            for cell in self._cells_touching(p):
                self.trigger_cells.add(cell)
                self._trigger_of[cell] = p.id
        for p in self.portals.values():
            self._link(p, walkable)

    def _cells_touching(self, p):
        # type: (Portal) -> List[Cell]
        if p.box is None:
            return []
        m = self.mapping
        minx, maxx, minz, maxz = p.box
        lo = m.to_grid(minx - CHEF_CLEARANCE, minz - CHEF_CLEARANCE)
        hi = m.to_grid(maxx + CHEF_CLEARANCE, maxz + CHEF_CLEARANCE)
        out = []
        for gx in range(min(lo[0], hi[0]) - 1, max(lo[0], hi[0]) + 2):
            for gz in range(min(lo[1], hi[1]) - 1, max(lo[1], hi[1]) + 2):
                x, z = m.to_world(gx, gz)
                if p.touches(x, z):
                    out.append((gx, gz))
        return out

    def _link(self, p, walkable):
        # type: (Portal, Callable[[int, int], bool]) -> None
        """p 作入口：p 触发格的扇区内邻格 → 出口门落地格的邻格。"""
        out = self.portals.get(p.exit)
        if not (p.active and p.player_sender and out is not None and out.active
                and out.player_receiver and out.land is not None):
            return
        mine = [c for c, pid in self._trigger_of.items() if pid == p.id]
        if not mine:
            return
        m = self.mapping
        entries = set()  # type: Set[Cell]
        for c in mine:
            for dx, dz in _NEIGHBORS8[:4]:
                n = (c[0] + dx, c[1] + dz)
                if n in self.trigger_cells or not walkable(*n):
                    continue
                if p.in_arc(*m.to_world(*n)):
                    entries.add(n)
        land_cell = m.to_grid(*out.land)
        exits = set()  # type: Set[Cell]
        for dx, dz in _NEIGHBORS8[:4]:
            n = (land_cell[0] + dx, land_cell[1] + dz)
            if n not in self.trigger_cells and walkable(*n):
                exits.add(n)
        if land_cell not in self.trigger_cells and walkable(*land_cell):
            exits = {land_cell}
        push = [round(p.center[0], 3), round(p.center[1], 3)]
        land = [round(out.land[0], 3), round(out.land[1], 3)]
        for a in entries:
            for b in exits:
                self.hops[(a, b)] = {"portal": p.id, "exit_portal": out.id,
                                     "push": push, "land": land}
                self._entry_trigger[(a, b)] = set(mine)

    def jumps(self, blocked=None):
        # type: (Optional[Set[Cell]]) -> Tuple[Dict[Cell, List[Cell]], Dict[Tuple[Cell, Cell], int]]
        """(jumps, jump_cost)。入口触发格被拉黑（插件报 portal busy）的门不连。"""
        blocked = blocked or set()
        jumps = {}       # type: Dict[Cell, List[Cell]]
        cost = {}        # type: Dict[Tuple[Cell, Cell], int]
        for (a, b), _hop in self.hops.items():
            if self._entry_trigger[(a, b)] & blocked:
                continue
            jumps.setdefault(a, []).append(b)
            cost[(a, b)] = 2 + TELEPORT_STEPS
        return jumps, cost


def build(raw_portals, mapping, walkable):
    # type: (Optional[List[Dict]], object, Callable[[int, int], bool]) -> Optional[PortalMap]
    """插件 portals[] → PortalMap；没有门（旧帧 / 普通关）返回 None。"""
    ps = [Portal(d) for d in raw_portals or () if isinstance(d, dict)]
    ps = [p for p in ps if p.id and p.box is not None]
    if not ps:
        return None
    return PortalMap(ps, mapping, walkable)


def nav_args(world, bb=None, blocked=None):
    # type: (object, object, Optional[Set[Cell]]) -> Dict
    """distance_field 的寻路参数：门洞（seams）叠传送门。

    返回 dict(is_walkable, edge_blocked, wait_cost, jumps, jump_cost)。触发格对
    寻路不可走（起点不受影响：distance_field 总收 start）；跳边与门洞跳边合并。
    """
    walkable, edge_blocked = seams.nav_predicates(world, bb)
    jumps = {k: list(v) for k, v in (seams.nav_jumps(world, bb)[0] or {}).items()}
    jump_cost = {}  # type: Dict[Tuple[Cell, Cell], int]
    pm = getattr(world, "portal_map", None)
    if pm is not None and pm.trigger_cells:
        trig = pm.trigger_cells
        base = walkable

        def walkable(gx, gz):  # noqa: F811 - 叠一层触发格
            # type: (int, int) -> bool
            return (gx, gz) not in trig and base(gx, gz)

        pj, jump_cost = pm.jumps(blocked)
        for a, bs in pj.items():
            jumps.setdefault(a, []).extend(bs)
    nav = dict(is_walkable=walkable, edge_blocked=edge_blocked,
               wait_cost=seams.wait_cost_of(world, bb), jumps=jumps,
               jump_cost=jump_cost)
    # 半格走廊（ai/space/lanes.py）：只补普通格子断开的 S 弯，不碰门洞/触发格。
    return lanes.wrap(nav, getattr(world, "lane_map", None), lanes.excluded_cells(world))


def route(world, path, field=None, bb=None):
    # type: (object, List[Cell], Optional[Dict], object) -> Tuple[List[List[float]], List[Dict], List[Dict], float]
    """格子路径 → (路点, gates, portals, 额外超时秒)。

    无门时等价 seams.path_points。路径里相邻两格是过门跳边时，在两格之间插
    「推门点、落地点」，portals[] 记它们的下标（enter / exit = enter+1）。
    门洞（gates）按段各算一遍再平移下标，两者互不干扰。
    """
    pm = getattr(world, "portal_map", None)
    hops = pm.hops if pm is not None else {}
    # 两类切点：过传送门（插推门点/落地点）、半格走廊（插两个拐点，ai/space/lanes.py）
    cuts = []     # type: List[Tuple[int, Optional[Dict], Optional[List]]]
    for i in range(1, len(path)):
        pair = (tuple(path[i - 1]), tuple(path[i]))
        if pair in hops:
            cuts.append((i, hops[pair], None))
            continue
        corners = lanes.lane_corners(world, pair[0], pair[1])
        if corners:
            cuts.append((i, None, corners))
    if not cuts:
        points, gates = seams.path_points(world, path, field, bb)
        return points, gates, [], seams.gates_wait_seconds(gates)
    points = []   # type: List[List[float]]
    gates = []    # type: List[Dict]
    portals = []  # type: List[Dict]
    start = 0
    for c, hop, corners in cuts + [(len(path), None, None)]:
        seg = path[start:c]
        pts, gs = seams.path_points(world, seg, field, bb)
        off = len(points)
        for g in gs:
            g = dict(g)
            g["enter"] += off
            g["exit"] += off
            gates.append(g)
        points.extend(pts)
        if hop is not None:
            portals.append({"enter": len(points), "exit": len(points) + 1,
                            "portal": hop["portal"], "push": hop["push"],
                            "land": hop["land"], "max_wait": PORTAL_WAIT_S})
            points.append(list(hop["push"]))
            points.append(list(hop["land"]))
        elif corners:
            points.extend([round(p[0], 4), round(p[1], 4)] for p in corners)
        start = c
    extra = seams.gates_wait_seconds(gates) + PORTAL_HOP_S * len(portals)
    return points, gates, portals, extra

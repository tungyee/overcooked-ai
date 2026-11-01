"""半格走廊（lane）：通道夹在两排格中心之间时，补一条斜对角的寻路边。

3_6_Dynamic_Lvl_02 的中间平台上两只料箱错开半格，中间 z∈(-8.4,-7.2) 一整行
全开，P1 实际走得过去（trace-20261101-184314 t=9700）。但主网格格中心
z=-8.4/-7.2 正好压在料箱边上：下一排只有右半边可走、上一排只有左半边可走，
上下错开不相邻，S 弯被切断，上层木筏连同出餐口整段判不可达。

这里不改 `walkable`（站位/交互位仍按格中心），只给寻路叠一类跳边：
  - A、B 都可走，跨一排（或一列）且横向错开 1~MAX_SHIFT 格；
  - 普通格子走不过去（A→B 的单调格路全被挡，见 _grid_connected）——
    只补断开的地方，已连通处的距离场一格不变；
  - 几何上走得过去：A → 拐点1 → 拐点2 → B 三段直线（拐点在两排格中心的中线
    上），沿线每 SAMPLE_STEP 取点，厨师中心按与 walkable 相同的阻挡外扩
    CHEF_CLEARANCE 判净空，并且脚下实打实是地板（离地板边 ≥ SUPPORT_MARGIN）。

跨不过的缝：反编译 GroundCast / ApplyGravityForce（chef-fall-mechanics），
中心离开地板边就开始受重力、滑落进 RespawnCollider。SUPPORT_MARGIN 让走廊点
不贴着地板边缘，地板之间真有缝时整条走廊不连（宁可不连，不让 P2 掉水）。

路点：distance_field 把它当跳边（jumps / jump_cost），move 命令在 A、B 之间
插入两个拐点（portals.route），插件沿直线段走，不斜穿料箱。

只读 terrain（层号 AABB），纯函数 + 每帧缓存，无副作用。
"""
from typing import Callable, Dict, List, Optional, Set, Tuple

from ai.space import occupancy

Cell = Tuple[int, int]
Point = Tuple[float, float]

# 横向最多错开几格：Platform 实测 1~2 格（随 Raft Two 横漂，格中心相对料箱的
# 偏移在 0~0.3m 间变）。再大就是绕远路，交给普通格子。
MAX_SHIFT = 2
# 沿线采样步长（米）：远小于最窄的阻挡（料箱 1.0~1.2m）与外扩 0.35。
SAMPLE_STEP = 0.1
# 走廊点离地板边至少这么远才算脚下有地（米）。胶囊半径 0.4，中心离边 0.1 内
# 身体有一大半悬空；拼接平台之间 0.16~0.22m 的缝会因此被判不连，也有意为之：
# 跨缝由格子本身（格中心在地板里）负责，走廊只认整块地板上的通道。
SUPPORT_MARGIN = 0.1

_DIRS = ((0, 1), (0, -1), (1, 0), (-1, 0))


class LaneMap(object):
    """一帧的走廊几何。walkable 由调用方传入（寻路口径，含门洞/传送门修正）。"""

    def __init__(self, ground, blocking, killplane, hazards, occupied, mapping,
                 belt_cells=None):
        # type: (List, List, List, List, Set[Cell], object, Optional[Set[Cell]]) -> None
        self.ground = ground
        self.blocking = blocking
        self.killplane = killplane
        self.hazards = hazards
        self.occupied = set(occupied or ()) - set(belt_cells or ())
        self.mapping = mapping
        # 纯几何缓存（与寻路口径无关）；拓扑（hops_from）随 walkable 变，不缓存在这里。
        self._geo = {}    # type: Dict[Tuple[Cell, Cell], Optional[List[Point]]]

    # ------------------------------------------------------------ 几何

    def _clear(self, x, z):
        # type: (float, float) -> bool
        if not occupancy._point_in_any_box(x, z, self.ground, margin=-SUPPORT_MARGIN):
            return False
        if occupancy._point_in_any_box(x, z, self.killplane):
            return False
        if occupancy._point_in_any_box(x, z, self.hazards):
            return False
        if occupancy._point_in_any_box(x, z, self.blocking,
                                       margin=occupancy.CHEF_CLEARANCE):
            return False
        # 运行时占用格（站点占格）：中线点正好落在两格交界，两边都查。
        m = self.mapping
        e = 1e-3
        for dx in (-e, e):
            for dz in (-e, e):
                if m.to_grid(x + dx, z + dz) in self.occupied:
                    return False
        return True

    def _segment_clear(self, p, q):
        # type: (Point, Point) -> bool
        dx, dz = q[0] - p[0], q[1] - p[1]
        length = max(abs(dx), abs(dz))
        n = max(1, int(length / SAMPLE_STEP + 0.999))
        for i in range(n + 1):
            t = float(i) / n
            if not self._clear(p[0] + dx * t, p[1] + dz * t):
                return False
        return True

    def corners(self, a, b):
        # type: (Cell, Cell) -> Optional[List[Point]]
        """A→B 走廊的两个拐点（世界坐标）；几何上走不通返回 None。"""
        key = (a, b)
        if key in self._geo:
            return self._geo[key]
        m = self.mapping
        pa, pb = m.to_world(*a), m.to_world(*b)
        if a[1] != b[1] and abs(a[1] - b[1]) == 1:
            mid = (pa[1] + pb[1]) / 2.0          # 跨一排：中线是 z
            c1, c2 = (pa[0], mid), (pb[0], mid)
        elif a[0] != b[0] and abs(a[0] - b[0]) == 1:
            mid = (pa[0] + pb[0]) / 2.0          # 跨一列：中线是 x
            c1, c2 = (mid, pa[1]), (mid, pb[1])
        else:
            self._geo[key] = self._geo[(b, a)] = None
            return None
        ok = (self._segment_clear(pa, c1) and self._segment_clear(c1, c2)
              and self._segment_clear(c2, pb))
        got = [c1, c2] if ok else None
        self._geo[key] = got
        self._geo[(b, a)] = list(reversed(got)) if got else None
        return got

    # ------------------------------------------------------------ 拓扑

    def hops_from(self, a, walkable):
        # type: (Cell, Callable[[int, int], bool]) -> List[Cell]
        """从 a 出发的走廊跳边终点。只在普通格子走不过去时才有。"""
        out = []  # type: List[Cell]
        if walkable(*a):
            for fx, fz in _DIRS:
                if walkable(a[0] + fx, a[1] + fz):
                    continue     # 正前方可走：普通四邻已覆盖
                sx, sz = abs(fz), abs(fx)    # 横向单位（与前进方向垂直）
                for sign in (1, -1):
                    for k in range(1, MAX_SHIFT + 1):
                        b = (a[0] + fx + sign * sx * k, a[1] + fz + sign * sz * k)
                        if not walkable(*b):
                            continue
                        if not _grid_connected(a, b, walkable) and self.corners(a, b):
                            out.append(b)
                        break    # 这一侧最近的可走格才算；更远的由它接力
        return out


def _grid_connected(a, b, walkable):
    # type: (Cell, Cell, Callable[[int, int], bool]) -> bool
    """a、b 在两者包围盒内有没有单调格路（只走 a→b 方向的四邻步）。"""
    dx = 1 if b[0] > a[0] else -1
    dz = 1 if b[1] > a[1] else -1
    nx, nz = abs(b[0] - a[0]), abs(b[1] - a[1])
    reach = {(0, 0)}
    for i in range(nx + 1):
        for j in range(nz + 1):
            if (i, j) == (0, 0):
                continue
            cell = (a[0] + dx * i, a[1] + dz * j)
            if not walkable(*cell):
                continue
            if (i - 1, j) in reach or (i, j - 1) in reach:
                reach.add((i, j))
    return (nx, nz) in reach


def build(terrain_by_layer, hazard_boxes, occupied, mapping, belt_cells=None):
    # type: (Dict[int, List], List, Set[Cell], object, Optional[Set[Cell]]) -> Optional[LaneMap]
    """没有 Ground 层（旧合成帧）返回 None：走廊判定需要地板几何。"""
    ground = terrain_by_layer.get(occupancy.LAYER_GROUND) or []
    if not ground:
        return None
    blocking = []  # type: List
    for layer in occupancy.BLOCKING_LAYERS:
        blocking.extend(terrain_by_layer.get(layer) or ())
    killplane = terrain_by_layer.get(occupancy.LAYER_KILLPLANE) or []
    return LaneMap(ground, blocking, killplane, list(hazard_boxes or ()),
                   occupied, mapping, belt_cells)


class _Jumps(object):
    """distance_field 的 jumps：静态跳边（门洞/传送门）+ 现算的走廊边。"""

    def __init__(self, static, lane_map, walkable, excluded):
        # type: (Dict[Cell, List[Cell]], LaneMap, Callable[[int, int], bool], Set[Cell]) -> None
        self.static = static
        self.lane_map = lane_map
        self.excluded = excluded
        self._hops = {}   # type: Dict[Cell, List[Cell]]
        # 一次 distance_field 内 walkable 不变：记忆化，hops_from 每格要查十几个邻格，
        # 而 walkable 每次都要扫全部地形盒。
        memo = {}  # type: Dict[Cell, bool]

        def cached(gx, gz):
            # type: (int, int) -> bool
            v = memo.get((gx, gz))
            if v is None:
                v = memo[(gx, gz)] = bool(walkable(gx, gz))
            return v

        self.walkable = cached

    def __bool__(self):
        return True

    __nonzero__ = __bool__

    def get(self, cell, default=()):
        out = list(self.static.get(cell, ()))
        if cell not in self.excluded:
            hops = self._hops.get(cell)
            if hops is None:
                hops = self._hops[cell] = [
                    b for b in self.lane_map.hops_from(cell, self.walkable)
                    if b not in self.excluded]
            out.extend(b for b in hops if b not in out)
        return out or default


class _JumpCost(object):
    """跳边代价：静态表优先；走廊边 = 等价格路步数（1 + 横向错开格数）。"""

    def __init__(self, static):
        # type: (Dict[Tuple[Cell, Cell], int]) -> None
        self.static = static

    def __bool__(self):
        return True

    __nonzero__ = __bool__

    def get(self, key, default=1):
        if key in self.static:
            return self.static[key]
        (a, b) = key
        return abs(a[0] - b[0]) + abs(a[1] - b[1])


def wrap(nav, lane_map, excluded):
    # type: (Dict, Optional[LaneMap], Set[Cell]) -> Dict
    """把走廊边叠进 nav_args 的 jumps / jump_cost。lane_map 为 None 时原样返回。

    excluded：门洞格 / 传送门触发格——它们的通行规则（只许穿缝、等门、触发
    传送）不是纯几何，走廊边不碰它们。
    """
    if lane_map is None:
        return nav
    out = dict(nav)
    out["jumps"] = _Jumps(nav.get("jumps") or {}, lane_map, nav["is_walkable"], excluded)
    out["jump_cost"] = _JumpCost(nav.get("jump_cost") or {})
    return out


def excluded_cells(world):
    # type: (object) -> Set[Cell]
    """走廊边不碰的格：门洞格（只许穿缝 / 等门）+ 传送门触发格。"""
    out = set()  # type: Set[Cell]
    sg = getattr(world, "seam_gates", None)
    if sg is not None:
        out |= set(sg.gates)
    pm = getattr(world, "portal_map", None)
    if pm is not None:
        out |= set(pm.trigger_cells)
    return out


def lane_corners(world, a, b):
    # type: (object, Cell, Cell) -> Optional[List[Point]]
    """路径里相邻两格 a→b 若是走廊边，返回两个拐点；否则 None。

    门洞跳边（seams.nav_jumps）也是非相邻格对，但它总有一端是门洞格，这里
    按 excluded_cells 排除，不会被误当走廊插拐点。
    """
    lm = getattr(world, "lane_map", None)
    if lm is None:
        return None
    a, b = tuple(a), tuple(b)
    if abs(a[0] - b[0]) + abs(a[1] - b[1]) < 2:
        return None
    ex = excluded_cells(world)
    if a in ex or b in ex:
        return None
    return lm.corners(a, b)

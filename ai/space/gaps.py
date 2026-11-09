# -*- coding: utf-8 -*-
"""地板缝隙识别：两块地板之间的水面只能 dash 跨过，不能当普通邻接。

3-5 的木筏地板随木筏巡航，和静止厨房之间始终有一条 0.5~2.5m 的水面。
格心可走性只看点是否落在地板上（occupancy.make_walkable_from_terrain），
所以木筏靠近时两格相邻、中间却是水：普通 4 邻接会把 P2 直接带进水里
（trace-20261109-221602 的 6 次 respawn）。

本模块只回答「哪些格对跨了地板缝、缝多宽」，并把它叠成寻路参数：
  - 缝隙对的普通邻接一律 `edge_blocked=True`；
  - 宽度 <= GAP_MAX_M 且中间没有阻挡层的缝隙对，加一条 `jumps` 跳边，
    代价 DASH_COST；插件看到 move 的 `dash` 标记后在缝前按 Dash。

不读 World 类，只读 terrain / walkable / to_grid / to_world。
"""
import math
from typing import Any, Dict, List, Optional, Tuple

from ai.space import occupancy

# 可 dash 跨过的最大缝隙（米）。实测 dash 从头前冲约 2.2~2.4m，但触发点是
# 缝前 DASH_PROBE(0.55m)，再留一点余量；trace-20261110-003648 里 1.5m 与
# 1.99m 的缝都掉水，0.57m 的缝稳定通过。取 1.4m：允许 3-5 靠岸缝，拒绝
# 木筏漂开后的宽缝，让寻路等下一次靠岸而不是硬跳。
GAP_MAX_M = 1.4
# dash 跳边的寻路代价：略高于一格，避免为了省一步到处 dash。
DASH_COST = 2
# 缝隙宽度采样数；格心间距 1.2m 时步长约 0.06m，足够分辨半格缝。
_SAMPLES = 21
# 小于这个宽度不算缝：厨师胶囊半径 0.4m，半格以下的接缝/停靠缝人直接
# 走得过去（Balloon_1_5 的 0.36m 停靠缝、3-6 的 0.18m 地板接缝）。
_MIN_GAP_M = 0.5

Cell = Tuple[int, int]
Pair = Tuple[Cell, Cell]


def _ground(world):
    # type: (Any) -> List[Tuple[float, float, float, float]]
    return list(getattr(world, "terrain", {}).get(occupancy.LAYER_GROUND) or ())


def _blocking(world):
    # type: (Any) -> List[Tuple[float, float, float, float]]
    out = []  # type: List[Tuple[float, float, float, float]]
    terrain = getattr(world, "terrain", {}) or {}
    for layer in occupancy.BLOCKING_LAYERS:
        out.extend(terrain.get(layer) or ())
    return out


def _measure(world, a, b):
    # type: (Any, Cell, Cell) -> float
    """沿 a→b 格心连线采样，返回最长一段不在地板上的长度（米）。"""
    ground = _ground(world)
    if not ground:
        return 0.0
    pa = world.to_world(*a)
    pb = world.to_world(*b)
    if pa is None or pb is None:
        return 0.0
    dx, dz = pb[0] - pa[0], pb[1] - pa[1]
    dist = math.hypot(dx, dz)
    if dist <= 0.0:
        return 0.0
    step = dist / (_SAMPLES - 1)
    run = best = 0
    for i in range(_SAMPLES):
        t = i / float(_SAMPLES - 1)
        x, z = pa[0] + dx * t, pa[1] + dz * t
        if occupancy._point_in_any_box(x, z, ground):
            run = 0
        else:
            run += 1
            if run > best:
                best = run
    return best * step


def _range(world, ground):
    # type: (Any, List[Tuple[float, float, float, float]]) -> Optional[Tuple[int, int, int, int]]
    """Ground 盒并集外扩一格的主网格范围。"""
    minx = min(b[0] for b in ground)
    maxx = max(b[1] for b in ground)
    minz = min(b[2] for b in ground)
    maxz = max(b[3] for b in ground)
    p0 = world.to_grid(minx, minz)
    p1 = world.to_grid(maxx, maxz)
    if p0 is None or p1 is None:
        return None
    return (min(p0[0], p1[0]) - 1, max(p0[0], p1[0]) + 1,
            min(p0[1], p1[1]) - 1, max(p0[1], p1[1]) + 1)


def gap_pairs(world):
    # type: (Any) -> Dict[Pair, float]
    """所有检测到的缝隙对（双向），值为缝隙宽度（米）。按 world 缓存。"""
    cached = getattr(world, "_gap_pairs", None)
    if cached is not None:
        return cached
    ground = _ground(world)
    bounds = _range(world, ground) if ground else None
    if bounds is None:
        world._gap_pairs = {}
        return {}
    gx0, gx1, gz0, gz1 = bounds
    pairs = {}  # type: Dict[Pair, float]
    seen = set()  # type: set

    def add(a, b):
        # type: (Cell, Cell) -> None
        if (a, b) in seen:
            return
        seen.add((a, b))
        width = _measure(world, a, b)
        if width <= _MIN_GAP_M:
            return
        pairs[(a, b)] = width
        pairs[(b, a)] = width

    def walkable(cell):
        # type: (Cell) -> bool
        return bool(world.walkable(cell[0], cell[1]))

    def inside(cell):
        # type: (Cell) -> bool
        return gx0 <= cell[0] <= gx1 and gz0 <= cell[1] <= gz1

    for gx in range(gx0, gx1 + 1):
        for gz in range(gz0, gz1 + 1):
            a = (gx, gz)
            if not walkable(a):
                continue
            for dx, dz in ((1, 0), (0, 1)):
                b = (gx + dx, gz + dz)
                if walkable(b):
                    add(a, b)
                    continue
                # 中间格不可走：再看隔一格的落点。木筏离厨房 1.5m 时，
                # 中间那一格就是水面，只有 2 格跳边才能 dash 过去。
                far = (gx + 2 * dx, gz + 2 * dz)
                if inside(far) and walkable(far):
                    add(a, far)
    world._gap_pairs = pairs
    return pairs


def _edge_blocked(base, pairs):
    # type: (Optional[Any], Dict[Pair, float]) -> Any
    def edge_blocked(cur, nxt):
        if (tuple(cur), tuple(nxt)) in pairs:
            return True
        return bool(base and base(cur, nxt))
    return edge_blocked


class _MergedJumps(object):
    """distance_field 的 jumps：原有跳边（门洞/传送门/走廊）+ 缝隙跳边。"""

    def __init__(self, base, extra):
        # type: (Optional[Any], Dict[Cell, List[Cell]]) -> None
        self.base = base
        self.extra = extra

    def get(self, cell, default=()):
        out = list(self.base.get(cell, ()) if self.base is not None else ())
        out.extend(self.extra.get(cell, ()))
        seen = set()
        deduped = []
        for nxt in out:
            if nxt in seen:
                continue
            seen.add(nxt)
            deduped.append(nxt)
        return deduped or default


class _MergedJumpCost(object):
    def __init__(self, base, extra):
        # type: (Optional[Any], Dict[Pair, int]) -> None
        self.base = base
        self.extra = extra

    def get(self, key, default=1):
        if key in self.extra:
            return self.extra[key]
        if self.base is not None:
            return self.base.get(key, default)
        return default


def jump_pairs(world):
    # type: (Any) -> Dict[Pair, int]
    """可 dash 的缝隙对：宽度 <= GAP_MAX_M 且连线中点不在阻挡层里。

    与 wrap 加跳边的判据同一口径；命令侧的 dash_edges 也用它，避免
    「寻路说能跳、插件收到 dash 授权」两套阈值不一致。
    """
    pairs = gap_pairs(world)
    if not pairs:
        return {}
    blocking = _blocking(world)
    out = {}  # type: Dict[Pair, int]
    for (a, b), width in pairs.items():
        if width > GAP_MAX_M:
            continue
        pa = world.to_world(*a)
        pb = world.to_world(*b)
        if pa is None or pb is None:
            continue
        mx, mz = (pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0
        if occupancy._point_in_any_box(mx, mz, blocking):
            continue
        out[(a, b)] = DASH_COST
    return out


def wrap(nav, world):
    # type: (Dict, Any) -> Dict
    """给 portals.nav_args 的结果叠一层缝隙规则。"""
    pairs = gap_pairs(world)
    if not pairs:
        return nav
    jumpable = jump_pairs(world)
    jumps = {}  # type: Dict[Cell, List[Cell]]
    costs = {}  # type: Dict[Pair, int]
    for (a, b) in jumpable:
        jumps.setdefault(a, [])
        if b not in jumps[a]:
            jumps[a].append(b)
        costs[(a, b)] = DASH_COST

    out = dict(nav)
    out["edge_blocked"] = _edge_blocked(nav.get("edge_blocked"), pairs)
    out["jumps"] = _MergedJumps(nav.get("jumps"), jumps)
    out["jump_cost"] = _MergedJumpCost(nav.get("jump_cost"), costs)
    return out


def path_has_gap(world, path):
    # type: (Any, List[Cell]) -> bool
    """path 里是否有相邻两格属于可 dash 的缝隙跳边。"""
    return bool(path_gap_edges(world, path))


def path_gap_edges(world, path):
    # type: (Any, List[Cell]) -> List[int]
    """path 里哪些**落点下标**是从缝隙跳边跨过去的。

    返回的是「拐到这一格」的下标（与 move 命令的 path 下标同口径），
    插件据此只在跳边段按 Dash，而不是整条路径随便遇到平台边缘就跳。
    宽度超过 GAP_MAX_M 的缝隙不算，和寻路 / 插件落点校验同一口径。
    """
    if not path:
        return []
    jumpable = jump_pairs(world)
    if not jumpable:
        return []
    out = []  # type: List[int]
    for i in range(1, len(path)):
        if (tuple(path[i - 1]), tuple(path[i])) in jumpable:
            out.append(i)
    return out


def command_gap_edges(world, points):
    # type: (Any, List[Any]) -> List[int]
    """move 命令的世界路点里，哪些落点下标是缝隙跳边。

    `portals.route` 可能为了传送门/半格走廊插入额外路点，下标与格子路径
    不再一一对应；这里把每个世界点投影回格，再按 jump_pairs 判段。
    """
    if not points:
        return []
    jumpable = jump_pairs(world)
    if not jumpable:
        return []
    out = []  # type: List[int]
    prev = None
    for i, p in enumerate(points):
        try:
            cell = world.to_grid(float(p[0]), float(p[1]))
        except (TypeError, ValueError, IndexError):
            cell = None
        if cell is not None and prev is not None \
                and (tuple(prev), tuple(cell)) in jumpable:
            out.append(i)
        prev = cell
    return out

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

# 可 dash 跨过的最大缝隙（米）。Overcooked 的 dash 约 2.5m，留 0.5m 余量。
GAP_MAX_M = 2.0
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


def wrap(nav, world):
    # type: (Dict, Any) -> Dict
    """给 portals.nav_args 的结果叠一层缝隙规则。"""
    pairs = gap_pairs(world)
    if not pairs:
        return nav
    blocking = _blocking(world)
    jumps = {}  # type: Dict[Cell, List[Cell]]
    costs = {}  # type: Dict[Pair, int]
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
    """path 里是否有相邻两格属于缝隙对。"""
    pairs = gap_pairs(world)
    if not pairs:
        return False
    for i in range(len(path) - 1):
        if (tuple(path[i]), tuple(path[i + 1])) in pairs:
            return True
    return False

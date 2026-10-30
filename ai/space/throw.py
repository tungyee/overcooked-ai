"""投掷几何：落点预测与出手站位（纯函数，不碰 World 类以外的状态）。

射程常量来自 trace-20261028-234917（Rapids_2_1）P1 的 4 次投掷：出手点
(19.73,2.6)/(20.68,2.6)/(21.64,2.36)/(23.8,2.44) 均朝 +z，落地点距出手点
7.3~7.8m、横向偏差 <0.2m、12~15 拍（20Hz）落地。投掷没有蓄力，射程固定，
落点只由「出手点 + 朝向」决定——所以规划只需要选站位，不需要选力度。

落地被挡：飞行路径上的墙/站点会把料截下（参考项目 cortico-world 的
ServerAttachStation.HandleCatch：站点接住飞来的料）。这里不建弹道，只把
名义落点沿出手方向往回收，取第一个「落在目标区域」的点，近似「撞墙掉在
墙前 / 被台面接住」。
"""
import math
from typing import Callable, Dict, Iterable, Optional, Set, Tuple

# 射程（m）与容差：4 个实测样本 7.3~7.8m 的中位数，横偏实测 <0.2m 取 0.3 留余量。
THROW_RANGE = 7.5
THROW_RANGE_TOL = 0.4
THROW_LATERAL_TOL = 0.3
# 飞行拍数（20Hz）：实测 12~15 拍。
THROW_FLIGHT_TICKS = 15
# 落点往回收的步长（m）：一格 1.2m 的一半，保证不跨过整格。
_RETRACT_STEP = 0.6
# 落点离目标太远就不算「投到它那儿」（m）：超过约 2 格，接收方还得走一段，
# 远距投掷的意义就没了。
MAX_LAND_DIST = 2.6


Vec = Tuple[float, float]
Cell = Tuple[int, int]


def landing(x, z, dx, dz, rng=THROW_RANGE):
    # type: (float, float, float, float, float) -> Vec
    """从 (x,z) 朝方向 (dx,dz) 投出的名义落点（方向不必归一）。"""
    n = math.hypot(dx, dz)
    if n < 1e-6:
        return (x, z)
    return (x + dx / n * rng, z + dz / n * rng)


def _safe(pt, dx, dz, ok):
    # type: (Vec, float, float, Callable[[float, float], bool]) -> bool
    """落点含横偏 / 射程误差的几个样本点都要落在目标区域。"""
    n = math.hypot(dx, dz) or 1.0
    ux, uz = dx / n, dz / n
    px, pz = -uz, ux
    for a, b in ((0.0, 0.0), (THROW_LATERAL_TOL, 0.0), (-THROW_LATERAL_TOL, 0.0),
                 (0.0, THROW_RANGE_TOL), (0.0, -THROW_RANGE_TOL)):
        if not ok(pt[0] + px * a + ux * b, pt[1] + pz * a + uz * b):
            return False
    return True


def effective_landing(start, target, ok, rng=THROW_RANGE):
    # type: (Vec, Vec, Callable[[float, float], bool], float) -> Optional[Vec]
    """朝 target 投出后料实际停下的点；不安全返回 None。

    名义落点超过 target（射程 > 距离）时，沿出手方向往回收，取第一个安全点
    ——近似「越过目标撞到后面的墙/台面掉下来」。回收不越过 target 往出手点
    那侧：再近就等于没投过去。ok(x,z) 判一个点是否落在目标区域。
    """
    dx, dz = target[0] - start[0], target[1] - start[1]
    dist = math.hypot(dx, dz)
    if dist < 1e-6:
        return None
    d = rng
    floor = min(dist, rng) - THROW_RANGE_TOL
    while d >= floor:
        pt = landing(start[0], start[1], dx, dz, d)
        if _safe(pt, dx, dz, ok):
            return pt
        d -= _RETRACT_STEP
    return None


def plan_launch(cells, cost, to_world, target, ok, max_land=MAX_LAND_DIST):
    # type: (Iterable[Cell], Callable[[Cell], Optional[int]], Callable[[int, int], Vec], Vec, Callable[[float, float], bool], float) -> Optional[Dict]
    """在出手候选格里选一个：落点安全、离 target ≤ max_land，按「走过去的
    代价 + 落点离目标的距离」最小。

    cells: 投掷方可站的格（P2 可达场的键）；cost(cell) 是走到该格的代价（拍
    数量级），None = 不可达。返回 {cell, world, landing, land_dist} 或 None。
    """
    best = None  # type: Optional[Tuple[float, Dict]]
    for cell in cells:
        c = cost(cell)
        if c is None:
            continue
        wx, wz = to_world(cell[0], cell[1])
        dist = math.hypot(target[0] - wx, target[1] - wz)
        # 太近：射程固定 7.5m，名义落点远远越过目标，靠回收近似不可信。
        if dist < THROW_RANGE - 2 * THROW_RANGE_TOL - max_land:
            continue
        # 太远：最远也落在 target 前 dist-R 处。
        if dist - THROW_RANGE > max_land:
            continue
        pt = effective_landing((wx, wz), target, ok)
        if pt is None:
            continue
        land = math.hypot(target[0] - pt[0], target[1] - pt[1])
        if land > max_land:
            continue
        score = c + land * 2.0
        if best is None or score < best[0]:
            best = (score, dict(cell=cell, world=(wx, wz), landing=pt,
                                land_dist=land))
    return best[1] if best else None


# ---------------------------------------------------------------- 投进锅

# 投进锅的出手距离带（m，出手点到锅心）。锅接料靠触发器：飞行中的料穿过锅
# 的触发器、锅能收这种料就当场收下（反编译 ServerAttachmentCatchingProxy /
# ServerIngredientCatcher），不必落在锅里。唯一实测成功样本 4.7m
# （trace-20261029-213506 t=558→561，朝向偏 4°）。下限排除相邻/斜邻格
# （直接放更快），也避开出手后料还在上升段、可能越过触发器的近距离；
# 上限留在射程（7.5m 落地）内、料仍在锅高度附近。待实机标定。
POT_THROW_MIN = 3.0
POT_THROW_MAX = 5.0
# 飞行路径采样步长（m）：小于半格，不会跳过整格。
_LINE_STEP = 0.3


def pot_line_clear(start, target, to_grid, passable):
    # type: (Vec, Vec, Callable[[float, float], Cell], Callable[[Cell], bool]) -> bool
    """出手点到锅心的直线只经过可飞越的格（出手格与锅所在格除外）。

    passable(cell) 由调用方给：可走的地板、且没有别的厨师（空手的人会接住
    飞来的料）。台面/别的灶不算——别的灶能收同一种料，会半路截走。
    """
    s_cell, t_cell = to_grid(*start), to_grid(*target)
    dx, dz = target[0] - start[0], target[1] - start[1]
    dist = math.hypot(dx, dz)
    if dist < 1e-6:
        return False
    n = int(dist / _LINE_STEP)
    for i in range(1, n + 1):
        f = i * _LINE_STEP / dist
        cell = to_grid(start[0] + dx * f, start[1] + dz * f)
        if cell in (s_cell, t_cell):
            continue
        if not passable(cell):
            return False
    return True


def pot_throw_ok(start, target, to_grid, passable):
    # type: (Vec, Vec, Callable[[float, float], Cell], Callable[[Cell], bool]) -> bool
    """站在 start 能不能把料投进 target 处的锅：距离在带内、路径无遮挡。"""
    dist = math.hypot(target[0] - start[0], target[1] - start[1])
    if dist < POT_THROW_MIN or dist > POT_THROW_MAX:
        return False
    return pot_line_clear(start, target, to_grid, passable)


def plan_pot_throw(cells, cost, to_world, to_grid, target, passable):
    # type: (Iterable[Cell], Callable[[Cell], Optional[int]], Callable[[int, int], Vec], Callable[[float, float], Cell], Vec, Callable[[Cell], bool]) -> Optional[Dict]
    """在可站格里选投锅出手位：能投进（pot_throw_ok），走过去代价最小。

    返回 {cell, world, cost, dist} 或 None。
    """
    best = None  # type: Optional[Dict]
    for cell in cells:
        c = cost(cell)
        if c is None:
            continue
        wx, wz = to_world(cell[0], cell[1])
        if not pot_throw_ok((wx, wz), target, to_grid, passable):
            continue
        if best is None or c < best["cost"]:
            best = dict(cell=cell, world=(wx, wz), cost=c,
                        dist=math.hypot(target[0] - wx, target[1] - wz))
    return best


def region_test(to_grid, dest_cells, src_cells=None, station_cells=None):
    # type: (Callable[[float, float], Cell], Set[Cell], Optional[Set[Cell]], Optional[Set[Cell]]) -> Callable[[float, float], bool]
    """点 (x,z) 是否落在目标区域：落在接收方可达格上，或落在紧邻可达格的
    站点格上（台面/砧板/灶接住）。src_cells 非空时排除投掷方自己能走到的
    格——分割地图上落回本侧等于没投。"""
    dest = set(dest_cells or ())
    src = set(src_cells or ())
    stations = set(station_cells or ())

    def ok(x, z):
        cell = to_grid(x, z)
        if cell in src:
            return False
        if cell in dest:
            return True
        if cell in stations:
            gx, gz = cell
            return any((gx + a, gz + b) in dest
                       for a, b in ((1, 0), (-1, 0), (0, 1), (0, -1)))
        return False
    return ok

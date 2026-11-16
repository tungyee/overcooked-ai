"""投掷几何：落点预测与出手站位（纯函数，不碰 World 类以外的状态）。

射程常量来自 trace-20261028-234917（Rapids_2_1）P1 的 4 次投掷：出手点
(19.73,2.6)/(20.68,2.6)/(21.64,2.36)/(23.8,2.44) 均朝 +z，落地点距出手点
7.3~7.8m、横向偏差 <0.2m、12~15 拍（20Hz）落地。投掷没有蓄力，射程固定，
落点只由「出手点 + 朝向」决定——所以规划只需要选站位，不需要选力度。

落地被挡：飞行路径上的墙/站点会把料截下（参考项目 cortico-world 的
ServerAttachStation.HandleCatch：站点接住飞来的料）。这里不建弹道，只把
名义落点沿出手方向往回收，取第一个「落在目标区域」的点，近似「撞墙掉在
墙前 / 被台面接住」。

飞行路径上的高墙（terrain Walls 层的薄墙）单独判：料撞上就掉在墙前，不会
飞到墙后。Balloon_2_2（trace-20261031-123418 t=4571/4671）：P2 在左地板
(7.2,12) 朝右篮炸锅投蘑菇，直线在 x=13.21 处穿过右篮左墙
[13.21,13.81]×[12.44,15.03]（门洞在 z 8.03~12.44），旧实现只看落点，判
「落在炸锅旁」，实际两份都撞墙掉进缝里。TableBlock 层的矮栏料能飞过去
（同一 trace 料飞越左地板 x≈8 的矮栏），不当挡板。

高度差：弧线按投掷者脚下算、只会下落。比脚下高的地面（楼梯台阶、高台
地板）在飞行路径上当挡板（raised_ground），落点地面比脚下高太多也判投不到
（floor_reachable）。Wizard_5_5 一楼往二楼砧板投肉，料撞楼梯弹回
（trace-20261101-142950）。
"""
import math
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

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
# 挡料的墙：terrain 层号（Walls 边墙）与厚度上限（m）。篮子/地板边墙厚 0.6；
# 更厚的盒是底座之类（Balloon_2_2 发餐气球下 10.7×6m），不当墙。
# TableBlock(26) 不算：Balloon_2_2 门洞里那条 [0.49,1.33] 的盒只挡人走，
# 边框中间是空的，料能从门洞投过去。
WALL_LAYERS = (10,)
WALL_MAX_THICK = 1.0
# 有墙时瞄准的精度：插件 throw 命令 aim_dot（0.996 ≈ 5.1°）+ strict（没对准
# 不投）。默认 0.97（14°）6m 外横偏 1.5m，从门洞斜穿必擦框。
WALL_AIM_DOT = 0.996
AIM_TOL_DEG = math.degrees(math.acos(WALL_AIM_DOT))
# 正对被挡时瞄准方向的偏转候选（度）与每度的代价（m 落点距离当量）。
AIM_OFFSETS_DEG = (0,) + tuple(s * d for d in range(4, 64, 4) for s in (1, -1))
AIM_OFFSET_COST = 0.01
# 发给插件的瞄准点放多远（m，沿出手方向）。远点抗站位偏差：trace-20261031-
# 150043 P2 实停位置离出手格心 0.3m，瞄 10m 处的点方向差 ≈1.7°，瞄 30m 处 <0.6°。
AIM_FAR_DIST = 30.0
# 飞行弧线高度（m，相对投掷者脚下）：平飞段 + 末段下落。
#   h(d) = THROW_RELEASE_H                         d ≤ THROW_FLAT_D
#   h(d) = THROW_RELEASE_H × (1 − ((d−D0)/(R−D0))²)  D0 < d ≤ R
# 标定（插件 0.23.5 items[].y，trace-20261031-150043 两投，出手 (7.46,10.65)
# / (7.47,10.8)）：d=3.8 y=0.99、5.6 0.93/0.99、6.5 0.67~0.75、7.0 0.43、
# 7.5 ≈0（这几点是撞框后的下落，下落段偏保守）。
# 推论：1.32m 的边墙在射程内任何距离都挡料（Balloon_2_2 只能从门洞穿）；
# Rapids_2_1 木筏→厨房料在 ≈6m 处越过厨房边墙，那面墙应低于 h(6)≈0.9m
# （墙高待 0.23.5 Rapids 帧确认）。
THROW_RELEASE_H = 1.0
THROW_FLAT_D = 5.4
# 撞墙后料停在墙前多远（m）。
WALL_STOP_MARGIN = 0.3
# 墙盒外扩（m）：飞行料自身半径的近似。
WALL_PAD = 0.15
# 地面高度剖面（Wizard_5_5，trace-20261101-142950 t=262/307）：P2 在一楼楼梯口
# 往二楼（地板顶 y=2.0）砧板投肉，料出手不到 2m 就撞上楼梯台阶立面（顶 1.39）
# 弹回一楼，连投 5 次。弧线只会下落，比它高的地面（Ground/SlopedGround 盒顶）
# 就是挡板；比出手点高太多的落点本身也够不着。
GROUND_LAYERS = (9, 28)
# 地面盒顶比投掷者脚下高出这么多（m）才当挡板：滤掉同层地板的浮点噪声。
RAISED_MIN = 0.05
# 落点地面比投掷者脚下最多高这么多（m）还算投得上去：Balloon_2_2 篮子地板
# 高 0.51 实测能投进；Wizard_3_1 二楼高 1.21、Wizard_5_5 二楼高 2.0 都超过
# 出手高度 THROW_RELEASE_H，投不上去。
LAND_RISE_MAX = 0.6


Vec = Tuple[float, float]
Cell = Tuple[int, int]
Box = Tuple[float, float, float, float]   # (minx, maxx, minz, maxz)


def arc_height(d, rng=THROW_RANGE, release_h=THROW_RELEASE_H, flat_d=THROW_FLAT_D):
    # type: (float, float, float, float) -> float
    """出手后飞了 d 米时料离投掷者脚下的高度（平飞到 flat_d，再落到射程处着地）。"""
    if d <= flat_d:
        return release_h
    if rng <= flat_d:
        return 0.0
    f = min(1.0, (d - flat_d) / (rng - flat_d))
    return release_h * (1.0 - f * f)


def throw_walls(terrain_y, floor_y=0.0):
    # type: (Optional[Dict[int, List[Tuple[Box, float, float]]]], float) -> List[Tuple[float, ...]]
    """可能挡住飞行料的薄墙：[(minx,maxx,minz,maxz,lo,hi)]，lo/hi 是墙的
    下沿/上沿相对 floor_y（投掷者脚下）的高度。挡不挡看飞到墙那儿时的
    arc_height 落不落在 [lo,hi] 里（wall_hit）。

    terrain_y 是 occupancy.parse_terrain_y 的结果。旧插件帧没有高度 → 空
    列表：分不出飞得过/飞不过时不拦，保持旧行为。
    """
    out = []  # type: List[Tuple[float, ...]]
    for layer in WALL_LAYERS:
        for b, miny, maxy in (terrain_y or {}).get(layer) or ():
            if min(b[1] - b[0], b[3] - b[2]) > WALL_MAX_THICK:
                continue
            if maxy - floor_y <= 0:
                continue      # 墙顶在脚下（篮子底座边缘之类）
            out.append(tuple(b[:4]) + (miny - floor_y, maxy - floor_y))
    return out


def raised_ground(terrain_y, floor_y=0.0):
    # type: (Optional[Dict[int, List[Tuple[Box, float, float]]]], float) -> List[Tuple[float, ...]]
    """比投掷者脚下高的地面盒（楼梯台阶、高台地板）当挡板：
    [(minx,maxx,minz,maxz,-inf,top)]，top 相对 floor_y。

    下沿记 -inf：地面盒下面是实心的，不存在「从下面穿过」。wall_hit 对这种盒
    只看料飞到盒边时够不够高——够高就是落在上面（落点由 region_test 判），
    不够高就撞侧面。不受 WALL_MAX_THICK 限制（高台地板很大）。
    """
    out = []  # type: List[Tuple[float, ...]]
    for layer in GROUND_LAYERS:
        for b, _miny, maxy in (terrain_y or {}).get(layer) or ():
            if maxy - floor_y > RAISED_MIN:
                out.append(tuple(b[:4]) + (float("-inf"), maxy - floor_y))
    return out


def ground_top(terrain_y, x, z):
    # type: (Optional[Dict[int, List[Tuple[Box, float, float]]]], float, float) -> Optional[float]
    """(x,z) 处地面顶面高度（Ground/SlopedGround 盒顶取最高）；没有地面盒 None。"""
    best = None  # type: Optional[float]
    for layer in GROUND_LAYERS:
        for b, _miny, maxy in (terrain_y or {}).get(layer) or ():
            if b[0] <= x <= b[1] and b[2] <= z <= b[3] and (best is None or maxy > best):
                best = maxy
    return best


def floor_reachable(terrain_y, floor_y=0.0):
    # type: (Optional[Dict[int, List[Tuple[Box, float, float]]]], float) -> Callable[[float, float], bool]
    """落点 (x,z) 的地面是否不比投掷者脚下高太多（LAND_RISE_MAX）。
    查不到地面高度（旧帧 / 盒缝）时不拦，保持旧行为。"""
    def ok(x, z):
        top = ground_top(terrain_y, x, z)
        return top is None or top - floor_y <= LAND_RISE_MAX
    return ok


def _seg_box_span(a, b, box):
    # type: (Vec, Vec, Tuple[float, ...]) -> Optional[Tuple[float, float]]
    """线段 a→b 穿过 box 平面范围的参数区间 [t0,t1]⊆[0,1]（slab 法）；不相交 None。"""
    t0, t1 = 0.0, 1.0
    for p, d, lo, hi in ((a[0], b[0] - a[0], box[0], box[1]),
                         (a[1], b[1] - a[1], box[2], box[3])):
        if abs(d) < 1e-9:
            if p < lo or p > hi:
                return None
            continue
        u, v = (lo - p) / d, (hi - p) / d
        if u > v:
            u, v = v, u
        t0, t1 = max(t0, u), min(t1, v)
        if t0 > t1:
            return None
    return t0, t1


def wall_hit(start, ux, uz, length, walls, rng=THROW_RANGE):
    # type: (Vec, float, float, float, Iterable[Tuple[float, ...]], float) -> Optional[float]
    """从 start 沿单位方向 (ux,uz) 飞 length 米，第一次撞墙的距离；没撞返回 None。

    墙是 (minx,maxx,minz,maxz) 或带高度的 (…, lo, hi)（throw_walls）。带高度
    时，料穿过墙平面范围那段的弧线高度 [h(出), h(入)] 与 [lo,hi] 有交集才算
    撞；不带高度一律算撞。出手点已在墙盒里（贴墙站、碰撞盒溢出）的墙跳过。
    lo=-inf 的是高出脚下的地面盒（raised_ground）：料到盒边时高度 ≤ 盒顶才
    撞侧面，高于盒顶就是飞过或落在上面。
    """
    end = (start[0] + ux * length, start[1] + uz * length)
    best = None  # type: Optional[float]
    for box in walls:
        if box[0] <= start[0] <= box[1] and box[2] <= start[1] <= box[3]:
            continue
        span = _seg_box_span(start, end, box)
        if span is None:
            continue
        d0, d1 = span[0] * length, span[1] * length
        if len(box) >= 6:
            h_hi, h_lo = arc_height(d0, rng), arc_height(d1, rng)
            if box[4] == float("-inf"):
                if h_hi > box[5]:
                    continue  # 到盒边时比地面高：飞过 / 落在上面
            elif h_lo > box[5] or h_hi < box[4]:
                continue      # 从墙上飞过 / 从墙下（悬空栏杆）穿过
        if best is None or d0 < best:
            best = d0
    return best


def _fan_hit(start, ux, uz, length, walls):
    # type: (Vec, float, float, float, Iterable[Tuple[float, ...]]) -> Optional[float]
    """中线与左右偏 AIM_TOL_DEG 两条线里最早撞墙的距离；墙盒平面四周外扩
    WALL_PAD（料自身半径）。

    角度偏差按插件精准对准门槛（WALL_AIM_DOT）算，离得越远横偏越大：从门洞
    斜穿时 6m 外偏 ≈0.5m，贴着门框瞄会擦框。
    """
    walls = [(b[0] - WALL_PAD, b[1] + WALL_PAD, b[2] - WALL_PAD, b[3] + WALL_PAD)
             + tuple(b[4:]) for b in walls]
    if not walls:
        return None
    best = None  # type: Optional[float]
    for deg in (0.0, AIM_TOL_DEG, -AIM_TOL_DEG):
        a = math.radians(deg)
        rx = ux * math.cos(a) - uz * math.sin(a)
        rz = ux * math.sin(a) + uz * math.cos(a)
        h = wall_hit(start, rx, rz, length, walls)
        if h is not None and (best is None or h < best):
            best = h
    return best


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


def effective_landing(start, target, ok, rng=THROW_RANGE, walls=None):
    # type: (Vec, Vec, Callable[[float, float], bool], float, Optional[Iterable[Box]]) -> Optional[Vec]
    """朝 target 投出后料实际停下的点；不安全返回 None。

    名义落点超过 target（射程 > 距离）时，沿出手方向往回收，取第一个安全点
    ——近似「越过目标撞到后面的墙/台面掉下来」。回收不越过 target 往出手点
    那侧：再近就等于没投过去。ok(x,z) 判一个点是否落在目标区域。

    walls（throw_walls 的结果）非空时，飞行中线先撞上的墙截断射程：
    料停在墙前 WALL_STOP_MARGIN，停点要是离 target 还差得远就投不过去。
    """
    dx, dz = target[0] - start[0], target[1] - start[1]
    dist = math.hypot(dx, dz)
    if dist < 1e-6:
        return None
    d = rng
    if walls:
        hit = _fan_hit(start, dx / dist, dz / dist, rng + THROW_RANGE_TOL, walls)
        if hit is not None:
            d = min(d, hit - WALL_STOP_MARGIN)
    floor = min(dist, rng) - THROW_RANGE_TOL
    while d >= floor:
        pt = landing(start[0], start[1], dx, dz, d)
        if _safe(pt, dx, dz, ok):
            return pt
        d -= _RETRACT_STEP
    return None


def best_aim(start, target, ok, max_land=MAX_LAND_DIST, walls=None):
    # type: (Vec, Vec, Callable[[float, float], bool], float, Optional[Iterable[Box]]) -> Optional[Dict]
    """站在 start 往哪儿瞄：{aim, landing, land_dist}；投不到 target 附近返回 None。

    先试正对 target；正对被墙挡住（或落点不安全）且有墙时，绕出手点把瞄准
    方向左右偏 AIM_OFFSETS_DEG 再试，取落点离 target 最近的——Balloon_2_2
    左地板右边框、右篮左边框中间各有门洞（z 8.03~12.44），正对炸锅的直线
    撞框，斜着从门洞穿过去能落在右篮里离炸锅 ≈4.5m 处（P1 去捡）。
    aim 与 target 同距，effective_landing 的回收逻辑照旧。
    """
    dx, dz = target[0] - start[0], target[1] - start[1]
    dist = math.hypot(dx, dz)
    if dist < 1e-6:
        return None
    walls = list(walls or ())
    offsets = AIM_OFFSETS_DEG if walls else (0,)
    base = math.atan2(dz, dx)
    best = None  # type: Optional[Tuple[float, Dict]]
    for deg in offsets:
        a = base + math.radians(deg)
        got = aim_ok(start, (start[0] + math.cos(a), start[1] + math.sin(a)),
                     target, ok, max_land, walls)
        if got is None:
            continue
        score = got["land_dist"] + AIM_OFFSET_COST * abs(deg)
        if best is None or score < best[0]:
            best = (score, got)
        if deg == 0:
            break          # 正对就能投：不偏
    return best[1] if best else None


def aim_ok(start, toward, target, ok, max_land=MAX_LAND_DIST, walls=None):
    # type: (Vec, Vec, Vec, Callable[[float, float], bool], float, Optional[Iterable[Box]]) -> Optional[Dict]
    """站在 start、朝 toward 方向投，能不能落到 target 附近：
    {aim, aim_far, landing, land_dist} 或 None。

    aim 在出手方向上、与 target 同距（effective_landing 的回收按它算）；
    aim_far 是同方向 AIM_FAR_DIST 外的点，发给插件当 target_world：插件按
    实时位置朝它转身，站位差十几厘米方向几乎不变（瞄近点会跟着站位偏）。
    """
    dist = math.hypot(target[0] - start[0], target[1] - start[1])
    ux, uz = toward[0] - start[0], toward[1] - start[1]
    n = math.hypot(ux, uz)
    if dist < 1e-6 or n < 1e-6:
        return None
    ux, uz = ux / n, uz / n
    aim = (start[0] + ux * dist, start[1] + uz * dist)
    pt = effective_landing(start, aim, ok, walls=list(walls or ()))
    if pt is None:
        return None
    land = math.hypot(target[0] - pt[0], target[1] - pt[1])
    if land > max_land:
        return None
    far = (start[0] + ux * AIM_FAR_DIST, start[1] + uz * AIM_FAR_DIST)
    return dict(aim=aim, aim_far=far, landing=pt, land_dist=land)


def plan_launch(cells, cost, to_world, target, ok, max_land=MAX_LAND_DIST, walls=None):
    # type: (Iterable[Cell], Callable[[Cell], Optional[int]], Callable[[int, int], Vec], Vec, Callable[[float, float], bool], float, Optional[Iterable[Box]]) -> Optional[Dict]
    """在出手候选格里选一个：落点安全、离 target ≤ max_land，按「走过去的
    代价 + 落点离目标的距离」最小。

    cells: 投掷方可站的格（P2 可达场的键）；cost(cell) 是走到该格的代价（拍
    数量级），None = 不可达。返回 {cell, world, aim, landing, land_dist, score} 或
    None；aim 是瞄准点（无墙时 = target）。
    """
    walls = list(walls or ())
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
        got = best_aim((wx, wz), target, ok, max_land, walls)
        if got is None:
            continue
        score = c + got["land_dist"] * 2.0
        if best is None or score < best[0]:
            best = (score, dict(got, cell=cell, world=(wx, wz), score=score))
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


def pot_throw_ok(start, target, to_grid, passable, walls=None):
    # type: (Vec, Vec, Callable[[float, float], Cell], Callable[[Cell], bool], Optional[Iterable[Box]]) -> bool
    """站在 start 能不能把料投进 target 处的锅：距离在带内、路径无遮挡
    （格判 + 薄墙线段判：两格之间的墙不盖格中心，格判漏掉）。"""
    dx, dz = target[0] - start[0], target[1] - start[1]
    dist = math.hypot(dx, dz)
    if dist < POT_THROW_MIN or dist > POT_THROW_MAX:
        return False
    # 只查到锅格边（半格前）：锅背后贴着的墙不算挡
    if walls and _fan_hit(start, dx / dist, dz / dist, dist - 0.6, walls) is not None:
        return False
    return pot_line_clear(start, target, to_grid, passable)


def plan_pot_throw(cells, cost, to_world, to_grid, target, passable, walls=None):
    # type: (Iterable[Cell], Callable[[Cell], Optional[int]], Callable[[int, int], Vec], Callable[[float, float], Cell], Vec, Callable[[Cell], bool], Optional[Iterable[Box]]) -> Optional[Dict]
    """在可站格里选投锅出手位：能投进（pot_throw_ok），走过去代价最小。

    返回 {cell, world, cost, dist} 或 None。
    """
    best = None  # type: Optional[Dict]
    for cell in cells:
        c = cost(cell)
        if c is None:
            continue
        wx, wz = to_world(cell[0], cell[1])
        if not pot_throw_ok((wx, wz), target, to_grid, passable, walls):
            continue
        if best is None or c < best["cost"]:
            best = dict(cell=cell, world=(wx, wz), cost=c,
                        dist=math.hypot(target[0] - wx, target[1] - wz))
    return best


def region_test(to_grid, dest_cells, src_cells=None, station_cells=None, floor_ok=None):
    # type: (Callable[[float, float], Cell], Set[Cell], Optional[Set[Cell]], Optional[Set[Cell]], Optional[Callable[[float, float], bool]]) -> Callable[[float, float], bool]
    """点 (x,z) 是否落在目标区域：落在接收方可达格上，或落在紧邻可达格的
    站点格上（台面/砧板/灶接住）。src_cells 非空时排除投掷方自己能走到的
    格——分割地图上落回本侧等于没投。floor_ok(x,z) 非空时还要落点地面
    够得着（floor_reachable：不比投掷者脚下高太多）。"""
    dest = set(dest_cells or ())
    src = set(src_cells or ())
    stations = set(station_cells or ())

    def ok(x, z):
        if floor_ok is not None and not floor_ok(x, z):
            return False
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

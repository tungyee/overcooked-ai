"""平台接缝门洞（gate）：两块地板贴边处的墙上开口，按当前帧算「门洞在哪、多宽」。

Balloon_2_3：固定地面与食材篮（x=13.2）、食材篮与右篮（x=19.2）之间各有一条
接缝，两侧各一道矮墙背靠背，墙上各开一个门洞；篮子沿 z 移动，两个门洞的
重叠区间（缝）跟着变。格中心采样（每 1.2m 一个点）只有在缝恰好盖住某排格
中心时才判得通——缝在两排格中心之间时整条接缝被判死，P1 走得过、P2 拿着
盘子原地不动（trace-20261029-234145 t=6199~6230；trace-20261030-001857
t=1097/1486 实测：缝宽 1.79m/1.26m，两排格中心都在墙里）。

这里不改 `walkable`（站位/交互位/出手位仍用它，门洞格不能当落脚点），只给
寻路层叠一层：
  - 接缝列上、地板重叠范围内的格是「门洞格」，只许沿穿缝方向进出；
  - 门洞格是否通看当前帧门洞：墙在接缝带上的投影取补集，再按厨师半径收窄，
    与该格所在行有交集就算通；
  - 过缝那段路不走门洞格中心，改走门洞中线，并把门洞描述随 move 命令发给
    插件（`gates` 字段），由插件每帧按墙碰撞体现算中线、门洞没开就在缝口等。

只读 terrain（层号 AABB），不读 World 类；纯函数，无副作用。
"""
import math
from typing import Callable, Dict, List, Optional, Tuple

from ai.space import occupancy
from ai.space.seam_clock import seam_key

# 门洞判定用的厨师半径：插件 walk_probe 实测 CapsuleCollider 半径 0.4，加 0.05 余量。
GATE_RADIUS = 0.45
# 收窄后门洞还要有这么宽（厨师中心可活动范围）才算通，避免擦边判通。
MIN_BAND = 0.1
# 等门换算：P2 走一格约 0.2s（trace-20261030-001857 中位 5.75m/s，格宽 1.2m），
# 取 0.25 偏保守；过缝（缝口入点→出点 ≈2.1m）按 0.6s 要求门一直开着。
SEC_PER_STEP = 0.25
SEC_PER_CROSS = 0.6
# 插件缝口等门上限：没有时间表时 4s；有预测等待时 = 预测 + 3s 余量。
GATE_WAIT_DEFAULT = 4.0
GATE_WAIT_MARGIN = 3.0
# 接缝判定：两块地板边缘差这么多以内算贴边。
EDGE_EPS = 0.05
# 墙离接缝线这么近（含）算贴着接缝的墙（篮子墙厚 0.6）。
STRIP_MAX = 0.8

Box = Tuple[float, float, float, float]   # (minx, maxx, minz, maxz)
Band = Tuple[float, float]


class Seam(object):
    """一条接缝。axis='x'：接缝线 x=line、沿 z 延伸，穿缝沿 x 走；axis='z' 反之。"""

    def __init__(self, axis, line, lo, hi, half, walls):
        # type: (str, float, float, float, float, List[Box]) -> None
        self.axis = axis
        self.line = line
        self.lo, self.hi = lo, hi      # 两侧地板沿接缝方向的重叠范围
        self.half = half               # 墙带半厚（接缝线两侧各 half）
        self.walls = walls             # 贴着接缝、与墙带相交的阻挡盒
        # (跨缝坐标小的一侧, 大的一侧) 地板沿缝方向的下沿；find_seams 填
        self.anchors = None            # type: Optional[Tuple[float, float]]

    def bands(self, radius=GATE_RADIUS):
        # type: (float) -> List[Band]
        """厨师中心沿接缝方向可站的区间（门洞按半径收窄后的结果）。"""
        return free_bands(self, radius)


def _along(box, axis):
    # type: (Box, str) -> Tuple[float, float]
    return (box[2], box[3]) if axis == "x" else (box[0], box[1])


def _cross(box, axis):
    # type: (Box, str) -> Tuple[float, float]
    return (box[0], box[1]) if axis == "x" else (box[2], box[3])


def find_seams(terrain_by_layer):
    # type: (Dict[int, List[Box]]) -> List[Seam]
    """Ground 盒两两贴边、且接缝上有墙的地方。没墙的贴边地板本来就通，不算。"""
    grounds = terrain_by_layer.get(occupancy.LAYER_GROUND) or []
    blocking = []  # type: List[Box]
    for layer in occupancy.BLOCKING_LAYERS:
        blocking.extend(terrain_by_layer.get(layer) or ())
    seams = []  # type: List[Seam]
    seen = set()
    for i, a in enumerate(grounds):
        for b in grounds[i + 1:]:
            for axis in ("x", "z"):
                ca, cb = _cross(a, axis), _cross(b, axis)
                if abs(ca[1] - cb[0]) <= EDGE_EPS:
                    line = (ca[1] + cb[0]) / 2.0
                elif abs(cb[1] - ca[0]) <= EDGE_EPS:
                    line = (cb[1] + ca[0]) / 2.0
                else:
                    continue
                la, lb = _along(a, axis), _along(b, axis)
                lo, hi = max(la[0], lb[0]), min(la[1], lb[1])
                if hi - lo < 2 * GATE_RADIUS:
                    continue      # 重叠太短，人过不去，也不是接缝
                key = (axis, round(line, 2), round(lo, 2), round(hi, 2))
                if key in seen:
                    continue
                seen.add(key)
                walls, half = _seam_walls(blocking, axis, line, lo, hi)
                if walls:
                    seam = Seam(axis, line, lo, hi, half, walls)
                    # 两侧地板沿缝方向的下沿：随各自平台平移，等门寻路用它推算
                    # 「开门时对面那格现在在哪」（静止一侧恒定）。
                    lo_side, hi_side = (a, b) if ca[0] < cb[0] else (b, a)
                    seam.anchors = (_along(lo_side, axis)[0], _along(hi_side, axis)[0])
                    seams.append(seam)
    return seams


def _seam_walls(blocking, axis, line, lo, hi):
    # type: (List[Box], str, float, float, float) -> Tuple[List[Box], float]
    """贴着接缝线的墙：跨缝方向上离接缝线 ≤ STRIP_MAX、沿缝方向与 [lo,hi] 相交。"""
    touching = []  # type: List[Box]
    half = 0.0
    for box in blocking:
        c0, c1 = _cross(box, axis)
        a0, a1 = _along(box, axis)
        if a1 <= lo or a0 >= hi:
            continue
        if c1 < line - STRIP_MAX - EDGE_EPS or c0 > line + STRIP_MAX + EDGE_EPS:
            continue
        # 接缝墙：跨缝方向薄（≤ STRIP_MAX），且贴着接缝线（某一边在线上或跨过线）。
        # 沿缝方向的长墙（篮子上下沿那种）也贴线，但它是两端的封口，不定墙带厚度。
        if c1 - c0 > STRIP_MAX + EDGE_EPS:
            continue
        if not (abs(c0 - line) <= EDGE_EPS or abs(c1 - line) <= EDGE_EPS or c0 < line < c1):
            continue
        touching.append(box)
        half = max(half, min(max(line - c0, c1 - line), STRIP_MAX))
    if not touching:
        return [], 0.0
    # 墙带内的其它阻挡（如紧贴接缝的燃烧器碰撞盒）也会挡人
    strip = []  # type: List[Box]
    for box in blocking:
        c0, c1 = _cross(box, axis)
        a0, a1 = _along(box, axis)
        if a1 <= lo or a0 >= hi:
            continue
        if c1 > line - half and c0 < line + half:
            strip.append(box)
    return strip, half


def free_bands(seam, radius=GATE_RADIUS):
    # type: (Seam, float) -> List[Band]
    """墙在接缝带上的投影取补集（限地板重叠范围内），两头各收窄 radius。"""
    spans = sorted(_along(b, seam.axis) for b in seam.walls)
    out = []  # type: List[Band]
    cur = seam.lo
    for a0, a1 in spans:
        if a0 > cur:
            out.append((cur, min(a0, seam.hi)))
        cur = max(cur, a1)
        if cur >= seam.hi:
            break
    if cur < seam.hi:
        out.append((cur, seam.hi))
    bands = []  # type: List[Band]
    for g0, g1 in out:
        b0, b1 = g0 + radius, g1 - radius
        if b1 - b0 >= MIN_BAND:
            bands.append((b0, b1))
    return bands


class SeamGates(object):
    """一帧的门洞格表 + 叠在 walkable / edge_blocked 上的寻路谓词。

    gates: {(gx, gz): (seam, band)}；band 为 None = 这格所在接缝此刻门洞没开
    （或没盖到这一行）。门洞格只许沿穿缝方向进出，不许沿接缝方向在墙里走。
    """

    def __init__(self, seams, mapping, clear=None, radius=GATE_RADIUS):
        # type: (List[Seam], object, Optional[Callable[[float, float], bool]], float) -> None
        self.seams = seams
        self.mapping = mapping
        self.radius = radius
        self.gates = {}  # type: Dict[Tuple[int, int], Tuple[Seam, Optional[Band]]]
        cell_x = mapping.cell
        cell_z = getattr(mapping, "cell_z", cell_x)
        for seam in seams:
            bands = seam.bands(radius)
            along_cell = cell_z if seam.axis == "x" else cell_x
            for gx, gz in self._seam_cells(seam):
                x, z = mapping.to_world(gx, gz)
                c, a = (x, z) if seam.axis == "x" else (z, x)
                if abs(c - seam.line) > seam.half + radius:
                    continue
                row = (a - along_cell / 2.0, a + along_cell / 2.0)
                band = _best_overlap(bands, row)
                if band is not None and clear is not None and not clear(x, z):
                    band = None   # 落火 / KillPlane 照样不能走
                self.gates[(gx, gz)] = (seam, band)

    def _seam_cells(self, seam):
        m = self.mapping
        if seam.axis == "x":
            g0 = m.to_grid(seam.line - seam.half - self.radius, seam.lo)
            g1 = m.to_grid(seam.line + seam.half + self.radius, seam.hi)
        else:
            g0 = m.to_grid(seam.lo, seam.line - seam.half - self.radius)
            g1 = m.to_grid(seam.hi, seam.line + seam.half + self.radius)
        for gx in range(min(g0[0], g1[0]), max(g0[0], g1[0]) + 1):
            for gz in range(min(g0[1], g1[1]), max(g0[1], g1[1]) + 1):
                yield gx, gz

    def walkable(self, base, clock=None):
        # type: (Callable[[int, int], bool], Optional[object]) -> Callable[[int, int], bool]
        """门洞格：此刻开着，或时间表已知且这格开过（关着的要等，见 wait_cost）。"""
        gates = self.gates

        def is_walkable(gx, gz):
            # type: (int, int) -> bool
            g = gates.get((gx, gz))
            if g is not None:
                if g[1] is not None:
                    return True
                key = seam_key(g[0])
                return bool(clock is not None and clock.known(key)
                            and clock.cell_mid(key, (gx, gz)) is not None)
            return base(gx, gz)

        return is_walkable

    def jumps(self, base, clock):
        # type: (Callable[[int, int], bool], Optional[object]) -> Tuple[Dict[Tuple[int, int], List[Tuple[int, int]]], set]
        """门洞此刻关着、靠时间表预测会开的格：对面那一格「到开门时」现在在哪。

        篮子在动，门关着时门洞格对面的格子此刻可能悬空（trace-20261030-125344
        t=1665~1830：对面 (5,0)/(5,1) 已不在篮子地板上，可达场止于门洞格，跨缝
        task 全判 unreachable，P2 空站 8s）。开门时两侧地板的沿缝下沿记在
        clock.cell_anchors 里，按「开门时 − 现在」的平移把对面那格挪回现在的
        位置，挪到的格此刻可走才连（篮子刚体平移，开门时站得下 ⇒ 现在随篮子
        平移过的那格也站得下）。

        返回 (jumps, stale)：jumps 双向 {格: [跳到的格]}（门洞格 ↔ 挪后的对面格）；
        stale 是作废的普通相邻步 {(门洞格, 原对面格)}——那一侧开门时已不在那里。
        此刻开着的门洞平移为 0，不跳；静止一侧平移为 0，照常相邻。
        """
        jumps = {}  # type: Dict[Tuple[int, int], List[Tuple[int, int]]]
        stale = set()
        if clock is None:
            return jumps, stale
        m = self.mapping
        for cell, (seam, band) in self.gates.items():
            if band is not None or seam.anchors is None:
                continue
            key = seam_key(seam)
            if not clock.known(key):
                continue
            then = clock.cell_anchors(key, cell)
            if not then:
                continue
            along_cell = getattr(m, "cell_z", m.cell) if seam.axis == "x" else m.cell
            for side, sign in ((0, -1), (1, 1)):
                shift = then[side] - seam.anchors[side]      # 开门时比现在多出的沿缝位移
                off = int(round(shift / along_cell))
                if off == 0:
                    continue
                nb = (cell[0] + sign, cell[1]) if seam.axis == "x" else (cell[0], cell[1] + sign)
                stale.add((cell, nb))
                # 开门时贴着门洞的那块地板，此刻已随平台平移了 −shift
                moved = (nb[0], nb[1] - off) if seam.axis == "x" else (nb[0] - off, nb[1])
                if moved in self.gates or not base(moved[0], moved[1]):
                    continue
                jumps.setdefault(cell, []).append(moved)
                jumps.setdefault(moved, []).append(cell)
        return jumps, stale

    def wait_cost(self, clock, now):
        # type: (object, float) -> Callable[[int, Tuple[int, int], Tuple[int, int]], Optional[int]]
        """distance_field 的等门代价：从普通格踏进门洞格时，按到达时刻查时间表。

        到达时刻 = now + 已走代价 × SEC_PER_STEP。门洞格之间（缝里）不再等。
        时间表未知时退回「此刻开着才通」（与没有时间表一致）。
        """
        gates = self.gates

        def cost(c, cur, nxt):
            # type: (int, Tuple[int, int], Tuple[int, int]) -> Optional[int]
            g = gates.get(nxt)
            if g is None or cur in gates:
                return 0
            key = seam_key(g[0])
            w = clock.wait(key, now + c * SEC_PER_STEP, need=SEC_PER_CROSS,
                           open_now=g[1] is not None)
            if w is None:
                return None
            return int(math.ceil(w / SEC_PER_STEP)) if w > 0 else 0

        return cost

    def edge_blocked(self, base):
        # type: (Optional[Callable]) -> Callable[[Tuple[int, int], Tuple[int, int]], bool]
        gates = self.gates

        def blocked(cur, nxt):
            # type: (Tuple[int, int], Tuple[int, int]) -> bool
            for cell in (cur, nxt):
                g = gates.get(cell)
                if g is None:
                    continue
                along_x = cur[0] != nxt[0]
                if (g[0].axis == "x") != along_x:
                    return True     # 门洞格里只能穿缝，不能顺着墙走
            return base(cur, nxt) if base is not None else False

        return blocked

    def path_points(self, path, clock=None, waits=None):
        # type: (List[Tuple[int, int]], Optional[object], Optional[Dict]) -> Tuple[List[List[float]], List[Dict]]
        """格子路径 → (世界坐标路点, gates 描述)。

        路径中间连续的门洞格（同一接缝）换成「缝口入点 + 缝口出点」两个路点，
        沿接缝方向取门洞中线；gates 里记这两个路点的下标，供插件逐帧改写。
        门洞格在路径两端（人正站在缝里 / 目标格落在缝里）时不改写，按格中心走。
        门洞此刻关着（按时间表要等）时中线取这格上次开时的中线，max_wait 按
        距离场里这一步的等门时长 + GATE_WAIT_MARGIN 给插件。
        """
        m = self.mapping
        points = []  # type: List[List[float]]
        gates = []   # type: List[Dict]
        i = 0
        while i < len(path):
            g = self.gates.get(tuple(path[i]))
            j = i
            if g is not None:
                while j + 1 < len(path) and self.gates.get(tuple(path[j + 1]), (None,))[0] is g[0]:
                    j += 1
            mid = None
            if g is not None and g[1] is not None:
                mid = (g[1][0] + g[1][1]) / 2.0
            elif g is not None and clock is not None:
                mid = clock.cell_mid(seam_key(g[0]), tuple(path[i]))
            if mid is None or i == 0 or j == len(path) - 1:
                points.append(list(m.to_world(*path[i])))
                i += 1
                continue
            seam = g[0]
            bx, bz = m.to_world(*path[i - 1])
            sign = -1.0 if (bx if seam.axis == "x" else bz) < seam.line else 1.0
            offset = seam.half + self.radius
            enter_c = seam.line + sign * offset
            exit_c = seam.line - sign * offset
            enter_idx = len(points)
            points.append(_pt(seam.axis, enter_c, mid))
            points.append(_pt(seam.axis, exit_c, mid))
            gates.append({
                "axis": seam.axis, "line": round(seam.line, 4), "half": round(seam.half, 4),
                "enter": enter_idx, "exit": enter_idx + 1,
                "enter_c": round(enter_c, 4), "exit_c": round(exit_c, 4),
                "lo": round(seam.lo, 4), "hi": round(seam.hi, 4),
                "mid": round(mid, 4), "radius": self.radius,
            })
            if waits:
                w = (waits.get(tuple(path[i]), 0) - waits.get(tuple(path[i - 1]), 0)) * SEC_PER_STEP
                if w > 0:
                    gates[-1]["max_wait"] = round(max(GATE_WAIT_DEFAULT, w + GATE_WAIT_MARGIN), 2)
            i = j + 1
        return points, gates


def _pt(axis, cross, along):
    # type: (str, float, float) -> List[float]
    return [round(cross, 4), round(along, 4)] if axis == "x" else [round(along, 4), round(cross, 4)]


def _best_overlap(bands, row):
    # type: (List[Band], Band) -> Optional[Band]
    best, best_len = None, 0.0
    for b in bands:
        overlap = min(b[1], row[1]) - max(b[0], row[0])
        if overlap > best_len:
            best, best_len = b, overlap
    return best


def build(terrain_by_layer, mapping, clear=None, radius=GATE_RADIUS):
    # type: (Dict[int, List[Box]], object, Optional[Callable[[float, float], bool]], float) -> Optional[SeamGates]
    """没有带墙接缝的关卡返回 None，调用方照旧用 walkable / edge_blocked。"""
    seams = find_seams(terrain_by_layer)
    if not seams:
        return None
    gates = SeamGates(seams, mapping, clear, radius)
    return gates if gates.gates else None


def _clock_of(bb):
    # type: (object) -> Tuple[Optional[object], Optional[float]]
    clock = getattr(bb, "seam_clock", None)
    now = getattr(clock, "now", None) if clock is not None else None
    return (clock, now) if now is not None else (None, None)


def nav_predicates(world, bb=None):
    # type: (object, object) -> Tuple[Callable[[int, int], bool], Optional[Callable]]
    """寻路用的 (walkable, edge_blocked)：有门洞时叠门洞规则，否则原样返回。

    bb 带门洞时间表（bb.seam_clock）时，关着但预测会开的门洞格也算可走；
    对面按开门时平移过的格经 nav_jumps 连过去，原相邻步作废。
    """
    walkable = world.walkable
    edge = getattr(world, "edge_blocked", None)
    sg = getattr(world, "seam_gates", None)
    if sg is None:
        return walkable, edge
    clock, _ = _clock_of(bb)
    _, stale = nav_jumps(world, bb)
    base_edge = sg.edge_blocked(edge)
    if not stale:
        return sg.walkable(walkable, clock), base_edge

    def edge_blocked(cur, nxt):
        # type: (Tuple[int, int], Tuple[int, int]) -> bool
        if (cur, nxt) in stale or (nxt, cur) in stale:
            return True
        return base_edge(cur, nxt)

    return sg.walkable(walkable, clock), edge_blocked


def nav_jumps(world, bb=None):
    # type: (object, object) -> Tuple[Dict[Tuple[int, int], List[Tuple[int, int]]], set]
    """(jumps, stale)：关着的预测门洞格到「开门时对面那格现在的位置」的跳边。"""
    sg = getattr(world, "seam_gates", None)
    clock, now = _clock_of(bb)
    if sg is None or clock is None:
        return {}, set()
    cached = getattr(sg, "_jumps_cache", None)
    if cached is not None and cached[0] == now:
        return cached[1]
    got = sg.jumps(world.walkable, clock)
    sg._jumps_cache = (now, got)
    return got


def wait_cost_of(world, bb=None):
    # type: (object, object) -> Optional[Callable]
    """distance_field 的 wait_cost；没门洞或没时间表时 None（与旧行为一致）。"""
    sg = getattr(world, "seam_gates", None)
    clock, now = _clock_of(bb)
    if sg is None or clock is None:
        return None
    return sg.wait_cost(clock, now)


def path_points(world, path, field=None, bb=None):
    # type: (object, List[Tuple[int, int]], Optional[Dict], object) -> Tuple[List[List[float]], List[Dict]]
    """move 命令的路点与 gates 字段；无门洞时等价于逐格 to_world。"""
    sg = getattr(world, "seam_gates", None)
    if sg is None:
        return [list(world.to_world(*c)) for c in path], []
    clock, _ = _clock_of(bb)
    return sg.path_points(path, clock, getattr(field, "waits", None))


def gates_wait_seconds(gates):
    # type: (List[Dict]) -> float
    """move 命令里各门洞的等门上限之和（秒），调用方据此放宽超时。

    没给 max_wait 的门洞按插件缺省 GATE_WAIT_DEFAULT 算：Python 的超时必须比
    插件等门长，插件才来得及回 gate closed（带门洞格）；否则 GoToCell 2.5s
    先超时、插件等门的结论永远回不来（trace-20261030-123850 缝口反复超时）。
    """
    return sum(float(g.get("max_wait") or GATE_WAIT_DEFAULT) + 1.0 for g in gates or ())

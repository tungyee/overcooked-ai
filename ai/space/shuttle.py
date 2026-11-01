"""渡运台面：人上不去、只能隔着围栏够到的移动台面（Balloon_2_2 = 游戏内 2-3）。

Balloon_2_2 下方的 Server_Balloon 篮子沿 x 来回（右停 bx≈18、左停 bx≈3.6，
各停 ≈6.4s，单程 ≈11.6s，周期 ≈36s）。篮子地板只有 0.63m 宽、后面就是台面，
人站不上去；两侧固定地板的围栏各开一个口，篮子停靠时隔着口放/取台面上的东西：
右侧装好的菜放上去送到左侧，左侧洗好的盘放上去送到右侧。

和平台接缝门洞（seams.py）不是一回事：门洞是人「走过去」，这里是人「够过去」。
trace-20261031-185032 t=57.7~64.1：两盘成品停在左口，P2 的 DeliverPlate 全判
unreachable——台面正前一排格心 z=6.0 落在固定地板外 3cm（Ground minz=6.03），
常规交互位（四邻可走格）一个都没有；P1 实际站在 z≈6.44（两排格心之间）。

本模块三件事：
  1. 识别渡运网格与渡运台面（`shuttle_grids` / `is_shuttle_station`）；
  2. 格外站位（`stances`）：从台面外沿往外找能站人的世界点，挂到最近的可走格上
     给寻路用，move 命令最后一段走到这个点；
  3. 可交互窗口时间表（`observe` / `wait_seconds`）：每个台面对 P2 的可达区
     「够得着 / 够不着」当一道门，复用 seam_clock 的开关沿学习（周期、开门时长、
     相位、防抖与断档处理都一样），预测「走过去之后还要等几秒」。

不读 Blackboard 以外的状态；不写 World。时间表挂 bb.shuttle_clock，由 agent
每拍 observe 一次。
"""
import math
from typing import Dict, List, Optional, Set, Tuple

from ai.query import predicates as P
from ai.space import occupancy as O

# 人站立点离墙/台面 collider 的最小距离（m）：厨师胶囊半径 0.4 + 0.05 余量。
# trace-20261031-185032 P1 隔口放盘实停 z≈6.44（围栏口地板沿 6.03 + 0.41）。
STANCE_CLEARANCE = 0.45
# 从台面外沿往外找站点的步长与最远距离（m）。够得着的上限按实测：P1 站 z=6.44
# 拿得到 z=4.15~5.35 的台面（离外沿 1.09m）；再远交互句柄就扫不到了。
STANCE_STEP = 0.05
STANCE_REACH = 1.25
# 沿台面边的取样偏移（m）：正中被墙角挡时，偏一点可能就站得下。
STANCE_ALONG = (0.0, -0.3, 0.3)
# 站点挂到可走格的最远距离（m）：格心离站点超过它就不算「走到这格再挪一步」。
LINK_DIST = 1.0
# 出餐口格外站位的挂格距离（m）：站点常落在两行格心之间（离格心最多 ≈0.85m
# 斜向），再加沿边偏移，1.0 会漏；不超过一格（1.2m）+ 余量，不会挂到隔一格的地方。
HATCH_LINK_DIST = 1.3
# move 终点比站点再往台面推多少（m）：插件 0.5m 内判到达，推一点让人贴到墙边停住，
# 交互句柄才稳定命中；推进墙里也没关系，人被 collider 挡住、停在站点附近。
GOAL_PUSH = 0.3
# 已到站点的判据（m）：离任一站点这么近就不再走。
AT_STANCE = 0.35
# 篮子上取一次东西要多久窗口（秒）：走最后一步 + tap + 等确认帧。
PICK_NEED_S = 1.0

_DIRS = ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0))


# ---------------------------------------------------------------- 识别


def shuttle_grids(world):
    # type: (object) -> Set[str]
    """渡运网格：会动、上面有台面、且台面整体外包盒里没有一格主网格可走格。

    人能走上去的移动平台（Balloon_2_3 中间气球、Rapids 木筏）台面之间就是可走
    地板，外包盒里必有可走格，不算渡运——它们走门洞 / 可达场那一套。
    每帧缓存在 world 上。
    """
    cached = getattr(world, "_shuttle_grids", None)
    if cached is not None:
        return cached
    raw = getattr(world, "raw", None) or {}
    moving = {str(g.get("id")) for g in (raw.get("grids") or ())
              if isinstance(g, dict) and g.get("moving") is True and g.get("id")}
    out = set()  # type: Set[str]
    for gid in moving:
        boxes = [O._box_of(st) for st in getattr(world, "stations", ()) or ()
                 if isinstance(st, dict) and str(st.get("grid") or "") == gid
                 and st.get("t") == "counter"]
        boxes = [b for b in boxes if b is not None]
        if not boxes:
            continue
        hull = (min(b[0] for b in boxes), max(b[1] for b in boxes),
                min(b[2] for b in boxes), max(b[3] for b in boxes))
        if not _hull_has_walkable(world, hull):
            out.add(gid)
    world._shuttle_grids = out
    return out


def _hull_has_walkable(world, hull):
    # type: (object, Tuple[float, float, float, float]) -> bool
    g0 = world.to_grid(hull[0], hull[2])
    g1 = world.to_grid(hull[1], hull[3])
    for gx in range(min(g0[0], g1[0]), max(g0[0], g1[0]) + 1):
        for gz in range(min(g0[1], g1[1]), max(g0[1], g1[1]) + 1):
            x, z = world.to_world(gx, gz)
            if not (hull[0] <= x <= hull[1] and hull[2] <= z <= hull[3]):
                continue
            if world.walkable(gx, gz):
                return True
    return False


def is_shuttle_station(world, st):
    # type: (object, Optional[Dict]) -> bool
    return (isinstance(st, dict) and st.get("t") == "counter"
            and str(st.get("grid") or "") in shuttle_grids(world))


def shuttle_stations(world):
    # type: (object) -> List[Dict]
    grids = shuttle_grids(world)
    if not grids:
        return []
    return [st for st in getattr(world, "stations", ()) or ()
            if isinstance(st, dict) and st.get("t") == "counter"
            and str(st.get("grid") or "") in grids]


def station_of(world, entry):
    # type: (object, Optional[Dict]) -> Optional[Dict]
    """entry 所在的渡运台面（entry 本身是台面 / 盘、料放在台面上）；不在返回 None。"""
    if not isinstance(entry, dict) or not shuttle_grids(world):
        return None
    if is_shuttle_station(world, entry):
        return entry
    if entry.get("held_by"):
        return None
    support = O._support_station(entry, shuttle_stations(world))
    return support if is_shuttle_station(world, support) else None


def slot_key(st):
    # type: (Dict) -> Tuple[str, str]
    """时间表里每个渡运台面一道「门」：(\"slot\", \"网格/站名\")。"""
    return ("slot", "%s/%s" % (st.get("grid"), st.get("n")))


# ---------------------------------------------------------------- 站位


def _blockers(world):
    # type: (object) -> List[Tuple[float, float, float, float]]
    cached = getattr(world, "_shuttle_blockers", None)
    if cached is None:
        cached = []
        for layer in O.BLOCKING_LAYERS:
            cached.extend(world.terrain.get(layer) or ())
        world._shuttle_blockers = cached
    return cached


def _standable(world, x, z):
    # type: (object, float, float) -> bool
    ground = world.terrain.get(O.LAYER_GROUND) or []
    if not ground or not O._point_in_any_box(x, z, ground):
        return False
    if O._point_in_any_box(x, z, world.terrain.get(O.LAYER_KILLPLANE) or []):
        return False
    hazards = getattr(world, "hazards", None)
    if hazards and O._point_in_any_box(
            x, z, O.hazard_boxes(hazards, cell=world.mapping.cell)):
        return False
    return not O._point_in_any_box(x, z, _blockers(world), margin=STANCE_CLEARANCE)


def _link_cell(world, x, z, link_dist=LINK_DIST):
    # type: (object, float, float, float) -> Optional[Tuple[int, int]]
    """站点挂到最近的可走格（站点所在格及八邻，格心距 ≤ link_dist）。"""
    c = world.to_grid(x, z)
    best, best_d = None, None
    for dx in (-1, 0, 1):
        for dz in (-1, 0, 1):
            cell = (c[0] + dx, c[1] + dz)
            if not world.walkable(*cell):
                continue
            cx, cz = world.to_world(*cell)
            d = math.hypot(cx - x, cz - z)
            if d <= link_dist and (best_d is None or d < best_d):
                best, best_d = cell, d
    return best


def stances(world, st):
    # type: (object, Dict) -> List[Dict]
    """渡运台面此刻的格外站位：[{x, z, face: (dx, dz), cell}]，cell 是寻路落脚格。

    四个方向各取最近的一个站点：从台面外沿（边中点，沿边偏 STANCE_ALONG）往外
    每 STANCE_STEP 找第一个能站的点，超过 STANCE_REACH 没找到就这个方向没有。
    每帧按台面名缓存。
    """
    return _edge_stances(world, st, O._box_of(st), LINK_DIST)


def hatch_stances(world, st):
    # type: (object, Dict) -> List[Dict]
    """出餐口的格外站位：格内交互位一个都没有时的兜底（同 stances 的形状）。

    会漂的出餐口（Rapids_3_5 的出餐船）不在任何网格上，占地按静态主网格量化。
    船身上下晃 0.1~0.2m，阻挡 box 就在两行之间跳；跳到外侧那行时，紧挨着它
    的那行恰是船舷外的水面（不可走），四邻交互位一个都没有，DeliverPlate 判
    unreachable_destination——但站在原来那格隔着 0.5m 照样送得到。
    trace-20261101-180609 t=12480~12520：P2 端着装好的鸡块盘，出餐口每隔几秒
    「消失」一次，P2 把成品盘放到木筏上去干别的，到收盘没送出去。

    按 PLATEBLOCK 阻挡 box（真实占地）从外沿找站点；落脚格可能离站点稍远
    （站点在两行格心之间），挂格距离放宽到 HATCH_LINK_DIST。
    """
    if not isinstance(st, dict) or st.get("t") != "plate_station":
        return []
    return _edge_stances(world, st, _hatch_box(world, st), HATCH_LINK_DIST)


def hatch_fallback(world, st):
    # type: (object, Optional[Dict]) -> List[Dict]
    """出餐口格内交互位为空时的格外站位；有格内交互位 / 不是出餐口 → []。

    规划层（reachability._interact_goals）与执行层（move.Approach）共用这一个
    判据：只在常规交互位一个都没有时启用，不改动静态出餐口的既有站位选择。
    """
    if not isinstance(st, dict) or st.get("t") != "plate_station":
        return []
    if not hasattr(world, "mapping") or not getattr(world, "terrain", None):
        return []
    from ai.space import navigation
    fp = O.station_block_cells(st, world.terrain, world.mapping)
    if navigation.serving_hatch_interact_cells(fp, world.walkable, world.belt_cells):
        return []
    return hatch_stances(world, st)


def _hatch_box(world, st):
    # type: (object, Dict) -> Optional[Tuple[float, float, float, float]]
    """出餐口的 PLATEBLOCK 阻挡 box（含站点中心那个）；没有则退回站点 AABB。"""
    sx, sz = st.get("x"), st.get("z")
    if sx is not None and sz is not None:
        for box in world.terrain.get(O.LAYER_PLATEBLOCK) or ():
            if box[0] <= sx <= box[1] and box[2] <= sz <= box[3]:
                return tuple(box[:4])
    return O._box_of(st)


def _edge_stances(world, st, box, link_dist):
    # type: (object, Dict, Optional[Tuple[float, float, float, float]], float) -> List[Dict]
    cache = getattr(world, "_shuttle_stances", None)
    if cache is None:
        cache = world._shuttle_stances = {}
    key = (st.get("t"), st.get("grid"), st.get("n"), link_dist)
    if key in cache:
        return cache[key]
    out = []  # type: List[Dict]
    if box is not None:
        cx, cz = (box[0] + box[1]) / 2.0, (box[2] + box[3]) / 2.0
        for dx, dz in _DIRS:
            best = None
            for along in STANCE_ALONG:
                # 外沿起点：沿 (dx,dz) 方向的那条边，沿边偏 along
                ex = box[1] if dx > 0 else box[0] if dx < 0 else cx + along
                ez = box[3] if dz > 0 else box[2] if dz < 0 else cz + along
                d = STANCE_STEP
                while d <= STANCE_REACH + 1e-6:
                    x, z = ex + dx * d, ez + dz * d
                    if _standable(world, x, z):
                        if best is None or d < best[0]:
                            best = (d, x, z)
                        break
                    d += STANCE_STEP
            if best is None:
                continue
            cell = _link_cell(world, best[1], best[2], link_dist)
            if cell is None:
                continue
            out.append({"x": best[1], "z": best[2], "face": (-dx, -dz), "cell": cell})
    cache[key] = out
    return out


def goal_point(stance):
    # type: (Dict) -> List[float]
    """move 命令的终点：站点再朝台面推 GOAL_PUSH。"""
    fx, fz = stance["face"]
    return [stance["x"] + fx * GOAL_PUSH, stance["z"] + fz * GOAL_PUSH]


def at_stance(chef, stance_list):
    # type: (Optional[Dict], List[Dict]) -> bool
    if not chef or chef.get("x") is None or chef.get("z") is None:
        return False
    return any(math.hypot(chef["x"] - s["x"], chef["z"] - s["z"]) <= AT_STANCE
               for s in stance_list)


def reachable_stances(world, st, field):
    # type: (object, Dict, Optional[Dict]) -> List[Dict]
    """落脚格在可达场里的站位；field 为 None（拓扑未知）时全给。"""
    got = stances(world, st)
    if field is None:
        return got
    return [s for s in got if s["cell"] in field]


# ---------------------------------------------------------------- 时间表


def observe(clock, world, field, now):
    # type: (object, object, Optional[Dict], float) -> None
    """记一帧每个渡运台面对这个可达场「够不够得着」（clock 是 SeamClock）。"""
    flags = {}  # type: Dict[Tuple[str, str], Dict[Tuple[int, int], float]]
    for st in shuttle_stations(world):
        cells = {s["cell"]: 0.0 for s in reachable_stances(world, st, field)}
        flags[slot_key(st)] = cells
    clock.observe_flags(now, flags)


def _field_cost_seconds(field, cells):
    # type: (Dict, object) -> Optional[float]
    from ai.space.seams import SEC_PER_STEP
    costs = [field[c] for c in cells if c in field]
    return min(costs) * SEC_PER_STEP if costs else None


def wait_seconds(world, entry, field, clock):
    # type: (object, Dict, Optional[Dict], object) -> Optional[float]
    """entry 在渡运台面上：从现在出发走过去，到了还要在口边等几秒才够得着。

    不在渡运台面上 / 拓扑未知 / 此刻就够得着 → 0。
    够不着且时间表未知 / 本圈来不及 → None（调用方按不可达处理）。
    """
    st = station_of(world, entry)
    if st is None or field is None:
        return 0.0
    if reachable_stances(world, st, field):
        return 0.0
    if clock is None or clock.now is None:
        return None
    key = slot_key(st)
    if not clock.known(key):
        return None
    walk = _field_cost_seconds(field, clock.open_cells(key))
    if walk is None:
        return None
    return clock.wait(key, clock.now + walk, need=PICK_NEED_S)


def staging_cell(world, entry, field, clock):
    # type: (object, Dict, Optional[Dict], object) -> Optional[Tuple[int, int]]
    """篮子还没到时先去哪格等：上一轮够得着时用过的落脚格里最近的那个。"""
    st = station_of(world, entry)
    if st is None or field is None or clock is None:
        return None
    cells = [c for c in clock.open_cells(slot_key(st)) if c in field]
    if not cells:
        return None
    return min(cells, key=lambda c: field[c])

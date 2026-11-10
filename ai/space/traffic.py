# -*- coding: utf-8 -*-
"""4-1 过街：红绿灯与车辆避让。

插件在 `state["traffic"]` 里给：
  - `road`：马路条带（主网格 gx -3..6 的格心范围）；
  - `lights`：每个灯的 `{n,x,z,on,on_car_b,red,yellow,green,has_lamps}`；
  - `cars`：两辆车的根位置 + 车下 KillPlane 的 AABB。

`red/yellow/green` 是玩家看到的灯色，从灯的 `red_glow` / `yellow_glow` /
`green_glow` 子物体直读。本关规则是「红灯行、绿灯停」：红灯 = 可以过街，
黄/绿 = 等。`on` / `on_car_b` 是两辆车的绿灯（车走），只保留在状态里供
诊断，**不参与过街判据**；读不到灯色时停在路沿，不猜车灯。

`cars` 的 KillPlane AABB 是第二道保险：即使灯是红的，车体当前正好压在
路径格上也不起步。

本模块只读 duck-typed 的 World，不依赖 World 类，方便单测。
"""
from typing import Any, Dict, List, Optional, Tuple

CAR_X_MARGIN = 0.8         # 车宽方向余量
CAR_Z_MARGIN = 3.0         # 车沿 z 跑，前后各留 3m
# 红灯起步窗口（拍，20 Hz）：刚亮起时可以起步（横穿要 ~2 秒，红灯周期约 2 秒，
# 踩在开头才够穿过）；红灯已经亮了很久（4-1 开局的长红灯，实测 20~27 秒）也当
# 起步窗口——车一直停在两端，是安全的。中间这段（短红灯的尾巴）不起步：等下一轮。
# 长红灯的尾巴由「途中变灯去中线等」兜底。
RED_FRESH_TICKS = 8
RED_LONG_TICKS = 60

def _truthy(value):
    # type: (Any) -> bool
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def road_box(world):
    # type: (Any) -> Optional[Tuple[float, float, float, float]]
    road = getattr(world, "traffic_road", None)
    if not isinstance(road, dict):
        return None
    try:
        return (float(road["minx"]), float(road["maxx"]),
                float(road["minz"]), float(road["maxz"]))
    except (KeyError, TypeError, ValueError):
        return None


def _point_in_road(world, cell):
    # type: (Any, Tuple[int, int]) -> bool
    box = road_box(world)
    if box is None or cell is None:
        return False
    try:
        x, z = world.to_world(*cell)
    except (AttributeError, TypeError, ValueError):
        return False
    return box[0] <= x <= box[1] and box[2] <= z <= box[3]


def _point_in_road_xy(world, x, z):
    # type: (Any, float, float) -> bool
    box = road_box(world)
    if box is None:
        return False
    return box[0] <= x <= box[1] and box[2] <= z <= box[3]


def in_road(world, cell):
    # type: (Any, Optional[Tuple[int, int]]) -> bool
    return cell is not None and _point_in_road(world, cell)


def chef_in_road(world, chef):
    # type: (Any, Any) -> bool
    """厨师**实际位置**是否已经在马路条带里。

    不能用格心判：路沿那一列（gx=-3 / gx=6）的格心正好等于 road 的
    minx / maxx，`in_road` 会把站在路沿的人当成「已在过街」，让
    `safe_to_cross` 的「半途不停」短路掉红绿灯门控——实机里 AI 就是这么
    在绿灯时从路沿起步被车撞的（trace-20261110-115339 t=390/t=591）。
    """
    if not isinstance(chef, dict):
        return False
    x, z = chef.get("x"), chef.get("z")
    if x is None or z is None:
        return False
    return _point_in_road_xy(world, x, z)


def road_side(world, chef):
    # type: (Any, Any) -> Optional[str]
    """实际位置在马路哪一侧：'west' / 'east'；在路里或没有路 → None。

    出手位选在马路对面没有意义（人都要站到车道上），用这个把候选格限制在
    厨师当前所在的一侧。
    """
    box = road_box(world)
    if box is None or not isinstance(chef, dict):
        return None
    x, z = chef.get("x"), chef.get("z")
    if x is None or z is None:
        return None
    if not (box[2] <= z <= box[3]):
        return None
    if x < box[0]:
        return "west"
    if x > box[1]:
        return "east"
    return None


def road_between(world, a, b):
    # type: (Any, Tuple[float, float], Tuple[float, float]) -> bool
    """两点是否分处马路两侧（4-1：马路纵贯整关，横穿即挡路）。"""
    box = road_box(world)
    if box is None or a is None or b is None:
        return False
    return ((a[0] < box[0] and b[0] > box[1])
            or (a[0] > box[1] and b[0] < box[0]))


def path_road_cells(world, path):
    # type: (Any, Optional[List[Tuple[int, int]]]) -> List[Tuple[int, int]]
    return [tuple(c) for c in (path or ()) if _point_in_road(world, c)]


def path_crosses_road(world, path):
    # type: (Any, Optional[List[Tuple[int, int]]]) -> bool
    return bool(path_road_cells(world, path))


def curb_index(world, path):
    # type: (Any, Optional[List[Tuple[int, int]]]) -> Optional[int]
    """路径里第一个马路格的前一格；起点就在马路上/不过街返回 None。"""
    for i, cell in enumerate(path or ()):
        if _point_in_road(world, cell):
            return i - 1 if i > 0 else None
    return None


def median_spot(world, path):
    # type: (Any, Optional[List[Tuple[int, int]]]) -> Optional[Tuple[Tuple[int, int], Tuple[float, float]]]
    """过街路径上的「两条车道中间」等待点：返回 (路径格, 等待点世界坐标)。

    车沿 z 跑，车的 KillPlane AABB 在 x 方向就是它压住的宽度，两辆车 x 区间
    之间的缝隙是撞不到的（4-1 实机：Green [27.68,29.38]、Blue [23.55,25.63]，
    缝隙 2.05m）。等待点取缝隙中点的 x，而不是格心——格心离某条车道只剩
    ~0.4m，站在正中间两边各留 ~1m。过街途中灯变了又还没过中线时先去这里等。
    """
    road_cells = path_road_cells(world, path)
    if not road_cells:
        return None
    spans = []
    for car in getattr(world, "vehicles", ()) or ():
        try:
            spans.append((float(car["minx"]), float(car["maxx"])))
        except (KeyError, TypeError, ValueError):
            continue
    if len(spans) < 2:
        return None
    spans.sort()
    gaps = [(hi, lo) for (_lo, hi), (lo, _hi) in zip(spans, spans[1:])
            if lo > hi]
    if not gaps:
        return None
    best = None  # type: Optional[Tuple[Tuple[int, int], Tuple[float, float], float]]
    for cell in road_cells:
        try:
            x, z = world.to_world(*cell)
        except (AttributeError, TypeError, ValueError):
            continue
        for lo, hi in gaps:
            if lo <= x <= hi:
                mid = (lo + hi) / 2.0
                dist = abs(x - mid)
                if best is None or dist < best[2]:
                    best = (cell, (mid, z), dist)
    return (best[0], best[1]) if best is not None else None


def median_cell(world, path):
    # type: (Any, Optional[List[Tuple[int, int]]]) -> Optional[Tuple[int, int]]
    """等待点所在的路径格（见 median_spot）；没有能站的缝隙返回 None。"""
    spot = median_spot(world, path)
    return spot[0] if spot is not None else None


def at_median(world, chef, spot):
    # type: (Any, Any, Any) -> bool
    """厨师此刻是否站在中线等待点上（按实际坐标，插件有 0.5m 到达容差）。

    只在**站定**时可靠：5Hz 快照最多滞后 0.2 秒（6 m/s ≈ 1 格），移动中
    不要用它做「继续/等待」判据（改道后的等待用原语的锁存状态）。
    """
    if spot is None or not isinstance(chef, dict):
        return False
    x, z = chef.get("x"), chef.get("z")
    if x is None or z is None:
        return False
    (mx, mz) = spot[1]
    return abs(x - mx) <= 0.6 and abs(z - mz) <= 0.9


def before_median(world, chef, spot, path):
    # type: (Any, Any, Any, Optional[List[Tuple[int, int]]]) -> bool
    """厨师还没越过中线等待点（沿 path 的前进方向）。"""
    if spot is None or not isinstance(chef, dict) or not path:
        return False
    x = chef.get("x")
    if x is None:
        return False
    try:
        mx = spot[1][0]
        sx = world.to_world(*path[0])[0]
        ex = world.to_world(*path[-1])[0]
    except (AttributeError, TypeError, ValueError):
        return False
    # 方向看路径终点相对起点，不能看起点相对中线：人已经过中线时，
    # 按起点判会把「继续向东」误判成向西，改道命令反而把人拽回中线。
    if abs(ex - sx) < 0.1:
        return False
    if ex > sx:
        return x < mx - 0.1
    return x > mx + 0.1


def _lamp_state(light):
    # type: (Any) -> Optional[str]
    """返回 'red' / 'yellow' / 'green' / 'off'；None = 旧帧没有灯色字段。"""
    if not isinstance(light, dict):
        return None
    if not any(k in light for k in ("red", "yellow", "green")):
        return None
    if _truthy(light.get("red")):
        return "red"
    if _truthy(light.get("yellow")):
        return "yellow"
    if _truthy(light.get("green")):
        return "green"
    return "off"


def pedestrian_red(world):
    # type: (Any) -> Optional[bool]
    """当前是否红灯（可过街）。None = 没有灯数据/读不到灯色。

    要求所有能读到灯色的灯都是红。这里**不看** `on` / `on_car_b` 车灯：
    那只是车的绿灯，不是行人看到的灯色。
    """
    lights = getattr(world, "traffic_lights", ()) or ()
    if not lights:
        return None
    states = [_lamp_state(light) for light in lights]
    lamp_states = [state for state in states if state is not None]
    if not lamp_states:
        return None
    return all(state == "red" for state in lamp_states)


def _vehicle_blocks(world, road_cells):
    # type: (Any, List[Tuple[int, int]]) -> bool
    for car in getattr(world, "vehicles", ()) or ():
        try:
            minx = float(car["minx"]) - CAR_X_MARGIN
            maxx = float(car["maxx"]) + CAR_X_MARGIN
            minz = float(car["minz"]) - CAR_Z_MARGIN
            maxz = float(car["maxz"]) + CAR_Z_MARGIN
        except (KeyError, TypeError, ValueError):
            continue
        for cell in road_cells:
            try:
                x, z = world.to_world(*cell)
            except (AttributeError, TypeError, ValueError):
                continue
            if minx <= x <= maxx and minz <= z <= maxz:
                return True
    return False


def observe(world, bb):
    # type: (Any, Any) -> None
    """每拍记录「行人红灯从哪一拍亮起」；没有灯/路网、或灯不是红时清空。"""
    if bb is None:
        return
    if not getattr(world, "traffic_lights", None) or road_box(world) is None:
        bb.traffic_red_since = None
        return
    if pedestrian_red(world) is not True:
        bb.traffic_red_since = None
        return
    if getattr(bb, "traffic_red_since", None) is None:
        bb.traffic_red_since = getattr(bb, "tick", 0) or 0


def red_start_ok(world, bb):
    # type: (Any, Any) -> bool
    """现在是不是可以起步的红灯窗口：红灯刚亮，或已经亮了很久（长红灯）。

    `bb` 为 None（离线调用）时只看灯色。
    """
    if pedestrian_red(world) is not True:
        return False
    if bb is None:
        return True
    since = getattr(bb, "traffic_red_since", None)
    if since is None:
        return False
    elapsed = (getattr(bb, "tick", 0) or 0) - since
    return elapsed <= RED_FRESH_TICKS or elapsed >= RED_LONG_TICKS


def safe_to_cross(world, bb, path, cur=None, chef=None):
    # type: (Any, Any, Optional[List[Tuple[int, int]]], Optional[Tuple[int, int]], Any) -> bool
    """路径要过街时是否可以从当前格起步。

    已经在马路条带里（厨师**实际位置**在 road）→ True：半途不因灯变绿停下，
    先把这一趟走完；否则只在红灯刚亮或长红灯时起步（见 `red_start_ok`），
    短红灯的尾巴等下一轮。红灯尾巴起步由 `_crossing_guard` 的「途中变灯去
    中线等」兜底。车 KillPlane AABB 覆盖到路径格时仍然不走（兜底）。

    `chef` 缺省时退回按格心判「是否已在马路上」，只给不认识厨师的旧调用方用。
    """
    road_cells = path_road_cells(world, path)
    if not road_cells:
        return True
    if chef is not None:
        if chef_in_road(world, chef):
            return True
    elif cur is not None and in_road(world, cur):
        return True
    if road_box(world) is None or not getattr(world, "traffic_lights", None):
        return True
    if not red_start_ok(world, bb):
        # 黄/绿、读不到灯色、或短红灯的尾巴（离绿灯太近）都不走。
        return False
    return not _vehicle_blocks(world, road_cells)


def state_text(world, bb):
    # type: (Any, Any) -> str
    """诊断字符串：灯色、车位置。"""
    lights = []
    for light in getattr(world, "traffic_lights", ()) or ():
        state = _lamp_state(light)
        lights.append("%s=%s" % (light.get("n"), state or "?"))
    cars = ["%s@(%s,%s)" % (c.get("n"), c.get("x"), c.get("z"))
            for c in (getattr(world, "vehicles", ()) or ())]
    return "lights=[%s] cars=[%s]" % (
        ",".join(lights), ",".join(cars))

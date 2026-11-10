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
# 红灯起步窗口（拍，20 Hz）：红灯只亮约 1.8 秒，而横穿要 ~2 秒，只有**刚亮**
# 的时候起步才来得及在黄灯前离开车道。红灯已经亮了一阵才走到路沿的，等下一轮。
RED_FRESH_TICKS = 6


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


def safe_to_cross(world, bb, path, cur=None, chef=None):
    # type: (Any, Any, Optional[List[Tuple[int, int]]], Optional[Tuple[int, int]], Any) -> bool
    """路径要过街时是否可以从当前格起步。

    已经在马路条带里（厨师**实际位置**在 road）→ True：半途不因灯变绿停下，
    先把这一趟走完；否则看行人灯：红 = 可走，黄/绿 = 等。红灯必须是刚亮起
    （`RED_FRESH_TICKS` 内），否则横穿到一半就撞上黄灯起步的车。车 KillPlane
    AABB 覆盖到路径格时仍然不走（兜底）。

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
    red = pedestrian_red(world)
    if not red:
        # 红之外（黄/绿）以及读不到灯色（旧 DLL/子灯没找到）都不走。
        return False
    since = getattr(bb, "traffic_red_since", None) if bb is not None else None
    if bb is not None:
        # 红灯已经亮了一阵才到路沿：这一轮来不及，等下一轮。
        if since is None or (getattr(bb, "tick", 0) or 0) - since > RED_FRESH_TICKS:
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

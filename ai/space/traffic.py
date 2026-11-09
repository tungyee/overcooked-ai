# -*- coding: utf-8 -*-
"""4-1 过街：信号灯与车辆避让。

插件在 `state["traffic"]` 里给：
  - `road`：马路条带（主网格 gx -3..6 的格心范围）；
  - `lights`：两盏灯的 `{n,x,z,on,on_car_b}`；
  - `cars`：两辆车的根位置 + 车下 KillPlane 的 AABB。

`LightsOn` / `LightsOn_CarB` 是**车**的信号（不是行人灯）：
  - on=True → 对应车道的车正在跑；
  - 两者都 False 才是安全窗口。
Blue 跑完到 Green 起步之间只有约 1–2 秒；Green 跑完到下一次 Blue
才是长窗口。所以不能“灯一灭就走”，要求双灭持续 `CROSS_SAFE_TICKS`
（3 秒），短窗口自然被排除。

本模块只读 duck-typed 的 World，不依赖 World 类，方便单测。
"""
from typing import Any, Dict, List, Optional, Tuple

CROSS_SAFE_TICKS = 60      # 20 Hz * 3s：双灯都灭持续 3 秒才起步
CAR_X_MARGIN = 0.8         # 车宽方向余量
CAR_Z_MARGIN = 3.0         # 车沿 z 跑，前后各留 3m


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


def in_road(world, cell):
    # type: (Any, Optional[Tuple[int, int]]) -> bool
    return cell is not None and _point_in_road(world, cell)


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


def lights_on(world):
    # type: (Any) -> bool
    for light in getattr(world, "traffic_lights", ()) or ():
        if _truthy(light.get("on")) or _truthy(light.get("on_car_b")):
            return True
    return False


def observe(world, bb):
    # type: (Any, Any) -> None
    """每拍记录“双灯都灭”从哪一拍开始；没有灯/路网时清空。"""
    if bb is None:
        return
    if not getattr(world, "traffic_lights", None) or road_box(world) is None:
        bb.traffic_off_since = None
        return
    if lights_on(world):
        bb.traffic_off_since = None
        return
    if getattr(bb, "traffic_off_since", None) is None:
        bb.traffic_off_since = getattr(bb, "tick", 0) or 0


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


def safe_to_cross(world, bb, path, cur=None):
    # type: (Any, Any, Optional[List[Tuple[int, int]]], Optional[Tuple[int, int]]) -> bool
    """路径要过街时是否可以从当前格起步。

    已经在马路条带里（cur 在 road）→ True：半途不因灯变红停下，先把这一趟
    走完；否则在路沿等双灯都灭满 3 秒、且车 AABB 不覆盖路径。
    """
    road_cells = path_road_cells(world, path)
    if not road_cells:
        return True
    if cur is not None and in_road(world, cur):
        return True
    if road_box(world) is None or not getattr(world, "traffic_lights", None):
        return True
    if bb is None:
        return True
    if lights_on(world):
        return False
    since = getattr(bb, "traffic_off_since", None)
    if since is None:
        return False
    if (getattr(bb, "tick", 0) or 0) - since < CROSS_SAFE_TICKS:
        return False
    return not _vehicle_blocks(world, road_cells)


def state_text(world, bb):
    # type: (Any, Any) -> str
    """诊断字符串：两灯状态、双灭拍数、车位置。"""
    lights = []
    for light in getattr(world, "traffic_lights", ()) or ():
        lights.append("%s=%s/%s" % (light.get("n"),
                                    "1" if _truthy(light.get("on")) else "0",
                                    "1" if _truthy(light.get("on_car_b")) else "0"))
    since = getattr(bb, "traffic_off_since", None) if bb is not None else None
    off = None if since is None else (getattr(bb, "tick", 0) or 0) - since
    cars = ["%s@(%s,%s)" % (c.get("n"), c.get("x"), c.get("z"))
            for c in (getattr(world, "vehicles", ()) or ())]
    return "lights=[%s] off_ticks=%s cars=[%s]" % (
        ",".join(lights), off, ",".join(cars))

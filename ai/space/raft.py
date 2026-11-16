# -*- coding: utf-8 -*-
"""舢板：可站人、按键控制、往返左右两岛的移动平台（5-1 过桥寿司）。

与其它移动平台机制的区别：
  - shuttle.py 渡运台面：人上不去、隔着围栏够（Balloon 篮子），且自动漂；
  - seams.py 门洞：有墙+门洞、沿固定接缝漂移的平台；
  - 本模块的舢板：人站上去、按键驱动、中间一竖列柜台（`Counters` 网格）是
    交接面（放/取料、盘堆）。识别它靠「船底地板比岛面低一截」这个几何特征。

三件事：
  1. raft_box：从 terrain_y 的 Ground(9) 层找「miny 显著低于其它地面」的那块
     船底（5-1 舢板 -2.37 vs 岛 -0.5）；
  2. docked_side：这块地板此刻贴左岛还是右岛（中间移动中返回 None）；
  3. countertop_slots / on_my_side：舢板上的柜台台面，以及船是否停在 P2 这一侧。

只读 World；不写状态。每帧重算（舢板位置随帧变化）。
"""
from typing import Dict, List, Optional, Tuple

from ai.query import predicates as P
from ai.space import occupancy as O

# 船底地板比岛面低多少（m）以上才算「下沉的移动平台」。岛面 Ground miny 约
# -0.5；5-1 舢板船底 -2.37。留 1.0 余量，避开楼梯/坡道等略微下沉的地板。
LOWERED_FLOOR_EPS = 1.0
# 停靠判定：船边贴岛边这么近（m）以内算靠岸。
DOCK_EPS = 0.1


def _ground_with_y(world):
    # type: (object) -> List[Tuple[Tuple[float, float, float, float], float, float]]
    """Ground 层带高度的盒子：[(box, miny, maxy), ...]；旧帧无高度返回空。"""
    terrain_y = getattr(world, "terrain_y", None) or {}
    return terrain_y.get(O.LAYER_GROUND) or []


def _non_main_grid_origins(world):
    # type: (object) -> List[Tuple[float, float]]
    """非主、非移动网格的原点（世界坐标）。舢板柜台 `Counters` 就是这类网格。

    排除 moving 网格：气球篮这类 animator 驱动的移动台面（shuttle.py 管）不该
    被当成舢板；排除主网格：5-1 主网格原点恰在船底板内，会把普通柜台算进来。
    """
    grids = (getattr(world, "raw", None) or {}).get("grids") or []
    gset = getattr(world, "grids", None)
    out = []  # type: List[Tuple[float, float]]
    for g in grids:
        if not isinstance(g, dict) or g.get("main") is True \
                or g.get("moving") is True or not g.get("id"):
            continue
        if gset is None:
            continue
        origin = gset.to_world(str(g["id"]), 0, 0)
        if origin is not None:
            out.append(origin)
    return out


def raft_box(world):
    # type: (object) -> Optional[Tuple[float, float, float, float]]
    """舢板地面 AABB（4 元素平面盒）。无舢板（其它关卡/旧帧无高度）返回 None。

    判据：miny 最低、且比次低低超过 LOWERED_FLOOR_EPS 的那块地面（船底低于
    岛面），并且里面落着一个非主、非移动网格的原点（`Counters` 柜台网格）。
    第二点把「多层地图的底层地板」这类只是高度低、但没有船上柜台的盒子排除掉
    （Wizard_3_1 的 FirstFloor 原点在上层地板，不在下沉盒里）。
    每帧缓存（World 构造一次 + 各处每拍读）。
    """
    cached = getattr(world, "_raft_box", None)
    if hasattr(world, "_raft_box"):
        return cached
    result = _find_raft_box(world)
    world._raft_box = result
    return result


def _find_raft_box(world):
    # type: (object) -> Optional[Tuple[float, float, float, float]]
    ground = _ground_with_y(world)
    if len(ground) < 2:
        return None
    minys = sorted(y0 for (_b, y0, _y1) in ground)
    if minys[0] >= minys[1] - LOWERED_FLOOR_EPS:
        return None
    origins = _non_main_grid_origins(world)
    if not origins:
        return None
    raft_miny = minys[0]
    for box, miny, _maxy in ground:
        if miny != raft_miny:
            continue
        if any(box[0] <= ox <= box[1] and box[2] <= oz <= box[3]
               for ox, oz in origins):
            return box
    return None


def _static_edges(world, raft):
    # type: (object, Tuple[float, float, float, float]) -> Tuple[Optional[float], Optional[float]]
    """(左岛右沿, 右岛左沿)：静态地面盒里贴舢板两侧的那两条边。"""
    boxes = (getattr(world, "terrain", None) or {}).get(O.LAYER_GROUND) or []
    left = max((b[1] for b in boxes if b != raft and b[1] <= raft[0]), default=None)
    right = min((b[0] for b in boxes if b != raft and b[0] >= raft[1]), default=None)
    return left, right


def docked_side(world):
    # type: (object) -> Optional[str]
    """舢板此刻停靠的岛：'left' / 'right'；在中间移动中或认不出返回 None。"""
    raft = raft_box(world)
    if raft is None:
        return None
    left, right = _static_edges(world, raft)
    if right is not None and raft[1] >= right - DOCK_EPS:
        return "right"
    if left is not None and raft[0] <= left + DOCK_EPS:
        return "left"
    return None


def is_raft_counter(world, st):
    # type: (object, Optional[Dict]) -> bool
    """站点是不是舢板上的柜台（柜台世界坐标落在船底板内）。

    用站点的世界 x/z 判，而不是网格原点：主网格（GridManager）原点恰好也在
    船底板内（5-1 原点 10.8/6.0），按网格原点会把左右两岛的普通柜台也算进来。
    """
    raft = raft_box(world)
    if raft is None or not isinstance(st, dict) or st.get("t") != "counter":
        return False
    x, z = st.get("x"), st.get("z")
    if x is None or z is None:
        return False
    return (raft[0] <= float(x) <= raft[1]
            and raft[2] <= float(z) <= raft[3])


def countertop_slots(world):
    # type: (object) -> List[Dict]
    """舢板上的柜台台面（放/取料的交接面）。"""
    return [st for st in (getattr(world, "stations", ()) or ())
            if is_raft_counter(world, st)]


def on_my_side(world, name=P.P2_NAME):
    # type: (object, str) -> bool
    """舢板是否停在 name（默认 P2）这一侧（= P2 能走上船）。

    判据用船边而非船中心：P2 站在船上任意位置（含开局船停右岸、P2 在船左半边）
    都算「这一侧」——船中心会把船左半边的 P2 判成「在对岸」，开局就上不了船。
    只在有可达场不可用时作兜底；channels 层优先用 reachability.can_interact 判。
    """
    side = docked_side(world)
    raft = raft_box(world)
    if side is None or raft is None:
        return False
    chef = P.chef_by_name(getattr(world, "chefs", None) or (), name)
    x = (chef or {}).get("x")
    if x is None:
        return False
    if side == "right":
        return float(x) >= raft[0] - DOCK_EPS   # 在船上或右岛
    return float(x) <= raft[1] + DOCK_EPS       # 在船上或左岛


def board_cell(world, name=P.P2_NAME):
    # type: (object, str) -> Optional[Tuple[int, int]]
    """舢板上一块可走的落脚格（主网格坐标），取离厨师最近的那块。

    船停在本侧时，这块地板与厨师所在岛连通，`GoToCell` 走上去就能触发开船。
    中间柜台（Counters）不可走，不会选中。
    """
    raft = raft_box(world)
    if raft is None:
        return None
    chef = P.chef_by_name(getattr(world, "chefs", None) or (), name)
    cx = (chef or {}).get("x")
    cz = (chef or {}).get("z")
    try:
        g0 = world.to_grid(raft[0], raft[2])
        g1 = world.to_grid(raft[1], raft[3])
    except Exception:  # noqa: BLE001 合成/旧帧 to_grid 可能没有
        return None
    best, best_d = None, None
    for gx in range(min(g0[0], g1[0]), max(g0[0], g1[0]) + 1):
        for gz in range(min(g0[1], g1[1]), max(g0[1], g1[1]) + 1):
            x, z = world.to_world(gx, gz)
            if not (raft[0] <= x <= raft[1] and raft[2] <= z <= raft[3]):
                continue
            if not world.walkable(gx, gz):
                continue
            if cx is None or cz is None:
                return (gx, gz)
            d = (x - cx) ** 2 + (z - cz) ** 2
            if best_d is None or d < best_d:
                best, best_d = (gx, gz), d
    return best

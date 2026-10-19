"""站点检索：按类型/食材/距离找站点，全部纯函数。

从 oc/ai/task.py 抽取检索部分。入参是 stations 列表而非 World，
这样单测不必造完整快照（find_plate_stack 例外——它要同时看
world.plates 与 world.level）。

与旧实现的差异：一律用 .get() 读 x/z 并跳过缺失项。旧代码 s["x"]
直接下标，遇到无位置站点抛 KeyError——实测快照里确有站点缺 gx/gz，
x/z 同样不保证存在。

不含 find_plate_counter / PLATE_COUNTERS：spec §9 记它归 Plan 3，
替代品是 Planner 的候选生成 + 认领，不是一个 find 函数。
"""
from typing import Dict, List, Optional, Set, Tuple

from ai.space.occupancy import _POS_ROUND
from ai.state.world import World

# 出餐口的站点类型名
PLATE_STATION = "plate_station"


def relay_cells_for(world):
    # type: (World) -> List[Tuple[int, int]]
    """当前关卡的中转台 counter 格。

    §7/Task 15：原按关卡名键的 RELAY_CELLS 硬编码表已删除——中转台没有
    数据驱动的替代来源（不同于 belts/plate 已有协议字段）。PlaceToRelay
    这条路径本来就已经停用（1-3 的实测数据早被注释掉），这里改成直接
    返回空集，行为与删表前完全一致（RELAY_CELLS 一直是空字典）。
    需要恢复中转台功能时，这里需要插件输出中转台站点或重新引入数据表。
    """
    return []


def _pos(st):
    # type: (Dict) -> Optional[Tuple[float, float]]
    """读站点世界坐标，缺任一轴返回 None。"""
    x, z = st.get("x"), st.get("z")
    if x is None or z is None:
        return None
    return (x, z)


def _dist2(st, x, z):
    # type: (Dict, float, float) -> Optional[float]
    """到 (x, z) 的平方距离。无需开方——只用于比较大小。"""
    pos = _pos(st)
    if pos is None:
        return None
    return (pos[0] - x) ** 2 + (pos[1] - z) ** 2


def nearest_station(stations, t, x, z):
    # type: (Optional[List[Dict]], Optional[str], float, float) -> Optional[Dict]
    """找离 (x, z) 最近的 t 类站点。t=None 表示不限类型。"""
    best, best_d = None, None
    for st in stations or []:
        if not isinstance(st, dict):
            continue
        if t is not None and st.get("t") != t:
            continue
        d = _dist2(st, x, z)
        if d is None:
            continue
        if best_d is None or d < best_d:
            best, best_d = st, d
    return best


def find_station(stations, t, near=None):
    # type: (Optional[List[Dict]], str, Optional[Tuple[float, float]]) -> Optional[Dict]
    """找某类型站点；near=(x, z) 时取最近，否则取第一个。"""
    if near is not None:
        return nearest_station(stations, t, near[0], near[1])
    for st in stations or []:
        if isinstance(st, dict) and st.get("t") == t:
            return st
    return None


def find_crate(stations, ingredient):
    # type: (Optional[List[Dict]], Optional[str]) -> Optional[Dict]
    """找出产指定食材的箱子（t == "crate" 且 ing 匹配）。

    不分大小写：配方名与快照名的大小写不保证一致，匹配不上会让
    "去拿米"整条链静默失败——没有报错，只是永远不动。
    """
    if not ingredient:
        return None
    want = str(ingredient).lower()
    for st in stations or []:
        if not isinstance(st, dict) or st.get("t") != "crate":
            continue
        if str(st.get("ing") or "").lower() == want:
            return st
    return None


def _is_plate_source(st):
    # type: (Dict) -> bool
    """盘堆出生点，或晾干架（洗完的盘在这里可取）。"""
    t = st.get("t")
    if t == "plate_stack":
        return True
    return t == "plate_return" and "drying" in str(st.get("n") or "").lower()


def find_plate_stack(world, near=None):
    # type: (World, Optional[Tuple[float, float]]) -> Optional[Dict]
    """找可拿干净盘的位置，多个取离 near 最近。

    二级退化：
      1. stations[] 里的盘堆与晾干架，**过滤出当前真有干净盘的**。
         盘被端走后出生点仍在快照里但已空；盲按距离选会选中空位，
         厨师走过去拿不到盘、整条做菜链静默卡死。
      2. 一个都没有 → 退到出餐口。都没有才返回 None。

    §7/Task 15：原第 2 级的 PLATE_STACKS 关卡硬编码坐标兜底已删除——
    泛化需插件输出盘堆站点，本轮不补。

    干净盘判据：plates 里 count == 0（count 是盘上食材数，0 = 空盘）。
    """
    seen = set()  # type: Set[Tuple[float, float]]
    stacks = []   # type: List[Dict]
    for st in world.stations or []:
        if not isinstance(st, dict) or not _is_plate_source(st):
            continue
        pos = _pos(st)
        if pos is None:
            continue
        key = (round(pos[0], _POS_ROUND), round(pos[1], _POS_ROUND))
        if key in seen:
            continue
        seen.add(key)
        stacks.append(st)

    clean_pos = set()
    for p in world.plates or []:
        if not isinstance(p, dict) or (p.get("count") or 0) != 0:
            continue
        pos = _pos(p)
        if pos is not None:
            clean_pos.add((round(pos[0], _POS_ROUND),
                           round(pos[1], _POS_ROUND)))
    if clean_pos:
        stocked = []  # type: List[Dict]
        for s in stacks:
            pos = _pos(s)
            if pos is None:
                continue
            if (round(pos[0], _POS_ROUND),
                    round(pos[1], _POS_ROUND)) in clean_pos:
                stocked.append(s)
        if stocked:
            stacks = stocked

    if not stacks:
        return find_station(world.stations, PLATE_STATION, near)
    if near is not None:
        best, best_d = None, None
        for st in stacks:
            d = _dist2(st, near[0], near[1])
            if d is None:
                continue
            if best_d is None or d < best_d:
                best, best_d = st, d
        return best
    return stacks[0]


def sink_place_cells(station, stations, to_grid):
    # type: (Optional[Dict], Optional[List[Dict]], Any) -> Set[Tuple[int, int]]
    """洗碗池放脏盘的占地格：洗碗池自身 + 相邻晾干架（合并成 1×2）。

    放脏盘（placement）时游戏接受站在洗碗池或晾干架任一侧都能放
    （placement 锚点仍报洗碗池单格）；但洗碗（wash）只能面向洗碗池。
    所以放脏盘的 approach 占地要把两者合并，否则只按洗碗池单格四邻会漏掉
    晾干架一侧的站位（Sushi_1_3：DryingPart 在洗碗池左侧一格）。

    **占地格必须用 x/z 世界坐标投影（to_grid）**，不能信站点自带的
    gx/gz 字段：实测 WashingPart 报 gz=2，而其世界坐标 z=2.4 投影到主网格
    是 gz=-4（局部网格坐标与主网格不一致）。直接信 gz 会把洗碗池算到
    (4,2)，approach 目标漂到 (4,1) 等远离实际位置的地方，脏盘永远放不进。
    """
    if not isinstance(station, dict):
        return set()
    x, z = station.get("x"), station.get("z")
    if x is None or z is None:
        return set()
    cell = to_grid(x, z)
    if cell is None:
        return set()
    cells = {(int(cell[0]), int(cell[1]))}  # type: Set[Tuple[int, int]]
    for s in stations or []:
        if not isinstance(s, dict) or s.get("t") != "plate_return":
            continue
        if "drying" not in str(s.get("n") or "").lower():
            continue
        sx, sz = s.get("x"), s.get("z")
        if sx is None or sz is None:
            continue
        sc = to_grid(sx, sz)
        if sc is None:
            continue
        if abs(int(sc[0]) - int(cell[0])) + abs(int(sc[1]) - int(cell[1])) == 1:
            cells.add((int(sc[0]), int(sc[1])))
    return cells

"""落点约束：手里的东西该放哪（spec §6.1 / §7.4）。

从 oc/ai/agent.py:483 的 _find_drop_cell 抽取。spec §6.1 原话：那段
筛选逻辑"是功能性的、不是优化，必须保留"——落点须可落脚、非传送带，
且不贴灶台（会继续加热）、不贴出餐口（会被误上菜）、不贴拼装台（会挡盘）。

一处升级：spec §7.4 要求"放**就近的合法空台面**而非地上（地上虽也能捡，
但可能挡路或在传送带上被冲走）"。旧实现只找地板格，这里先找空 counter
站点（find_drop_station），找不到才退到地板（find_drop_cell）。

不含 find_plate_counter / PLATE_COUNTERS：spec §9 明确归 Plan 3（替代品
是 Planner 的多拼装位候选生成 + 认领，不是一个 find 函数）。所以"拼装台"
禁区只能从**已就位的盘**位置反推——盘在哪格，那格就是在用的拼装位。
"""
from typing import Dict, Optional, Set, Tuple

from ai import navigation
from ai import predicates as P
from ai.occupancy import _POS_ROUND

# 这两类站点的四邻不可作落点：灶台会继续加热，出餐口会把东西当成上菜
_HOT_STATION_TYPES = ("cooking", "plate_station")

_NEIGHBORS = ((-1, 0), (1, 0), (0, -1), (0, 1))


def _grid_of(st):
    # type: (Dict) -> Optional[Tuple[int, int]]
    """站点网格坐标。缺失返回 None——实测 71 个站点里 10 个没有 gx/gz。"""
    gx, gz = st.get("gx"), st.get("gz")
    if gx is None or gz is None:
        return None
    return (int(gx), int(gz))


def forbidden_adjacent_cells(stations):
    # type: (Optional[list]) -> Set[Tuple[int, int]]
    """灶台 / 出餐口四邻的禁区格。

    只收这两类：普通台面若也算禁区，整张图就落不下任何东西了。
    """
    bad = set()  # type: Set[Tuple[int, int]]
    for st in stations or ():
        if not isinstance(st, dict) or st.get("t") not in _HOT_STATION_TYPES:
            continue
        cell = _grid_of(st)
        if cell is None:
            continue
        for dx, dz in _NEIGHBORS:
            bad.add((cell[0] + dx, cell[1] + dz))
    return bad


def _plate_cells(world):
    # type: (object) -> Set[Tuple[float, float]]
    """盘所在位置（世界坐标，量化后）。盘在哪就是在用的拼装位。"""
    out = set()  # type: Set[Tuple[float, float]]
    for p in getattr(world, "plates", None) or ():
        if not isinstance(p, dict):
            continue
        x, z = p.get("x"), p.get("z")
        if x is None or z is None:
            continue
        out.add((round(x, _POS_ROUND), round(z, _POS_ROUND)))
    return out


def _plate_grid_cells(world):
    # type: (object) -> Set[Tuple[int, int]]
    """盘所在的网格格。走 `World.to_grid`，**不手算**。

    ⚠️ 旧实现是 `int(round(plate_x / 1.2))`，漏了 mapping 偏移：本关的映射
    是 `(round(x/1.2) - 12, round(z/1.2) + 5)`，于是真实格 `(-1, 0)` 被算成
    `(11, -5)`——而全关 71 个站点的 gx 只到 7，禁的全是地图外的格子。结果
    这条"不贴拼装台"的规则**一直是空转**，真正拦住"往盘上放"的只剩
    `_occupied_station_cells`（那条按世界坐标比，是对的），而它只保护盘自己
    那一格、四邻没人管——实机地板兜底落点算出来是 `(0, 0)`，正好落在两列
    盘子正中间。
    """
    to_grid = getattr(world, "to_grid", None)
    out = set()  # type: Set[Tuple[int, int]]
    if to_grid is None:
        return out
    for p in getattr(world, "plates", None) or ():
        if not isinstance(p, dict):
            continue
        x, z = p.get("x"), p.get("z")
        if x is None or z is None:
            continue
        cell = to_grid(x, z)
        if cell is None:
            continue
        out.add((int(round(cell[0])), int(round(cell[1]))))
    return out


def _assembly_forbidden(world):
    # type: (object) -> Set[Tuple[int, int]]
    """已就位的盘所在格 + 其四邻（会挡盘）。"""
    bad = set()  # type: Set[Tuple[int, int]]
    plate_grid_cells = _plate_grid_cells(world)
    if not plate_grid_cells:
        return bad

    for cell in plate_grid_cells:
        bad.add(cell)
        for dx, dz in _NEIGHBORS:
            bad.add((cell[0] + dx, cell[1] + dz))
    return bad


def _occupied_station_cells(world):
    # type: (object) -> Set[Tuple[int, int]]
    """格子上**已经有东西**的站点格——往上放会顶掉/放不下。

    两个来源缺一不可：

      - `plates[]`：盘坐在台面上时，插件**不会**把台面那条的 `item` 填上
        （实测 27725 帧里 `stations[].item` 恒为 None），所以盘只能从
        plates 找到；
      - `items[]`：锅、料、灭火器这些散落物也一样占着那一格。旧实现只看
        盘，于是"已经躺着一口锅"的台面会被当成空台面选走 → 游戏忽略这次
        放置 → 后置校验失败 → 每 3-4 拍一次 FAILURE（实机：熟锅放不下去，
        树反复失败）。
    """
    cells = set()  # type: Set[Tuple[int, int]]
    to_grid = getattr(world, "to_grid", None)
    if to_grid is None:
        return cells
    for arr in (getattr(world, "plates", None),
                getattr(world, "items", None),
                getattr(world, "workstations", None)):
        for it in arr or ():
            if not isinstance(it, dict):
                continue
            x, z = it.get("x"), it.get("z")
            if x is None or z is None:
                continue
            cell = to_grid(x, z)
            if cell is not None:
                cells.add((int(round(cell[0])), int(round(cell[1]))))
    return cells


def find_drop_station(world, field, extra_forbidden=None):
    # type: (object, Dict, Optional[Set[Tuple[int, int]]]) -> Optional[Dict]
    """就近的合法空台面站点。按距离场代价取最近，不可达的跳过。

    field 是从厨师位置算出的 distance_field，所以"最近"按真实路径代价
    算而不是直线距离——隔着墙的台面不该被选中。
    """
    forbidden = set(extra_forbidden or ())
    forbidden |= forbidden_adjacent_cells(getattr(world, "stations", None))
    forbidden |= _assembly_forbidden(world)
    occupied = _occupied_station_cells(world)
    belt_cells = getattr(world, "belt_cells", None) or set()

    best, best_cost = None, None
    for st in getattr(world, "stations", None) or ():
        if not isinstance(st, dict) or st.get("t") != "counter":
            continue
        cell = _grid_of(st)
        if cell is None or cell in forbidden or cell in occupied:
            continue
        cells = navigation.interact_cells(
            cell[0], cell[1], world.walkable, belt_cells)
        got = navigation.nearest_cost(field, cells)
        if got is None:
            continue
        if best_cost is None or got[1] < best_cost:
            best, best_cost = st, got[1]
    return best


def find_empty_cooker(world, field):
    # type: (object, Dict) -> Optional[Dict]
    """就近的**空灶台**（灶上没锅）。手持空锅归位用（spec §6.1 第 5 号）。

    "灶上没锅"按插件约定判：`cooked is None` 表示灶上没锅，`false/true`
    表示有锅（predicates.cooker_has_pot）。状态读不到的灶台一律跳过——
    宁可不用也不能把"读不到"当成"是空的"。

    与 find_drop_station 的差别：这里**不禁"贴灶台"**。那套禁区是给"东西
    放哪儿"用的（贴灶台会继续加热），而这里目标本来就是灶台、落点就在灶
    台格上。
    """
    belt_cells = getattr(world, "belt_cells", None) or set()
    cookers = getattr(world, "cookers", None) or []

    best, best_cost = None, None
    for st in getattr(world, "stations", None) or ():
        if not isinstance(st, dict) or st.get("t") != "cooking":
            continue
        c = P.cooker_for(cookers, str(st.get("n") or ""))
        if c is None or P.cooker_has_pot(c):
            continue                       # 状态读不到 / 灶上已经有锅
        cell = _grid_of(st)
        if cell is None:
            continue
        cells = navigation.interact_cells(
            cell[0], cell[1], world.walkable, belt_cells)
        got = navigation.nearest_cost(field, cells)
        if got is None:
            continue
        if best_cost is None or got[1] < best_cost:
            best, best_cost = st, got[1]
    return best


def find_drop_cell(world, field, extra_forbidden=None):
    # type: (object, Dict, Optional[Set[Tuple[int, int]]]) -> Optional[Tuple[int, int]]
    """地板兜底落点：可走、非传送带、不在禁区，按距离场取最近。

    返回 None 表示全图无合法落点。调用方据此转 FAILURE 交还 Planner，
    不在这里无限找（spec §7.6 纪律 1）。
    """
    forbidden = set(extra_forbidden or ())
    forbidden |= forbidden_adjacent_cells(getattr(world, "stations", None))
    forbidden |= _assembly_forbidden(world)
    belt_dir = getattr(world, "belt_dir", None) or {}
    belt_cells = getattr(world, "belt_cells", None) or set()

    best, best_cost = None, None
    for cell, cost in field.items():
        if cell in forbidden or cell in belt_cells or cell in belt_dir:
            continue
        if best_cost is None or cost < best_cost:
            best, best_cost = cell, cost
    return best


def occupied_cells(world):
    # type: (object) -> Set[Tuple[int, int]]
    """格子上已有东西的站点格（公开封装 _occupied_station_cells，供 task 的 feasible 判定）。"""
    return _occupied_station_cells(world)


def find_drop_at(world, field, target_cell):
    # type: (object, Dict, Optional[Tuple[int, int]]) -> Optional[Dict]
    """指定 counter 格可放置则返回合成站点 dict，否则 None。

    用于中转台等指定落点：cell 必须空（未被盘/散落物/板占用）、非禁区、
    可达（field 里有路径代价）。找不到 → None，调用方（PlaceToRelay 的
    EnsureHandsFree(strict=True)）据此 FAILURE 交还 Planner，不 fallback
    到别处（中转台满时不能乱放）。
    """
    if target_cell is None:
        return None
    cell = (int(target_cell[0]), int(target_cell[1]))
    forbidden = forbidden_adjacent_cells(getattr(world, "stations", None))
    forbidden |= _assembly_forbidden(world)
    if cell in forbidden or cell in _occupied_station_cells(world):
        return None
    belt_cells = getattr(world, "belt_cells", None) or set()
    cells = navigation.interact_cells(
        cell[0], cell[1], world.walkable, belt_cells)
    got = navigation.nearest_cost(field, cells)
    if got is None:
        return None
    x, z = world.to_world(cell[0], cell[1])
    return {"t": "counter", "n": "", "gx": cell[0], "gz": cell[1],
            "x": x, "z": z}

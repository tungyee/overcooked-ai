"""落点约束：手里的东西该放哪（spec §6.1 / §7.4）。

从 oc/ai/agent.py:483 的 _find_drop_cell 抽取。spec §6.1 原话：那段
筛选逻辑"是功能性的、不是优化，必须保留"——落点须可落脚、非传送带，
不贴出餐口（会被误上菜），且不放在已有东西（盘/锅/料）的那一格上。

「不贴灶台」「不贴拼装台」已去掉（2026-10-30）：只有放**到灶台上**才会接着
加热，旁边的台面不会，落点候选本来就只取 counter / 地板、不含灶台本身；
放在盘旁边也不挡往盘里装菜，盘自己那一格由「已占用」挡住。两条四邻禁区
让灶旁、盘旁台面全不可用，Balloon_2_3 救锅时右篮近处台面全被禁，锅被搬
到另一只篮子上（trace-20261030-214824 t=1164）。

一处升级：spec §7.4 要求"放**就近的合法空台面**而非地上（地上虽也能捡，
但可能挡路或在传送带上被冲走）"。旧实现只找地板格，这里先找空 counter
站点（find_drop_station），找不到才退到地板（find_drop_cell）。

不含 find_plate_counter / PLATE_COUNTERS：spec §9 明确归 Plan 3（替代品
是 Planner 的多拼装位候选生成 + 认领，不是一个 find 函数）。
"""
from typing import Dict, Optional, Set, Tuple

from ai.space import navigation
from ai.query import predicates as P

# 这类站点的四邻不可作落点：出餐口会把东西当成上菜。灶台不在内——只有放
# 到灶台上才会继续加热，灶旁台面可以放（见模块说明）。
_FORBIDDEN_NEIGHBOR_TYPES = ("plate_station",)

_NEIGHBORS = ((-1, 0), (1, 0), (0, -1), (0, 1))


def _grid_of(st):
    # type: (Dict) -> Optional[Tuple[int, int]]
    """站点网格坐标。缺失返回 None——实测 71 个站点里 10 个没有 gx/gz。"""
    gx, gz = st.get("gx"), st.get("gz")
    if gx is None or gz is None:
        return None
    return (int(gx), int(gz))


def _station_cell(world, st):
    # type: (object, Dict) -> Optional[Tuple[int, int]]
    """站点在**主网格**坐标系里的格：世界坐标 x/z 投影，不用本地 gx/gz。

    多网格下站点的 gx/gz 是它自己网格的局部坐标（篮子台面报篮子局部坐标），
    而 field / walkable / forbidden / occupied 全是主网格坐标系。直接用 gx/gz
    会把篮子台面的局部坐标当主网格坐标、距离算错——trace-20261018-013849
    t=607：篮子台面局部 (-2,-2) 被当成主网格 (-2,-2)，离 P2 (0,-2) 只 1 格，
    被误选成「最近台面」，P2 端着盘冲向篮子。

    主网格站点 x/z 投影与 gx/gz 一致，行为不变；缺 x/z 时退回 gx/gz。
    """
    x, z = st.get("x"), st.get("z")
    if x is not None and z is not None:
        to_grid = getattr(world, "to_grid", None)
        if to_grid is not None:
            return to_grid(x, z)
    return _grid_of(st)


# ---------- 拼装位身份（主网格台面 = 坐标；移动网格台面 = (grid, 站名)） ----------


def _moving_grid_ids(world):
    # type: (object) -> Set[str]
    raw = getattr(world, "raw", None) or {}
    return {str(g.get("id")) for g in (raw.get("grids") or ())
            if isinstance(g, dict) and g.get("moving") is True and g.get("id")}


def counter_key(world, st):
    # type: (object, Dict) -> Optional[object]
    """台面站点的拼装位资源 id。

    主网格台面沿用「坐标即身份」(gx, gz)（spec §4.4）。移动网格（气球篮、
    木筏）上的台面用 (grid, 站名)，同灶/板：主网格投影随篮子漂，task 存下
    的格子过几秒就指向空地（trace-20261030-224658 t=2991→3127：拼装格
    (6,0) 上的台面 (13) 被篮子带到 (6,-2)，P2 站在空地上反复放盘失败）。
    """
    grid = st.get("grid")
    if grid and st.get("n") and str(grid) in _moving_grid_ids(world):
        return (str(grid), str(st["n"]))
    return _station_cell(world, st)


def is_station_key(key):
    # type: (object) -> bool
    """(grid, 站名) 形式的拼装位 id（区别于 (gx, gz) 整数坐标）。"""
    return (isinstance(key, tuple) and len(key) == 2
            and isinstance(key[0], str))


def counter_key_at(world, cell):
    # type: (object, Optional[Tuple[int, int]]) -> Optional[object]
    """当前帧投影到 cell 的台面的拼装位 id；没有台面就原样返回 cell。"""
    if cell is None:
        return None
    for st in world.stations:
        if st.get("t") != "counter":
            continue
        sc = _station_cell(world, st)
        if sc is not None and tuple(sc) == tuple(cell):
            return counter_key(world, st)
    return tuple(cell)


def station_by_key(world, key):
    # type: (object, object) -> Optional[Dict]
    """按 (grid, 站名) 取站点；取不到返回 None。"""
    grid, name = key
    for st in world.stations:
        if str(st.get("n") or "") == name and str(st.get("grid") or "") == grid:
            return st
    return None


def counter_cell_now(world, key):
    # type: (object, object) -> Optional[Tuple[int, int]]
    """拼装位此刻在主网格上的格：移动台面每帧按站点 x/z 重投影。"""
    if key is None:
        return None
    if is_station_key(key):
        st = station_by_key(world, key)
        return _station_cell(world, st) if st is not None else None
    return tuple(key)


def forbidden_adjacent_cells(stations):
    # type: (Optional[list]) -> Set[Tuple[int, int]]
    """出餐口四邻的禁区格。

    普通台面、灶台都不算：普通台面若也算禁区，整张图就落不下任何东西了；
    灶旁台面不会加热，放在灶台**上**才会，而落点候选不含灶台。
    """
    bad = set()  # type: Set[Tuple[int, int]]
    for st in stations or ():
        if not isinstance(st, dict) or st.get("t") not in _FORBIDDEN_NEIGHBOR_TYPES:
            continue
        cell = _grid_of(st)
        if cell is None:
            continue
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
    occupied = _occupied_station_cells(world)
    belt_cells = getattr(world, "belt_cells", None) or set()

    best, best_cost = None, None
    for st in getattr(world, "stations", None) or ():
        if not isinstance(st, dict) or st.get("t") != "counter":
            continue
        cell = _station_cell(world, st)
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

    与 find_drop_station 的差别：这里目标本来就是灶台、落点就在灶台格上；
    find_drop_station 只取 counter，熟料锅不会被放回灶台接着烧。
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
    forbidden |= _occupied_station_cells(world)   # 盘/锅/料自己那一格
    belt_dir = getattr(world, "belt_dir", None) or {}
    belt_cells = getattr(world, "belt_cells", None) or set()

    best, best_cost = None, None
    for cell, cost in field.items():
        if cell in forbidden or cell in belt_cells or cell in belt_dir:
            continue
        if best_cost is None or cost < best_cost:
            best, best_cost = cell, cost
    return best


def floor_drop_cells(world, field, extra_forbidden=None):
    # type: (object, Dict, Optional[Set[Tuple[int, int]]]) -> Set[Tuple[int, int]]
    """能把手里东西放上去的地板格（「面朝的那格」，东西落在厨师面前）。

    反编译 PlaceHeldItem_Client/Server：拿着东西按键，面前有 placement 句柄
    就往句柄上放，没有才落地。所以落地格要在可达场里、不是站点、上面没东西、
    不在传送带 / 出餐口禁区——面朝它时扇形扫描才大概率扫不到句柄
    （最终由插件 tap kind=floor 按此刻句柄确认）。
    """
    forbidden = set(extra_forbidden or ())
    forbidden |= forbidden_adjacent_cells(getattr(world, "stations", None))
    forbidden |= _occupied_station_cells(world)
    belt_dir = getattr(world, "belt_dir", None) or {}
    belt_cells = getattr(world, "belt_cells", None) or set()
    stations = set()  # type: Set[Tuple[int, int]]
    for st in getattr(world, "stations", None) or ():
        if isinstance(st, dict):
            cell = _station_cell(world, st)
            if cell is not None:
                stations.add(cell)
    return {c for c in field
            if c not in forbidden and c not in stations
            and c not in belt_cells and c not in belt_dir}


def find_floor_drop(world, field, extra_forbidden=None):
    # type: (object, Dict, Optional[Set[Tuple[int, int]]]) -> Optional[Tuple[int, int]]
    """地板落点（面朝的那格）：floor_drop_cells 里、有可达邻格可站的，按站位
    代价取最近。None = 没有合法落点，调用方 FAILURE 交还 Planner。"""
    cells = floor_drop_cells(world, field, extra_forbidden)
    best, best_cost = None, None
    for cell in cells:
        for dx, dz in _NEIGHBORS:
            stand = (cell[0] + dx, cell[1] + dz)
            cost = field.get(stand)
            if cost is None:
                continue
            if best_cost is None or cost < best_cost:
                best, best_cost = cell, cost
    return best


def occupied_cells(world):
    # type: (object) -> Set[Tuple[int, int]]
    """格子上已有东西的站点格（公开封装 _occupied_station_cells，供 task 的 feasible 判定）。"""
    return _occupied_station_cells(world)


def counter_occupied(world, key, ignore_plate=None):
    # type: (object, object, object) -> bool
    """拼装位上已经有别的东西（盘/锅/料/板），往上摆空盘不成立。

    摆盘到放着锅的台面上，游戏会把这次放置变成「锅里的料倒进/倒出盘」：
    实机 Balloon_2_3（trace-20261031-002224 t=2752~2837）P2 端着装了虾的盘
    对着放熟虾锅的台面摆盘，虾从盘倒回锅、再被 TakeCookedToPlate 盛回盘，
    0.4 s 一轮来回 6 次。`ignore_plate` 是本 task 自己要摆的盘（已摆好时
    它自己就占着那一格，不能算别人）。手持物不占格——
    World.plates/items 本就只含非手持。items 里的盘与 plates 重复，跳过以
    免按名字认不出 `ignore_plate`。
    """
    cell = counter_cell_now(world, key)
    to_grid = getattr(world, "to_grid", None)
    if cell is None or to_grid is None:
        return False
    cell = (int(cell[0]), int(cell[1]))
    for arr, skip in ((getattr(world, "plates", None), "plate"),
                      (getattr(world, "items", None), "item"),
                      (getattr(world, "workstations", None), None)):
        for it in arr or ():
            if not isinstance(it, dict) or it.get("held_by"):
                continue
            if skip == "plate" and ignore_plate is not None \
                    and str(it.get("n")) == str(ignore_plate):
                continue
            if skip == "item" and P.PLATE_KEYWORD in str(it.get("n") or "").lower():
                continue
            x, z = it.get("x"), it.get("z")
            if x is None or z is None:
                continue
            c = to_grid(x, z)
            if c is not None and (int(round(c[0])), int(round(c[1]))) == cell:
                return True
    return False

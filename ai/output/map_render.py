# -*- coding: utf-8 -*-
"""地图渲染：把一帧 `World` 画成 ASCII（对齐 docs/map 手绘格式），支持多网格。

只读 `World`，不写任何状态。两个入口：

- `render_map(world)`：**每个网格各画一张**局部 ASCII 图（主网格在前，木筏/
  篮子等移动网格在后），再加图例；给人看 / 调试用；
- `render_layout(world)`：紧凑「站点布局」文本（按网格分组），后续可喂给
  LLM 做分工。

多网格口径（spec §4.1 / §5.1）：每个网格有自己的原点与格宽，站点局部
`gx/gz` 只在它自己的网格里成立。所以不做「把移动网格投影到主网格」——
那会画到主网格地形外的一排 `#` 上；而是每个网格按自己的局部坐标各画一张。
主网格用 `world.walkable`（含占用/传送带）；移动网格用 terrain 现算一份
walkable（不解析移动网格的运行时占用，只画台面/墙/地板轮廓）。
"""
from typing import Dict, List, Optional, Set, Tuple

from ai.query.predicates import P1_NAME, P2_NAME
from ai.space.geometry import GridMapping

# 站点类型 → 地图符号（与 docs/map 图例一致）。
_STATION_SYMBOLS = {
    "chopping": "H",       # 砧板
    "cooking": "C",        # 灶台
    "washing": "W",        # 洗碗池
    "crate": "K",          # 料箱
    "bin": "B",            # 垃圾桶
    "plate_station": "O",  # 出餐口
    "plate_return": "R",   # 盘回收
    "drying": "D",         # 晾干架
    "counter": "X",        # 柜台
    "conveyor": "=",       # 传送带
    "switch": "S",         # 方向开关
    "mixing": "M",         # 搅拌站
}
# 地板/墙/动态盘堆等功能桩，不作为障碍符号画出来（墙由 walkable=# 表达）。
_SKIP_STATION_TYPES = ("floor", "wall", "plate_stack")

_LEGEND = {
    "H": "砧板", "C": "灶台", "W": "洗碗池", "K": "料箱", "B": "垃圾桶",
    "O": "出餐口", "R": "盘回收", "D": "晾干架", "X": "柜台",
    "=": "传送带", "S": "开关", "M": "搅拌站", ".": "地板", "#": "不可走",
    "1": "玩家", "2": "你",
}

_UNKNOWN_SYMBOLS = {}  # type: Dict[str, str]


def _fallback_symbol(t):
    # type: (str) -> str
    if t in _UNKNOWN_SYMBOLS:
        return _UNKNOWN_SYMBOLS[t]
    sym = (t[:1].upper() if t else "?")
    _UNKNOWN_SYMBOLS[t] = sym
    return sym


def _majority_grid(world):
    # type: (object) -> Optional[str]
    try:
        return world._majority_grid()
    except Exception:                     # noqa: BLE001
        return None


def _grids_ordered(world):
    # type: (object) -> List[str]
    """主网格在前，其余网格按名排序。"""
    main = _majority_grid(world)
    ids = []
    try:
        ids = list(getattr(getattr(world, "grids", None), "ids", lambda: [])())
    except Exception:                     # noqa: BLE001
        ids = []
    others = sorted(g for g in ids if g != main)
    if main:
        return [main] + others
    return others or [g for g in ids]


def _station_grid(world, st, main):
    # type: (object, Dict, Optional[str]) -> str
    g = str(st.get("grid") or "").strip()
    if not g:
        return main or "GridManager"
    return g


def _bucket_stations(world):
    # type: (object) -> Tuple[Optional[str], Dict[str, List[Dict]]]
    main = _majority_grid(world)
    buckets = {}  # type: Dict[str, List[Dict]]
    for st in getattr(world, "stations", None) or ():
        if not isinstance(st, dict):
            continue
        buckets.setdefault(_station_grid(world, st, main), []).append(st)
    return main, buckets


def _grid_mapping(world, grid_id):
    # type: (object, str) -> Optional[GridMapping]
    try:
        cell = world.grids.cell_size(grid_id)
        origin = world.grids.to_world(grid_id, 0, 0)
    except Exception:                     # noqa: BLE001
        return None
    if origin is None:
        return None
    return GridMapping(cell, 0, 0, origin[0], origin[1], cell_z=cell)


def _grid_walkable(world, grid_id, main):
    # type: (object, str, Optional[str]) -> Any
    """该网格的 is_walkable(gx,gz)。主网格复用 world.walkable；移动网格现算。"""
    if grid_id == main:
        return getattr(world, "walkable", None)
    mapping = _grid_mapping(world, grid_id)
    if mapping is None:
        return None
    try:
        from ai.space import occupancy
        return occupancy.make_walkable_from_terrain(
            world.terrain,
            occupancy.hazard_boxes(getattr(world, "hazards", None) or [],
                                   cell=mapping.cell),
            set(),                      # 移动网格的运行时占用不解析
            mapping,
            belt_cells=set(),           # 移动网格传送带（4-2）暂不画方向
            level_bounds=None,
            terrain_y=getattr(world, "terrain_y", None))
    except Exception:                     # noqa: BLE001
        return None


def _station_cell_local(world, grid_id, st):
    # type: (object, str, Dict) -> Optional[Tuple[int, int]]
    """站点/厨师在它自己网格里的局部格：gx/gz 优先，x/z 反投兜底。

    主网格用校准后的 `world.to_grid`（GridMapping，站点反解原点）；移动网格
    用 `world.grids.to_grid`（GridSet，插件给的原点）。两者对主网格可能差几格
    （geometry.py：Sushi_1_4 差 2 格），厨师只有世界坐标、必须走对，否则会
    画到主网格站点范围之外。
    """
    gx, gz = st.get("gx"), st.get("gz")
    if gx is not None and gz is not None:
        try:
            return (int(gx), int(gz))
        except (TypeError, ValueError):
            pass
    x, z = st.get("x"), st.get("z")
    if x is not None and z is not None:
        try:
            if grid_id == _majority_grid(world):
                return world.to_grid(x, z)
            return world.grids.to_grid(grid_id, x, z)
        except Exception:                 # noqa: BLE001
            pass
    return None


def _station_cells(world, grid_id, st, t):
    # type: (object, str, Dict, str) -> Set[Tuple[int, int]]
    """站点占的格（该网格局部坐标）。出餐口用 PLATEBLOCK 取 2 格真实占地。"""
    if t == "plate_station":
        try:
            from ai.space import occupancy
            cells = occupancy.station_block_cells(st, world.terrain, world.mapping)
            if cells:
                return set(cells)
        except Exception:                 # noqa: BLE001
            pass
    cell = _station_cell_local(world, grid_id, st)
    return {cell} if cell is not None else set()


def _main_grid_bounds(world, main):
    # type: (object, Optional[str]) -> Optional[Tuple[int, int, int, int]]
    """主网格站点包围盒（外扩 1 格）。没有主网格站点返回 None。"""
    cells = set()   # type: Set[Tuple[int, int]]
    for st in getattr(world, "stations", None) or ():
        if not isinstance(st, dict):
            continue
        if _station_grid(world, st, main) != main:
            continue
        cell = _station_cell_local(world, main, st)
        if cell is not None:
            cells.add(cell)
    if not cells:
        return None
    gxs = [c[0] for c in cells]
    gzs = [c[1] for c in cells]
    return (min(gxs) - 1, max(gxs) + 1, min(gzs) - 1, max(gzs) + 1)


def _in_bounds(cell, bounds):
    # type: (Tuple[int, int], Tuple[int, int, int, int]) -> bool
    gx, gz = cell
    min_gx, max_gx, min_gz, max_gz = bounds
    return min_gx <= gx <= max_gx and min_gz <= gz <= max_gz


def _chef_grid(world, chef):
    # type: (object, Dict) -> Optional[str]
    """厨师所在网格。

    先看厨师在主网格的投影是否落在主网格站点范围内——5-1 的舢板是移动台面、
    和主厨房共用同一块地板，`channels._chef_grid` 会把主网格上的厨师误判到
    舢板网格。否则退回 channels 的「独立平台（木筏/篮子）归属」（它们的厨师
    投影到主网格会落在站点范围之外）。
    """
    main = _majority_grid(world)
    x, z = chef.get("x"), chef.get("z")
    if x is not None and z is not None:
        try:
            cell = world.to_grid(x, z)
            bounds = _main_grid_bounds(world, main)
            if bounds is not None and _in_bounds(cell, bounds):
                return main
        except Exception:                 # noqa: BLE001
            pass
    try:
        from ai.space import channels
        gid = channels._chef_grid(world, chef)
        if gid:
            return gid
    except Exception:                     # noqa: BLE001
        pass
    return main


def _collect_grid(world, grid_id, stations, main):
    # type: (object, str, List[Dict], Optional[str]) -> Tuple[Dict[Tuple[int, int], str], List[str]]
    symbols = {}   # type: Dict[Tuple[int, int], str]
    crates = []    # type: List[str]
    for st in stations:
        t = str(st.get("t") or "")
        if t in _SKIP_STATION_TYPES:
            continue
        sym = _STATION_SYMBOLS.get(t, _fallback_symbol(t))
        if t == "plate_return":
            # 插件把「晾干架」也报成 plate_return，靠实例名区分。
            name = str(st.get("n") or "")
            if "drying" in name.lower():
                sym = "D"
        for cell in _station_cells(world, grid_id, st, t):
            symbols.setdefault(cell, sym)
        if t == "crate":
            ing = st.get("ing")
            cell = _station_cell_local(world, grid_id, st)
            if cell is not None and ing:
                tag = "" if grid_id == main else "[%s]" % grid_id
                crates.append("K%s(%d,%d)=%s" % (tag, cell[0], cell[1], ing))
    # 厨师位置：落在该网格上才画（木筏/篮子上也能标 1/2）。
    for ch in getattr(world, "chefs", None) or ():
        if not isinstance(ch, dict):
            continue
        name = str(ch.get("name") or "").strip().lower()
        if name == P2_NAME.lower():
            mark = "2"
        elif name == P1_NAME.lower():
            mark = "1"
        else:
            continue
        if _chef_grid(world, ch) != grid_id:
            continue
        cell = _station_cell_local(world, grid_id, ch)
        if cell is not None:
            symbols.setdefault(cell, mark)
    return symbols, crates


def _bounds(cells):
    # type: (Set[Tuple[int, int]]) -> Tuple[int, int, int, int]
    gxs = [c[0] for c in cells]
    gzs = [c[1] for c in cells]
    return (min(gxs) - 1, max(gxs) + 1, min(gzs) - 1, max(gzs) + 1)


def _grid_lines(world, grid_id, symbols, bounds, walkable, main):
    # type: (object, str, Dict[Tuple[int, int], str], Tuple[int, int, int, int], Any, Optional[str]) -> List[str]
    min_gx, max_gx, min_gz, max_gz = bounds
    moving = grid_id != main
    tag = "主" if not moving else "移动"
    lines = ["## 网格 %s（%s）" % (grid_id, tag)]
    header = ["      gx:"] + ["%3d" % gx for gx in range(min_gx, max_gx + 1)]
    lines.append(" ".join(header))
    for gz in range(max_gz, min_gz - 1, -1):
        row = ["gz %3d" % gz]
        for gx in range(min_gx, max_gx + 1):
            cell = (gx, gz)
            sym = symbols.get(cell)
            if sym is None:
                if walkable is not None:
                    try:
                        sym = "." if walkable(gx, gz) else "#"
                    except Exception:             # noqa: BLE001
                        sym = " "
                else:
                    sym = " "
            row.append("%3s" % sym)
        lines.append(" ".join(row))
    return lines


def render_map(world):
    # type: (object) -> str
    """一帧 → 多网格 ASCII 地图 + 图例。"""
    main, buckets = _bucket_stations(world)
    if not buckets:
        return ""
    blocks = []          # type: List[str]
    all_crates = []      # type: List[str]
    used = set()         # type: Set[str]
    for grid_id in _grids_ordered(world):
        stations = buckets.get(grid_id)
        if not stations:
            # 有些网格可能只有厨师没有站点；没有站点的网格跳过（不画空图）。
            continue
        symbols, crates = _collect_grid(world, grid_id, stations, main)
        cells = set(symbols)
        if not cells:
            continue
        walkable = _grid_walkable(world, grid_id, main)
        blocks.extend(_grid_lines(world, grid_id, symbols, _bounds(cells),
                                  walkable, main))
        blocks.append("")
        all_crates.extend(crates)
        used |= {s for s in symbols.values()} | {".", "#"}

    legend = [("%s=%s" % (s, _LEGEND.get(s, s))) for s in sorted(used)
              if s in _LEGEND]
    blocks.append("图例：" + "  ".join(legend))
    if all_crates:
        blocks.append("料箱：" + "  ".join(sorted(all_crates)))
    return "\n".join(blocks)


# ---------------------------------------------------------------- 紧凑布局

_LAYOUT_NAMES = {
    "chopping": "切", "cooking": "煮", "washing": "洗", "mixing": "拌",
    "bin": "垃圾桶", "plate_return": "盘回收", "drying": "晾干架",
    "counter": "柜台",
}


def render_layout(world):
    # type: (object) -> str
    """紧凑「站点布局」：按网格分组，网格内按左中右汇总，供分工参考。"""
    main, buckets = _bucket_stations(world)
    lines = []  # type: List[str]
    for grid_id in _grids_ordered(world):
        stations = buckets.get(grid_id)
        if not stations:
            continue
        groups = {}    # type: Dict[str, List[Tuple[int, int]]]
        crates = {}    # type: Dict[str, List[Tuple[int, int]]]
        plate_cells = []  # type: List[Tuple[int, int]]
        for st in stations:
            t = str(st.get("t") or "")
            cell = _station_cell_local(world, grid_id, st)
            if cell is None:
                continue
            if t == "crate":
                crates.setdefault(str(st.get("ing") or "?"), []).append(cell)
            elif t == "plate_station":
                plate_cells.append(cell)
            elif t in _STATION_SYMBOLS:
                groups.setdefault(t, []).append(cell)
        all_cells = (plate_cells + [c for v in groups.values() for c in v]
                     + [c for v in crates.values() for c in v])
        xs = [c[0] for c in all_cells]
        mid = (min(xs) + max(xs)) / 2.0 if xs else 0.0

        def side(cells):
            # type: (List[Tuple[int, int]]) -> str
            if not cells:
                return "-"
            sides = {("左" if c[0] < mid else "右") for c in cells}
            return "".join(sorted(sides)) if len(sides) == 1 else "中/跨"

        prefix = "" if grid_id == main else "[%s] " % grid_id
        for t in ("chopping", "cooking", "washing", "mixing",
                  "bin", "plate_return", "drying"):
            cells = groups.get(t)
            if cells:
                lines.append("%s%s(%s)：%s %d 个" % (
                    prefix, _LAYOUT_NAMES.get(t, t), t, side(cells), len(cells)))
        if plate_cells:
            lines.append("%s出餐口(plate_station)：%s %d 个"
                         % (prefix, side(plate_cells), len(plate_cells)))
        for ing, cells in sorted(crates.items()):
            lines.append("%s料箱 %s：%s" % (prefix, ing, side(cells)))
    return "\n".join(lines)

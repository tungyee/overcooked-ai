# -*- coding: utf-8 -*-
"""地图渲染：把一帧 `World` 画成 ASCII 网格（对齐 docs/map 的手绘格式）。

只读 `World`，不写任何状态。两个入口：

- `render_map(world)`：整张 ASCII 网格 + 简要图例，给人看 / 调试用；
- `render_layout(world)`：紧凑的「站点布局」文本，后续可喂给 LLM 做分工。

坐标口径：主网格（GridManager / 插件 grids[].main）。站点位置优先用世界
坐标 x/z 反投到主网格（`placement._station_cell` 同口径），缺 x/z 才退回
站点局部 gx/gz。非主网格（木筏/篮子）的站点不画进单网格地图——它们的局部
gx/gz 与主网格坐标不是一套（spec §4.1），硬画会把位置画错。
"""
from typing import Dict, List, Optional, Set, Tuple

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
# 这些类型是地板/墙/动态盘堆等功能桩，不作为障碍符号画出来：
# 墙由 walkable=# 表达，盘堆是动态物（且 gx/gz 为 None），地板本身可走。
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
    """未知站点类型给一个可读占位（取首字母大写），并缓存避免重复计算。"""
    if t in _UNKNOWN_SYMBOLS:
        return _UNKNOWN_SYMBOLS[t]
    sym = (t[:1].upper() if t else "?")
    _UNKNOWN_SYMBOLS[t] = sym
    return sym


def _majority_grid(world):
    # type: (object) -> Optional[str]
    """主网格名；优先 world 缓存的众数，失败退回 None（不过滤）。"""
    try:
        return world._majority_grid()
    except Exception:                     # noqa: BLE001
        return None


def _station_cell(world, st):
    # type: (object, Dict) -> Optional[Tuple[int, int]]
    """站点在主网格坐标里的格：x/z 反投优先，gx/gz 兜底。"""
    x, z = st.get("x"), st.get("z")
    if x is not None and z is not None:
        try:
            return world.to_grid(x, z)
        except Exception:                 # noqa: BLE001
            pass
    gx, gz = st.get("gx"), st.get("gz")
    if gx is not None and gz is not None:
        try:
            return (int(gx), int(gz))
        except (TypeError, ValueError):
            return None
    return None


def _station_cells(world, st, t):
    # type: (object, Dict, str) -> Set[Tuple[int, int]]
    """站点占的格（主网格）。出餐口用 PLATEBLOCK 层取 2 格真实占地，其余单格。"""
    if t == "plate_station":
        try:
            from ai.space import occupancy
            cells = occupancy.station_block_cells(st, world.terrain, world.mapping)
            if cells:
                return set(cells)
        except Exception:                 # noqa: BLE001
            pass
    cell = _station_cell(world, st)
    return {cell} if cell is not None else set()


def _collect(world):
    # type: (object) -> Tuple[Dict[Tuple[int, int], str], List[str], Set[Tuple[int, int]]]
    """扫描站点与厨师，返回 (格子符号表, 图例行, 已知格子集合)。"""
    symbols = {}   # type: Dict[Tuple[int, int], str]
    crates = []    # type: List[str]
    main_grid = _majority_grid(world)

    for st in getattr(world, "stations", None) or ():
        if not isinstance(st, dict):
            continue
        t = str(st.get("t") or "")
        if t in _SKIP_STATION_TYPES:
            continue
        g = str(st.get("grid") or "")
        if main_grid and g and g not in (main_grid, "GridManager"):
            continue                      # 非主网格站点（木筏/篮子）不画进单网格图
        sym = _STATION_SYMBOLS.get(t, _fallback_symbol(t))
        if t == "plate_return":
            # 插件把「晾干架」也报成 plate_return，靠实例名区分：
            # DryingPart → 晾干架(D)，workstation_plate_return → 盘回收(R)。
            name = str(st.get("n") or "")
            if "drying" in name.lower():
                sym = "D"
        for cell in _station_cells(world, st, t):
            symbols.setdefault(cell, sym)
        if t == "crate":
            ing = st.get("ing")
            cell = _station_cell(world, st)
            if cell is not None and ing:
                crates.append("K(%d,%d)=%s" % (cell[0], cell[1], ing))

    known = set(symbols)
    # 厨师当前位置（出生位/实时位置）。
    for ch in getattr(world, "chefs", None) or ():
        if not isinstance(ch, dict):
            continue
        name = str(ch.get("name") or "")
        mark = "2" if name == "P2" else ("1" if name == "P1" else None)
        if mark is None:
            continue
        x, z = ch.get("x"), ch.get("z")
        if x is None or z is None:
            continue
        try:
            cell = world.to_grid(x, z)
        except Exception:                 # noqa: BLE001
            continue
        symbols.setdefault(cell, mark)
        known.add(cell)
    return symbols, crates, known


def _bounds(cells):
    # type: (Set[Tuple[int, int]]) -> Tuple[int, int, int, int]
    """格子集合的最小包围盒，外扩 1 格留边。"""
    gxs = [c[0] for c in cells]
    gzs = [c[1] for c in cells]
    return (min(gxs) - 1, max(gxs) + 1, min(gzs) - 1, max(gzs) + 1)


def _grid_lines(world, symbols, bounds):
    # type: (object, List[Tuple[int, int]]) -> List[str]
    min_gx, max_gx, min_gz, max_gz = bounds
    walkable = getattr(world, "walkable", None)
    lines = []

    # 表头：gx 坐标。
    header = ["      gx:"] + ["%3d" % gx for gx in range(min_gx, max_gx + 1)]
    lines.append(" ".join(header))

    for gz in range(max_gz, min_gz - 1, -1):       # gz 从上往下
        row = ["gz %3d" % gz]
        for gx in range(min_gx, max_gx + 1):
            cell = (gx, gz)
            sym = symbols.get(cell)
            if sym is None:
                if walkable is not None:
                    try:
                        sym = "." if walkable(gx, gz) else "#"
                    except Exception:             # noqa: BLE001
                        sym = "?"
                else:
                    sym = " "
            row.append("%3s" % sym)
        lines.append(" ".join(row))
    return lines


def render_map(world):
    # type: (object) -> str
    """一帧 → ASCII 地图 + 图例（对齐 docs/map 格式）。"""
    symbols, crates, known = _collect(world)
    if not known:
        return ""
    bounds = _bounds(known)
    lines = _grid_lines(world, symbols, bounds)

    used = sorted({s for s in symbols.values()} | {"."})
    legend = [("%s=%s" % (s, _LEGEND.get(s, s))) for s in used
              if s in _LEGEND]
    lines.append("")
    lines.append("图例：" + "  ".join(legend))
    if crates:
        lines.append("料箱：" + "  ".join(sorted(crates)))
    return "\n".join(lines)


def render_layout(world):
    # type: (object) -> str
    """紧凑「站点布局」文本：按动作/站点类型汇总位置与左中右，供分工参考。

    只列主网格站点；左/右按主网格 x 中位数切（够用即可，不需要 zoning 的
    连通块语义）。料箱按产出物料归类，标注它喂的加工站在哪一侧。
    """
    main_grid = _majority_grid(world)
    groups = {}   # type: Dict[str, List[Tuple[int, int]]]
    crates = {}   # type: Dict[str, List[Tuple[int, int]]]
    plate_cells = []   # type: List[Tuple[int, int]]
    for st in getattr(world, "stations", None) or ():
        if not isinstance(st, dict):
            continue
        t = str(st.get("t") or "")
        g = str(st.get("grid") or "")
        if main_grid and g and g not in (main_grid, "GridManager"):
            continue
        cell = _station_cell(world, st)
        if cell is None:
            continue
        if t == "crate":
            crates.setdefault(str(st.get("ing") or "?"), []).append(cell)
        elif t == "plate_station":
            plate_cells.append(cell)
        elif t in _STATION_SYMBOLS:
            groups.setdefault(t, []).append(cell)

    xs = [c[0] for c in (plate_cells + [c for v in groups.values() for c in v]
                         + [c for v in crates.values() for c in v])]
    mid = (min(xs) + max(xs)) / 2.0 if xs else 0.0

    def side(cells):
        # type: (List[Tuple[int, int]]) -> str
        if not cells:
            return "-"
        sides = {("左" if c[0] < mid else "右") for c in cells}
        return "".join(sorted(sides)) if len(sides) == 1 else "中/跨"

    lines = []
    name = {"chopping": "切", "cooking": "煮", "washing": "洗",
            "mixing": "拌", "bin": "垃圾桶", "plate_return": "盘回收",
            "drying": "晾干架", "counter": "柜台"}
    for t in ("chopping", "cooking", "washing", "mixing",
              "bin", "plate_return", "drying"):
        cells = groups.get(t)
        if cells:
            lines.append("%s(%s)：%s %d 个"
                         % (name.get(t, t), t, side(cells), len(cells)))
    if plate_cells:
        lines.append("出餐口(plate_station)：%s %d 个"
                     % (side(plate_cells), len(plate_cells)))
    for ing, cells in sorted(crates.items()):
        lines.append("料箱 %s：%s" % (ing, side(cells)))
    return "\n".join(lines)

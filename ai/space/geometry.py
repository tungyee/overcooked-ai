"""网格 ↔ 世界坐标换算。

取代 oc/ai/world.py 的 GRID_OX=14.4 / GRID_OZ=-6.0 硬编码。

坐标是线性的（spec §9，61/61 站点零违例）：

    x = origin_x + cell * gx
    z = origin_z + cell * gz

⚠️ spec §9 曾断言 `origin_x = size.x * mapOffset.X`、`origin_z = -size.z * mapOffset.Y`，
这条推导在算术上是**假的**：对 Sushi_1_4 算出 9.6 / -7.2，而实测原点是 14.4 / -6.0。
原因是 `mapOffset`(8,6) 恰是 nodeMap 数组（17×13）的中心下标，是纯索引量，
不含任何世界空间位置信息，因此原则上无法从它推出世界原点。见 Ruling 8。

因此世界原点改为**从站点自动校准**（Option B，有 61/61 一致性支撑，换关卡免改代码）：
给 `from_grid` 传入站点列表，用任一带 gx/gz 的站点反解 `origin = coord - cell * g`。
无站点可用时回落到 ORIGIN_*_DEFAULT 兜底常量。

`mapOffset` 仍用于 `node_index()`——那部分是对的（61/61 站点的 node_index 落点
在 nodeMap 里都是被占用格，与"站点挡路"吻合）。
"""
from collections import Counter
from typing import Dict, List, Optional, Tuple

OFF_X_DEFAULT = 8
OFF_Z_DEFAULT = 6
CELL_DEFAULT = 1.2

# 世界原点兜底：无站点可校准时使用。取 Sushi_1_4 实测值，
# 换关卡且无站点时必然失真——所以优先走站点校准。
ORIGIN_X_DEFAULT = 14.4
ORIGIN_Z_DEFAULT = -6.0

# 校准反解时的量化精度（吸收浮点噪声后取众数，防个别脏站点带偏）
_CALIB_ROUND = 4


def parse_vec(s):
    # type: (Optional[str]) -> Optional[Tuple[float, ...]]
    """解析插件输出的 "(x, y, z)" 字符串。"""
    if not s or s == "null":
        return None
    try:
        return tuple(float(v) for v in s.strip("()").split(","))
    except (ValueError, AttributeError):
        return None


def _calibrate(stations, cell, axis_coord, axis_grid):
    # type: (Optional[List[Dict]], float, str, str) -> Optional[float]
    """从站点反解某一轴的世界原点：origin = coord - cell * g。

    对所有带该轴网格坐标的站点各解一次，取众数——单帧内理应完全一致，
    取众数只是防个别脏数据把原点带偏。都没有可用站点则返回 None。
    """
    if not stations:
        return None
    votes = Counter()  # type: Counter
    for st in stations:
        if not isinstance(st, dict):
            continue
        g = st.get(axis_grid)
        c = st.get(axis_coord)
        if g is None or c is None:
            continue
        try:
            votes[round(c - cell * g, _CALIB_ROUND)] += 1
        except TypeError:
            continue
    if not votes:
        return None
    return votes.most_common(1)[0][0]


class GridMapping:
    """一个关卡的网格映射。从 state["grid"]（+ 站点）构造，之后只读。"""

    def __init__(self, cell, off_x, off_z, origin_x, origin_z, cell_z=None):
        # type: (float, int, int, float, float, Optional[float]) -> None
        """cell 为 x 轴格宽；cell_z 省略时沿用 cell（方形格）。

        grid.size 是 (x, y, z)，x 与 z 的格宽原则上可以不同，所以两轴分开存。
        Sushi_1_4 恰好是 (1.2, 1.0, 1.2) 方形，但不能把这当成通例——
        本模块的目标就是换关卡免改代码。
        """
        self.cell = cell
        self.cell_z = cell if cell_z is None else cell_z
        self.off_x = off_x
        self.off_z = off_z
        self.origin_x = origin_x
        self.origin_z = origin_z

    @property
    def cell_x(self):
        # type: () -> float
        return self.cell

    @classmethod
    def from_grid(cls, grid, stations=None):
        # type: (Optional[Dict], Optional[List[Dict]]) -> "GridMapping"
        """从 grid 快照构造。传入 stations 时自动校准世界原点（推荐）。

        stations: state["stations"] 列表，用带 gx/gz 的站点反解原点。
                  不传或无可用站点时回落到 ORIGIN_*_DEFAULT 兜底常量。
                  多网格关卡下调用方应只传**主网格**的站点——移动平台
                  （气球篮）站点会把众数带漂（trace-20261017-215633）。
        """
        grid = grid or {}
        size = parse_vec(grid.get("size"))
        cell = size[0] if size else CELL_DEFAULT
        # size 是 (x, y, z)：z 轴格宽取 size[2]，缺失时退回 x 轴格宽
        cell_z = size[2] if size and len(size) >= 3 else cell

        off_x, off_z = OFF_X_DEFAULT, OFF_Z_DEFAULT
        mo = grid.get("mapOffset") or ""
        if "," in mo:
            parts = mo.split(",")
            try:
                off_x = int(float(parts[0]))
                off_z = int(float(parts[1]))
            except (ValueError, IndexError):
                pass

        origin_x = _calibrate(stations, cell, "x", "gx")
        if origin_x is None:
            origin_x = ORIGIN_X_DEFAULT
        origin_z = _calibrate(stations, cell_z, "z", "gz")
        if origin_z is None:
            origin_z = ORIGIN_Z_DEFAULT

        return cls(cell, off_x, off_z, origin_x, origin_z, cell_z)

    def to_world(self, gx, gz):
        # type: (int, int) -> Tuple[float, float]
        return (self.origin_x + self.cell * gx, self.origin_z + self.cell_z * gz)

    def to_grid(self, x, z):
        # type: (float, float) -> Tuple[int, int]
        gx = int(round((x - self.origin_x) / self.cell))
        gz = int(round((z - self.origin_z) / self.cell_z))
        return (gx, gz)

    def node_index(self, gx, gz):
        # type: (int, int) -> Tuple[int, int]
        """网格坐标 → nodeMap 下标。dim0=gx+off_x, dim1=gz+off_z。

        这里用 mapOffset 是正确的：它就是 nodeMap 数组的中心下标，
        GetIndexFromPoint(p) => (p.X - mapOffset.X, p.Y - mapOffset.Y)
        （oc/AI_REVIEW.md:125）。61/61 站点验证 node_index 落点均为占用格。
        """
        return (gx + self.off_x, gz + self.off_z)


class GridSet(object):
    """多网格坐标集合（spec §5.1）：每个网格有自己的原点与格宽。

    与 GridMapping 的关系：GridMapping 是单网格模型（靠站点反解原点），
    GridSet 是多网格模型（原点由插件直接给出，不反解）。换关卡/换帧时
    直接从 state["grids"] 重建，零猜测。

    局部坐标 (gx, gz) 对同一个格子在不同网格下代表不同世界位置——
    这正是 §4.1 的核心发现：两个网格原点相差 10.0，不能共用一套坐标。
    """

    def __init__(self, grids):
        # type: (Dict[str, Dict[str, float]]) -> None
        self._grids = grids

    @classmethod
    def from_frame(cls, grids_list):
        # type: (Optional[List[Dict]]) -> "GridSet"
        out = {}  # type: Dict[str, Dict[str, float]]
        for g in grids_list or ():
            if not isinstance(g, dict):
                continue
            gid = g.get("id")
            if not gid:
                continue
            out[str(gid)] = {
                "ox": float(g.get("ox", 0.0) or 0.0),
                "oz": float(g.get("oz", 0.0) or 0.0),
                "cell": float(g.get("cell", CELL_DEFAULT) or CELL_DEFAULT),
            }
        return cls(out)

    def ids(self):
        # type: () -> List[str]
        return list(self._grids.keys())

    def cell_size(self, grid_id):
        # type: (str) -> float
        g = self._grids.get(str(grid_id))
        return g["cell"] if g else CELL_DEFAULT

    def to_world(self, grid_id, gx, gz):
        # type: (str, int, int) -> Optional[Tuple[float, float]]
        g = self._grids.get(str(grid_id))
        if g is None:
            return None
        return (g["ox"] + g["cell"] * gx, g["oz"] + g["cell"] * gz)

    def to_grid(self, grid_id, x, z):
        # type: (str, float, float) -> Optional[Tuple[int, int]]
        g = self._grids.get(str(grid_id))
        if g is None:
            return None
        return (int(round((x - g["ox"]) / g["cell"])),
                int(round((z - g["oz"]) / g["cell"])))

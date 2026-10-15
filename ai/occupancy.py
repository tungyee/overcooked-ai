"""可走性判定与实体去重：全部纯函数。

从 oc/ai/world.py 的 World.__init__ 抽取。原代码把 nodeMap 解析、边界
计算、AABB 占用、传送带格、三类去重全挤在一个构造函数里，单测必须造
完整快照。这里改为入参是 dict 与基本类型的纯函数，与 Plan 1 的
predicates / navigation 风格一致。

与旧实现的两处行为差异（均为修 bug，有测试钉住）：
  1. parse_node_map 校验位串长度，不符返回 None；旧代码直接下标会抛
     IndexError 把主循环带崩。
  2. playable_bounds 统一用 .get() is not None 判站点 gx/gz；旧代码在
     边界计算处用 "gx" in s（判键存在）、在占用计算处用 .get()（判非
     None），两种写法不一致。实测 71 站点里 10 个无 gx 键。
"""
import math
from typing import Callable, Dict, List, Optional, Set, Tuple

from ai.geometry import GridMapping

# nodeMap 下标偏移不在这里定义：GridMapping 已持有 off_x / off_z
# （geometry.OFF_X_DEFAULT 是它的兜底）。同一个常量定义两处必然漂移，
# 本模块一律从调用方传入。

# 传送带（ConveyorStation）：nodeMap 里标 '0'，但角色可以走上去、被推向固定方向。
# 方向只影响移动速度，BFS 暂当普通可走格处理。位置/方向由用户实测标注，
# 后续改为从 ConveyorStation.m_conveyanceDirectionXZ 通用读取。
BELTS = {
    "Sushi_1_4_2P": [
        {"cells": [(gx, 2) for gx in range(-4, 5)], "dir": (1, 0)},    # K 排，向右
        {"cells": [(gx, -3) for gx in range(-4, 5)], "dir": (-1, 0)},   # Y 排，向左
    ],
}


def parse_node_map(nm):
    # type: (Optional[str]) -> Optional[Tuple[List[List[bool]], int, int]]
    """解析 "ROWSxCOLS=位串" 为 (矩阵, rows, cols)。

    返回 None 表示"无可用 nodeMap"——调用方应退化为只看站点占用，
    而不是把 None 当"全不可走"。
    """
    if not nm or "=" not in nm:
        return None
    dims, bits = nm.split("=", 1)
    if "x" not in dims:
        return None
    try:
        rows_s, cols_s = dims.split("x", 1)
        rows, cols = int(rows_s), int(cols_s)
    except ValueError:
        return None
    if rows <= 0 or cols <= 0:
        return None
    # 长度必须精确匹配：短了会下标越界，长了说明维度声明与数据不一致，
    # 两种情况都不该猜。
    if len(bits) != rows * cols:
        return None
    matrix = [[bits[r * cols + c] == "1" for c in range(cols)]
              for r in range(rows)]
    return (matrix, rows, cols)


def playable_bounds(stations):
    # type: (Optional[List[Dict]]) -> Optional[Tuple[int, int, int, int]]
    """用站点 (gx, gz) 的范围界定厨房可玩区，返回 (min_gx, max_gx, min_gz, max_gz)。

    为什么需要它：nodeMap 外围有一圈误标为 '1' 的格子（厨房围墙之外）。
    只看 nodeMap 会把厨师往墙外引，所以要用站点范围夹一道。

    只统计 gx 与 gz **都**有值的站点——单轴站点无法定位。

    返回值标注为 int 是**契约**而非强转：插件输出的 gx/gz 保证是整数
    （全部真实帧实测无一例外），故此处原样透传。不加 int() 是有意的——
    int() 向零截断，int(-2.7) == -2 会把 min_gx 错误地**收窄**，比放一个
    float 过去更糟；而 bounds 只参与 <= 比较，float 同样算得对。
    """
    gxs = []  # type: List[int]
    gzs = []  # type: List[int]
    for st in stations or []:
        if not isinstance(st, dict):
            continue
        gx, gz = st.get("gx"), st.get("gz")
        if gx is None or gz is None:
            continue
        gxs.append(gx)
        gzs.append(gz)
    if not gxs:
        return None
    return (min(gxs), max(gxs), min(gzs), max(gzs))


# AABB 与格子重叠判定的浮点容差。边界贴合（相邻格恰好挨着 AABB）
# 不算重叠，但浮点噪声会让"恰好相等"变成"差 1e-16"，故留容差。
_OVERLAP_EPS = 1e-3


def station_blocked_cells(stations, mapping):
    # type: (Optional[List[Dict]], GridMapping) -> Set[Tuple[int, int]]
    """站点物理占用的不可走格集合。

    两条来源，取并集：
      1. Collider AABB（minx/maxx/minz/maxz）——能正确覆盖多格站点，
         如出餐口横跨两格。
      2. gx/gz 单格兜底——AABB 字段缺失时（旧录制格式）仍有结果。

    为什么不能只靠 nodeMap：ServerPlateStation 这类功能站不进游戏的
    占用表，nodeMap 把它标成可走，厨师会撞上去。
    """
    blocked = set()  # type: Set[Tuple[int, int]]
    # 旧实现只有单一 half = cell / 2.0（两轴同用）。这里按 GridMapping
    # 的两轴格宽模型拆成 half_x / half_z（见 ai/geometry.py 第 80-85 行
    # 的 cell/cell_z 设计说明），是有意的泛化。当前关卡 cell == cell_z
    # == 1.2，两者数值相同，926,782 条 parity 断言全部测不出这处分歧——
    # 换成矩形格的关卡才会体现差异，且目前无样本可验证。
    half_x = mapping.cell / 2.0
    half_z = mapping.cell_z / 2.0
    for st in stations or []:
        if not isinstance(st, dict):
            continue
        # floor 站点代表可走地板（Col_Floor），不是障碍，跳过——它只用于
        # 扩展 playable_bounds（见 world.py），不能被标成不可走。
        if st.get("t") == "floor":
            continue
        gx0, gz0 = st.get("gx"), st.get("gz")
        if gx0 is not None and gz0 is not None:
            blocked.add((int(gx0), int(gz0)))
        minx, minz = st.get("minx"), st.get("minz")
        maxx, maxz = st.get("maxx"), st.get("maxz")
        if minx is None or minz is None or maxx is None or maxz is None:
            continue
        # 扫描范围取 AABB 所跨格区间再各外扩 1 格，确保边缘格也被检查
        gx_lo = int(math.floor((minx - mapping.origin_x) / mapping.cell)) - 1
        gx_hi = int(math.floor((maxx - mapping.origin_x) / mapping.cell)) + 1
        gz_lo = int(math.floor((minz - mapping.origin_z) / mapping.cell_z)) - 1
        gz_hi = int(math.floor((maxz - mapping.origin_z) / mapping.cell_z)) + 1
        for gxc in range(gx_lo, gx_hi + 1):
            cx, _ = mapping.to_world(gxc, 0)
            if not (cx + half_x > minx + _OVERLAP_EPS
                    and cx - half_x < maxx - _OVERLAP_EPS):
                continue
            for gzc in range(gz_lo, gz_hi + 1):
                _, cz = mapping.to_world(0, gzc)
                if (cz + half_z > minz + _OVERLAP_EPS
                        and cz - half_z < maxz - _OVERLAP_EPS):
                    blocked.add((gxc, gzc))
    return blocked


def belt_cells_and_dirs(level):
    # type: (Optional[str]) -> Tuple[Set[Tuple[int, int]], Dict[Tuple[int, int], Tuple[int, int]]]
    """传送带格集合与每格方向。

    传送带格 nodeMap 标 '0'（不可走），但角色实际能走上去，所以
    walkable 要把它们当可走；navigation 则对逆向通行加代价。

    ⚠️ BELTS 仍是关卡硬编码。泛化需插件输出传送带区域（spec §10.1），
    本轮插件冻结。未知关卡退化为空集——walkable 退回纯 nodeMap +
    站点占用，不崩。
    """
    cells = set()  # type: Set[Tuple[int, int]]
    dirs = {}      # type: Dict[Tuple[int, int], Tuple[int, int]]
    for belt in BELTS.get(level) or []:
        for cell in belt["cells"]:
            cells.add(cell)
            dirs[cell] = belt["dir"]
    return (cells, dirs)


def make_walkable(node_map, rows, cols, bounds, blocked, belt_cells,
                  off_x, off_z):
    # type: (Optional[List[List[bool]]], int, int, Optional[Tuple[int,int,int,int]], Set[Tuple[int,int]], Set[Tuple[int,int]], int, int) -> Callable[[int, int], bool]
    """返回 is_walkable(gx, gz) 闭包，供 navigation.distance_field 使用。

    返回闭包而非每次传八个参数：distance_field 每拍调用上千次，参数
    绑定一次就好。闭包捕获的全是不可变快照数据，无副作用。

    判定优先级（顺序有意义）：
      1. 站点物理占用 → 不可走。功能站 nodeMap 标可走但撞不过去。
      2. 越界（可玩边界外）→ 不可走。nodeMap 外围有一圈误标 '1' 的墙外格。
         放在 nodeMap 缺失判断之前：没有 nodeMap 但有 bounds 时，
         bounds 是唯一还能约束可走范围的信息，不能被跳过——否则
         nodeMap 为 None 的帧会让 distance_field 在无边界谓词上无限
         flood-fill（见 navigation.distance_field 的 max_cells 兜底）。
      3. nodeMap 缺失（且无 bounds 可用）→ 可走。宁可走错也不要让 AI
         整体瘫掉。
      4. 传送带 → 可走。nodeMap 标 '0' 但角色能走上去。
      5. 查 nodeMap，下标越界且**在 bounds 内、且位于 nodeMap 下沿之下**时
         当可走。nodeMap 只覆盖 gz=-4..4 的 9 行，但实际地板（Col_Floor）
         往下延伸到 gz=-6，底部有连接左右半边的通道（实机：P1 从 (1,-6)
         穿过中央柜台）。这几行不在 nodeMap 里，只能靠「bounds 已由 world.py
         用 floor 的 minz 扩到地板底部 + 墙碰撞体标记障碍」来判定。
         只放行 gz < -off_z（下沿以下），左右/上沿越界仍不可走，避免把
         其它关卡的墙外格误当可走。没有 bounds 时仍按不可走，防 flood-fill。
    """
    def is_walkable(gx, gz):
        # type: (int, int) -> bool
        if (gx, gz) in blocked:
            return False
        if bounds is not None:
            min_gx, max_gx, min_gz, max_gz = bounds
            if not (min_gx <= gx <= max_gx and min_gz <= gz <= max_gz):
                return False
        if node_map is None:
            return True
        if (gx, gz) in belt_cells:
            return True
        r, c = gx + off_x, gz + off_z
        if 0 <= r < rows and 0 <= c < cols:
            return node_map[r][c]
        # nodeMap 之外的格：只放行「下沿以下」（地板扩展出的底部通道），
        # 左右/上沿一律不可走，防止泛化到别的关卡时把墙外格误标可走。
        return bounds is not None and gz < -off_z

    return is_walkable


# 位置去重的小数位数。插件两端上报同一对象时末位可能有浮点噪声。
_POS_ROUND = 2

# 判断烹饪台条目"信息量"的状态键。cooking_progress/cooking_time 是
# 连续进度，帮助去重保留 Server 那份更完整的条目。
_COOKER_STATE_KEYS = ("isCooking", "cooked", "burning", "onFire",
                      "cooking_progress", "cooking_time")


def dedupe_cookers(raw):
    # type: (Optional[List]) -> List[Dict]
    """按位置去重烹饪台，同位置保留状态字段更完整的那条。

    插件会同时报 Client 与 Server 两个烹饪台对象。Client 那份常常只有
    位置，Server 那份才带 isCooking/cooked/burning/onFire——保留错了
    就永远读不到"锅好没好"。
    """
    def score(entry):
        # type: (Dict) -> int
        return sum(1 for k in _COOKER_STATE_KEYS if entry.get(k) is not None)

    by_pos = {}  # type: Dict[Tuple[float, float], Dict]
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        x, z = entry.get("x"), entry.get("z")
        if x is None or z is None:
            continue
        key = (round(x, _POS_ROUND), round(z, _POS_ROUND))
        cur = by_pos.get(key)
        if cur is None or score(entry) > score(cur):
            by_pos[key] = entry
    return list(by_pos.values())


def dedupe_items(raw):
    # type: (Optional[List]) -> List[Dict]
    """按 名字+位置 去重散落物品，保留首次出现顺序。

    键里带名字（灶不带）：两个不同食材可以叠在同一格，只按位置去重
    会吞掉一个。
    """
    seen = set()  # type: Set[Tuple[str, float, float]]
    out = []      # type: List[Dict]
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        x, z = entry.get("x"), entry.get("z")
        if x is None or z is None:
            continue
        key = (str(entry.get("n") or ""),
               round(x, _POS_ROUND), round(z, _POS_ROUND))
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def dedupe_extinguishers(raw):
    # type: (Optional[List]) -> List[Dict]
    """按位置去重灭火器，同位置优选名字不含 "rigidbody" 的那条。

    插件会报出同一个灭火器对象上的多个同名组件。Rigidbody 组件的位置
    是物理中心而非交互点，取错了会站错位拿不到。只有 Rigidbody 版本
    时仍保留——有总比没有好。
    """
    by_pos = {}  # type: Dict[Tuple[float, float], Dict]
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        x, z = entry.get("x"), entry.get("z")
        if x is None or z is None:
            continue
        key = (round(x, _POS_ROUND), round(z, _POS_ROUND))
        cur = by_pos.get(key)
        cur_is_rigid = (cur is not None
                        and "rigidbody" in str(cur.get("n") or "").lower())
        new_is_rigid = "rigidbody" in str(entry.get("n") or "").lower()
        if cur is None or (cur_is_rigid and not new_is_rigid):
            by_pos[key] = entry
    return list(by_pos.values())

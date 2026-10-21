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

from ai.space.geometry import GridMapping

# nodeMap 下标偏移不在这里定义：GridMapping 已持有 off_x / off_z
# （geometry.OFF_X_DEFAULT 是它的兜底）。同一个常量定义两处必然漂移，
# 本模块一律从调用方传入。


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

# 点判（格中心是否落在 box 内）的浮点容差。格中心恰好落在 box 边界
# （如墙 AABB 的 maxz）时，origin + cell*gz 的浮点误差会让它偏到边界外
# 1e-16 处，导致墙边界那一整行被误判可走（Sushi_1_3 的 gz=-6，角色卡在
# 墙里顶墙 no_progress）。远小于格宽 1.2，只兜住边界浮点噪声，不误伤相邻格。
_POINT_EPS = 1e-6

# 厨师胶囊体半径约 0.4，但阻挡层外扩只取 0.35——比半径略小，让「恰好贴墙」
# 的格（格心距障碍物 0.4，胶囊体刚好触碰）仍可走；只挡「身体明显过不去」
# 的格（障碍物溢出邻格 0.2~0.3，如绿植 collider 溢出 0.3）。取 0.4 会把
# Balloon_1_5 顶层地板那排「墙只溢出 0.2」的格误封（tests 钉住）。
CHEF_CLEARANCE = 0.35


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


LAYER_GROUND = 9
LAYER_WALLS = 10
LAYER_PLATEBLOCK = 20
LAYER_COOKINGBLOCK = 22
LAYER_TABLEBLOCK = 26
LAYER_KILLPLANE = 30
BLOCKING_LAYERS = (LAYER_WALLS, LAYER_TABLEBLOCK, LAYER_COOKINGBLOCK, LAYER_PLATEBLOCK)


def parse_terrain(terrain_list):
    # type: (Optional[List[Dict]]) -> Dict[int, List[Tuple[float, float, float, float]]]
    """按层号归类 AABB 列表（spec §4.4/§6）：{layer: [(minx,maxx,minz,maxz), ...]}。

    层号跨关稳定（Unity 工程配置，不是场景实例名），取代 Col_* 名字前缀。
    """
    out = {}  # type: Dict[int, List[Tuple[float, float, float, float]]]
    for entry in terrain_list or ():
        if not isinstance(entry, dict):
            continue
        layer = entry.get("layer")
        if layer is None:
            continue
        try:
            layer_i = int(layer)
        except (TypeError, ValueError):
            continue
        boxes = []  # type: List[Tuple[float, float, float, float]]
        for b in entry.get("boxes") or ():
            if not isinstance(b, (list, tuple)) or len(b) != 4:
                continue
            try:
                boxes.append(tuple(float(v) for v in b))
            except (TypeError, ValueError):
                continue
        if boxes:
            out.setdefault(layer_i, []).extend(boxes)
    return out


def parse_level_bounds(bounds_list):
    # type: (Optional[List[Dict]]) -> List[Tuple[float, float, float, float]]
    """解析可玩区域 AABB（插件 level_bounds 字段，LevelBounds.m_bounds）。

    返回 [(minx, maxx, minz, maxz), ...]，box 顺序与 terrain 一致。用于
    Ground(9) 层缺失时兜底裁剪可走范围（1-6 掉出范围的根因：terrain 的
    Ground 为空 → 「不在 Ground 内即不可走」整体失效，见
    make_walkable_from_terrain 第 1 步的 fallback 分支）。
    """
    out = []  # type: List[Tuple[float, float, float, float]]
    for entry in bounds_list or ():
        if not isinstance(entry, dict):
            continue
        minx, maxx = entry.get("minx"), entry.get("maxx")
        minz, maxz = entry.get("minz"), entry.get("maxz")
        if minx is None or maxx is None or minz is None or maxz is None:
            continue
        try:
            out.append((float(minx), float(maxx), float(minz), float(maxz)))
        except (TypeError, ValueError):
            continue
    return out


def _point_in_any_box(x, z, boxes, margin=0.0):
    # type: (float, float, List[Tuple[float, float, float, float]], float) -> bool
    for minx, maxx, minz, maxz in boxes:
        if (minx - margin - _POINT_EPS <= x <= maxx + margin + _POINT_EPS
                and minz - margin - _POINT_EPS <= z <= maxz + margin + _POINT_EPS):
            return True
    return False


def make_walkable_from_terrain(terrain_by_layer, hazard_boxes, occupancy_cells,
                               mapping, belt_cells=None, level_bounds=None):
    # type: (Dict[int, List[Tuple]], List[Tuple[float,float,float,float]], Set[Tuple[int,int]], GridMapping, Optional[Set[Tuple[int,int]]], Optional[List[Tuple[float,float,float,float]]]) -> Callable[[int, int], bool]
    """按 §5.3 判定顺序构造 is_walkable(gx,gz)。替代 `make_walkable`。

    判定顺序：
      1. 不在任意 Ground(9) AABB 内 -> 不可走。**Ground 层非空才启用**；
         Ground 缺失时退而用 level_bounds（LevelBounds.m_bounds）兜底裁剪
         —— 两个都缺失才放行。放行是对「合成帧/旧帧无 terrain」的降级，
         不能在有 level_bounds 时继续放行（1-6 掉出范围的根因）。
      2. 在 KillPlane(30) 内 -> 不可走
      3. 在任意 hazard box 内 -> 不可走（落火，§5.7）
      4. 在 Walls/TableBlock/CookingStationBlock/PlateStationBlock 内 -> 不可走
      5. 在 occupancy_cells 里**且不是传送带格** -> 不可走（运行时占用，
         §5.3 第 5 步）
      6. 否则可走

    第 5 步的传送带豁免是旧 `make_walkable` 第 4 步的等价物（"传送带 →
    可走"）。ConveyorStation 自身占住它所在的格，于是 m_gridOccupancy
    把全部传送带格都报成占用，而角色实际能走上去。漏掉这条豁免在寿司关
    是致命的：两排传送带是左右两半唯一通路，全标不可走后 P2 从出生点只
    够得到 6 格，所有 task 的 Approach 第一拍 FAILURE、整局零按键
    （实机 trace-20261016-175943，334 拍一个键都没发）。

    只豁免运行时占用，不豁免地形/墙/落火——传送带上着火仍然不能走。
    """
    ground = terrain_by_layer.get(LAYER_GROUND) or []
    killplane = terrain_by_layer.get(LAYER_KILLPLANE) or []
    blocking = []  # type: List[Tuple[float, float, float, float]]
    for layer in BLOCKING_LAYERS:
        blocking.extend(terrain_by_layer.get(layer) or ())
    hazard_boxes = list(hazard_boxes or ())
    belts = set(belt_cells or ())
    level_bounds = list(level_bounds or ())

    def is_walkable(gx, gz):
        # type: (int, int) -> bool
        x, z = mapping.to_world(gx, gz)
        if ground:
            if not _point_in_any_box(x, z, ground):
                return False
        elif level_bounds:
            # Ground 层缺失时的兜底：格中心不在任意 LevelBounds AABB 内即
            # 不可走。只在 Ground 为空时启用——Ground 更细（多块分离地板/
            # 篮子），level_bounds 是单个粗 AABB，有 Ground 时不用它。
            if not _point_in_any_box(x, z, level_bounds):
                return False
        if _point_in_any_box(x, z, killplane):
            return False
        if _point_in_any_box(x, z, hazard_boxes):
            return False
        # 阻挡层（墙 / 台面 / 灶台 / 盘站）用格中心点判：站点/墙的 collider
        # AABB 会朝邻格溢出一点点，若按格子区域相交判，会把贴着墙站立的
        # 可走地板整排误判成不可走（Balloon_1_5 上层地板、上左走廊被封死，
        # trace-20261017-193620）。点判只拦「格中心真的被占」的格子。
        if _point_in_any_box(x, z, blocking, margin=CHEF_CLEARANCE):
            return False
        if (gx, gz) in occupancy_cells and (gx, gz) not in belts:
            return False
        return True

    return is_walkable


def make_edge_blocker(terrain_by_layer, mapping):
    # type: (Dict[int, List[Tuple[float, float, float, float]]], GridMapping) -> Callable[[Tuple[int, int], Tuple[int, int]], bool]
    """返回 edge_blocked(cur, nxt) -> bool：两格之间是否被薄墙挡住。

    薄墙 = collider AABB **严格夹在两格中心之间**（比一格窄、盖不住任何格
    中心）。点判（格中心在 box 内）会漏掉它，但角色物理上跨不过去，实机
    表现为朝凸起墙冲、顶墙 no_progress（Balloon_1_5 两网格边缘的凸起墙，
    trace-20261017-191650/193620）。

    站点/厚块不在这里处理：它们盖住自己那格的中心，点判已把该格判不可走，
    距离场不会往里扩。「严格夹在中心之间」这个条件天然排除 ≥1 格的厚块。

    判定只关心**通道中线**：水平移动看墙是否夹在两 x 中心之间且盖住本行 z；
    垂直移动看墙是否夹在两 z 中心之间且盖住本列 x。不按格子整区域判——
    站点 collider 会朝邻格溢出一点点，按区域判会把贴墙站立的可走地板整排
    误杀（同 is_walkable 点判的教训）。
    """
    blocking = []  # type: List[Tuple[float, float, float, float]]
    for layer in BLOCKING_LAYERS:
        blocking.extend(terrain_by_layer.get(layer) or ())

    def edge_blocked(cur, nxt):
        # type: (Tuple[int, int], Tuple[int, int]) -> bool
        x0, z0 = mapping.to_world(cur[0], cur[1])
        x1, z1 = mapping.to_world(nxt[0], nxt[1])
        if cur[0] != nxt[0]:
            # 水平移动：墙夹在左右两格中心之间，且高度盖住本行中心 z
            lo, hi = (x0, x1) if x0 < x1 else (x1, x0)
            for minx, maxx, minz, maxz in blocking:
                if lo < minx and maxx < hi and minz <= z0 <= maxz:
                    return True
        else:
            # 垂直移动：墙夹在上下两格中心之间，且横向盖住本列中心 x
            lo, hi = (z0, z1) if z0 < z1 else (z1, z0)
            for minx, maxx, minz, maxz in blocking:
                if lo < minz and maxz < hi and minx <= x0 <= maxx:
                    return True
        return False

    return edge_blocked


def parse_hazards(hazards_list):
    # type: (Optional[List[Dict]]) -> List[Dict]
    """落火列表（spec §4.7/§6）：{grid, x, z, ttl}。ttl<=0 或缺坐标丢弃。"""
    out = []  # type: List[Dict]
    for h in hazards_list or ():
        if not isinstance(h, dict):
            continue
        x, z, ttl = h.get("x"), h.get("z"), h.get("ttl")
        if x is None or z is None or ttl is None:
            continue
        try:
            ttl_f = float(ttl)
        except (TypeError, ValueError):
            continue
        if ttl_f <= 0:
            continue
        out.append({"grid": h.get("grid"), "x": float(x), "z": float(z), "ttl": ttl_f})
    return out


def hazard_boxes(hazards, cell=1.2):
    # type: (List[Dict], float) -> List[Tuple[float, float, float, float]]
    """每个 hazard 给一个边长 = cell 的正方形框（工程假设，见本任务说明）。"""
    half = cell / 2.0
    return [(h["x"] - half, h["x"] + half, h["z"] - half, h["z"] + half)
            for h in hazards]


def parse_wind_zones(wind_list):
    # type: (Optional[List[Dict]]) -> List[Dict]
    """风区列表（插件 wind_zones 字段：WindVolume.m_volumeBounds + 风向风速）。

    返回 [{minx, maxx, minz, maxz, vx, vz}, ...]。vx/vz 是风向风速（非零 =
    正在起风）。供 note_motion_failure 在「被风吹得动不了」时豁免资源拉黑。
    """
    out = []  # type: List[Dict]
    for wz in wind_list or ():
        if not isinstance(wz, dict):
            continue
        minx, maxx = wz.get("minx"), wz.get("maxx")
        minz, maxz = wz.get("minz"), wz.get("maxz")
        vx, vz = wz.get("vx"), wz.get("vz")
        if minx is None or maxx is None or minz is None or maxz is None:
            continue
        try:
            out.append({
                "minx": float(minx), "maxx": float(maxx),
                "minz": float(minz), "maxz": float(maxz),
                "vx": float(vx or 0), "vz": float(vz or 0),
            })
        except (TypeError, ValueError):
            continue
    return out


def parse_belts(belts_list):
    # type: (Optional[List[Dict]]) -> Tuple[Set[Tuple[int, int]], Dict[Tuple[int, int], Tuple[int, int]]]
    """传送带格集合与方向（spec §5.4）：方向来自 GetSurfaceVelocity() 归一化。

    取代 occupancy.BELTS 关卡硬编码表。vx/vz 都是 0（速度为零，异常数据）
    时该格仍算传送带（belt_cells 决定"是不是传送带"，不决定推力方向），
    但不记方向——navigation 的逆向代价判定对无方向格天然不生效。
    """
    cells = set()  # type: Set[Tuple[int, int]]
    dirs = {}      # type: Dict[Tuple[int, int], Tuple[int, int]]
    for b in belts_list or ():
        if not isinstance(b, dict):
            continue
        gx, gz = b.get("gx"), b.get("gz")
        if gx is None or gz is None:
            continue
        cell = (int(gx), int(gz))
        cells.add(cell)
        vx, vz = b.get("vx"), b.get("vz")
        if vx is None or vz is None:
            continue
        try:
            vx_f, vz_f = float(vx), float(vz)
        except (TypeError, ValueError):
            continue
        if vx_f == 0.0 and vz_f == 0.0:
            continue
        # 归一化到单位方向（±1,0)/(0,±1)：当前只有轴向传送带样本（§4.5），
        # 斜向传送带会给出非整数，调用方（navigation）按符号取整。
        dirs[cell] = (
            (1 if vx_f > 0 else -1) if vx_f != 0 else 0,
            (1 if vz_f > 0 else -1) if vz_f != 0 else 0,
        )
    return cells, dirs


def station_block_cells(station, terrain_by_layer, mapping):
    # type: (Optional[Dict], Dict[int, List[Tuple[float, float, float, float]]], GridMapping) -> Set[Tuple[int, int]]
    """站点在 terrain 阻挡层里点判覆盖的格（多格站点返回真实占地）。

    出餐口（plate_station）是特殊的多格站点：交互格 gx/gz 只有 1 格，但
    它的 PLATEBLOCK(20) 阻挡 box 是 1×2 或 2×1（2 格）。若 approach 只用
    交互格单格的四邻，会漏掉另一半占地旁边的可交互站位（Sushi_1_3 的
    (-5,-3)：角色已站在能送餐的位置，却因目标不含它而多挪一格）。

    不能用 station 上报的 minx/maxx/minz/maxz 反算占地——那个 collider
    AABB 会朝一侧溢出半格（报成 2×2），真实阻挡层 box 只有 2 格。这里用
    站点 x/z 匹配 PLATEBLOCK 层的 box，再点判该 box 覆盖的格。

    其余站点阻挡格 == gx/gz 单格，直接退回，不查 terrain。
    """
    if not isinstance(station, dict):
        return set()
    if station.get("t") != "plate_station":
        gx, gz = station.get("gx"), station.get("gz")
        return {(int(gx), int(gz))} if gx is not None and gz is not None else set()

    sx, sz = station.get("x"), station.get("z")
    if sx is None or sz is None:
        gx, gz = station.get("gx"), station.get("gz")
        return {(int(gx), int(gz))} if gx is not None and gz is not None else set()

    for box in terrain_by_layer.get(LAYER_PLATEBLOCK) or ():
        minx, maxx, minz, maxz = box
        if not (minx - _POINT_EPS <= sx <= maxx + _POINT_EPS
                and minz - _POINT_EPS <= sz <= maxz + _POINT_EPS):
            continue
        cells = set()  # type: Set[Tuple[int, int]]
        gx_lo = int(math.floor((minx - mapping.origin_x) / mapping.cell)) - 1
        gx_hi = int(math.ceil((maxx - mapping.origin_x) / mapping.cell)) + 1
        gz_lo = int(math.floor((minz - mapping.origin_z) / mapping.cell_z)) - 1
        gz_hi = int(math.ceil((maxz - mapping.origin_z) / mapping.cell_z)) + 1
        for gx in range(gx_lo, gx_hi + 1):
            for gz in range(gz_lo, gz_hi + 1):
                x, z = mapping.to_world(gx, gz)
                if (minx - _POINT_EPS <= x <= maxx + _POINT_EPS
                        and minz - _POINT_EPS <= z <= maxz + _POINT_EPS):
                    cells.add((gx, gz))
        if cells:
            return cells

    # 兜底：匹配不到 box（旧帧无 terrain），退回交互格单格
    gx, gz = station.get("gx"), station.get("gz")
    return {(int(gx), int(gz))} if gx is not None and gz is not None else set()

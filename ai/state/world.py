"""状态读取与世界模型：socket 收帧 + 快照解析 + 派生数据装配。

从 oc/ai/world.py 移植。两处结构性改动：
  1. 分帧逻辑抽成模块级 split_frames 纯函数。旧 read() 把 socket 读取
     与缓冲切分揉在一个 while 里，要测分帧就得起真 socket。
  2. 可走性/去重/寻路都不在这里实现——分别委托 occupancy 与
     navigation（Plan 1 已交付）。本模块只负责"把快照变成它们的入参"
     并缓存结果。
"""
import json
import math
import select
import socket
from collections import Counter
from typing import Callable, Dict, List, Optional, Set, Tuple

from ai.space import occupancy
from ai.space.geometry import GridMapping, GridSet

_FRAME_SEP = b"\n"
_RECV_SIZE = 65536


def split_frames(buf):
    # type: (bytes) -> Tuple[List[bytes], bytes]
    """把字节缓冲按 \\n 切成完整帧，返回 (完整行列表, 剩余缓冲)。

    一次 recv 既可能带回半帧，也可能带回多帧——两种都要正确处理：
    半帧留在剩余缓冲里等补齐，多帧全部切出（漏切会让缓冲无限增长）。
    空行丢弃，免得下游 json.loads 抛。
    """
    if _FRAME_SEP not in buf:
        return ([], buf)
    *complete, rest = buf.split(_FRAME_SEP)
    return ([line for line in complete if line.strip()], rest)


class StateClient(object):
    """按行读取插件推送的 JSON 帧。

    connect 参数用于注入假 socket：分帧的边界条件（半帧、多帧、EOF）
    在真 socket 上无法稳定复现——内核什么时候切包不由测试决定。
    """

    def __init__(self, host="127.0.0.1", port=27411, connect=None):
        # type: (str, int, Optional[Callable[[], object]]) -> None
        if connect is None:
            self.s = socket.create_connection((host, port), timeout=10)
        else:
            self.s = connect()
        self.buf = b""
        self._pending = []  # type: List[bytes]

    def _next_frame(self):
        # type: () -> Optional[Dict]
        """从缓冲取下一个合法帧；缓冲空返回 None。"""
        while self._pending:
            line = self._pending.pop(0)
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            # 下游一律按 dict 访问 frame["..."]，标量/数组必须拦掉
            if isinstance(obj, dict):
                return obj
        return None

    def read(self):
        # type: () -> Dict
        """返回下一个可用帧（阻塞）。坏帧跳过，socket 关闭时抛 EOFError。

        跳过而非抛出：录制与回放都见过截断行，运行时同样可能遇到。
        一行坏 JSON 不该终结整个会话。

        另注：__init__ 的 timeout=10 作用在已连接的 socket 上，对端静默
        超过 10s（插件暂停、游戏卡住）时 recv 抛 socket.timeout（OSError
        子类），而非 EOFError。调用方若要兜住"连接还在但没数据"，需同时
        捕获 socket.timeout。
        """
        while True:
            obj = self._next_frame()
            if obj is not None:
                return obj
            data = self.s.recv(_RECV_SIZE)
            if not data:
                raise EOFError("socket closed")
            self.buf += data
            # extend 而非覆写：当前内层循环只在 _pending 空时才退出，覆写等价；
            # 但日后若在内层加 break，覆写会悄悄丢掉未消费的帧，extend 不会。
            new, self.buf = split_frames(self.buf)
            self._pending.extend(new)

    def read_latest(self):
        # type: () -> Optional[Dict]
        """非阻塞排空当前积压，返回最新合法完整帧；无新帧则 None。

        主循环 20 Hz 用这个：插件 5 Hz 推帧，中间 4 拍复用同一帧重 tick；
        否则阻塞的 read() 会把主循环拖成 5 Hz（实机踩坑）。
        """
        latest = None
        # 限制单拍读取量，防止异常的连续洪流饿死按键调度。
        for _ in range(64):
            while self._pending:
                obj = self._next_frame()
                if obj is not None:
                    latest = obj
            readable, _w, _x = select.select([self.s], [], [], 0)
            if not readable:
                return latest
            data = self.s.recv(_RECV_SIZE)
            if not data:
                raise EOFError("socket closed")
            self.buf += data
            new, self.buf = split_frames(self.buf)
            self._pending.extend(new)
        while self._pending:
            obj = self._next_frame()
            if obj is not None:
                latest = obj
        return latest

    def close(self):
        # type: () -> None
        self.s.close()


def _dicts(raw):
    # type: (Optional[List]) -> List[Dict]
    """只保留列表里的 dict 条目。

    下游谓词层一律按 dict 访问 entry["..."]，标量混进来会在很远的
    地方炸，且堆栈看不出是数据问题。在入口拦掉。
    """
    return [e for e in (raw or []) if isinstance(e, dict)]


class World(object):
    """一帧快照的只读视图：解析 + 派生数据装配。

    构造函数只做三件事：解析快照列表、建 GridMapping、调 occupancy
    算派生数据并缓存。可走性与去重的实现都在 occupancy，寻路在
    navigation——本类不自己实现算法。

    与 oc/ai/world.py 的差异：
      - GRID_OX/GRID_OZ 类常量换成 GridMapping（从 grid.size 读格宽、
        用站点反解原点，换关卡免改代码）。
      - walkable 是 occupancy.make_walkable 返回的闭包，构造时绑定一次。
      - 寻路方法不在这里；调用方用 navigation.distance_field(w.walkable, ...)。
    """

    def __init__(self, state):
        # type: (Dict) -> None
        state = state or {}
        self.raw = state
        self.phase = state.get("phase")
        self.level = state.get("level")
        # 用 `is True`：插件漏发该字段时是 None，不能当成"已暂停"
        self.paused = state.get("paused") is True
        # 插件不发 tick 字段（实测帧无 tick），缺失或为 None 时退化为 0。
        self.tick = state.get("tick") or 0

        self.orders = _dicts(state.get("orders"))
        self.chefs = _dicts(state.get("chefs"))
        self.stations = _dicts(state.get("stations"))
        self.recipes = _dicts(state.get("recipes"))
        # plates[] 混合散落/就位盘与手持盘（插件给手持盘打 held_by）。散落语义
        # （clean_plate_exists / placed_plates / _emit_plate …）读 self.plates，
        # 只含非手持；按 id 找盘（plate_by_id / 盘匹配 / 送餐）必须能看见 P2 自己
        # 端着的盘，走 self.all_plates。旧帧无 held_by 时全部归非手持，保持原行为。
        deduped_plates = _dicts(state.get("plates"))
        self.plates = [p for p in deduped_plates if not p.get("held_by")]
        self.held_plates = [p for p in deduped_plates if p.get("held_by")]
        self.workstations = _dicts(state.get("workstations"))

        # 这三类插件会报重复，构造时就去重（各自规则不同，见 occupancy）
        self.cookers = occupancy.dedupe_cookers(state.get("cookers"))
        deduped_items = occupancy.dedupe_items(state.get("items"))
        # items[] 现在混合散落物和手持物（插件给手持物打 held_by）。散落语义的
        # 谓词（find_ingredient / loose_count / cooked_sources …）读 self.items，
        # 必须只含散落；手持物的内容状态由 self.held_items 供 held_cookware_state
        # 读（手持锅的 has_contents/cooked 只有这一处来源）。旧帧无 held_by 时
        # 全部归入散落，保持原行为。
        self.items = [it for it in deduped_items if not it.get("held_by")]
        self.held_items = [it for it in deduped_items if it.get("held_by")]
        deduped_ext = occupancy.dedupe_extinguishers(state.get("extinguishers"))
        # 灭火器手持后也会留在 extinguishers[]（坐标跟厨师），按 held_by 拆开：
        # 灭火 task 只认非手持（挂墙/地上），手持灭火器不能当可用的取。
        self.extinguishers = [e for e in deduped_ext if not e.get("held_by")]
        self.held_extinguishers = [e for e in deduped_ext if e.get("held_by")]

        # 注意：self.stations 现在混合了全部网格的站点（Task 7 Step 11 起
        # stations 不再按单一网格缓存）。GridMapping 的众数校准如果对全部站点
        # 取，移动中的气球篮站点会把 origin_z 带漂（trace-20261017-215633）。
        # 所以这里只把**主网格**的站点喂给校准：主网格由 stations[].grid 众数
        # 判定（_majority_grid），篮子等附属网格的站点不参与，原点就不随篮子
        # 漂移。注意不能用 state["grids"] 的 ox/oz 当原点——插件读的是
        # QuadGridManager.transform.position，在 Sushi_1_4 里是 (12,-6)，而真
        # 原点在校准里是 (14.4,-6)，差 2 格（trace-20261017-225023 Sushi 段）。
        primary_grid = self._majority_grid()
        if primary_grid is not None:
            primary_stations = [s for s in self.stations
                                if str(s.get("grid") or "") == primary_grid]
        else:
            primary_stations = self.stations
        self.mapping = GridMapping.from_grid(
            state.get("grid"), primary_stations)

        parsed = occupancy.parse_node_map(
            (state.get("grid") or {}).get("nodeMap"))
        if parsed is None:
            self._node_map, self._nm_rows, self._nm_cols = None, 0, 0
        else:
            self._node_map, self._nm_rows, self._nm_cols = parsed

        # 多网格坐标集合（spec §5.1）：换关卡/换帧直接重建，不反解。
        self.grids = GridSet.from_frame(state.get("grids"))

        # station_blocked 不再喂给 walkable（新判定走 terrain 的
        # BLOCKING_LAYERS），但仍作为独立属性保留——调用方（如
        # test_world 的空快照用例）仍按旧习惯读它，退化为空集不影响
        # 新判定路径。
        self.station_blocked = occupancy.station_blocked_cells(
            self.stations, self.mapping)

        # 可走性改用层号地形 + 运行时占用（spec §5.3），取代
        # playable_bounds + floor 扩 bounds + nodeMap。
        self.terrain = occupancy.parse_terrain(state.get("terrain"))
        level_bounds = occupancy.parse_level_bounds(state.get("level_bounds"))
        self.hazards = occupancy.parse_hazards(state.get("hazards"))
        self.wind_zones = occupancy.parse_wind_zones(state.get("wind_zones"))
        hazard_boxes = occupancy.hazard_boxes(self.hazards, cell=self.mapping.cell)
        occ_cells = self._occupancy_cells(state.get("occupancy"))
        # 写死（1-6）：出餐口（plate_station）grid=None，碰撞 footprint 没进
        # occupancy；且碰撞盒北缘（maxz≈3.6）伸进北邻那排，角色半径过不去 →
        # 把出餐口北邻一排标成不可走，避免寻路北上钻墙后死路（1-6 实机两处卡死）。
        if str(self.level or "").startswith("1_6_Dynamic"):
            for st in self.stations:
                if not isinstance(st, dict) or st.get("t") != "plate_station":
                    continue
                x, z = st.get("x"), st.get("z")
                if x is None or z is None:
                    continue
                cell = self.to_grid(x, z)
                if cell is None:
                    continue
                for dx in (0, 1):      # 出餐口约 2 格宽
                    for dz in (1,):    # 只挡北邻一排（墙后死路）
                        occ_cells.add((cell[0] + dx, cell[1] + dz))
        self.belt_cells, self.belt_dir = occupancy.parse_belts(state.get("belts"))

        # belt_cells 必须在 walkable 之前解析好：传送带格要从运行时占用里
        # 豁免（ConveyorStation 自己占住那格，但角色能走上去）。
        self.walkable = occupancy.make_walkable_from_terrain(
            self.terrain, hazard_boxes, occ_cells, self.mapping,
            belt_cells=self.belt_cells, level_bounds=level_bounds)

        # 薄墙拦「边」不拦「格」：凸起在网格边缘的 collider 夹在两格之间、
        # 盖不住任何格中心，点判漏掉它，交给 distance_field 的边检查。
        self.edge_blocked = occupancy.make_edge_blocker(self.terrain, self.mapping)

    @property
    def all_plates(self):
        # type: () -> List[Dict]
        """全量盘（非手持 + 手持）。按 id 找盘 / 盘匹配 / 送餐用。

        用 property 动态拼：测试里常直接覆盖 world.plates，构造时快照会失同步。
        """
        return self.plates + self.held_plates

    def _occupancy_cells(self, occupancy_list):
        # type: (Optional[List[Dict]]) -> Set[Tuple[int, int]]
        """把 occupancy[] 按主网格坐标并成一个集合。

        §4.1 的多网格事实在这里被简化：当前只有主网格（GridManager）的
        occupancy 用于 walkable 判定，因为 World.to_grid/walkable 仍是
        单网格坐标系（主网格），非主网格的占用格暂不参与——这与现状
        一致（旧实现从未处理过非主网格占用），留给 Task 12 的跨网格
        寻路一并处理（见该任务的"已知限制"）。
        """
        out = set()  # type: Set[Tuple[int, int]]
        primary = self._primary_grid_name(occupancy_list)
        for entry in occupancy_list or ():
            if not isinstance(entry, dict):
                continue
            if primary is not None and entry.get("grid") != primary:
                continue
            for cell in entry.get("cells") or ():
                if isinstance(cell, (list, tuple)) and len(cell) == 2:
                    out.add((int(cell[0]), int(cell[1])))
        return out

    def _majority_grid(self):
        # type: () -> Optional[str]
        """stations[].grid 出现次数最多的网格名（静态计数，不随篮子漂移）。"""
        votes = Counter()  # type: Counter
        for st in self.stations:
            g = st.get("grid")
            if g:
                votes[str(g)] += 1
        return votes.most_common(1)[0][0] if votes else None

    def _primary_grid_name(self, occupancy_list):
        # type: (Optional[List[Dict]]) -> Optional[str]
        """主网格名：stations[].grid 的静态众数，不跟 occupancy 顺序走。

        为什么不用 GridMapping 的校准原点反查：校准对全部站点取众数，移动
        中的气球篮站点会把 origin_z 众数带漂（trace-20261017-215633 里
        origin_z 随篮子下移从 -2.4 一路漂到 -4.5），用原点反查网格在篮子
        移动时失配。按 grid 字段计数才是稳定的：主厨房站点恒多于附属篮子
        站点，且不随篮子漂移。
        """
        primary = self._majority_grid()
        if primary is not None:
            return primary
        # 兜底：无 stations grid 信息（旧 fixture）时退回 occupancy 第一项。
        for entry in occupancy_list or ():
            if isinstance(entry, dict) and entry.get("grid"):
                return entry.get("grid")
        return None

    def to_grid(self, x, z):
        # type: (float, float) -> Tuple[int, int]
        return self.mapping.to_grid(x, z)

    def to_world(self, gx, gz):
        # type: (int, int) -> Tuple[float, float]
        return self.mapping.to_world(gx, gz)

    def balloon_ground_box(self):
        # type: () -> Optional[Tuple[float, float, float, float]]
        """气球篮（移动平台）当前的地面 AABB。

        terrain 的 Ground(9) 层里，主地板 minx ≈ 3.6，篮子地板 minx ≈ 16.0。
        用 minx >= 15 把篮子地板挑出来（Balloon_1_5 的几何常量）。篮子移动
        时这个盒子的 minz/maxz 会跟着漂，供跨帧移动检测与「移动中避让」用。
        无篮子地板（其它关卡）返回 None。
        """
        terrain = occupancy.parse_terrain(self.raw.get("terrain"))
        ground = terrain.get(occupancy.LAYER_GROUND) or []
        for box in ground:
            if box[0] >= 15.0:
                return box
        return None

    def balloon_cells(self):
        # type: () -> Set[Tuple[int, int]]
        """气球篮地面投影到主网格的格子集合（点判：格中心落在盒子内）。

        篮子移动时寻路应避开这些格，防止角色在跨网格瞬间踩空掉落
        （trace-20261017-215633 t=1121 的 respawn）。
        """
        box = self.balloon_ground_box()
        if box is None:
            return set()
        minx, maxx, minz, maxz = box
        gx_lo = int(math.floor((minx - self.mapping.origin_x) / self.mapping.cell))
        gx_hi = int(math.ceil((maxx - self.mapping.origin_x) / self.mapping.cell))
        gz_lo = int(math.floor((minz - self.mapping.origin_z) / self.mapping.cell_z))
        gz_hi = int(math.ceil((maxz - self.mapping.origin_z) / self.mapping.cell_z))
        cells = set()  # type: Set[Tuple[int, int]]
        for gx in range(gx_lo, gx_hi + 1):
            for gz in range(gz_lo, gz_hi + 1):
                x, z = self.mapping.to_world(gx, gz)
                if minx <= x <= maxx and minz <= z <= maxz:
                    cells.add((gx, gz))
        return cells

    def chef(self, name):
        # type: (str) -> Optional[Dict]
        for c in self.chefs:
            if c.get("name") == name:
                return c

    def chef_in_wind(self, chef):
        # type: (Optional[Dict]) -> bool
        """chef 是否在「正在起风」的风区里（风速非零）。

        供 note_motion_failure 用：被风吹得动不了导致的 no_progress /
        observation_timeout 不该拉黑资源（trace：端番茄时被风推、位置无法
        移动，把料箱资源判死刑）。
        """
        x, z = (chef or {}).get("x"), (chef or {}).get("z")
        if x is None or z is None:
            return False
        for wz in self.wind_zones:
            vx, vz = wz.get("vx", 0), wz.get("vz", 0)
            try:
                if abs(float(vx)) + abs(float(vz)) <= 0:
                    continue
                if float(wz["minx"]) <= x <= float(wz["maxx"]) \
                        and float(wz["minz"]) <= z <= float(wz["maxz"]):
                    return True
            except (TypeError, ValueError, KeyError):
                continue
        return False
        return None

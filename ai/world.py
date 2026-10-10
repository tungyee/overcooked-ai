"""状态读取与世界模型：socket 收帧 + 快照解析 + 派生数据装配。

从 oc/ai/world.py 移植。两处结构性改动：
  1. 分帧逻辑抽成模块级 split_frames 纯函数。旧 read() 把 socket 读取
     与缓冲切分揉在一个 while 里，要测分帧就得起真 socket。
  2. 可走性/去重/寻路都不在这里实现——分别委托 occupancy 与
     navigation（Plan 1 已交付）。本模块只负责"把快照变成它们的入参"
     并缓存结果。
"""
import json
import select
import socket
from typing import Callable, Dict, List, Optional, Tuple

from ai import occupancy
from ai.geometry import GridMapping

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
        # p1model 用 world.tick 记 last_evidence_tick。插件不发该字段
        # （实测帧无 tick），缺失或为 None 时退化为 0，不能让 .tick 抛。
        self.tick = state.get("tick") or 0

        self.orders = _dicts(state.get("orders"))
        self.chefs = _dicts(state.get("chefs"))
        self.stations = _dicts(state.get("stations"))
        self.recipes = _dicts(state.get("recipes"))
        self.plates = _dicts(state.get("plates"))
        self.workstations = _dicts(state.get("workstations"))

        # 这三类插件会报重复，构造时就去重（各自规则不同，见 occupancy）
        self.cookers = occupancy.dedupe_cookers(state.get("cookers"))
        self.items = occupancy.dedupe_items(state.get("items"))
        self.extinguishers = occupancy.dedupe_extinguishers(
            state.get("extinguishers"))

        self.mapping = GridMapping.from_grid(state.get("grid"), self.stations)

        parsed = occupancy.parse_node_map(
            (state.get("grid") or {}).get("nodeMap"))
        if parsed is None:
            self._node_map, self._nm_rows, self._nm_cols = None, 0, 0
        else:
            self._node_map, self._nm_rows, self._nm_cols = parsed

        self.bounds = occupancy.playable_bounds(self.stations)
        self.station_blocked = occupancy.station_blocked_cells(
            self.stations, self.mapping)
        self.belt_cells, self.belt_dir = occupancy.belt_cells_and_dirs(
            self.level)

        self.walkable = occupancy.make_walkable(
            self._node_map, self._nm_rows, self._nm_cols,
            self.bounds, self.station_blocked, self.belt_cells,
            self.mapping.off_x, self.mapping.off_z,
        )

    def to_grid(self, x, z):
        # type: (float, float) -> Tuple[int, int]
        return self.mapping.to_grid(x, z)

    def to_world(self, gx, gz):
        # type: (int, int) -> Tuple[float, float]
        return self.mapping.to_world(gx, gz)

    def chef(self, name):
        # type: (str) -> Optional[Dict]
        for c in self.chefs:
            if c.get("name") == name:
                return c
        return None

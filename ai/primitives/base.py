"""原语基类与解析工具。

ctx 契约（整个执行层共用）：
    ctx = {
        "w":     World,          # 本拍快照
        "chef":  Dict,           # P2 的 chefs[] 条目
        "hands": Hands,          # 按键出口（唯一）
        "bb":    Blackboard,     # 只读 claims，可写 scratch（spec §4.5）
        "field": Dict,           # 从 chef 格算出的 distance_field
        "soft_cost": Dict,       # 可选，建场 / 回溯共用的 P1 避让代价
        "book":  RecipeBook,     # 配方视图
        "tick":  int,            # 当前拍号
        "frame_seq": int,        # 可选，真实收帧编号；不能用 tick 代替
        "frame_received_at": float,  # 可选，单调时钟的收帧时刻
        "now": float,            # 可选，本次控制的单调时钟时刻
    }

target 一律可以是 dict（静态）或 ctx -> Optional[Dict] 的可调用。优先传
可调用：树是跨拍复用的对象（spec §7.6"task 身份与上拍相同 → 复用同一棵树"），
构造时抓的 dict 第二拍就是过期数据——锅被 P1 端走、盘被端走、灭火器被捡走
都会让它失真。解析不出来 → FAILURE 交还 Planner（spec §7.6 纪律 1）。
"""
import time
from typing import Any, Callable, Dict, Optional, Set, Tuple

from ai.query import predicates as P
from ai.exec.bt import Node, Status
from ai.exec.commands import build_command

# 方向键 → 网格位移。exec 委托路径里传送带顶向（hold_dir）需要它；
# 原 ai.output.hands 已随键盘注入轨删除，这两个纯函数挪到这里。
DIR2D = {"left": (-1, 0), "right": (1, 0), "up": (0, 1), "down": (0, -1)}


def counter_dir(belt_dir, cell):
    # type: (Dict[Tuple[int, int], Tuple[int, int]], Tuple[int, int]) -> Optional[str]
    """站在传送带上要顶住的反向键；不在带上则 None。"""
    bd = (belt_dir or {}).get(cell)
    if not bd:
        return None
    for name, vec in DIR2D.items():
        if vec[0] == -bd[0] and vec[1] == -bd[1]:
            return name
    return None

# "还没看过手里有什么"的哨兵。不能用 None 或 ""——held_name 对空手正是
# 返回 ""，两者混用会让"首次 tick"与"手本来就空"无法区分。
_UNSET = object()


def resolve(target, ctx):
    # type: (Any, Dict) -> Optional[Dict]
    """把 target 解析成本拍的 dict。可调用的每拍重算，不缓存。"""
    if target is None:
        return None
    if callable(target):
        got = target(ctx)
    else:
        got = target
    return _surface_hint(got, ctx) if isinstance(got, dict) and got else None


def _surface_hint(target, ctx):
    # type: (Dict, Dict) -> Dict
    """台面/沥水架/回盘台上的盘/散物借用容器交互坐标，保留物品自身身份与世界位置。

    物品没有 GridLocation 不代表在地面。将它一律跳过 Face 会在 1-4
    唯一可用站位上校验失败，然后规划器反复重选。只借同位置的真实静态
    台面，不借远处/相邻台面，也不改原始快照或资源认领键。

    plate_return（沥水架 DryingPart / 回盘台 workstation_plate_return）是
    功能站、不进 BuildStationMap，其上的盘 grid 为 null；不把 plate_return
    算进去，非主网格的沥水架（1-5 气球篮）上的盘会回退到主网格算句柄
    坐标，句柄对不上拾取失败（实机 1-5 拿不起沥水架上的盘）。
    """
    if (target.get('held_by') or (target.get('gx') is not None and target.get('gz') is not None)
            or not any(k in target for k in ('name', 'detail', 'contents'))):
        return target
    w = ctx.get('w')
    x, z = target.get('x'), target.get('z')
    if w is None or not isinstance(x, (int, float)) or not isinstance(z, (int, float)):
        return target
    cell = getattr(getattr(w, 'mapping', None), 'cell', 1.2)
    best, best_dist = None, (cell / 4.) ** 2
    for station in getattr(w, 'stations', ()):
        if station.get('t') not in ('counter', 'chopping', 'cooking', 'cooker', 'plate_return'):
            continue
        if station.get('gx') is None or station.get('gz') is None:
            continue
        sx, sz = station.get('x'), station.get('z')
        if not isinstance(sx, (int, float)) or not isinstance(sz, (int, float)):
            continue
        distance = (sx - x) ** 2 + (sz - z) ** 2
        if distance <= best_dist:
            best, best_dist = station, distance
    if best is None:
        return target
    return dict(target, interaction_surface=best.get('n'), interaction_grid=best.get('grid'),
                interaction_gx=best['gx'], interaction_gz=best['gz'])


def _blocked_cells(ctx, resource_id=None):
    # type: (Dict, Any) -> Set[Tuple[int, int]]
    """脱困拉黑的格子（MarkBlocked 写进 scratch，这里读）。

    TTL 按拍计：值是失效拍号，当前拍 >= 它就不再算拉黑。

    `resource_id` 非 None 时，额外并入 `scratch.bad_pairs` 里属于该资源的
    格 —— 那是「这一格对这个目标用不了，但对别的目标还行」的二元粒度
    （AlignToCell 写、Approach 读，理由见 blackboard.StuckScratch.bad_pairs）。
    不传时逐字等价于旧行为，既有调用点无需改。
    """
    bb = ctx.get("bb")
    scratch = getattr(bb, "scratch", None)
    tick = ctx.get("tick") or 0
    out = set()  # type: Set[Tuple[int, int]]

    bad = getattr(scratch, "bad_cells", None)
    if bad:
        out |= set(cell for cell, expire in bad.items() if expire > tick)

    if resource_id is not None:
        pairs = getattr(scratch, "bad_pairs", None)
        if pairs:
            out |= set(cell for (res, cell), expire in pairs.items()
                       if res == resource_id and expire > tick)
    return out


def target_cell(target, ctx):
    # type: (Optional[Dict], Dict) -> Optional[Tuple[int, int]]
    """目标所在格：优先用世界坐标 x/z 经 World.to_grid 投影，缺失时才退回
    站点自带的 gx/gz（spec §7 的发现：站点的 gx/gz 是它所在网格的局部
    索引，多网格下与调用方坐标系——通常是主网格——不是一回事；x/z 永远
    是全局世界坐标，经 to_grid 投影才能跟 walkable/distance_field 的坐标
    系统对齐）。

    退回 gx/gz 只用于缺 x/z 的旧 fixture / 合成测试数据，不是真实站点的
    主路径——AppendStations 对所有真实站点都发 x/z。
    """
    if not target:
        return None
    x, z = target.get("x"), target.get("z")
    if x is not None and z is not None:
        return ctx["w"].to_grid(x, z)
    gx, gz = target.get("gx"), target.get("gz")
    if gx is not None and gz is not None:
        return (int(round(gx)), int(round(gz)))
    return None


def target_identity(target):
    # type: (Optional[Dict]) -> Any
    """目标的身份（spec §5.2/§7）：优先用 (grid, n)，没有 n 时退化为
    None（调用方据此退回按坐标比较——拼装格这类无实例名的目标仍只能
    按坐标判断"换了没有"，这是合理的，因为它们本来就没有稳定身份）。
    """
    if not target:
        return None
    n = target.get("n")
    if not n:
        return None
    return (target.get("grid"), str(n))


def target_grid_callbacks(world, target):
    # type: (Any, Optional[Dict]) -> Tuple[Any, Any, float]
    """target 所属网格的 (to_grid, to_world, cell_size)。

    多网格关卡下「格心/朝向」必须用目标自己网格的本地坐标：插件对篮子
    上的功能站/料箱报的 gx/gz 是篮子本地索引，而 `world.to_grid`/
    `world.to_world` 是主网格（GridManager）——两者原点差 10.0，统一走
    主网格会把篮子上的站位方向算反（trace-20261018-005334 t=16903）。

    优先用 target 的 grid 名查 world.grids；target 无 grid、或该网格不在
    world.grids 里时退回主网格（旧行为，合成帧/旧 fixture 无 grids 数据）。
    """
    grid_name = None
    if isinstance(target, dict):
        grid_name = target.get("interaction_grid") or target.get("grid")
    grids = getattr(world, "grids", None)
    if grid_name and grids is not None:
        gid = str(grid_name)

        def _to_grid(x, z):
            # type: (float, float) -> Optional[Tuple[int, int]]
            return grids.to_grid(gid, x, z)

        def _to_world(gx, gz):
            # type: (int, int) -> Optional[Tuple[float, float]]
            return grids.to_world(gid, gx, gz)

        if _to_world(0, 0) is not None:   # 该网格确实存在
            return _to_grid, _to_world, grids.cell_size(gid)
    cell = getattr(getattr(world, "mapping", None), "cell", 1.2)
    return world.to_grid, world.to_world, cell


def handle_desc(target, to_grid=None):
    # type: (Optional[Dict], Any) -> Dict
    """目标句柄描述（exec 命令用）。核心是**网格坐标**：放置/拾取句柄按
    「网格位置」匹配（盘放在柜台上时，句柄指向柜台，但盘的 x/z 换算成网格
    跟柜台同格，所以按格匹配才对）。name 只做无网格时的兜底。

    网格优先用 target 自带的 gx/gz；缺失时（盘/散料只有 x/z）用 to_grid
    从世界坐标换算——这正是旧路径 interact_ready 的 _target_cell 语义。"""
    if not target:
        return {"grid": None, "gx": None, "gz": None, "name": None}
    gx, gz = target.get("gx"), target.get("gz")
    if gx is None and to_grid is not None:
        x, z = target.get("x"), target.get("z")
        if x is not None and z is not None:
            cell = to_grid(x, z)
            if cell is not None:
                gx, gz = cell
    return {"grid": target.get("grid"), "gx": gx, "gz": gz,
            "name": target.get("name") or target.get("n")}


def target_world(target):
    # type: (Optional[Dict]) -> Optional[List[float]]
    """目标世界坐标 [x,z]（exec 命令重瞄/朝向用）；缺坐标返回 None。"""
    if not target:
        return None
    x, z = target.get("x"), target.get("z")
    if x is None or z is None:
        return None
    return [x, z]


class Primitive(Node):
    """原语基类：halt 松键、reset 清边沿状态、手部连续性不变式。

    spec §7.6 纪律 2：halt 只松键并清移动方向，**不碰物品**。手里的东西
    交给抢占方树首的 EnsureHandsFree 处理——"优雅退出由抢占方负责前置
    条件，而非被打断方负责清理"（spec §7.4）。
    """

    def __init__(self):
        # type: () -> None
        self._ticks = 0
        self._held_seen = _UNSET   # type: Any
        self._exec = None        # type: Any   # 委托后端的 ExecClient（首拍从 ctx 记）
        self._pending_cmd = None # type: Optional[int]  # 在途命令 id
        self._exec_deadline = 0.0  # type: float  # 在途命令的客户端截止时间

    def tick(self, ctx):
        # type: (Dict) -> str
        raise NotImplementedError

    def reset(self):
        # type: () -> None
        self._ticks = 0
        self._held_seen = _UNSET
        self._pending_cmd = None
        self._exec_deadline = 0.0

    def halt(self):
        # type: () -> None
        self._exec_halt()
        self.reset()

    # ---------- exec 委托（EXEC_BACKEND=plugin 时叶子原语发命令而非按键）----------
    def _begin_exec(self, ctx, ex, cmd_dict):
        # type: (Dict, Any, Dict) -> str
        """发一条 exec 命令并进入轮询；未连接返回 FAILURE。"""
        self._exec = ex
        self._pending_cmd = ex.issue(cmd_dict)
        if self._pending_cmd < 0:
            self._pending_cmd = None
            return Status.FAILURE
        # 客户端截止 = 命令 timeout + 1s 裕度（插件结果丢失时兜底 FAILURE）。
        self._exec_deadline = self._now(ctx) + (cmd_dict.get("timeout_ms", 8000) / 1000.0) + 1.0
        return Status.RUNNING

    def _poll_exec(self, ctx, ex):
        # type: (Dict, Any) -> str
        """轮询在途命令结果；拿到则 SUCCESS/FAILURE，超时 FAILURE，否则 RUNNING。"""
        r = ex.poll()
        if r is not None and r["cmd_id"] == self._pending_cmd:
            self._pending_cmd = None
            # move 命令 stuck 会把挡路的目标格（世界坐标）回传。把它翻译成
            # 主网格格写入 motion_failure，note_motion_failure 据此写 bad_cells，
            # 下一次寻路绕开这一格——否则同一个 stuck move 会被反复重发。
            if not r.get("ok") and r.get("why") == "stuck" \
                    and isinstance(r.get("blocked_cell"), (list, tuple)) \
                    and len(r["blocked_cell"]) >= 2:
                w = ctx.get("w")
                if w is not None:
                    try:
                        cell = w.to_grid(float(r["blocked_cell"][0]),
                                         float(r["blocked_cell"][1]))
                        ctx.setdefault("motion_failure", {}).update(
                            reason="no_progress", blocked_cell=cell)
                    except (TypeError, ValueError):
                        pass
            return Status.SUCCESS if r["ok"] else Status.FAILURE
        if self._now(ctx) > self._exec_deadline:
            self._exec_halt()
            return Status.FAILURE
        return Status.RUNNING

    def _now(self, ctx):
        # type: (Dict) -> float
        n = ctx.get("now")
        return n if n is not None else time.monotonic()

    def _exec_halt(self):
        # type: () -> None
        """有在途命令时发 halt，让插件松键（任务抢占/树被丢弃）。"""
        if self._exec is not None and self._pending_cmd is not None:
            self._exec.issue(build_command("halt"))
            self._pending_cmd = None

    def _enter(self, ctx):
        # type: (Dict) -> None
        """子类在 tick 开头调，拍号 +1。"""
        self._ticks += 1

    def hands_changed(self, ctx):
        # type: (Dict) -> bool
        """手里的东西与上一拍不一致吗（spec §7.5 第三类故障）。

        spec §7.5 原话："**误放下 / 误拾**（没打算放却放了）→ 手部连续性
        不变式 → 每个原语开头确认'手里的东西与我以为的一致'，不一致说明
        中间发生意外 → 停下来让 Planner 按现实重算。"

        为什么是"与上一拍比"而不是"与期望比"：原语不知道自己的期望——
        期望写在 Confirm 的 pred 里，而那是动作**之后**的事。能在动作
        **之前**低成本检出的异常是"我没按键，手里的东西却变了"：被 P1
        换走、传送带冲掉、误拾。

        首次 tick 没有上一拍，一律返回 False（没有"不一致"可言）。
        调用方在确认后用 _remember_hands 记下本拍的值。
        """
        cur = P.held_name(ctx.get("chef"))
        if self._held_seen is _UNSET:
            return False
        return cur != self._held_seen

    def _remember_hands(self, ctx):
        # type: (Dict) -> None
        self._held_seen = P.held_name(ctx.get("chef"))

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
from typing import Any, Callable, Dict, Optional, Set, Tuple

from ai.query import predicates as P
from ai.exec.bt import Node
from ai.output.hands import DIR2D

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
    """台面上的盘/散物借用容器交互坐标，保留物品自身身份与世界位置。

    物品没有 GridLocation 不代表在地面。将它一律跳过 Face 会在 1-4
    唯一可用站位上校验失败，然后规划器反复重选。只借同位置的真实静态
    台面，不借远处/相邻台面，也不改原始快照或资源认领键。
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
        if station.get('t') not in ('counter', 'chopping', 'cooking', 'cooker'):
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


def _dir_towards(cur, nxt):
    # type: (Tuple[int, int], Tuple[int, int]) -> Optional[str]
    """cur → nxt 的方向键名。两格不相邻则 None。"""
    d = (nxt[0] - cur[0], nxt[1] - cur[1])
    for name, vec in DIR2D.items():
        if vec == d:
            return name
    return None


class Primitive(Node):
    """原语基类：halt 松键、reset 清边沿状态、手部连续性不变式。

    spec §7.6 纪律 2：halt 只松键并清移动方向，**不碰物品**。手里的东西
    交给抢占方树首的 EnsureHandsFree 处理——"优雅退出由抢占方负责前置
    条件，而非被打断方负责清理"（spec §7.4）。
    """

    def __init__(self):
        # type: () -> None
        self._hands = None   # type: Any
        self._ticks = 0
        self._held_seen = _UNSET   # type: Any

    def tick(self, ctx):
        # type: (Dict) -> str
        raise NotImplementedError

    def reset(self):
        # type: () -> None
        self._ticks = 0
        self._held_seen = _UNSET

    def halt(self):
        # type: () -> None
        if self._hands is not None:
            self._hands.set_move(None)
        self.reset()

    def _enter(self, ctx):
        # type: (Dict) -> None
        """子类在 tick 开头调，记下 hands 以便 halt 能松键。"""
        self._hands = ctx.get("hands")
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

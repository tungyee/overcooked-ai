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

from ai import predicates as P
from ai.bt import Node
from ai.hands import DIR2D

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
    return got if isinstance(got, dict) and got else None


def _blocked_cells(ctx):
    # type: (Dict) -> Set[Tuple[int, int]]
    """脱困拉黑的格子（MarkBlocked 写进 scratch，这里读）。

    TTL 按拍计：值是失效拍号，当前拍 >= 它就不再算拉黑。
    """
    bb = ctx.get("bb")
    scratch = getattr(bb, "scratch", None)
    bad = getattr(scratch, "bad_cells", None)
    if not bad:
        return set()
    tick = ctx.get("tick") or 0
    return set(cell for cell, expire in bad.items() if expire > tick)


def target_cell(target, ctx):
    # type: (Optional[Dict], Dict) -> Optional[Tuple[int, int]]
    """目标所在格：优先 gx/gz，缺失时用 World.to_grid 从 x/z 换算。"""
    if not target:
        return None
    gx, gz = target.get("gx"), target.get("gz")
    if gx is not None and gz is not None:
        return (int(round(gx)), int(round(gz)))
    x, z = target.get("x"), target.get("z")
    if x is None or z is None:
        return None
    return ctx["w"].to_grid(x, z)


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

"""投掷原语：ThrowTo（朝目标投出）与 ThrowIntoCooker（下锅最后几步改成投进锅）。

投掷键 = chop，持可投掷物（食材）时按它就是投掷（spec §7.9）。锅/盘不可投掷，
走 EnsureHandsFree 走近放下，不进这里。

只校验「手空」（离手成功）：投掷物有飞行时间，落地要到下一帧 items[] 才
体现。落点偏差 / 落地结果由 Planner 无状态重算接管，不在原语里猜。

委托后端下 throw 命令由插件朝向闭环（dot>0.97）+ 单击投掷 + 确认离手，
本地 Confirm 再按快照确认一次。
"""
from typing import Any, Callable, Dict, Optional, Tuple

from ai.query import predicates as P
from ai.exec.bt import Node, Sequence, Status
from ai.exec.commands import build_command
from ai.primitives.base import Primitive, resolve, target_world
from ai.primitives.hold import WaitUntil
from ai.primitives.move import GoToCell, _station_interact_cells
from ai.primitives.tap import Confirm
from ai.revenue import throw_supply as TS
from ai.space import navigation
from ai.space import throw as TH

# throw 命令超时（毫秒）：插件转向 ≤0.6s + 按键 0.1s + 确认离手 ≤0.6s。
THROW_TIMEOUT_MS = 2000


class ThrowTo(Primitive):
    """= throw 命令（插件面朝 + 单击投掷）→ Confirm(手空)。"""

    def __init__(self, target, retries=5, aim_dot=None, strict=False,
                 face_timeout_ms=None, precise=None):
        # type: (Any, int, Optional[float], bool, Optional[int], Optional[Callable[[Dict], bool]]) -> None
        Primitive.__init__(self)
        self.target = target
        # 精准投掷（可选）：插件朝向门槛、没对准就不投、转向时限。缺省 = 旧行为。
        self.aim_dot = aim_dot
        self.strict = strict
        self.face_timeout_ms = face_timeout_ms
        # precise(ctx) 为真时发命令那一拍临时切到精准投掷（墙边投，见 TH.WALL_AIM_DOT）
        self.precise = precise
        self._confirm = Confirm(lambda c: P.hands_free(c["chef"]), retries)
        self._thrown = False   # throw 命令是否已发出并回 ok

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._thrown = False
        self._confirm.reset()

    def halt(self):
        # type: () -> None
        self._confirm.halt()
        Primitive.halt(self)

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        # 手已空 → 无需投掷（可能上一拍已经投出 / 被 P1 换走）
        if P.hands_free(ctx["chef"]):
            self.reset()
            return Status.SUCCESS
        return self._tick_delegated(ctx, ctx["exec"])

    def _tick_delegated(self, ctx, ex):
        # type: (Dict, Any) -> str
        """throw 命令（插件面朝 + 单击投掷）→ 本地 Confirm 确认离手。"""
        if not self._thrown:
            if self._pending_cmd is None:
                st = resolve(self.target, ctx)
                tw = target_world(st)
                if tw is None:
                    # 没有朝向目标的 throw 会朝当前面向直接投——宁可不投
                    return Status.FAILURE
                fields = dict(target_world=tw, timeout_ms=THROW_TIMEOUT_MS)
                aim_dot, strict, face_ms = self.aim_dot, self.strict, self.face_timeout_ms
                if self.precise is not None and self.precise(ctx):
                    aim_dot = max(aim_dot or 0.0, TH.WALL_AIM_DOT)
                    strict = True
                    face_ms = max(face_ms or 0, POT_FACE_TIMEOUT_MS)
                if aim_dot is not None:
                    fields["aim_dot"] = aim_dot
                if strict:
                    fields["strict"] = True
                if face_ms is not None:
                    fields["face_timeout_ms"] = face_ms
                    # 转向时限变长，命令总超时跟着放宽（+ 按键 0.1s + 确认离手 0.6s + 余量）
                    fields["timeout_ms"] = max(THROW_TIMEOUT_MS, face_ms + 1400)
                cmd = build_command("throw", **fields)
                # trace 记下发出去的 throw（瞄哪儿、从哪儿、精度）：以前只能
                # 从料的飞行轨迹倒推朝向（trace-20261031-150043）
                chef = ctx.get("chef") or {}
                ctx["throw"] = dict(fields, from_world=[chef.get("x"), chef.get("z")],
                                    frame_seq=ctx.get("frame_seq"))
                return self._begin_exec(ctx, ex, cmd)
            s = self._poll_exec(ctx, ex)
            if s == Status.RUNNING:
                return Status.RUNNING
            if s == Status.FAILURE:
                return Status.FAILURE
            self._thrown = True
        s = self._confirm.tick(ctx)
        if s != Status.SUCCESS:
            return s
        self.reset()
        return Status.SUCCESS


# ---------------------------------------------------------------- 投进锅

# 精准投锅的朝向门槛：dot > 0.996 ≈ 5°。锅的接料触发器约一格宽，5m 外 5°
# 横偏 ≈ 0.44m，仍在锅里；旧门槛 0.97（14°）横偏 1.2m，实测落在锅旁地上。
POT_AIM_DOT = 0.996
# 转向时限（毫秒）：小推杆原地转身，精准对准比 0.97 久。
POT_FACE_TIMEOUT_MS = 1200
# 投出后等锅收下的上限（拍）：飞行 ≤15 拍 + 5Hz 帧延迟 4 拍，留余量。
POT_CATCH_WAIT_TICKS = 30
# 投偏/没对准后这口灶多久不再投（拍）：20 秒，期间走过去放。
POT_THROW_COOLDOWN_TICKS = 400
# 至少省几格路才投：投要额外花转身 + 飞行（约 1 秒 ≈ 走 2~3 格），省得少就直接走。
POT_THROW_SAVE_CELLS = 3


class ThrowIntoCooker(Node):
    """下锅的最后几步改成投：手上拿着料、离锅还远时，走到 3~5m 外一个能
    直线投进锅的格子，精准对准后投出，等锅收下。

    门槛（任一不满足 → 立刻 FAILURE，调用方 Selector 退回走过去放）：
      - 这口灶不在投锅冷却里（bb.pot_throw_cooldown）；
      - 手上拿着要下的料、锅还没收到这份；
      - 有出手位（space.throw.plan_pot_throw），且比走到锅旁至少省
        POT_THROW_SAVE_CELLS 格。

    投出后 POT_CATCH_WAIT_TICKS 内锅没收到（投偏落地 / 没对准没投 / 走位
    失败）→ 写冷却、FAILURE。料若已落地，调用方下一步从地上捡回来再放。

    cooker(ctx) -> 灶站点 dict；holding / placed 是调用方的手上 / 已下锅谓词；
    cooldown_key 是写冷却用的灶资源 id。
    """

    def __init__(self, cooker, holding, placed, cooldown_key):
        # type: (Callable[[Dict], Optional[Dict]], Callable[[Dict], bool], Callable[[Dict], bool], Any) -> None
        self.cooker = cooker
        self.holding = holding
        self.placed = placed
        self.cooldown_key = cooldown_key
        self._plan = None    # type: Optional[Dict]   # 本轮锁定的出手位
        self._child = None   # type: Optional[Node]

    # ---------- 几何 ----------
    @staticmethod
    def _passable_fn(ctx):
        # type: (Dict) -> Callable[[Tuple[int, int]], bool]
        w, field = ctx["w"], ctx.get("field") or {}
        others = set()
        for c in getattr(w, "chefs", None) or ():
            if c.get("name") == P.P2_NAME or c.get("x") is None:
                continue
            others.add(w.to_grid(c["x"], c["z"]))
        return lambda cell: cell in field and cell not in others

    def _pot_point(self, ctx):
        # type: (Dict) -> Optional[Tuple[float, float]]
        st = self.cooker(ctx)
        if not st or st.get("x") is None or st.get("z") is None:
            return None
        return (float(st["x"]), float(st["z"]))

    def ok_here(self, ctx):
        # type: (Dict) -> bool
        """P2 此刻的真实位置就能投进（平台漂移、站在格边也算）。"""
        pot, chef = self._pot_point(ctx), ctx["chef"]
        if pot is None or chef.get("x") is None:
            return False
        return TH.pot_throw_ok((chef["x"], chef["z"]), pot, ctx["w"].to_grid,
                               self._passable_fn(ctx), TS.walls_of(ctx["w"]))

    def plan(self, ctx):
        # type: (Dict) -> Optional[Dict]
        """门槛 + 出手位；不该投返回 None（纯判定，不写黑板）。"""
        bb = ctx.get("bb")
        until = (getattr(bb, "pot_throw_cooldown", None) or {}).get(self.cooldown_key, 0)
        if until > ctx.get("tick", 0):
            return None
        if not self.holding(ctx) or self.placed(ctx):
            return None
        pot = self._pot_point(ctx)
        field = ctx.get("field")
        if pot is None or not field:
            return None
        w = ctx["w"]
        if self.ok_here(ctx):
            cur = P.chef_cell(ctx["chef"], w.to_grid)
            return dict(cell=cur, world=pot, cost=0, dist=0.0)
        best = TH.plan_pot_throw(field.keys(), field.get, w.to_world, w.to_grid,
                                 pot, self._passable_fn(ctx), TS.walls_of(w))
        if best is None:
            return None
        st = self.cooker(ctx)
        walk = navigation.nearest_cost(
            field, _station_interact_cells(st, w.to_grid(*pot), w, "placement"))
        if walk is not None and best["cost"] + POT_THROW_SAVE_CELLS > walk[1]:
            return None      # 省不了几步：直接走过去放
        return best

    # ---------- 节点 ----------
    def _build_child(self):
        # type: () -> Node
        node = self

        def stance(c):
            return node._plan["cell"] if node._plan else None

        return Sequence([
            GoToCell(stance, arrived=self.ok_here, precise=True),
            ThrowTo(self.cooker, aim_dot=POT_AIM_DOT, strict=True,
                    face_timeout_ms=POT_FACE_TIMEOUT_MS),
            WaitUntil(self.placed, POT_CATCH_WAIT_TICKS),
        ])

    def _cooldown(self, ctx):
        # type: (Dict) -> None
        bb = ctx.get("bb")
        table = getattr(bb, "pot_throw_cooldown", None)
        if table is None:
            return
        tick = ctx.get("tick", 0)
        for key in [k for k, v in table.items() if v <= tick]:
            del table[key]
        table[self.cooldown_key] = tick + POT_THROW_COOLDOWN_TICKS

    def _end(self):
        # type: () -> None
        self._plan = None
        self._child = None

    def tick(self, ctx):
        # type: (Dict) -> str
        if self._child is None:
            self._plan = self.plan(ctx)
            if self._plan is None:
                return Status.FAILURE      # 门槛不过：不算投偏，不写冷却
            self._child = self._build_child()
        s = self._child.tick(ctx)
        if s == Status.RUNNING:
            return s
        if s == Status.FAILURE:
            self._cooldown(ctx)
        self._end()
        return s

    def halt(self):
        # type: () -> None
        if self._child is not None:
            self._child.halt()
        self._end()

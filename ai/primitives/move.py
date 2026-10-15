"""移动与转向原语。

Approach：走到能跟 target 交互的位置。
Face：转向 target。
"""
from typing import Any, Dict, Optional

from ai import navigation
from ai import predicates as P
from ai.bt import Status
from ai.primitives.base import Primitive, resolve, target_cell, _dir_towards, _blocked_cells
from ai.hands import DIR2D, OPPOSITE
from ai.primitives.motion import MotionControl

# 转向也走短按 / 观察，预算要覆盖多次 5Hz 新帧；最终仍以交互信号为准。
FACE_MAX_TICKS = 40

# 距目标还有几步就松开冲刺（刹车距离）。冲刺速度下 5 Hz 的位置反馈有
# 200 ms 延迟，倒数第二格才松冲刺时惯性已经把人带过交互格（实机：横穿
# 传送带去中间柜台反复冲过头）。留 3 步足够把速度降下来，又不牺牲长距离
# 的巡航时间（3 格 ≈ 0.5 s）。
DASH_BRAKE_CELLS = 3


class Approach(Primitive):
    """走到能跟 target 交互的位置。

    到位判据是**位置**：`chef_cell ∈ interact_cells(target)`，即站在目标
    相邻的可交互格（或目标格）并停稳后到位，**不看向**。朝向交给 Face——
    用朝向敏感的 interact_ready（placement_gx/gz）当到位判据，会在面向
    相邻目标时反复翻转，导致左右摇摆停不下来（实机：两个灶之间横跳）。

    移动默认按老跑法一路巡航（快）；只在近目标处检测到「反向折返」（越过
    目标又掉头，即 a—c—b 震荡）时切到短按+停稳，真正落定后恢复巡航。
    这样既不会默认对最后两格每格等停稳（慢），又能在震荡时兜底收敛。

    **只保证站位，不保证朝向**（spec §7.2）。需要定向交互的地方必须显式
    带 Face——灭火必须面朝火源才喷得到。
    """

    def __init__(self, target, kind=None, allow_target=False):
        # type: (Any, Optional[str], bool) -> None
        Primitive.__init__(self)
        self.target = target
        self.kind = kind
        self.allow_target = allow_target
        self._goal = None
        self._target_cell = None
        self._motion = MotionControl()
        self._oscillating = False
        self._last_dir = None

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._goal = self._target_cell = None
        self._motion = MotionControl()
        self._oscillating = False
        self._last_dir = None

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        w, chef, hands = ctx["w"], ctx["chef"], ctx["hands"]
        st = resolve(self.target, ctx)
        if st is None:
            hands.set_move(None)
            return Status.FAILURE

        cell = target_cell(st, ctx)
        if cell is None:
            hands.set_move(None)
            return Status.FAILURE

        if cell != self._target_cell:
            self.reset()
            self._target_cell = cell

        cur = P.chef_cell(chef, w.to_grid)
        if cur is None:
            hands.set_move(None)
            return Status.FAILURE

        # 每拍重算距离场代价太贵（spec §6.4 要求每拍只 flood-fill 一次），
        # 但拉黑格是脱困期才有的少量格子——有拉黑时才重算一次。
        field = ctx["field"]
        soft = ctx.get("soft_cost")
        blocked = _blocked_cells(ctx)
        if blocked:
            field = navigation.distance_field(
                cur, w.walkable, w.belt_dir, blocked, soft_cost=soft)

        # 目标就在脚下：散落物（投掷落地的料、掉落的灭火器）没有站点语义，
        # 可能正落在自己站的那一格。此时已经够得着，直接到位——旧逻辑只取
        # 四邻当 goal，于是先走开一格，距离场下一拍又把人拉回来，表现为
        # 两格之间来回震荡（实机：灭火器掉脚下，[5,2]↔[6,2] 抖了 700 拍，
        # 火烧到关卡结束，trace-20261014-004742 t=11000~11580）。
        # 只对「目标格可走」成立——站点格不可走，仍走四邻。
        if cur == cell and w.walkable(cell[0], cell[1]):
            hands.set_move(None)
            ctx["motion"] = {"phase": "arrived", "frame_seq": ctx.get("frame_seq"),
                             "fresh": True, "direction": None,
                             "target": list(cell), "goal": list(cell)}
            return Status.SUCCESS

        goals = navigation.interact_cells(
            cell[0], cell[1], w.walkable, w.belt_cells)
        if self.allow_target:
            # 裸地板落点允许站上目标格，沿用 EnsureHandsFree 的原契约。
            goals = [cell] + goals
        if not goals:
            # 四邻全不可走：站点被围死。也允许直接站上目标格（地面物）
            goals = [cell]

        got = ((self._goal, field[self._goal])
               if self._goal in goals and self._goal in field and self._goal not in blocked
               else navigation.nearest_cost(field, [g for g in goals if g not in blocked]))
        if got is None:
            # 非传送带交互位全不可达 → 放回传送带位再试。站在传送带上会被
            # 推走，交互由 TapWhileHolding 顶住反向键兜底（spec §7.2），
            # 但「站传送带上够得着」总比「永远够不着」强。
            goals = navigation.interact_cells_with_belts(
                cell[0], cell[1], w.walkable, w.belt_cells)
            got = ((self._goal, field[self._goal])
                   if self._goal in goals and self._goal in field and self._goal not in blocked
                   else navigation.nearest_cost(field, [g for g in goals if g not in blocked]))
        if got is None:
            hands.set_move(None)
            return Status.FAILURE      # 不可达 → 交还 Planner

        if self._goal != got[0]:
            self._goal = got[0]
            self._motion = MotionControl()
            self._oscillating = False
            self._last_dir = None
        path = navigation.trace_back(field, self._goal, w.belt_dir, soft)
        if not path:
            hands.set_move(None)
            return Status.FAILURE
        nxt = path[1] if len(path) > 1 else None
        d = _dir_towards(cur, nxt) if nxt is not None else None
        arrived = cur == self._goal
        if d is None and not arrived:
            hands.set_move(None)
            return Status.FAILURE

        # 最后一步仍走精确档（短按 + 停稳）：满速冲过交互格会来回振荡
        # ——实机端饭时在盘旁振荡约 8 秒、米烧糊起火，这条不能退。
        final = (nxt == self._goal)

        # 自适应防震荡：默认按老跑法一路巡航（快）；只在近目标处检测到
        # 「反向折返」（越过目标又掉头，即 a—c—b 震荡）时切到短按+停稳，
        # 真正落定（motion 报 SUCCESS）后恢复巡航。不能用瞬态 arrived 复位：
        # 人还在滑、压过目标格的那一帧 arrived 也是 True，会把震荡标记清掉，
        # 导致永远检不出来。
        if d is not None:
            if len(path) <= 3:
                # 震荡检测暂时注释掉（实机：捡脚下散料时 left/right 折返，
                # 一触发就短按停稳、卡住恼火）。后续重新设计震荡处理再启用。
                # if self._last_dir is not None and d == OPPOSITE.get(self._last_dir):
                #     self._oscillating = True
                self._last_dir = d
            else:
                self._last_dir = d
                self._oscillating = False

        # 站在传送带上横穿时顶住反向键，否则被推偏绕圈
        extra = None
        against_belt = False
        bd = w.belt_dir.get(cur)
        if bd is not None and d is not None:
            dv = DIR2D[d]
            if dv[0] * bd[0] + dv[1] * bd[1] == 0:
                # 垂直穿越：额外按住反向键顶住，避免被推偏
                extra = hands.counter_dir(w.belt_dir, cur)
            elif dv[0] == -bd[0] and dv[1] == -bd[1]:
                # 逆向行走：必须长按顶住传送带推力。此时哪怕检测到震荡也
                # 不许切短按——短按+松键会被传送带推回来，永远走不过去。
                against_belt = True
        # 冲刺要**提前**退出，给惯性留刹车距离：旧判据只看路径长短
        # （len(path) > 3），于是一路冲到倒数第二格才松冲刺，惯性已经把人
        # 带过了交互格（实机：横穿传送带去中间柜台反复冲过头、错过好几次）。
        # 现在距目标 ≤ DASH_BRAKE_CELLS 步就停冲——方向键仍按住（继续走、
        # 不慢），只是不再加速。顶带/逆向/震荡一律不冲（同原逻辑）。
        remaining = len(path) - 1          # 还要走几步
        dash = (d is not None and remaining > DASH_BRAKE_CELLS
                and not against_belt and extra is None
                and not self._oscillating)
        gx, gz = w.to_world(*self._goal)
        error = ((chef["x"] - gx)**2 + (chef["z"] - gz)**2)**0.5
        status = self._motion.tick(ctx, d, extra,
                                   self._oscillating and not against_belt,
                                   arrived, error, cur in w.belt_cells,
                                   final=final, dash=dash)
        if status == Status.SUCCESS:
            self._oscillating = False
            self._last_dir = None
        ctx["motion"].update(target=list(cell), goal=list(self._goal))
        if status == Status.FAILURE:
            ctx["motion_failure"].update(target=st.get("n") or cell,
                                         blocked_cell=nxt or self._goal)
        return status


class Face(Primitive):
    """转向 target。朝向以 interactions.gx/gz 为准。

    Approach 只管站位（spec §7.2 定稿），所以所有定向交互都要显式带这个
    节点。旧 _fire_tick 有专门的 face_fire 子状态 + face_ticks 计数，
    证明这是真实需求：灭火必须面朝火源才喷得到。

    转向用短按方向键——移动方向 = 面向方向。每次松键后观察新帧，直到
    交互格信号指向目标；max_ticks 只是兜底，不作为"转好了"的判据（旧实现用
    face_ticks >= 2 当判据，那是赌两拍必然转过去）。
    """

    def __init__(self, target, max_ticks=FACE_MAX_TICKS):
        # type: (Any, int) -> None
        Primitive.__init__(self)
        self.target = target
        self.max_ticks = max_ticks
        self._motion = MotionControl()

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._motion = MotionControl()

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        w, chef, hands = ctx["w"], ctx["chef"], ctx["hands"]
        st = resolve(self.target, ctx)
        if st is None:
            hands.set_move(None)
            return Status.FAILURE

        cell = target_cell(st, ctx)
        if cell is None:
            hands.set_move(None)
            return Status.FAILURE

        facing = P.facing_cell(chef)
        arrived = facing == cell

        # 出餐口等站点不给 gx/gz 朝向信号，只报交互句柄名（placement /
        # pickup 的字符串）。此时 facing_cell 永远为 None，退而用
        # interact_ready 的名字兜底判断"已经面朝目标"——和 Approach / Tap
        # 同一套判据。否则这里会在 max_ticks 兜底后 FAILURE，送餐的 pickup
        # 键永远发不出去（实机复现：角色站在出餐口前不动，手动按 Shift 才上菜）。
        if facing is None and P.interact_ready(chef, st, None, w.to_grid):
            arrived = True

        if not arrived and self._ticks > self.max_ticks:
            hands.set_move(None)
            self.reset()
            return Status.FAILURE

        cur = P.chef_cell(chef, w.to_grid)
        if cur is None:
            hands.set_move(None)
            return Status.FAILURE
        d = _dir_towards(cur, cell)
        if d is None:
            # 不相邻：按世界坐标的主轴差压方向（Approach 还在路上时的过渡）
            dx = cell[0] - cur[0]
            dz = cell[1] - cur[1]
            if abs(dx) >= abs(dz):
                d = "right" if dx > 0 else "left"
            else:
                d = "up" if dz > 0 else "down"
        status = self._motion.tick(ctx, d, None, True, arrived,
                                   belt=cur in w.belt_cells)
        ctx["motion"].update(target=list(cell))
        if status != Status.RUNNING:
            self.reset()
        return status

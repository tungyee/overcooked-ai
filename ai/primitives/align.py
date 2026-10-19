"""AlignToCell：句柄校验不符时往格心收紧站位。

**为什么需要这个原语**（反编译 + 实机双重依据，详见
docs/superpowers/specs/2026-10-16-interact-handle-feedback-design.md）：

`PlayerControls.FindNearbyObjects()` 做两次**独立**扫描，于是
`interactions` 里有两个语义不同的字段：

  - `gx/gz` 来自 `ScanForComponent(m_GridLocationCondition)` 走
    `GetFacingGridOccupant` —— 按**朝向**打分挑「我正对着的那一格」。
  - `placement_gx/gz` 来自 `ScanForObject(m_PickupOrPlaceCondition)` ——
    按**距离**取基准点 `pos + 0.5*radius*forward` 最近的那一个，不看朝向。

`Face` 的到位判据读前者（`interact_ready(kind=None)`），`Tap` 的前置校验读
后者（`kind="placement"`）。所以 **Face SUCCESS 不蕴含 Tap 能按** ——
偏离格心时，相邻的其它可交互物会以微弱距离优势抢走 placement 句柄。

实机代价（trace-20261016-220409）：P2 手持空锅站在格 (1,-4)、正对着台面
(2,-4)，但出餐口 (0,-5) 以 1.07 vs 1.12（差 1.4%）抢走了 placement 句柄。
`EnsureHandsFree` 是每棵树的树首，它 FAILURE 就整棵树塌 → Planner 换 task
→ 新树树首又是它 → 再塌。连败 3 次写 `scratch.abandoned` 拉黑，候选挨个
被吃掉，最后无候选可选，**P2 钉在那一格 287 拍直到关卡结束**。

修法是反馈而非预测：游戏每帧都在算权威答案，我们只需在它说「不行」时做一个
能改变输入的动作。往格心走正是那个动作 —— 实测把坐标换成格心后，90°/116°/
130° 三个朝向全部是台面赢，四个交互位也全部可用。

**不走的两条路**（都验证过不可行）：
  - 读高亮：`ClientAnticipateInteractionHighlight` 的
    `OnInteractionAnticipationStart(type, _player)` 拿到了 `_player` 却直接
    丢弃，只 `IncrementHighlight()`。计数不区分玩家（P1 站同一台面前也 +1）
    也不区分动作类型（五类共享一个计数），分屏下无法回答「这是给 P2 的吗」。
  - 复刻 `ScanForObject` 几何做预测：胜负 margin 实测 1.4%，而 collider 真实
    形状、坐标精度、浮点噪声都大于这个余量，必然时对时错。
"""
from typing import Any, Dict, Optional

from ai.query import predicates as P
from ai.exec.bt import Status
from ai.primitives.base import Primitive, resolve, target_grid_callbacks
from ai.primitives.motion import MotionControl

# 收紧的拍数上限。传送带上外力持续、永远到不了格心容差内，没有这个兜底会
# 无限 RUNNING 直到 task 级 Timeout 才响（白耗 200 拍）。40 拍 = 2 秒，
# 按一次短按+观察 ≈ 8 拍算够走五步。
ALIGN_MAX_TICKS = 40

# 浮点零阈值：主轴偏差小于它就认为该轴已对齐，不按那个方向。
_AXIS_EPSILON = 1e-6

# (资源,格) 拉黑的 TTL（拍）。与 stuck.ABANDON_TTL_TICKS 同量级：都是
# 「换个位置重试一次」的时间尺度。200 拍 = 10 秒。
ALIGN_PAIR_TTL_TICKS = 200


class AlignToCell(Primitive):
    """句柄不指向 target 时朝格心收紧一步（见模块注释）。

    **只放在 `Face` 之后**：`Approach` 的到位判据是位置而非朝向（spec §7.2
    定稿，注释记着「用朝向敏感的 interact_ready 当到位判据会在面向相邻目标时
    反复翻转，左右摇摆停不下来 —— 实机两个灶之间横跳」）。本原语会平移，
    放在 `Face` 之前会与那条教训撞车；放在之后朝向已稳定，不存在翻转源。

    **地板格不要用**：裸地板格插件不给 placement/facing 信号，
    `interact_ready` 恒 False，收紧会一路白走到 max_ticks。
    """

    def __init__(self, target, kind, max_ticks=ALIGN_MAX_TICKS):
        # type: (Any, Optional[str], int) -> None
        Primitive.__init__(self)
        self.target = target
        self.kind = kind
        self.max_ticks = max_ticks
        self._motion = MotionControl()

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._motion = MotionControl()

    def halt(self):
        # type: () -> None
        if self._hands is not None:
            self._hands.set_move(None)
        Primitive.halt(self)

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        w, chef, hands = ctx["w"], ctx["chef"], ctx["hands"]

        st = resolve(self.target, ctx)
        if st is None:
            hands.set_move(None)
            return Status.FAILURE

        # 对齐方向/偏移用目标所属网格的本地坐标：主网格投影在篮子上是错的
        # （trace-20261018-005334 t=16903/t=32314，篮子上的目标按主网格格心
        # 走，把角色从目标旁推走）。
        to_grid, to_world, cell_size = target_grid_callbacks(w, st)
        cur = P.chef_cell(chef, to_grid)
        if cur is None:
            hands.set_move(None)
            return Status.FAILURE

        off = P.cell_center_offset(chef, cur, to_world)
        if off is None:
            hands.set_move(None)
            return Status.FAILURE

        # 只有「已站到格心容差内」才允许用句柄判定成功。句柄（pickup/placement
        # 的 ScanForObject 结果）是纯距离扫描：偏离格心时它仍会指向目标柜台，
        # 但实际拾取/放置够不着，于是 tap 发出、世界不变、Confirm 超时 → 整棵树
        # 塌 → 原地重建死循环（trace-20261018-134403 卡锅 75 秒）。先看格心、
        # 再看句柄，收紧才能在「句柄假对」时也介入。
        if off <= P.align_epsilon(cell_size):
            if P.interact_ready(chef, st, self.kind, w.to_grid):
                self.reset()
                return Status.SUCCESS
            # 本地格心也站到位了，句柄仍不对。拉黑仍用主网格格：Approach 的
            # 距离场/交互位是主网格坐标系（target_cell 投主网格），两套坐标
            # 对不上会黑错格。
            self._blacklist_pair(ctx, st, P.chef_cell(chef, w.to_grid))
            hands.set_move(None)
            self.reset()
            return Status.FAILURE

        if self._ticks > self.max_ticks:
            hands.set_move(None)
            self.reset()
            return Status.FAILURE

        d = self._toward_center(chef, cur, to_world)
        if d is None:
            hands.set_move(None)
            return Status.FAILURE

        status = self._motion.tick(ctx, d, None, True, False,
                                   belt=cur in w.belt_cells)
        # 用独立键而不是覆盖 motion["phase"]：phase 归 MotionControl 所有
        # （observe/pulse/cruise），而 observe 的占比是 AGENTS.md §10.1 归因
        # 瓶颈的依据，覆盖掉会把那份账搞坏。两个量要同时可见。
        ctx.setdefault("motion", {})["aligning"] = True
        if status == Status.FAILURE:
            reason = (ctx.get("motion_failure") or {}).get("reason")
            if reason == "no_progress":
                # 走不动给退路（trace-20261019-113524：角落短按补不动亚格偏差
                # → no_progress → 树塌 → 重建 → 同一落点再来，死循环）。此时
                # 句柄已指向目标就放行去 tap（失败有 Confirm 兜底），句柄不对
                # 就拉黑这个 (资源,格) 再失败，让下次 Approach 换位重试。
                if P.interact_ready(chef, st, self.kind, w.to_grid):
                    hands.set_move(None)
                    self.reset()
                    return Status.SUCCESS
                self._blacklist_pair(ctx, st, P.chef_cell(chef, w.to_grid))
                hands.set_move(None)
                self.reset()
                return Status.FAILURE
        return status

    @staticmethod
    def _blacklist_pair(ctx, station, cell):
        # type: (Dict, Dict, Any) -> None
        """记下「这一格对这个资源用不了」。

        资源 id 走 `P.resource_id`（spec §5.2 的 `(grid, name)`），与 task 的
        `claims()` 同一套，便于日后对账。

        scratch 可能还不是 StuckScratch（卡住守卫是跨模式前置，可能先于任何
        模式进入），复用 stuck 里的保障函数而不是自己再写一遍类型检查。
        """
        from ai.primitives.stuck import _ensure_stuck_scratch
        scratch = _ensure_stuck_scratch(ctx)
        key = (P.resource_id(station), tuple(cell))
        scratch.bad_pairs[key] = (ctx.get("tick") or 0) + ALIGN_PAIR_TTL_TICKS

    @staticmethod
    def _toward_center(chef, cell, to_world):
        # type: (Dict, Any, Any) -> Optional[str]
        """朝格心的方向键名。按偏差更大的那一轴走，两轴都在零附近则 None。"""
        cx, cz = to_world(cell[0], cell[1])
        dx, dz = cx - chef["x"], cz - chef["z"]
        if abs(dx) < _AXIS_EPSILON and abs(dz) < _AXIS_EPSILON:
            return None
        if abs(dx) >= abs(dz):
            return "right" if dx > 0 else "left"
        return "up" if dz > 0 else "down"

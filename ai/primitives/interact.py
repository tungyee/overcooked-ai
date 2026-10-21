"""交互复合原语。

EnsureHandsFree：手里有东西就放到就近的合法落点（幂等）。
InteractWith：= Approach + (Face) + TapWhileHolding + Confirm（前置 + 后置齐备）。
"""
from typing import Any, Callable, Dict, Optional

from ai.query import placement
from ai.query import predicates as P
from ai.exec.bt import Status
from ai.primitives.align import AlignToCell
from ai.primitives.base import Primitive, resolve, counter_dir
from ai.primitives.hold import TapWhileHolding, WaitUntil
from ai.primitives.move import Approach, Face
from ai.primitives.tap import Tap, Confirm
from ai.primitives.interaction_recovery import InteractionRecovery

# 放下动作的后置校验重试上限。插件 5 Hz 推帧（200ms），按键后"手空了"
# 至少要等下一帧才体现；正式运行按新观测计重试，不消耗旧帧预算。
PUT_DOWN_RETRIES = 5

# EnsureHandsFree 的四个阶段（形态对齐 InteractWith）。
_P_APPROACH, _P_FACE, _P_ALIGN, _P_REFACE, _P_TAP, _P_CONFIRM = range(6)


def _belt_hold_key(ctx):
    # type: (Dict) -> Optional[str]
    """站在传送带上时要顶住的反向键；不在带上则 None（退化为普通单击）。

    交给 TapWhileHolding 做 hold_key：可调用每拍重算，站在传送带交互位时
    按住反向键防被推走（spec §7.2），站普通地面时反回 None 走普通 Tap。
    """
    w = ctx.get("w")
    chef = ctx.get("chef")
    if not w or not chef:
        return None
    cell = P.chef_cell(chef, w.to_grid)
    if cell is None:
        return None
    return counter_dir(w.belt_dir, cell)


class EnsureHandsFree(Primitive):
    """手里有东西就放到就近的合法落点（spec §6.1 落点约束 / §7.4 抢占方树首）。

    **幂等**（spec §7.2）：手已空直接 SUCCESS，故可无条件置于任何树首。

    这个节点承担 spec §7.4 的优雅退出："优雅退出由抢占方负责前置条件，
    而非被打断方负责清理。"被打断的 task 树 halt 只松键然后丢弃；抢占方
    （灭火、丢弃等中断 task）树首放这个节点，把手清空再去拿灭火器。
    理由是职责清晰——"灭火需要空手"是灭火的领域知识，切菜树不需要知道
    灭火的任何事。

    形态与 InteractWith 一致：**Approach → Face → Tap → Confirm**。旧实现
    只有"_walk_to_cell + Tap"，少了两样，实机各踩了一次：

      - **不转身**：只凭帧快照里的 `interact_ready` 就按键。厨师在左右掉头
        时（实测帧 `interactions.gx` 每拍在两侧跳），按键落到游戏里时朝向
        已经不是那一拍的了。
      - **无后置校验**：键发出去就 SUCCESS，世界没变也报成功。于是"清手"
        这个前置会被静默跳过，下游 tree 拿着上一口空锅继续跑——
        TakeCookedToPlate 端着空锅反复去盘子倒，就是从这里进去的死循环
        （实机：25 拍内 tap 了十几次，台面 `item` 始终 None，锅一直攥在
        手里，直到 Confirm 缺失让整棵树一路"成功"到底）。

    落点在**本次激活的第一拍锁定**：两个台面路径代价并列时（实测
    countertop(3) 与 (17) 都是 cost 0），每拍重算会随位置反复换目标，
    表现为原地左右横跳。

    落点优先级（spec §6.1）：就近合法空台面 > 地板格。台面有交互句柄，
    走 Approach（以 placement 句柄到位）→ Face → Tap（按键前复核）→
    Confirm；地板格插件不给 facing/placement 信号，退化成"走到那格上或
    它旁边 → 原地按下 → Confirm"（只校验"手真的空了"，核不了落点）。

    放下的东西不需要专门回收（spec §7.4）：火灭后回 Normal，Planner 看
    当前世界"黄瓜在台面上、没切、盘里缺黄瓜"自然重新规划。这是无状态
    重算的红利。

    物料感知的落点规则（空锅回灶台、熟锅不上灶台……）由 `pick` 插口注入；
    不注入时是"就近合法空台面"。`home_cooker=False` 整体关掉"空锅归位"
    这一路——灭火端下来的锅绝不能回灶台（刚被清空的那口灶可能还在烧），
    见 ai/tasks/fire.py。
    """

    def __init__(self, pick=None, keep=None, home_cooker=True, strict=False):
        # type: (Optional[Callable[[Dict], Optional[Dict]]], Optional[Callable[[Dict], bool]], bool, bool) -> None
        Primitive.__init__(self)
        self._pick = pick
        self._keep = keep
        self._home_cooker = home_cooker
        self._strict = strict
        self._tgt = None          # 锁定的落点：台面站点 dict 或合成的格子 dict
        self._on_station = False
        self._phase = _P_APPROACH
        self._approach = Approach(lambda c: self._tgt, "placement")
        self._floor_approach = Approach(lambda c: self._tgt, allow_target=True)
        self._face = Face(lambda c: self._tgt)
        # 台面落点按键前复核交互句柄（瞎按会把东西放到错误的格子上）；
        # 地板格没有句柄可复核——插件对裸地板格不给 placement/facing 信号，
        # 传来传来的 None 会被 Tap 当成"目标消失"直接 FAILURE，所以那一路
        # 用一个无 target 的 Tap 盲按。
        # 台面落点才对齐：裸地板格插件不给 placement 信号，interact_ready
        # 恒 False，收紧会一路白走到 max_ticks（见 align.py 模块注释）。
        self._align_station = AlignToCell(lambda c: self._tgt, "placement")
        self._reface_station = Face(lambda c: self._tgt)
        self._tap_station = TapWhileHolding(
            "pickup", _belt_hold_key, lambda c: self._tgt, "placement")
        self._tap_floor = Tap("pickup")
        self._confirm = Confirm(lambda c: P.hands_free(c["chef"]),
                                PUT_DOWN_RETRIES)
        self._recovery = InteractionRecovery()
        self._drop_held = None

    def _nodes(self):
        return (self._approach, self._floor_approach, self._face,
                self._align_station, self._reface_station, self._tap_station,
                self._tap_floor, self._confirm)

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._tgt = None
        self._on_station = False
        self._phase = _P_APPROACH
        self._recovery = InteractionRecovery()
        self._drop_held = None
        for n in self._nodes():
            n.reset()

    def halt(self):
        # type: () -> None
        for n in self._nodes():
            n.halt()
        Primitive.halt(self)

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        if not self._recovery.ready(ctx):
            return Status.RUNNING
        # 目标随平台移动时用本帧站点；不能拿锁定落点时的旧世界坐标一直走。
        if self._on_station and self._tgt is not None:
            rid = P.resource_id(self._tgt)
            self._tgt = next((s for s in ctx['w'].stations if P.resource_id(s) == rid), self._tgt)
        if (ctx.get('frame_seq') is not None and self._on_station
                and self._phase not in (_P_APPROACH, _P_CONFIRM)
                and not self._tap_station._fired
                and self._approach.needs_reapproach(ctx)):
            if self._recovery.retry(ctx, self._tgt, 'stance', 'lost_stance', blacklist=False):
                self._restart_movement()
                return Status.RUNNING
            self.reset()
            return Status.FAILURE
        s = self._step(ctx)
        if s == Status.FAILURE:
            phase = (ctx.get('interaction') or {}).get('phase', '')
            if (self._on_station and phase != 'approach'
                    and P.held_name(ctx['chef']) == self._drop_held
                    and self._recovery.retry(ctx, self._tgt, phase)):
                self._restart_movement()
                return Status.RUNNING
            # 与 InteractWith 同一约定：失败即复位，下一拍从锁定落点重来，
            # 不把半截状态（锁定的落点 / 已按过的键）留给下一次激活。
            self.reset()
        return s

    def _restart_movement(self):
        for node in self._nodes():
            node.halt()
        self._phase = _P_APPROACH

    def _call(self, node, phase, ctx):
        ctx['interaction'] = {'phase': phase, 'attempt': self._recovery.attempts,
                              'target': P.resource_id(self._tgt), 'kind': 'placement'}
        return node.tick(ctx)

    def _step(self, ctx):
        # type: (Dict) -> str
        w, chef = ctx["w"], ctx["chef"]

        # 手里拿的正是任务要保留的目标物 → 视为"已空手"，别放下再捡
        if self._keep is not None and self._keep(ctx):
            self.reset()
            return Status.SUCCESS

        # 手已空 → 直接 SUCCESS
        if P.hands_free(chef):
            self.reset()
            return Status.SUCCESS

        if self._tgt is None:
            self._lock_target(ctx)
            self._drop_held = P.held_name(chef)
            if self._tgt is None:
                return Status.FAILURE      # 无处可放 → 交还 Planner

        if self._phase == _P_APPROACH:
            s = (self._call(self._approach, 'approach', ctx) if self._on_station
                 else self._floor_approach.tick(ctx))
            if s != Status.SUCCESS:
                return s
            self._phase = _P_FACE if self._on_station else _P_TAP
        if self._phase == _P_FACE:
            s = self._call(self._face, 'face', ctx)
            if s != Status.SUCCESS:
                return s
            self._phase = _P_ALIGN
        if self._phase == _P_ALIGN:
            s = self._call(self._align_station, 'align', ctx)
            if s != Status.SUCCESS:
                return s
            self._phase = _P_REFACE
        if self._phase == _P_REFACE:
            s = self._call(self._reface_station, 'reface', ctx)
            if s != Status.SUCCESS:
                return s
            self._phase = _P_TAP
        if self._phase == _P_TAP:
            s = (self._call(self._tap_station, 'tap', ctx) if self._on_station
                 else self._tap_floor.tick(ctx))
            if s != Status.SUCCESS:
                return s
            self._phase = _P_CONFIRM
        return self._call(self._confirm, 'confirm', ctx)

    def _lock_target(self, ctx):
        # type: (Dict) -> None
        """按手持物分派落点（spec §6.1 落点约束）。放不下才退下一条：

          手持物                落点
          ────────────────────────────────────────────────────────────
          空锅 / 生料锅          ① 空灶台（归位；"灶上有锅"是个全局资源）
                                ② 就近合法空台面 → ③ 地板格
          熟料锅                 ① 就近合法空台面 → ② 地板格
                                （**不上灶台**：会重新点火，接着烧糊）
          盘 / 料 / 杂物          同上（generic 一路）
          ────────────────────────────────────────────────────────────

        "空锅回灶台"与 ReturnPot 的目标相同，但这里**不写认领**——
        执行层对 claims 没有写权（spec §4.5），所以原语没法"变成 ReturnPot"。
        真正把锅归位成一趟差事、带认领和打分，交给 ReturnPot 那个 task 去做
        （见 spec §6.1"手持状态必须纳入可行性"）。

        `home_cooker=False` 时上表第一行的"① 空灶台"整条去掉（灭火端下来的
        锅只能去台面/地板）。
        """
        w = ctx["w"]
        state = P.held_cookware_state(ctx["chef"], P.held_items(w), w.recipes)
        if self._home_cooker and state in (P.COOKWARE_EMPTY, P.COOKWARE_RAW):
            st = placement.find_empty_cooker(w, ctx["field"])
            if st is not None:
                self._tgt, self._on_station = st, True
                return

        pick = self._pick or (lambda c: placement.find_drop_station(c["w"],
                                                                    c["field"]))
        st = pick(ctx)
        if st is not None:
            self._tgt, self._on_station = st, True
            return
        # strict：指定落点（中转台等）满/不可达就 FAILURE 交还 Planner，
        # 不 fallback 到地板乱放（PlaceToRelay 用）。
        if self._strict:
            return
        cell = placement.find_drop_cell(w, ctx["field"])
        if cell is not None:
            self._tgt = {"gx": int(cell[0]), "gz": int(cell[1])}
            self._on_station = False


class InteractWith(Primitive):
    """= Approach + (Face) + TapWhileHolding + Confirm。前置 + 后置齐备。

    按键用 TapWhileHolding 而非 Tap：站在传送带交互位时按住反向键防被
    推走（spec §7.2），站普通地面时 hold_key 解析为 None、自动退化为普通
    单击，所以对非传送带场景零行为变化。

    这一个复合原语替代今天约 **22 处**逐字复制的 "goto → tap → confirm"
    模板（实测 oc/ai/actions.py：confirm* 子状态分支 22 个、_goto* 调用点
    22 个、confirm_wait < 2 重试样板 17 处）。97 行的 MoveAction → 两行
    声明（spec §7.3）。

    **expect 是位置参数，构造时漏给就 TypeError**。spec §7.3 原话：
    "校验写不掉：InteractWith 的构造要求必须给 expect，没有后置校验就
    建不出节点。这把'验证优先'从口号变成结构强制。"

    face=False 用于不需要精确朝向的场景（拾取地面物，旧
    _goto_grab_cell 的那条路）。
    """

    def __init__(self, target, kind, expect, face=True, retries=5,
                 wait_pred=None, wait_timeout=None):
        # type: (Any, Optional[str], Callable[[Dict], bool], bool, int, Optional[Callable[[Dict], bool]], Optional[int]) -> None
        Primitive.__init__(self)
        self.target = target
        self.kind = kind
        self.expect = expect
        self._approach = Approach(target, kind)
        self._face = Face(target) if face else None
        self._align = AlignToCell(target, kind)
        self._reface = Face(target) if face else None
        # 可选「到位后先等世界谓词成立再 tap」：传送带上的料在持续移动，
        # 走到固定段旁后要等料经过该段（wait_pred 成立）再按，否则按早了
        # 段上空无一物。非传送带路径不传，行为不变。
        self._wait = WaitUntil(wait_pred, wait_timeout) if wait_pred is not None else None
        self._tap = TapWhileHolding("pickup", _belt_hold_key, target, kind)
        self._confirm = Confirm(expect, retries)
        self._phase = 0
        self._recovery = InteractionRecovery()
        self._attempt_held = None

    def _children(self):
        # type: () -> list
        """返回子节点序列（用于迭代和 reset/halt）。

        AlignToCell 夹在 Face 与 Tap 之间：Face 的到位判据读通用 gx/gz，
        Tap 的前置校验读 placement_gx/gz，两者来自游戏的两次独立扫描，
        Face SUCCESS 不蕴含 Tap 能按（见 primitives/align.py 模块注释）。

        `face=False` 的路径（拾取散落物）同样要带它：句柄抢占按距离发生，
        与朝向无关。
        """
        out = [self._approach]
        if self._face is not None:
            out.append(self._face)
        out.append(self._align)
        if self._reface is not None:
            out.append(self._reface)
        if self._wait is not None:
            out.append(self._wait)
        out.extend([self._tap, self._confirm])
        return out

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._phase = 0
        self._recovery = InteractionRecovery()
        self._attempt_held = None
        for c in self._children():
            c.reset()

    def halt(self):
        # type: () -> None
        for c in self._children():
            c.halt()
        Primitive.halt(self)

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)

        # 后置条件已成立 → 这一步本来就做完了（上游 task 的 done 即
        # 下游的 feasible，spec §6.1 上下游咬合，所以这种情况是常态）
        if self.expect(ctx):
            self.reset()
            return Status.SUCCESS

        if not self._recovery.ready(ctx):
            return Status.RUNNING

        if self._attempt_held is None:
            self._attempt_held = P.held_name(ctx['chef'])

        # 手部连续性不变式（spec §7.5 第三类故障）：我没按键，手里的东西
        # 却变了——被 P1 换走、传送带冲掉、误拾。停下来让 Planner 按现实
        # 重算，不在这里猜发生了什么（spec §7.6 纪律 1）。
        #
        # 只在 Tap 还没按下时查：按下之后手里本来就该变，那是 expect 的
        # 职责。_tap._fired 正好是"本次激活期按过没"的权威标记。
        if not self._tap._fired and self.hands_changed(ctx):
            self.reset()
            return Status.FAILURE
        self._remember_hands(ctx)

        # 拾取后目标可以先从台面消失、手持状态稍后才更新；这时必须留在
        # Confirm，而不是把目标消失当成失败、重新走路甚至重按 toggle。
        if not self._tap._fired and resolve(self.target, ctx) is None:
            self.reset()
            return Status.FAILURE

        if (ctx.get('frame_seq') is not None and self._phase > 0
                and not self._tap._fired and self._approach.needs_reapproach(ctx)):
            if self._recovery.retry(ctx, resolve(self.target, ctx), 'stance',
                                    'lost_stance', blacklist=False):
                self._restart_movement()
                return Status.RUNNING
            self.halt()
            return Status.FAILURE

        # 与有记忆 Sequence 一致：转向、按键、等待确认期间不重跑 Approach。
        children = self._children()
        while self._phase < len(children):
            child = children[self._phase]
            phase = ('approach' if child is self._approach else 'face' if child is self._face
                     else 'align' if child is self._align else 'reface' if child is self._reface
                     else 'wait' if child is self._wait else 'tap' if child is self._tap else 'confirm')
            ctx['interaction'] = {'phase': phase, 'attempt': self._recovery.attempts,
                                  'target': P.resource_id(resolve(self.target, ctx)), 'kind': self.kind}
            s = child.tick(ctx)
            if s == Status.RUNNING:
                return Status.RUNNING
            if s == Status.FAILURE:
                if (phase != 'approach' and P.held_name(ctx['chef']) == self._attempt_held
                        and self._recovery.retry(ctx, resolve(self.target, ctx), phase)):
                    self._restart_movement()
                    return Status.RUNNING
                self.halt()
                return Status.FAILURE
            self._phase += 1
        self.reset()
        return Status.SUCCESS

    def _restart_movement(self):
        for child in self._children():
            child.halt()
        self._phase = 0

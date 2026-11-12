# -*- coding: utf-8 -*-
"""跨区交接 task：SendAcross / ThrowBatch。

SendAcross：不可投掷的干净盘 / 脏盘 / 成品盘 / 生载体放到通道台面上交给
对面（ai/space/channels.py：渡运篮子 / 台面传送带 / 两边都够得着的静态
共享台面）。每种
kind 的触发、取放与完成判定在 ai/revenue/handover.py 的规格表里。

ThrowBatch：把一种可投掷料批量投向下一站消费站（砧板/灶/装盘）。两类触发：
  - 分割地图：消费站只有对面够得着（Rapids_2_1 木筏 ↔ 厨房）；
  - 连通但远：料到消费站 > THROW_MIN_DIST，投过去比来回跑快。
历史教训（tests/test_tasks.py 的两条 no_batch_throw 钉子）：批量取+投曾塞在
FetchToBoard/FetchToCooker 里，「投满 N 份才 drop」+ 整树 200 拍，drop 一次
没执行过。这里的差别：每投一份就是一次兑现（朝消费站投，被接住或落在旁边
都算已就位），没有尾部 drop；每轮单独限时，整树按批量放宽。

外部不直接 import 本模块，一律 `from ai import tasks as T`。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai.query import predicates as P
from ai import primitives as PR
from ai.exec.bt import Condition, Node, Selector, Sequence
from ai.revenue import handover as HO
from ai.revenue import throw_supply as TS
from ai.space import channels as CH
from ai.space import reachability
from ai.space import regions as RG
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register,
                           _grid_matches, _station_for_item,
                           _unpack_resource_id)
from ai.tasks.plate import holding_valuable

# 一轮「取 → 走到出手位 → 投」的拍数上限（20Hz）：取料 ~40 拍 + 走位 ~60 拍
# + 转身投出 ~25 拍，留一倍余量。单轮超时 = 进度看门狗：一轮投不出去就交还
# Planner，不让整批空耗。
THROW_ROUND_TICKS = 260
# 拿着料等出手位的上限（拍）：篮子/木筏漂走后通常 1~2 秒内转回来
# （trace-20261029-220619 t=706→720 约 0.7 秒）。等满仍没有就失败交还，
# 不让 P2 端着料一直站着。
LAUNCH_WAIT_TICKS = 60
# 到出手位后等新帧的上限（拍）：5 Hz 一帧 4 拍，留一倍余量。
FRESH_FRAME_TICKS = 10
# 按真实位置找不到能投通的瞄准时最多再等几拍（约 4 帧），等不到整轮失败。
AIM_WAIT_TICKS = 20
# 整批上限：N 不在身份里（每拍重算），按「足够大的一批」给。真正的收敛靠
# done（缺口归零）与「没有可投的料」。
THROW_BATCH_TICKS = THROW_ROUND_TICKS * 12
# 一批最多几份。暂不做批量：一次只投一份，用在「这一步非投不可」的场景
# （Rapids_2_1 分割地图把料投给对面的 P1）。批量的问题——缺口不按锅位封顶、
# batch_bonus 叠加后压过别的备料、中途常被打断——等单份跑稳再说。
MAX_BATCH_UNITS = 1

# 传送带接缝漂开时，端着东西在带子旁等它重新对上的上限（拍）。10 秒：
# 4-2 两筏一圈约 30~40 秒，但接缝窗口不止一次；等满仍没有就交还 Planner，
# 不让 P2 端着成品盘无限站桩。
CONVEYOR_WAIT_TICKS = 200
# SendAcross 正常取放 + 等接缝的总预算。内层 Timeout 必须与外层一致，
# 否则 Executor 的外层放宽会被内层更紧的 Timeout 静默吃掉。
SEND_ACROSS_TIMEOUT_TICKS = TASK_TIMEOUT_TICKS + CONVEYOR_WAIT_TICKS


def _channel_slot(ctx, key, lock, allow_boards=False):
    # type: (Dict, str, Dict, bool) -> Optional[Dict]
    """往通道 key 上放的台面：锁住选中的那格（按站名跟着篮子走）；被占了重选。

    选法：此刻够得着的空格里走得最近的；静态通道优先普通台面、其次共享砧板
    （只有载体允许放砧板，盘/脏盘不放，免得占住切菜位）；传送带通道按段心
    距离选最近的一段。渡运台面都够不着时按预测等待最短的。
    """
    from ai.space import channels as CH
    from ai.space import reachability, shuttle
    w, field, bb = ctx["w"], ctx.get("field"), ctx.get("bb")
    name = lock.get("n")
    if name is not None:
        st = next((s for s in CH.slots(w, key, bb) if s.get("n") == name), None)
        if st is not None and not CH.slot_occupied(w, st):
            return st
        lock.clear()
    if key == CH.STATIC and allow_boards:
        free = CH.free_carrier_slots(w, bb)
    else:
        free = CH.free_slots(w, key, bb)
    if not free:
        return None

    own = CH._chef_grid(w, ctx.get("chef"))
    def cost(st):
        if key in (CH.STATIC, CH.CONVEYOR):
            goals = reachability._interact_goals(ctx, st)
            if goals is True:
                return (0, 0)
            got = [field[g] for g in goals or () if field and g in field]
            # 自己这侧整条带都够得着：主网格投影会闪断，别因一帧没有投影
            # 交互格就把带子判成不可放（trace-20261111-115549 t=367）。
            if key == CH.CONVEYOR and str(st.get("grid") or "") == own:
                got = [0]
            if not got:
                return (2, 1e9)
            tie = 0 if st.get("t") == "counter" else 1
            if key == CH.CONVEYOR:
                # 固定在带子开头放：先大 gz、再大 gx，最后才看走过去多远。
                # 反向放到远端会让成品盘停在玩家够不着的那一端。
                gz_rank, gx_rank = CH.conveyor_start_rank(st)
                tie = gz_rank * 10000 + gx_rank * 100 + min(got)
            return (0, tie)
        now = shuttle.reachable_stances(w, st, field)
        if now:
            return (0, min(field[s["cell"]] for s in now) if field else 0)
        wait = reachability.shuttle_wait(ctx, st)
        return (1, wait if wait is not None else 1e9)
    best = min(free, key=cost)
    if cost(best)[0] >= 2 or cost(best)[1] >= 1e9:
        return None
    lock["n"] = best.get("n")
    return best


@_register
class SendAcross(Task):
    """把东西放到通道台面上交给对面区（ai/space/channels.py）。

    身份 = item 种类（cross_supply.KIND_*）+ resources (来源实例 id, 通道 key)。
    通道 key：渡运网格名（篮子运过去）/ channels.STATIC（两边都够得着的台面）。

    本类只做「拿起来 → 放到通道格」；每种 kind 的“该不该送、从哪拿、怎么算
    送完、能不能放共享砧板”在 ai/revenue/handover.py 的规格表里：
      - clean_plate：对面缺干净盘；
      - carrier：料齐的生载体，对应灶只在对面；
      - dirty_plate：洗碗池只在对面；
      - plated_dish：出餐口只在对面。
    加新 kind 不需要改本类。
    """

    TYPE = "SendAcross"
    DESC = "把东西放到通道台面上送去对面"
    LABEL = "送过去"
    HANDS = "empty"

    @property
    def TIMEOUT_TICKS(self):
        # type: () -> int
        """取放 + 等接缝的总预算（内层 Timeout 用同一表达式）。"""
        return SEND_ACROSS_TIMEOUT_TICKS

    @property
    def source_id(self):
        return self._resources[0]

    @property
    def channel(self):
        return self._resources[1]

    def _spec(self):
        return HO.spec_for(self.item)

    def claims(self):
        spec = self._spec()
        return spec.claims(self.source_id, self.order_id) if spec else ()

    def _source(self, ctx):
        spec = self._spec()
        if spec is None:
            return None
        source = spec.resolve(ctx, self.source_id, self.order_id)
        if source is not None:
            return source
        # 接缝刚漂开的那一帧，send_requests/sources 会因为 channel_keys 为空
        # 不再返回来源；但东西还在 P2 手里/这侧，应该能等下一帧重开，而不是
        # 把任务判成不可行。只在通道宽限期内给 fallback，避免把本侧带子当
        # 送件通道（那种情况 channel 从没开过，waitable 为假）。
        if self.channel == CH.CONVEYOR and CH.conveyor_waitable(ctx.get("bb")):
            probe = HO.SendSource(self.item, self.source_id, self.order_id)
            target = spec.pickup(ctx, probe)
            if target is not None and target.get("held_by") in (None, P.P2_NAME):
                return probe
        return None

    def wait_targets(self, ctx):
        # type: (Dict) -> List[Dict]
        """要等篮子的目标（reachability.task_*wait_seconds 读）：放哪一格。
        静态通道不用等。"""
        from ai.space import channels as CH
        if self.channel == CH.STATIC:
            return []
        spec = self._spec()
        if spec is None:
            return []
        st = _channel_slot(ctx, self.channel, {}, spec.allow_boards)
        return [st] if st is not None else []

    def done(self, ctx):
        # type: (Dict) -> bool
        spec = self._spec()
        if spec is None:
            return False
        source = self._source(ctx)
        if source is None:
            source = HO.SendSource(self.item, self.source_id, self.order_id)
        return spec.done(ctx, source, self.channel)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        spec = self._spec()
        if spec is None or self.done(ctx):
            return False
        # 手里端着成品盘 / 生载体时，别为了送脏盘/干净盘把它放下——放下就被
        # 下一个任务捡起来，来回循环（trace-20261109-212622 t=94~99：
        # SendAcross(dirty_plate) 放披萨底 ↔ PlaceCarrierInCooker 捡起）。
        if self.item in ("clean_plate", "dirty_plate") and holding_valuable(ctx):
            return False
        source = self._source(ctx)
        if source is None:
            return False
        slot = _channel_slot(ctx, self.channel, {}, spec.allow_boards)
        if slot is None:
            # 传送带接缝漂开：刚开过就允许端着成品盘等下一帧重开。
            # 没开过的通道仍然不可送（自己这侧的带子会进本侧桶）。
            return (self.channel == CH.CONVEYOR
                    and CH.conveyor_waitable(ctx.get("bb")))
        if not spec.capacity(ctx, self.channel):
            return False
        return True

    def build(self):
        # type: () -> Node
        spec = self._spec()
        if spec is None:
            return PR.Timeout(PR.EnsureHandsFree(), TASK_TIMEOUT_TICKS)
        key = self.channel
        lock = {}  # type: Dict[str, Any]
        timeout = self.TIMEOUT_TICKS

        def source(c):
            return self._source(c)

        def held(c):
            s = source(c)
            return spec.held(c, s) if s is not None else False

        def pickup(c):
            s = source(c)
            return spec.pickup(c, s) if s is not None else None

        def placed(c):
            if not P.hands_free(c["chef"]):
                return False
            s = source(c) or HO.SendSource(self.item, self.source_id,
                                           self.order_id)
            return spec.done(c, s, key)

        steps = []  # type: List[Node]
        if key == CH.CONVEYOR:
            # 4-5 方向不对时先翻开关（前置动作），否则料放上去直接进桶。
            steps.append(_ensure_direction_ok())
        steps.append(PR.EnsureHandsFree(keep=held))
        steps.append(PR.InteractWith(pickup, "pickup", held))
        if key == CH.CONVEYOR:
            # 4-2 接缝会逐帧漂开：先把东西拿在手里，等在带子旁直到有槽位，
            # 而不是让 InteractWith 拿到 None 直接 failure。
            steps.append(PR.WaitUntil(
                lambda c: _channel_slot(c, key, lock,
                                        spec.allow_boards) is not None,
                CONVEYOR_WAIT_TICKS))
        steps.append(
            # 篮子不在口上时 Approach 原地等 / 先去候车格（不占本 task 超时预算）
            PR.InteractWith(lambda c: _channel_slot(c, key, lock,
                                                    spec.allow_boards),
                            "placement", placed))
        return PR.Timeout(Sequence(steps), timeout)


# 按开关后等方向翻转到位的拍数（20Hz）：开关翻转有 1~2 帧延迟。
SWITCH_PRESS_TICKS = 40


def _switch_target(ctx):
    # type: (Dict) -> Optional[Dict]
    """P2 够得着的那台方向开关（两人各一台，只能按自己这侧那台）。"""
    for st in ctx["w"].stations:
        if isinstance(st, dict) and st.get("t") == "switch" \
                and reachability.can_interact(ctx, st):
            return st
    return None


def _ensure_direction_ok():
    # type: () -> Node
    """方向已正确 → 直接过；否则走到开关按到正确（4-5 送料的前置动作）。

    不是独立打分 task：作为 SendAcross 树里的一个前置节点，只在该送料时顺手
    把方向翻对。
    """
    def direction_ok(c):
        return CH.conveyor_direction_ok(c["w"], c.get("bb"))

    return Selector([
        Condition(direction_ok),
        Sequence([
            PR.Approach(_switch_target, None),
            PR.Face(_switch_target),
            # kind="spray"：开关是 grid 对象（m_gridLocation）、m_interactable=null，
            # HoldCommand 的 UseMatches 门恒不中 → drifted 按不出键。spray 改走
            # 朝向句柄（HandleMatches(kind=null)），同 Extinguish 喷灶。
            PR.HoldUntil("switch", direction_ok, timeout=SWITCH_PRESS_TICKS,
                         target=_switch_target, kind="spray"),
        ]),
    ])


def _dest_station(ctx, dest_id):
    # type: (Dict, Any) -> Optional[Dict]
    """按 (grid, name) 资源 id 找消费站的站点 dict（砧板 / 灶）。"""
    grid, name = _unpack_resource_id(dest_id)
    for st in ctx["w"].stations:
        if st.get("t") not in ("chopping", "cooking"):
            continue
        if str(st.get("n") or "") != name:
            continue
        if not _grid_matches(st.get("grid"), grid):
            continue
        return st
    return None


@_register
class ThrowBatch(Task):
    """把一种可投掷料批量投向消费站（砧板 / 灶 / 装盘）：一份一投，直到缺口归零。

    身份 = (dest_id, consumer, raw, product) + item（要投的形态）。N 不进身份：
    缺口每拍重算，投一份少一份，进身份的话每投一次就换一个 task、承诺惯性失效。
    """

    TYPE = "ThrowBatch"
    DESC = "把这种料批量扔到对面/远处的砧板或灶旁"
    LABEL = "扔过去"
    HANDS = "any"

    @property
    def dest_id(self):
        return self._resources[0]

    @property
    def consumer(self):
        return self._resources[1]

    @property
    def raw(self):
        return self._resources[2]

    @property
    def product(self):
        return self._resources[3]

    def claims(self):
        return ((self.dest_id, "throw_to"),)

    # ---------- 每拍求值 ----------
    def _full_state(self, ctx):
        # type: (Dict) -> Optional[Tuple[Dict, str, List[Dict], int]]
        """(消费站, 模式, 可取来源, 未封顶缺口)；不构成投掷场景返回 None。"""
        w, bb = ctx["w"], ctx.get("bb")
        dest = _dest_station(ctx, self.dest_id)
        if dest is None:
            return None
        # 先按 split 口径找来源（分割与否看消费站可达性，不看来源）
        probe = TS.MODE_SPLIT if not RG.reachable_by(w, dest, P.P2_NAME, bb) \
            else TS.MODE_FAR
        dests = TS.dest_candidates(w, ctx["book"], self.consumer, self.product)
        srcs = TS.sources(w, self.item, self.raw, dest, probe, bb, dests)
        mode = TS.mode_for(w, dest, srcs, bb)
        if mode is None and self._carrying_this_batch(ctx, srcs, probe):
            # 本批已在进行、料拿在手上：按探测口径继续投完这份，不因单帧判据
            # 翻转就放下。far：走向出手位时人离灶近了（trace-20261029-130632）；
            # split：篮子在动，「P1 能到砧板」逐帧翻转，拿起→StashHeld 放下→
            # 再拿起循环（trace-20261029-220619 t=685~942）。落点仍按 P1 可达
            # 格判，P1 暂时到不了砧板也能先捡起来。等不到出手位由树里的
            # WaitUntil 兜底（LAUNCH_WAIT_TICKS）。
            mode = probe
        if mode is None:
            return None
        n = TS.gap(w, ctx["book"], self.product, self.item, self.consumer,
                   self.raw, dest, mode, bb)
        return dest, mode, srcs, n

    def _state(self, ctx):
        # type: (Dict) -> Optional[Tuple[Dict, str, List[Dict], int]]
        """(消费站, 模式, 可取来源, 本批份数)：缺口按 MAX_BATCH_UNITS 封顶。"""
        st = self._full_state(ctx)
        if st is None:
            return None
        return st[0], st[1], st[2], min(st[3], MAX_BATCH_UNITS)

    def full_gap(self, ctx):
        # type: (Dict) -> int
        """未封顶的缺口：可见订单还差几份没越过这一站（打分绑单用）。"""
        st = self._full_state(ctx)
        return st[3] if st else 0

    def _carrying_this_batch(self, ctx, srcs, probe):
        # type: (Dict, List[Dict], str) -> bool
        """P2 手上拿着本批的料，且当前承诺就是这个 ThrowBatch（far / split 都适用）。"""
        if probe not in (TS.MODE_FAR, TS.MODE_SPLIT) \
                or not any(s["kind"] == "held" for s in srcs):
            return False
        bb = ctx.get("bb")
        commit = getattr(bb, "commitment", None)
        active = getattr(commit, "active_task", None)
        return getattr(active, "key", None) == self.key

    def batch_size(self, ctx):
        # type: (Dict) -> int
        st = self._state(ctx)
        return st[3] if st else 0

    def done(self, ctx):
        # type: (Dict) -> bool
        st = self._state(ctx)
        return st is not None and st[3] <= 0

    def feasible(self, ctx):
        # type: (Dict) -> bool
        st = self._state(ctx)
        if st is None:
            return False
        dest, mode, srcs, n = st
        if n <= 0 or not srcs:
            return False
        if TS.launch_plan(ctx["w"], dest, mode, ctx.get("bb")) is not None:
            return True
        # 拿着本批料、这一帧暂无出手位（平台漂走）：仍可行，交给树里的
        # WaitUntil 等 LAUNCH_WAIT_TICKS；等不到树失败、承诺清空，保护随之解除。
        probe = TS.MODE_SPLIT if mode == TS.MODE_SPLIT else TS.MODE_FAR
        return self._carrying_this_batch(ctx, srcs, probe)

    # ---------- 行为树 ----------
    def build(self):
        # type: () -> Node
        task, form = self, self.item

        def holding(c):
            return P.held_matches(c["chef"], form)

        def source(c):
            st = task._state(c)
            if st is None:
                return None
            for s in st[2]:
                if s["kind"] == "loose":
                    return _station_for_item(c["w"], s["entry"]) or s["entry"]
            for s in st[2]:
                if s["kind"] == "crate":
                    return s["entry"]
            return None

        def launch_cell(c):
            st = task._state(c)
            if st is None:
                return None
            plan = TS.launch_plan(c["w"], st[0], st[1], c.get("bb"))
            return plan["cell"] if plan else None

        # 本轮锁定的瞄准：{"toward": 方向点, "target": 发给插件的 target_world dict}
        lock = {}  # type: Dict[str, Any]

        def resolve_aim(c):
            """按新帧位置定瞄准，定下就锁住；每拍按此刻位置复核，过不去就换。

            依次试：已锁定的方向 → 出手方案的方向（规划时按出手格心算的）→
            此刻位置重新搜。有墙时三者都不行就不投（WaitUntil 等下一帧，超时
            整轮失败交还 Planner）；**不再退回瞄消费站本身**——正对炸锅那条线
            正是撞框的线（trace-20261031-150043 t=160/265）。无墙（旧帧）仍退回
            消费站，木筏漂移等老场景行为不变。
            """
            st = task._state(c)
            if st is None:
                lock.clear()
                return False
            w, dest, mode, bb = c["w"], st[0], st[1], c.get("bb")
            tries = []
            if lock.get("toward") is not None:
                tries.append(lock["toward"])
            plan = TS.launch_plan(w, dest, mode, bb)
            if plan is not None and plan.get("aim_far") is not None:
                tries.append(plan["aim_far"])
            for toward in tries:
                got = TS.aim_still_ok(w, dest, mode, toward, bb)
                if got is not None:
                    lock.update(toward=toward, target=dict(x=got["aim_far"][0],
                                                           z=got["aim_far"][1]))
                    return True
            got = TS.aim_here(w, dest, mode, bb)
            if got is not None:
                lock.update(toward=got["aim_far"], target=dict(x=got["aim_far"][0],
                                                               z=got["aim_far"][1]))
                return True
            lock.clear()
            if not TS.walls_of(w):
                lock["target"] = _dest_station(c, task.dest_id)
                return lock["target"] is not None
            return False

        def aim(c):
            return lock.get("target")

        def throw_ok_here(c):
            st = task._state(c)
            return st is not None and TS.throw_ok_here(c["w"], st[0], st[1], c.get("bb"))

        # 单份：一棵树只投一轮（取 → 走到出手位 → 投），投完 SUCCESS 交还
        # Planner，要不要再投下一份每拍重新决定，中间可以穿插别的事。
        return PR.Timeout(Sequence([
            PR.EnsureHandsFree(keep=holding),
            Selector([
                Condition(holding),
                PR.InteractWith(source, "pickup", holding, face=True),
            ]),
            # 平台漂走时出手位可能暂时没有：拿着料等一会儿，等不到就失败交还
            # Planner（承诺清空 → 拿着料的保护解除 → StashHeld 放下）。
            PR.WaitUntil(lambda c: throw_ok_here(c) or launch_cell(c) is not None,
                         LAUNCH_WAIT_TICKS),
            # precise：出手方案按格心算，人在格里但离格心远时可能投不过去
            #（trace-20261031-201659 t=37~93），到格后再朝格心挪一次
            PR.GoToCell(launch_cell, arrived=throw_ok_here, precise=True),
            # move 到位那一拍快照还是旧位置：等下一帧再按真实位置定瞄准
            PR.WaitNewFrame(FRESH_FRAME_TICKS),
            PR.WaitUntil(resolve_aim, AIM_WAIT_TICKS),
            # 有墙（带高度帧）就精准对准、没对准不投：擦框的料掉进缝里就没了
            PR.ThrowTo(aim, precise=lambda c: bool(TS.walls_of(c["w"]))),
        ]), THROW_ROUND_TICKS)

    TIMEOUT_TICKS = THROW_ROUND_TICKS

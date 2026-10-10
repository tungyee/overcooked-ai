# -*- coding: utf-8 -*-
"""AI 主循环（新架构，Plan 3 Task 8）。

职责：13 步装配：收帧 → 对账 → 推断 → 仲裁 → 规划 → 执行 → 日志。
与 oc/ai/agent.py 的差异：旧实现 1180 行 God Object，新实现 ~180 行装配层。
Spec: §8.1 主循环
"""
import time
from typing import Any, Dict, Optional

from ai import arbiter, executor, navigation, p1model, planner, reconcile, state_dump, trace
from ai import predicates as P
from ai.blackboard import Blackboard, Claim, Commitment
from ai.hands import Hands
from ai.primitives.stuck import ABANDON_TTL_TICKS, note_motion_failure
from ai.primitives.motion import FRAME_MAX_AGE
from ai.recipes import RecipeBook
from ai.world import StateClient, World

# 主循环周期（秒）= 50 ms，即 20 Hz。全项目代码里的"拍"（tick）统一指
# **本循环拍**：所有按拍计的阈值（task 超时、按键冷却、卡住判定等）都按
# 20 Hz 理解，与插件 5 Hz 推帧（200 ms）不是一个量级——混用会让阈值差 4 倍。
TICK_INTERVAL = 0.05

# L3 兜底：同一 task 连续执行失败满 N 次，就把它的认领资源写进
# scratch.abandoned 拉黑一段时间（TTL 见 ABANDON_TTL_TICKS），让 planner
# 跳过它、去做别的任务（实机复现：切菜被传送带反复推走时 flap）。
ABANDON_FAIL_STREAK = 3


def _p1_record(p1_model):
    # type: (Any) -> Dict
    """把 P1 推断压成可序列化 dict，供 trace 的 p1 字段记录。"""
    if not p1_model:
        return {}
    conf = getattr(p1_model, "confidence", 0.0) or 0.0
    try:
        conf = round(float(conf), 3)
    except (TypeError, ValueError):
        conf = 0.0
    return {
        "holding": getattr(p1_model, "holding", None),
        "task": getattr(p1_model, "p1_active_task", None),
        "confidence": conf,
    }


def _claims_binding(bb):
    # type: (Any) -> Dict
    """认领绑定：{资源:动作} → 归属者 / 订单 / 已持有拍数，供 trace 排查。"""
    out = {}
    tick = getattr(bb, "tick", 0) or 0
    for (res_id, action), c in (getattr(bb, "claims", None) or {}).items():
        out["%s:%s" % (res_id, action)] = {
            "owner": getattr(c, "owner", None),
            "order": str(getattr(c, "order_id", None)),
            "age": max(0, tick - getattr(c, "claimed_tick", 0)),
        }
    return out


def _order_assignment_binding(bb):
    # type: (Any) -> Dict
    """分单归属：订单 → 归属玩家（split 模式用，当前多为空）。"""
    return {
        str(oid): (owner or None)
        for oid, owner in (getattr(bb, "order_assignment", None) or {}).items()
    }


class NullController:
    """默认 controller（仅用于测试）。"""
    def tap(self, name, hold=0.12): pass
    def press(self, name): pass
    def release(self, name): pass
    def release_all(self): pass


class Agent:
    """AI 主循环：13 步装配。"""

    def __init__(self, ctrl=None):
        # type: (Optional[Any]) -> None
        self.client = StateClient()
        self.bb = Blackboard()
        self.executor_inst = executor.Executor()
        self.ctrl = ctrl or NullController()
        self.hands = Hands(self.ctrl)
        self.book = None  # type: Optional[RecipeBook]
        self.tick = 0
        self.idle_logged = False  # 暂停/非关卡的心跳日志是否已写过
        self._fail_task_key = None  # 连续执行失败计数：上一个失败的 task key
        self._fail_streak = 0       # 同一 task 的连续失败次数（L3 兜底用）
        self._frame_seq = None      # 游戏收帧编号，与 Python tick 分开
        self._frame_received_at = None

    def run(self):
        """主循环入口（20 Hz）。插件 5 Hz 推帧，中间 4 拍复用最新帧重 tick。"""
        latest = None  # type: Optional[Any]
        try:
            while True:
                started = time.monotonic()
                self.hands.service(started)
                snapshot = self.client.read_latest()
                if snapshot is not None:
                    latest = World(snapshot)
                    self._frame_seq = (self._frame_seq or 0) + 1
                    self._frame_received_at = time.monotonic()
                if latest is None:
                    time.sleep(TICK_INTERVAL)
                    continue
                self._tick_once(latest)
                self.tick += 1
                # 处理耗时计入周期，不能变成“处理耗时 + 50ms”。短按松键
                # 另有 controller 定时器，即使某拍规划较慢也不会被拉长。
                time.sleep(max(0.0, TICK_INTERVAL - (time.monotonic() - started)))
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            # 断线、读帧错误与意外异常均要释放持续方向键。
            self.hands.release_all()

    def _tick_once(self, world):
        # type: (World) -> None
        """执行一拍的 13 步主循环。"""
        # 生命周期守卫（spec §8.1）：暂停 / 非关卡 → 松键、不做任何决策。
        stale = (self._frame_received_at is not None and
                 time.monotonic() - self._frame_received_at > FRAME_MAX_AGE)
        if stale or world.paused or world.phase != "level":
            self.hands.release_all()
            self.executor_inst.halt_and_discard()
            self.bb.commitment = Commitment()
            # 进入这个状态时记一条，之后不再逐拍记（一局里大部分时间可能
            # 都在暂停，逐拍记只会把日志刷成噪声）。留一条是为了区分
            # "trace 里什么都没有"到底是 agent 没起来还是 AI 没决策。
            if not self.idle_logged:
                self.idle_logged = True
                reason = ("stale_frame" if stale else "paused" if world.paused
                          else "phase=%s" % world.phase)
                trace.emit({"mode_reason": reason, "p1": {}, "candidates": []},
                           self.bb.mode, self.bb.commitment,
                           {"status": "skipped"}, [], self.tick)
            return
        self.idle_logged = False

        # 黑板拍号与主循环对齐：planner 的可行性 ctx、认领过期、以及
        # abandoned 的 TTL 都读 bb.tick，必须在每拍推进（否则 TTL 恒为 0）。
        self.bb.tick = self.tick

        # 配方书每拍按当前帧重建。recipes.py 的注释写的是"一次构造、
        # **整帧**复用"，不是整局复用——只在第一帧构造一次会让早帧/残帧
        # 污染整局：若那帧 crate 的 chop 还没带上，needs_chopping 恒为
        # False，AI 会以为"鱼/黄瓜不用切"，拿生料直接去装盘（实机复现过）。
        self.book = RecipeBook(world.recipes, world.stations)

        # 拍号推进：Tap 冷却按拍计（hands 内部状态）
        self.hands.tick()

        # ② skip_ticks 跳过
        if self.bb.scratch and getattr(self.bb.scratch, "skip_ticks", 0) > 0:
            self.hands.release_all()
            self.bb.scratch.skip_ticks -= 1
            return

        # ③ 认领对账
        live_order_ids = {o.get("id") for o in world.orders if o.get("id")}
        view = self._make_resource_view(world)
        reconcile.reconcile_claims(view, self.bb, live_order_ids, self.tick)

        # ④ P1 推断（Plan 4 Task 2）
        p1model.update(world, self.bb)

        # ⑥ 模式仲裁
        new_mode, mode_reason = arbiter.select_mode(world, self.bb)

        # ⑦ 模式变化 → halt
        if new_mode != self.bb.mode:
            self.executor_inst.halt_and_discard()
            arbiter.on_mode_transition(self.bb.mode, new_mode, self.bb)
            self.bb.mode = new_mode

        # ⑨ 规划（多订单模式：不再选择单个订单）
        ctx = self._exec_context(world)
        if new_mode == "Normal":
            chosen_task, plan_record = planner.plan(world, self.bb, None, self.book)
            # ⑨.5 正在跑的那棵树：如果它的后置条件**已经成立**，这一拍先让
            # 它把 SUCCESS 报完，别被 planner 抢走。
            #
            # 为什么需要：planner 每拍重选，而任务一旦自己做成了，就对自己
            # 不可行了（倒完米饭 plate_lacks 为假、下完锅 cooker_started 为
            # 真）→ 换任务 → executor 见身份变化就 halt 掉旧树 → ⑬ 的成功
            # 记账（释放认领）连带被吞掉。实机后果：倒完米饭那一拍被换成
            # ReturnPot，认领没释放 → 下一拍又去取熟料。
            #
            # 只在 Normal 分支里做：模式切换（火情等）已经 halt_and_discard
            # 过了，此时 active_task 是 None，不会把任务"复活"。
            active = self.executor_inst.active_task
            if active is not None and active != chosen_task \
                    and self._task_done(active, world, ctx):
                chosen_task = active
            claims_new = {}
            if chosen_task and hasattr(chosen_task, "claims"):
                # 认领绑定到任务的 order_id（而非全局 current_order_id）
                for res_id, action in chosen_task.claims():
                    claims_new[(res_id, action)] = Claim("P2", chosen_task.order_id, self.tick)
        elif new_mode == "Firefighting":
            # Firefighting 模式：只生成 RescueCooked 候选，优先灭火。
            # 实机复现 trace-20261009-130459 拍 596-727: Firefighting 触发但
            # P2 完全 idle (candidates=0)，原因是旧分支直接跳过了 Planner 调用。
            chosen_task, plan_record = planner.plan_firefighting(world, self.bb, self.book)
            claims_new = {}
            if chosen_task and hasattr(chosen_task, "claims"):
                for res_id, action in chosen_task.claims():
                    # Firefighting task 不绑订单（灭火与订单无关）
                    claims_new[(res_id, action)] = Claim("P2", None, self.tick)
        else:
            # 其他模式（Assigned 等）暂无实现，留空壳
            chosen_task = None
            claims_new = {}
            plan_record = {"candidates": []}

        record = {
            "mode_reason": mode_reason,
            "p1": _p1_record(self.bb.p1_model),
            "candidates": plan_record.get("candidates", []),
            "bindings": {
                "plate_order": plan_record.get("plate_match", {}),
                "cooker_order": plan_record.get("cooker_match", {}),
                "claims": _claims_binding(self.bb),
                "order_assignment": _order_assignment_binding(self.bb),
            },
        }

        # ⑩ 写回认领
        self.bb.claims.update(claims_new)

        # ⑪ 登记 commitment（存 Task 对象本身：承诺惯性按四元身份比较）
        if chosen_task:
            if not self.bb.commitment or self.bb.commitment.active_task != chosen_task:
                self.bb.commitment = Commitment(chosen_task, self.tick, 0, 0, 0)

        # ⑫ 执行
        if chosen_task:
            if self._frame_seq is not None:
                # 规划可能耗时；短按的时基必须接近真正发键的时刻。
                ctx["now"] = time.monotonic()
            status = self.executor_inst.tick(chosen_task, ctx)
        else:
            # 无任务可选时也要 halt 旧树松键：上一次 Approach 的移动键还按着，
            # 不松的话角色会一直朝一个方向顶墙（实机复现：RecoverLoosePot 因
            # 锅被 P1 端走变不可行后，角色卡在原地一直按 right）。
            self.executor_inst.halt_and_discard()
            status = "idle"

        # ⑬ 处理结果
        success_task = None  # type: Optional[Any]
        if status == "success":
            success_task = chosen_task  # 保存成功的 task（trace 记录用）
            self._fail_task_key = None
            self._fail_streak = 0
            if chosen_task and hasattr(chosen_task, "claims"):
                for res_id, action in chosen_task.claims():
                    self.bb.claims.pop((res_id, action), None)
            self.bb.commitment = None
        elif status == "failure":
            if chosen_task is not None:
                self._note_motion_failure(chosen_task, ctx)
                self._note_failure(chosen_task)
            if self.bb.commitment:
                self.bb.commitment.interrupt_failure_count += 1
            self.bb.commitment = None

        # ⑭ 日志
        keys = []
        if self.hands.move_dir:
            keys.append(self.hands.move_dir)
        if self.hands.move_extra:
            keys.append(self.hands.move_extra)
        # commitment 在终态已清空，仍记录本拍 task 和移动/观测阶段。
        exec_state = trace.execution_record(
            status, success_task if status == "success" else chosen_task, ctx,
            self._frame_seq, self._frame_received_at)
        trace.emit(record, new_mode, self.bb.commitment, exec_state, keys,
                   self.tick, state_dump.snapshot(world))

    def _task_done(self, task, world, ctx):
        # type: (Any, World, Dict) -> bool
        """该 task 的后置条件是否已经成立（done() 是纯读谓词）。

        用来判断"这棵树还差最后一口气"：后置条件已成立说明它这一拍就能报
        SUCCESS，不该被 planner 抢走（见 ⑨.5 的说明）。必须**明确返回 True**
        才算数（不做真值判断：这既挡住了 Mock，也要求 done() 的返回是布尔
        声明而不是随手一个真值）；抛异常一律当成"没成立"。
        """
        done = getattr(task, "done", None)
        if not callable(done):
            return False
        try:
            return done(ctx) is True
        except Exception:                       # noqa: BLE001
            return False

    def _note_failure(self, task):
        # type: (Any) -> None
        """记录同一 task 的连续执行失败，满阈值拉黑其认领资源（L3 兜底）。

        自愈层（HoldUntil 位置感知）已处理单次"被推离目标"；这里兜住
        "重新走位也反复失败"的情况——连续 ABANDON_FAIL_STREAK 次失败就
        把该 task 的资源写进 scratch.abandoned，planner 过滤时跳过它，
        让 AI 先去做别的任务（实机复现：切菜被传送带反复推走时 flap）。
        """
        key = task.key
        if key == self._fail_task_key:
            self._fail_streak += 1
        else:
            self._fail_task_key = key
            self._fail_streak = 1
        if self._fail_streak < ABANDON_FAIL_STREAK:
            return
        from ai.blackboard import StuckScratch
        if not isinstance(self.bb.scratch, StuckScratch):
            self.bb.scratch = StuckScratch()
        for res_id, _action in task.claims():
            self.bb.scratch.abandoned[res_id] = self.tick + ABANDON_TTL_TICKS
        self._fail_task_key = None
        self._fail_streak = 0

    def _note_motion_failure(self, task, ctx):
        # type: (Any, Dict) -> None
        note_motion_failure(self.bb, task, self.tick, ctx.get("motion_failure") or {})

    def _exec_context(self, world):
        # type: (World) -> dict
        """构造执行层上下文（primitives/base.py 的 ctx 契约）。

        keys: w / chef / hands / bb / field / book / tick。
        field 是每拍从 P2 位置做一次的 flood-fill 距离场（spec §6.4），
        并叠加 P1 周边软代价（Plan 4 Task 2）。
        """
        chef = P.chef_by_name(world.chefs, P.P2_NAME)
        field = {}
        soft = {}
        if chef is not None:
            cell = P.chef_cell(chef, world.to_grid)
            if cell is not None:
                scratch = self.bb.scratch
                bad = getattr(scratch, "bad_cells", None) or {}
                blocked = {c for c, expire in bad.items() if expire > self.tick}
                soft = p1model.p1_soft_cost_map(world, self.bb.p1_model)
                field = navigation.distance_field(
                    cell, world.walkable, world.belt_dir, blocked, soft_cost=soft)
        ctx = {
            "w": world,
            "chef": chef,
            "hands": self.hands,
            "bb": self.bb,
            "field": field, "soft_cost": soft,
            "book": self.book,
            "tick": self.tick,
        }
        if self._frame_seq is not None:
            ctx.update(frame_seq=self._frame_seq,
                       frame_received_at=self._frame_received_at,
                       now=time.monotonic())
        return ctx

    def _make_resource_view(self, world):
        # type: (World) -> reconcile.ResourceView
        """构造对账所需的资源视图。

        资源 id 必须与 task.claims() 的键一致，否则每个认领都会被对账
        误判为"资源消失"而释放：站点/锅/盘用实例名（scalar），拼装格用
        网格坐标 (gx,gz)（spec §4.4"坐标即身份"）。
        """
        from ai.reconcile import ResourceView
        existing_ids = set()
        contents = {}
        for st in world.stations:
            sid = st.get("n")
            if not sid:
                continue
            existing_ids.add(sid)
            if (st.get("t") == "counter"
                    and st.get("gx") is not None and st.get("gz") is not None):
                existing_ids.add((st.get("gx"), st.get("gz")))
            item = st.get("item")
            if item:
                contents[sid] = item
        for cooker in world.cookers:
            cid = cooker.get("n")
            if cid:
                existing_ids.add(cid)
                item = cooker.get("item")
                if item:
                    contents[cid] = item
        for plate in world.plates:
            pid = plate.get("n")
            if pid:
                existing_ids.add(pid)
        return ResourceView(existing_ids=existing_ids, contents=contents)

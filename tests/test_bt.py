"""BT 引擎语义。

重点钉两件事：
  1. **Sequence 有记忆**：已 SUCCESS 的子节点下一拍不再被 tick，进度从当前
     子节点继续（spec §7.1 / §7.6："顺序状态由 BT 的 RUNNING/halt 机制承载"
     "复用同一棵树（进度自然保留）"）。
  2. halt 沿 RUNNING 链向下传播（按键释放依赖它）。
"""
from ai.exec import bt
from ai.exec.bt import Status


class Rec(bt.Node):
    """记录 tick / halt 调用次数的探针节点。"""

    def __init__(self, statuses):
        # statuses: 每次 tick 依次返回的状态列表，耗尽后重复最后一个
        self.statuses = list(statuses)
        self.ticks = 0
        self.halts = 0

    def tick(self, ctx):
        i = min(self.ticks, len(self.statuses) - 1)
        self.ticks += 1
        return self.statuses[i]

    def halt(self):
        self.halts += 1


def test_sequence_all_success():
    a, b = Rec([Status.SUCCESS]), Rec([Status.SUCCESS])
    assert bt.Sequence([a, b]).tick({}) == Status.SUCCESS
    assert (a.ticks, b.ticks) == (1, 1)


def test_sequence_stops_at_failure():
    a, b = Rec([Status.FAILURE]), Rec([Status.SUCCESS])
    assert bt.Sequence([a, b]).tick({}) == Status.FAILURE
    assert b.ticks == 0, "FAILURE 后不该继续 tick 右边的兄弟"


def test_sequence_stops_at_running():
    a, b = Rec([Status.RUNNING]), Rec([Status.SUCCESS])
    assert bt.Sequence([a, b]).tick({}) == Status.RUNNING
    assert b.ticks == 0


def test_sequence_does_not_retick_completed_children():
    """有记忆：已 SUCCESS 的子节点下一拍不再被 tick（spec §7.1 / §7.6）。

    旧的无记忆语义会重 tick 已完成的 a，是"拿起又放下"的根因。
    """
    a = Rec([Status.SUCCESS])
    b = Rec([Status.RUNNING])
    seq = bt.Sequence([a, b])
    seq.tick({})
    seq.tick({})
    assert a.ticks == 1, "已完成的子节点不该被重 tick"
    assert b.ticks == 2


def test_sequence_resumes_at_current_child():
    """进度从当前子节点继续：a 成功后只在 b 上推进，不再回到 a。"""
    a = Rec([Status.SUCCESS, Status.FAILURE])   # 若被重 tick 第二拍就 FAILURE
    b = Rec([Status.RUNNING, Status.SUCCESS])
    seq = bt.Sequence([a, b])
    assert seq.tick({}) == Status.RUNNING       # a SUCCESS -> b RUNNING
    assert seq.tick({}) == Status.SUCCESS        # 从 b 继续，不回头 tick a
    assert a.ticks == 1, "a 不该被重 tick"


def test_sequence_halt_propagates_to_running_child():
    a = Rec([Status.RUNNING])
    seq = bt.Sequence([a])
    seq.tick({})
    seq.halt()
    assert a.halts == 1


def test_sequence_halts_previous_running_child_on_switch():
    """RUNNING 从 a 转到 b 时，b 是当前活动节点，halt 应打到 b。"""
    a = Rec([Status.RUNNING, Status.SUCCESS])
    b = Rec([Status.RUNNING])
    seq = bt.Sequence([a, b])
    seq.tick({})     # a RUNNING
    seq.tick({})     # a SUCCESS -> b RUNNING
    assert b.ticks == 1
    seq.halt()
    assert b.halts == 1


def test_sequence_halts_running_child_when_later_sibling_fails():
    a = Rec([Status.RUNNING, Status.SUCCESS])
    b = Rec([Status.FAILURE])
    seq = bt.Sequence([a, b])
    seq.tick({})
    assert seq.tick({}) == Status.FAILURE
    assert a.halts == 0, "子节点自己完成时不调用 halt（防止 spurious halt）"


def test_sequence_restarts_after_all_success():
    """整体成功后进度复位：再 tick 会从头再来。"""
    a = Rec([Status.SUCCESS])
    b = Rec([Status.SUCCESS])
    seq = bt.Sequence([a, b])
    assert seq.tick({}) == Status.SUCCESS
    assert seq.tick({}) == Status.SUCCESS
    assert a.ticks == 2 and b.ticks == 2


def test_selector_returns_first_non_failure():
    a, b = Rec([Status.FAILURE]), Rec([Status.SUCCESS])
    assert bt.Selector([a, b]).tick({}) == Status.SUCCESS
    assert (a.ticks, b.ticks) == (1, 1)


def test_selector_all_failure():
    a, b = Rec([Status.FAILURE]), Rec([Status.FAILURE])
    assert bt.Selector([a, b]).tick({}) == Status.FAILURE


def test_selector_does_not_reevaluate_earlier_failed_child():
    """有记忆：已 FAILURE 的候选不重评估，直接在当前 RUNNING 的候选上继续。"""
    a = Rec([Status.FAILURE, Status.SUCCESS])   # 若被重评估会变 SUCCESS
    b = Rec([Status.RUNNING, Status.SUCCESS])
    sel = bt.Selector([a, b])
    assert sel.tick({}) == Status.RUNNING
    assert sel.tick({}) == Status.SUCCESS
    assert a.ticks == 1


def test_selector_halt_propagates():
    a = Rec([Status.RUNNING])
    sel = bt.Selector([a])
    sel.tick({})
    sel.halt()
    assert a.halts == 1


def test_action_and_condition():
    assert bt.Action(lambda ctx: Status.SUCCESS).tick({}) == Status.SUCCESS
    assert bt.Condition(lambda ctx: True).tick({}) == Status.SUCCESS
    assert bt.Condition(lambda ctx: False).tick({}) == Status.FAILURE


def test_condition_receives_ctx():
    seen = []
    bt.Condition(lambda ctx: seen.append(ctx) or True).tick({"w": 1})
    assert seen == [{"w": 1}]


def test_parallel_is_gone():
    """spec §7.7：Parallel 从未接入，精简掉。

    留着一个没人用、没测试覆盖的并行节点，日后会有人以为它能用。
    """
    assert not hasattr(bt, "Parallel")


def test_repeat_until_pred_true_short_circuits():
    """pred 一开始成立 → 直接 SUCCESS，不 tick child。"""
    child = Rec([Status.SUCCESS])
    node = bt.RepeatUntil(lambda ctx: True, child)
    assert node.tick({}) == Status.SUCCESS
    assert child.ticks == 0


def test_repeat_until_loops_until_pred():
    """child 每轮 SUCCESS，pred 第三拍才成立 → 前两拍 RUNNING，第三拍 SUCCESS。"""
    state = {"n": 0}
    child = Rec([Status.SUCCESS])
    node = bt.RepeatUntil(lambda ctx: state["n"] >= 2, child)
    assert node.tick({}) == Status.RUNNING   # 第一轮 child SUCCESS，pred 未满足
    state["n"] = 1
    assert node.tick({}) == Status.RUNNING   # 第二轮
    state["n"] = 2
    assert node.tick({}) == Status.SUCCESS   # pred 成立，退出
    assert child.ticks == 2                  # child 只 tick 了两轮


def test_repeat_until_failure_propagates():
    child = Rec([Status.FAILURE])
    node = bt.RepeatUntil(lambda ctx: False, child)
    assert node.tick({}) == Status.FAILURE


def test_repeat_until_running_propagates():
    child = Rec([Status.RUNNING])
    node = bt.RepeatUntil(lambda ctx: False, child)
    assert node.tick({}) == Status.RUNNING
    assert child.ticks == 1


def test_repeat_until_halt_propagates():
    child = Rec([Status.RUNNING])
    node = bt.RepeatUntil(lambda ctx: False, child)
    node.tick({})
    node.halt()
    assert child.halts == 1


def test_repeat_until_pred_exit_halts_running_child():
    """pred 在 child RUNNING 期间成立 → 提前退出时必须 halt child（松键）。

    否则循环里按住键的叶子（喷灭火器）会在 pred 突然成立时被跳过 halt，
    键一直按着到进程退出。
    """
    state = {"done": False}
    child = Rec([Status.RUNNING])
    node = bt.RepeatUntil(lambda ctx: state["done"], child)
    assert node.tick({}) == Status.RUNNING     # child RUNNING
    state["done"] = True                       # pred 成立
    assert node.tick({}) == Status.SUCCESS
    assert child.halts == 1, "pred 提前退出要 halt 正在 RUNNING 的 child"


def test_guard_delegates_when_pred_true():
    """pred 成立 → 代理 child 的返回值。"""
    assert bt.Guard(lambda ctx: True, Rec([Status.SUCCESS])).tick({}) == Status.SUCCESS
    assert bt.Guard(lambda ctx: True, Rec([Status.FAILURE])).tick({}) == Status.FAILURE
    running = Rec([Status.RUNNING])
    assert bt.Guard(lambda ctx: True, running).tick({}) == Status.RUNNING
    assert running.ticks == 1


def test_guard_aborts_when_pred_turns_false():
    """pred 在 child RUNNING 期间变假 → halt child + FAILURE（observer abort）。"""
    state = {"ok": True}
    child = Rec([Status.RUNNING])
    node = bt.Guard(lambda ctx: state["ok"], child)
    assert node.tick({}) == Status.RUNNING     # 先跑起来
    state["ok"] = False                        # 条件翻转
    assert node.tick({}) == Status.FAILURE
    assert child.halts == 1, "中断必须 halt child（松键）"


def test_guard_false_pred_does_not_tick_child():
    """pred 一开始就是假 → 不 tick child，直接 halt + FAILURE。"""
    child = Rec([Status.SUCCESS])
    node = bt.Guard(lambda ctx: False, child)
    assert node.tick({}) == Status.FAILURE
    assert child.ticks == 0


def test_guard_halt_propagates():
    child = Rec([Status.RUNNING])
    node = bt.Guard(lambda ctx: True, child)
    node.tick({})
    node.halt()
    assert child.halts == 1

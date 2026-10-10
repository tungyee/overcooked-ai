"""BT 引擎语义。

重点钉两件事：
  1. **Sequence 有记忆**：已 SUCCESS 的子节点下一拍不再被 tick，进度从当前
     子节点继续（spec §7.1 / §7.6："顺序状态由 BT 的 RUNNING/halt 机制承载"
     "复用同一棵树（进度自然保留）"）。
  2. halt 沿 RUNNING 链向下传播（按键释放依赖它）。
"""
from ai import bt
from ai.bt import Status


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

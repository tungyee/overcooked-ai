"""对话策略：事件触发、限频、新单合并窗口与固定模板（spec §5.1–5.3）。"""
from ai.dialog.policy import (
    merge_new_orders,
    should_trigger,
)
from ai.dialog.schema import DialogState


# ---------------------------------------------------------------- speech

def test_speech_is_not_rate_limited():
    state = DialogState(last_speech_tick=100)
    assert should_trigger("speech", state, tick=110, transcript="我来切") is True


def test_identical_speech_within_4s_is_suppressed():
    state = DialogState()
    assert should_trigger("speech", state, tick=100, transcript="我先洗") is True
    # last_speech_tick 由 worker 在触发后回写
    state.last_speech_tick = 100
    # 空白与标点规范化后仍是同一句
    assert should_trigger("speech", state, tick=179, transcript=" 我先洗。") is False


def test_identical_speech_after_4s_triggers_again():
    state = DialogState(last_speech_tick=100, last_transcript="我先洗")
    assert should_trigger("speech", state, tick=179, transcript="我先洗") is False
    assert should_trigger("speech", state, tick=180, transcript="我先洗") is True


def test_different_speech_inside_4s_still_triggers():
    state = DialogState(last_speech_tick=100, last_transcript="我先洗")
    assert should_trigger("speech", state, tick=110, transcript="那你切") is True


def test_empty_final_does_not_break_speech_dedupe():
    state = DialogState()
    assert should_trigger("speech", state, tick=100, transcript="我先洗") is True
    state.last_speech_tick = 100
    # 空/纯标点的 ASR final 不是一句话：不触发，也不覆盖去重锚点
    for empty in ("", "   ", "，。！？"):
        assert should_trigger("speech", state, tick=150, transcript=empty) is False
        assert state.last_transcript == "我先洗"
    # 距 100 只有 51 拍（< 4 秒），仍应被原锚点抑制
    assert should_trigger("speech", state, tick=151, transcript="我先洗") is False


# ------------------------------------------------------------ rescue

def test_rescue_4s_interval():
    state = DialogState(last_rescue_tick=100)
    assert should_trigger("rescue", state, tick=150) is False
    assert should_trigger("rescue", state, tick=179) is False
    assert should_trigger("rescue", state, tick=180) is True


def test_help_event_no_longer_triggers():
    assert should_trigger("help", DialogState(), tick=10**6) is False


def test_new_order_merges_within_one_second():
    state = DialogState()
    first, batch = merge_new_orders([1], state, tick=100)
    assert first is False and batch == [1]
    second, batch = merge_new_orders([2], state, tick=110)
    assert second is False and batch == [1, 2]
    # 窗口内（< 20 拍）空调用也不触发
    third, batch = merge_new_orders([], state, tick=119)
    assert third is False and batch == [1, 2]


def test_new_order_flushes_when_window_elapses_with_empty_ids():
    state = DialogState()
    merge_new_orders([1], state, tick=100)
    merge_new_orders([2], state, tick=110)
    fired, batch = merge_new_orders([], state, tick=120)
    assert fired is True and batch == [1, 2]
    assert state.pending_orders == []
    # 第二批已经交出去，不会重复触发
    fired, batch = merge_new_orders([], state, tick=140)
    assert fired is False and batch == []


def test_new_order_held_batch_is_flushed_before_new_ids():
    state = DialogState()
    merge_new_orders([1], state, tick=100)
    merge_new_orders([2], state, tick=110)
    # 180 拍时 [1,2] 早已过窗：先冲出旧批，新单另起一批，谁都不丢
    fired, batch = merge_new_orders([3], state, tick=180)
    assert fired is True and batch == [1, 2]
    assert state.pending_orders == [3]
    # 下一拍把新单这批也交出去
    fired, batch = merge_new_orders([], state, tick=200)
    assert fired is True and batch == [3]


def test_new_order_window_starts_at_first_order_of_batch():
    state = DialogState()
    merge_new_orders([1], state, tick=100)
    merge_new_orders([2], state, tick=115)
    # 追加不刷新窗口：从 100 起算，120 已满 20 拍
    fired, batch = merge_new_orders([3], state, tick=120)
    assert fired is True and batch == [1, 2]
    assert state.pending_orders == [3]


def test_new_order_deduplicates_ids_within_a_call():
    state = DialogState()
    merge_new_orders([1], state, tick=100)
    _, batch = merge_new_orders([1, 2, 2], state, tick=105)
    assert batch == [1, 2]


def test_new_order_gate_reports_only_aged_batch():
    state = DialogState()
    merge_new_orders([1], state, tick=100)
    assert should_trigger("new_order", state, tick=110) is False
    assert should_trigger("new_order", state, tick=120) is True
    # 谓词不消费批次；清批由 merge_new_orders 负责
    assert state.pending_orders == [1]


# ---------------------------------------------------------------- other

def test_unknown_event_never_triggers():
    assert should_trigger("level_start", DialogState(), tick=100) is False



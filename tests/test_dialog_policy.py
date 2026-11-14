"""对话策略：事件触发、限频与固定模板（spec §5.1–5.3）。"""
from ai.dialog.policy import (
    should_checkin,
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


# ---------------------------------------------------------------- other

def test_unknown_event_never_triggers():
    assert should_trigger("level_start", DialogState(), tick=100) is False


# -------------------------------------------------------------- check-in

def test_checkin_requires_schedule_orders_level_live():
    now = 1_000_000.0
    # 没排期：不触发
    assert should_checkin(None, now, True, True, True) is False
    # 排期还没到：不触发
    assert should_checkin(now + 10, now, True, True, True) is False
    # 到点且有关卡有订单：触发
    assert should_checkin(now - 1, now, True, True, True) is True
    # 无订单不聊；不在关卡不聊；暂停/退出不聊
    assert should_checkin(now - 1, now, False, True, True) is False
    assert should_checkin(now - 1, now, True, False, True) is False
    assert should_checkin(now - 1, now, True, True, False) is False




def test_identical_speech_uses_wall_clock_when_available():
    """tick 可能滞后；有墙钟时按墙钟去重，避免 12 秒后同一句仍被吞。"""
    state = DialogState()
    assert should_trigger("speech", state, tick=160, transcript="去送餐吧",
                          now=1.0) is True
    assert should_trigger("speech", state, tick=170, transcript="去送餐吧",
                          now=13.0) is True
    assert should_trigger("speech", state, tick=175, transcript="去送餐吧",
                          now=14.0) is False

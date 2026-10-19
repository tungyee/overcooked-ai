"""按键截止时间的回归；注入假定时器，不碰 Quartz，也不 sleep。"""
from ai.output.key_scheduler import DeadlineKeys


class ManualTimer:
    def __init__(self, seconds, callback):
        self.seconds, self.callback = seconds, callback
        self.started = self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


def scheduler():
    events, timers = [], []

    def timer(seconds, callback):
        result = ManualTimer(seconds, callback)
        timers.append(result)
        return result

    return DeadlineKeys(lambda key, down: events.append((key, down)), timer), events, timers


def test_tap_returns_before_release_and_timer_releases():
    keys, events, timers = scheduler()
    keys.tap("pickup", 0.12)
    assert events == [("pickup", True)]
    assert keys.tapping("pickup")
    assert timers[0].seconds == 0.12 and timers[0].started
    timers[0].callback()
    assert events[-1] == ("pickup", False)
    assert not keys.tapping("pickup")


def test_cancelled_timer_cannot_release_a_new_press():
    keys, events, timers = scheduler()
    keys.tap("right", 0.05)
    keys.release("right")
    keys.press("right")
    timers[0].callback()
    assert events[-1] == ("right", True)
    assert timers[0].cancelled


def test_release_all_cancels_pending_taps_and_steps():
    keys, events, timers = scheduler()
    keys.tap("pickup", 0.12)
    keys.tap("right", 0.05)
    keys.release_all(["pickup", "right", "chop"])
    count = len(events)
    for timer in timers:
        assert timer.cancelled
        timer.callback()
    assert len(events) == count
    assert not keys.tapping("pickup")


def test_continuous_press_cancels_an_earlier_deadline():
    keys, events, timers = scheduler()
    keys.press("right")
    keys.schedule_release("right", 0.05)
    keys.press("right")
    timers[0].callback()
    assert events == [("right", True)]


def test_release_all_does_not_lift_keys_it_never_pressed():
    """release_all 只松自己真正按下的键，不补发未按过键的 key-up。

    主循环空闲守卫每拍调一次 release_all；若每次都对全部方向键补发
    key-up，会把人类玩家在选关页按住的同一批方向键反复「抬起」（车一顿
    一顿）。这里钉住：没按过的键不产生任何事件。
    """
    keys, events, _ = scheduler()
    keys.release_all(["up", "down", "left", "right", "pickup", "chop", "dash"])
    assert events == []


def test_release_all_is_idempotent_after_first_release():
    keys, events, _ = scheduler()
    keys.press("right")
    keys.release_all(["right"])
    n = len(events)
    assert ("right", False) in events
    keys.release_all(["right"])          # 第二次应空操作
    assert len(events) == n


def test_release_all_hard_lifts_every_given_key():
    keys, events, _ = scheduler()
    keys.release_all_hard(["up", "down", "left", "right"])
    assert events == [("up", False), ("down", False),
                      ("left", False), ("right", False)]

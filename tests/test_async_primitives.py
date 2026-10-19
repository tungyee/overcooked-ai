"""真实非阻塞按键契约下的原语；假事件与手动截止时间，不碰游戏。"""
from ai import primitives as PR
from ai.exec.bt import Status
from ai.output.hands import Hands
from tests.test_key_scheduler import scheduler
from tests.test_primitives import make_ctx, _inter


class TimedCtrl:
    def __init__(self):
        self.keys, self.events, self.timers = scheduler()

    def press(self, key):
        self.keys.press(key)

    def release(self, key):
        self.keys.release(key)

    def tap(self, key):
        self.keys.tap(key)

    def tapping(self, key):
        return self.keys.tapping(key)

    def schedule_release(self, key, seconds):
        self.keys.schedule_release(key, seconds)

    def release_all(self):
        self.keys.release_all(["left", "right", "up", "down", "pickup", "chop"])


def context():
    ctx, _ = make_ctx()
    ctrl = TimedCtrl()
    ctx["hands"] = Hands(ctrl)
    return ctx, ctrl


def test_tap_waits_for_key_release_without_repeating_press():
    ctx, ctrl = context()
    node = PR.Tap("pickup")
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.RUNNING
    assert ctrl.events == [("pickup", True)]
    ctrl.timers[0].callback()
    assert node.tick(ctx) == Status.SUCCESS
    assert ctrl.events == [("pickup", True), ("pickup", False)]


def test_belt_counter_covers_whole_tap_and_has_its_own_deadline():
    ctx, ctrl = context()
    node = PR.TapWhileHolding("pickup", "left")
    assert node.tick(ctx) == Status.RUNNING
    assert "left" in ctx["hands"].held_keys
    assert ctrl.events == [("left", True), ("pickup", True)]
    assert len(ctrl.timers) == 2
    for timer in ctrl.timers:
        timer.callback()
    assert node.tick(ctx) == Status.SUCCESS
    assert ctx["hands"].held_keys == set()
    assert ctrl.events[-2:] == [("pickup", False), ("left", False)]


def test_halt_cancels_pending_tap_and_counter():
    ctx, ctrl = context()
    node = PR.TapWhileHolding("pickup", "left")
    node.tick(ctx)
    node.halt()
    assert not ctrl.tapping("pickup")
    assert ctx["hands"].held_keys == set()
    assert all(timer.cancelled for timer in ctrl.timers)


def test_interaction_does_not_walk_while_async_tap_or_confirmation_pending():
    ctx, ctrl = context()
    target = {"n": "crate", "gx": 1, "gz": 0}
    ctx["chef"]["interactions"] = _inter(gx=1, gz=0, pickup_gx=1, pickup_gz=0)
    node = PR.InteractWith(lambda c: target, "pickup",
                           lambda c: c["chef"].get("held") == "Cucumber")
    assert node.tick(ctx) == Status.RUNNING
    # 已发出拾取，但后置事实还没来；被推走、源消失也只能等待确认。
    target = None
    ctx["chef"]["x"] = 6.0
    ctx["chef"]["interactions"] = _inter()
    assert node.tick(ctx) == Status.RUNNING
    ctrl.timers[0].callback()
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir is None
    ctx["chef"]["held"] = "Cucumber"
    assert node.tick(ctx) == Status.SUCCESS
    assert ctrl.events == [("pickup", True), ("pickup", False)]


def test_step_timer_releases_even_without_another_agent_tick():
    ctx, ctrl = context()
    ctx["hands"].pulse_move("right", now=1.0)
    assert ctrl.events == [("right", True)]
    ctrl.timers[0].callback()
    assert ctrl.events[-1] == ("right", False)
    ctx["hands"].service(1.05)
    assert ctx["hands"].move_dir is None

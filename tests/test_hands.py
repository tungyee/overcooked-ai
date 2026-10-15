"""Hands 按键输出层单测。

全程假 controller，零真实按键注入——Plan 2 的交付标准之一。
"""
from ai.hands import Hands, DIR2D, OPPOSITE, TAP_COOLDOWN_TICKS


class FakeCtrl(object):
    """记录调用序列的假 controller。接口与 oc/ai/controller.py 对齐。"""

    def __init__(self):
        self.calls = []

    def tap(self, name, hold=0.12):
        self.calls.append(("tap", name))

    def press(self, name):
        self.calls.append(("press", name))

    def release(self, name):
        self.calls.append(("release", name))

    def release_all(self):
        self.calls.append(("release_all",))


def test_set_move_presses_once_and_is_idempotent():
    """同方向重复 set_move 不重按——按住是持续态，重按会打断移动。"""
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("left")
    h.set_move("left")
    assert c.calls == [("press", "left")]
    assert h.move_dir == "left"


def test_set_move_switch_releases_old():
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("left")
    h.set_move("up")
    assert c.calls == [("press", "left"), ("release", "left"), ("press", "up")]


def test_set_move_none_releases():
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("left")
    h.set_move(None)
    assert c.calls[-1] == ("release", "left")
    assert h.move_dir is None


def test_set_move_extra_for_belt():
    """传送带上垂直穿越：主方向 + 顶带方向同时按住。"""
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("up", "left")
    assert ("press", "up") in c.calls and ("press", "left") in c.calls
    assert h.move_extra == "left"
    h.set_move("up", None)
    assert ("release", "left") in c.calls
    assert h.move_extra is None


def test_set_move_belt_entry_unchanged_dir_not_retouched():
    """进带时只改 extra，main 方向不变不重按。"""
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("up")
    c.calls.clear()
    h.set_move("up", "left")
    # 第二次调用只应该 press left，不碰 up
    assert c.calls == [("press", "left")]
    assert h.move_dir == "up" and h.move_extra == "left"


def test_set_move_belt_exit_only_releases_extra():
    """出带时只释放 extra，main 方向不变不重按。"""
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("up", "left")
    c.calls.clear()
    h.set_move("up", None)
    # 第二次调用只应该 release left，不碰 up
    assert c.calls == [("release", "left")]
    assert h.move_dir == "up" and h.move_extra is None


def test_set_move_swap_both_keys_remain_held():
    """交换 main 和 extra 的值：两键仍被按住，无虚假抬起。"""
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("up", "left")
    c.calls.clear()
    h.set_move("left", "up")
    # 交换应该无事件，因为按住的键集合相同
    assert c.calls == []
    assert h.move_dir == "left" and h.move_extra == "up"


def test_tap_edge_triggered_within_cooldown():
    """Review Focus 1：冷却内的第二次 tap 不按键，且返回 False。

    拾取是 toggle（oc/ai/agent.py:921），连点 = 拿起又放下。
    """
    c = FakeCtrl()
    h = Hands(c)
    assert h.tap("pickup") is True
    assert h.tap("pickup") is False
    assert c.calls == [("tap", "pickup")]


def test_tap_allowed_after_cooldown():
    c = FakeCtrl()
    h = Hands(c)
    h.tap("pickup")
    for _ in range(TAP_COOLDOWN_TICKS):
        h.tick()
    assert h.tap("pickup") is True
    assert c.calls.count(("tap", "pickup")) == 2


def test_tap_cooldown_is_per_key():
    """不同键各自冷却：按了 pickup 不该挡住 chop。"""
    c = FakeCtrl()
    h = Hands(c)
    assert h.tap("pickup") is True
    assert h.tap("chop") is True


def test_active_keys_reports_moves_holds_and_taps():
    """`active_keys()` 要报出本拍**所有**按键，供 trace 复盘用。

    trace 过去只记 move_dir/move_extra，动作键（pickup/chop/dash）一个都
    没记。于是「喷灭火器那 50 拍到底按住 chop 了没有」这种问题无法从
    trace 回答（trace-20261014-011202 t=2477~2526，火没灭、keys 全为空），
    只能靠猜 —— 观测缺口比 bug 本身更贵。
    """
    c = FakeCtrl()
    h = Hands(c)
    assert h.active_keys() == []

    h.set_move("left", "up", dash=True)
    assert set(h.active_keys()) == {"left", "up", "dash"}

    h.hold("chop")
    assert set(h.active_keys()) == {"left", "up", "dash", "chop"}

    # tap 是瞬时的，也要记上：否则「按了但没生效」与「根本没按」无法区分
    h.tap("pickup")
    assert "pickup" in h.active_keys()


def test_active_keys_forgets_tap_next_tick():
    """tap 只属于按下的那一拍，推进一拍就不该再报。"""
    c = FakeCtrl()
    h = Hands(c)
    h.tap("pickup")
    assert "pickup" in h.active_keys()
    h.tick()
    assert "pickup" not in h.active_keys()


def test_active_keys_includes_held_hold_across_ticks():
    """hold 是持续态，跨拍仍要报（与 tap 相反）。"""
    c = FakeCtrl()
    h = Hands(c)
    h.hold("chop")
    h.tick()
    assert "chop" in h.active_keys()
    h.release("chop")
    assert "chop" not in h.active_keys()


def test_hold_and_release_tracked():
    c = FakeCtrl()
    h = Hands(c)
    h.hold("chop")
    assert "chop" in h.held_keys
    h.release("chop")
    assert "chop" not in h.held_keys
    assert c.calls == [("press", "chop"), ("release", "chop")]


def test_hold_is_idempotent():
    """已按住再 hold 不重按——重按会让游戏侧看到一次抬起。"""
    c = FakeCtrl()
    h = Hands(c)
    h.hold("chop")
    h.hold("chop")
    assert c.calls == [("press", "chop")]


def test_release_unheld_key_is_noop():
    c = FakeCtrl()
    h = Hands(c)
    h.release("chop")
    assert c.calls == []


def test_release_all_clears_state():
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("left", "up")
    h.hold("chop")
    h.release_all()
    assert ("release_all",) in c.calls
    assert h.move_dir is None and h.move_extra is None
    assert h.held_keys == set()


def test_release_all_lets_next_tap_through():
    """release_all 是"全部收手"，不该让后续 tap 被旧冷却挡住。

    中断（火情/脱困）会调 release_all，此后抢占方的第一个 tap 必须生效，
    否则抢占方的首个交互要白等几拍。
    """
    c = FakeCtrl()
    h = Hands(c)
    h.tap("pickup")
    h.release_all()
    assert h.tap("pickup") is True


def test_counter_dir():
    """站在往右推的带上 → 顶左键。"""
    h = Hands(FakeCtrl())
    belt = {(0, 0): (1, 0), (1, 0): (0, -1)}
    assert h.counter_dir(belt, (0, 0)) == "left"
    assert h.counter_dir(belt, (1, 0)) == "up"
    assert h.counter_dir(belt, (5, 5)) is None
    assert h.counter_dir({}, (0, 0)) is None


def test_dir_tables_are_consistent():
    for d, opp in OPPOSITE.items():
        assert d in DIR2D and opp in DIR2D
        v, ov = DIR2D[d], DIR2D[opp]
        assert (v[0] + ov[0], v[1] + ov[1]) == (0, 0)


def test_set_move_dash_presses_and_is_idempotent():
    """冲刺键随方向一起按住；同状态重复调用不重按。"""
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("left", dash=True)
    h.set_move("left", dash=True)
    assert ("press", "dash") in c.calls
    assert c.calls.count(("press", "dash")) == 1
    assert h.move_dash is True


def test_set_move_dash_off_releases_but_keeps_direction():
    """近处松开冲刺、只放掉 dash，方向键保持按住。"""
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("left", dash=True)
    c.calls.clear()
    h.set_move("left", dash=False)
    assert c.calls == [("release", "dash")]
    assert h.move_dir == "left" and h.move_dash is False


def test_set_move_none_releases_dash():
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("left", dash=True)
    h.set_move(None)
    assert ("release", "dash") in c.calls
    assert h.move_dash is False


def test_release_all_clears_dash():
    c = FakeCtrl()
    h = Hands(c)
    h.set_move("left", dash=True)
    h.release_all()
    assert h.move_dash is False


def test_step_deadline_releases_without_any_new_position():
    c = FakeCtrl()
    h = Hands(c)
    h.pulse_move("up", seconds=0.05, extra="left", now=1.0)
    h.service(1.049)
    assert h.move_dir == "up"
    h.service(1.05)
    assert h.move_dir is None and h.move_extra is None
    assert ("release", "up") in c.calls
    assert ("release", "left") in c.calls


def test_cancelling_step_does_not_stop_a_new_continuous_move():
    c = FakeCtrl()
    h = Hands(c)
    h.pulse_move("right", now=1.0)
    h.set_move("up")
    h.service(2.0)
    assert h.move_dir == "up"

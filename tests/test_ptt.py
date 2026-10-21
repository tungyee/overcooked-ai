"""PTT 按键监听的离线测试（不创建真实 event tap）。"""
import pytest

from ai.dialog.ptt import PushToTalk, keycode_for


def test_keycode_for_known_and_numeric():
    assert keycode_for("f8", platform="darwin") == 100
    assert keycode_for("F8", platform="darwin") == 100
    assert keycode_for("right_option", platform="darwin") == 61
    assert keycode_for("100", platform="darwin") == 100
    with pytest.raises(ValueError):
        keycode_for("nope")


def test_poll_drains_events():
    ptt = PushToTalk(100)
    ptt._events.put("down")
    ptt._events.put("up")
    assert ptt.poll() == ["down", "up"]
    assert ptt.poll() == []


def test_start_fails_cleanly_without_event_tap():
    ptt = PushToTalk(100, tap_factory=lambda *args: None, ready_timeout=0.5,
                     platform="darwin")
    with pytest.raises(RuntimeError, match="PTT"):
        ptt.start()
    ptt.stop()      # 失败后 stop 必须可重复调用且不挂住

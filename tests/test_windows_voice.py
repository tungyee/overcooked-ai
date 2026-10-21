"""Windows 语音入口：键码、PTT 边沿、配置文件和启动器参数。"""
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from ai.dialog.ptt import PushToTalk, keycode_for
from ai.dialog.env import load_env_file


def test_windows_ptt_uses_vk_f8_instead_of_mac_keycode():
    assert keycode_for('F8', platform='win32') == 0x77
    assert keycode_for('f1', platform='win32') == 0x70
    assert keycode_for('f12', platform='win32') == 0x7b


def test_windows_right_alt_is_available_without_function_key_mode():
    assert keycode_for('right_alt', platform='win32') == 0xa5
    assert keycode_for('right_option', platform='win32') == 0xa5
    with pytest.raises(ValueError, match='right_alt'):
        keycode_for('invalid-key', platform='win32')


def test_windows_caps_lock_ptt_is_not_a_modifier_or_game_binding():
    from ai.dialog.ptt import WINDOWS_KEYCODES
    code = keycode_for('caps_lock', platform='win32')
    assert code == 0x14
    assert code not in {0x10, 0x11, 0x12, 0x5b, 0x5c, *range(0xa0, 0xa6)}
    assert code not in {WINDOWS_KEYCODES[n] for n in ('right_alt', 'right_control', 'right_shift')}


def test_windows_caps_lock_ptt_uses_current_pressed_bit_not_low_bit():
    finished = threading.Event()
    # 低位只能表示曾按过；只认实际按下的高位，不能由低位把 PTT 常开。
    values = iter([0x0001, 0x8001, 0x8000, 0x0001, 0x0000])
    def read(code):
        assert code == 0x14
        value = next(values, None)
        if value is None:
            finished.set()
            return 0x0001
        return value
    ptt = PushToTalk(keycode_for('caps_lock', platform='win32'),
                     platform='win32', state_reader=read)
    try:
        ptt.start()
        assert finished.wait(1.)
        assert ptt.poll() == ['down', 'up']
    finally:
        ptt.stop()


def test_windows_ptt_generates_one_event_per_edge_and_stops_thread():
    finished = threading.Event()
    values = iter([0, 0x8000, 0x8000, 0, 0])
    def read(code):
        assert code == 0x77
        value = next(values, None)
        if value is None:
            finished.set()
            return 0
        return value
    ptt = PushToTalk(0x77, platform='win32', state_reader=read)
    try:
        ptt.start()
        assert finished.wait(1.)
        assert ptt.poll() == ['down', 'up']
    finally:
        ptt.stop()
    assert ptt._thread is None


def test_windows_ptt_reports_reader_failure_without_hanging():
    def fail(code):
        raise OSError('test reader unavailable')
    ptt = PushToTalk(0x77, platform='win32', state_reader=fail)
    try:
        with pytest.raises(RuntimeError, match='PTT Windows'):
            ptt.start()
    finally:
        ptt.stop()


def test_powershell_utf8_bom_env_does_not_corrupt_first_variable(tmp_path):
    path = tmp_path/'voice.env'
    path.write_text('VOLC_ASR_API_KEY=fake-test-only\nARK_MODEL=fake-model\n', encoding='utf-8-sig')
    with patch.dict(os.environ, {}, clear=True):
        assert load_env_file(path) == 2
        assert os.environ['VOLC_ASR_API_KEY'] == 'fake-test-only'
        assert os.environ['ARK_MODEL'] == 'fake-model'

"""限时/失焦/协议守卫与安全键位，不发送实际输入。"""
from unittest.mock import Mock, patch
import sys
import pytest

from ai.state.world import World
from ai.output import trace
from tests.test_agent_lifecycle import _agent, _snapshot, RecordingCtrl
from run_agent import _parse_args


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows controller')
def test_windows_controller_does_not_send_dash_or_right_alt():
    from ai.output.controller import Controller
    backend = Mock()
    with patch('ai.output.windows_input.WindowsInput', return_value=backend):
        ctrl = Controller()
        ctrl.press('left')
        ctrl.press('dash')
        ctrl.release_all()
    assert backend.send.call_args_list == [((('left', True)),), ((('left', False)),)]


def test_focus_loss_releases_keys_and_does_not_plan():
    ctrl = RecordingCtrl()
    ctrl.is_game_foreground = lambda: False
    agent = _agent(ctrl)
    with patch('ai.agent.planner.plan') as plan:
        agent._tick_once(World(_snapshot()))
    plan.assert_not_called()
    assert ('release_all',) in ctrl.calls
    assert trace.read_log()[-1]['mode_reason'] == 'unfocused'


def test_live_entry_rejects_old_plugin_without_terrain_and_grids():
    agent = _agent(RecordingCtrl())
    agent.require_live_protocol = True
    with patch('ai.agent.planner.plan') as plan:
        agent._tick_once(World(_snapshot()))
    plan.assert_not_called()
    assert trace.read_log()[-1]['mode_reason'] == 'protocol_unavailable'


def test_time_limit_exits_even_while_menu_sends_no_frames(monkeypatch):
    agent = _agent(RecordingCtrl())
    agent.client = Mock()
    agent.client.read_latest.return_value = None
    clock = {'now': 0.0}
    monkeypatch.setattr('ai.agent.time.monotonic', lambda: clock['now'])
    def sleep(seconds):
        clock['now'] += seconds
    monkeypatch.setattr('ai.agent.time.sleep', sleep)
    agent.run(max_seconds=.1)
    assert clock['now'] >= .1
    assert ('release_all',) in agent.ctrl.calls


def test_stop_file_callback_exits_before_any_decision():
    agent = _agent(RecordingCtrl())
    agent.run(stop_requested=lambda: True)
    assert ('release_all',) in agent.ctrl.calls


def test_cli_has_safe_defaults_and_finite_limit():
    args = _parse_args(['--seconds', '150'])
    assert args.seconds == 150 and args.pickup_key == 'right_shift' and args.chop_key == 'right_ctrl'
    with pytest.raises(SystemExit):
        _parse_args(['--seconds', '0'])

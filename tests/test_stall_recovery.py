"""卡住恢复的行为回归；只用记录控制器，不连接游戏。"""
from unittest.mock import patch

from ai import tasks as T
from ai.core import planner, arbiter
from ai.state.blackboard import StuckScratch
from ai.state.world import World
from tests.test_agent_lifecycle import _agent, _snapshot, RecordingCtrl
from tests.test_arbiter import _make_world, _make_bb, _make_chef, _make_cooker, _make_extinguisher


def test_alternating_failures_eventually_back_off_each_operation():
    agent = _agent(RecordingCtrl())
    a, b = T.ReturnPot(resources=('a',)), T.ReturnPot(resources=('b',))
    for _ in range(3):
        agent._note_failure(a)
        agent._note_failure(b)
        agent.tick += 1
    assert planner._is_abandoned(a, agent.bb)
    assert planner._is_abandoned(b, agent.bb)


def test_order_changes_do_not_erase_repeated_physical_failure():
    agent = _agent(RecordingCtrl())
    for oid in ('1', '2', '3'):
        task = T.FetchToCooker(oid, ('hob', ('Rice',)), 'BoiledRice')
        agent._note_failure(task)
    assert planner._is_abandoned(task, agent.bb)


def test_another_task_success_does_not_erase_failures():
    agent = _agent(RecordingCtrl())
    a, b = T.ReturnPot(resources=('a',)), T.ReturnPot(resources=('b',))
    for _ in range(3):
        agent._note_failure(a)
        agent._note_success(b)
    assert planner._is_abandoned(a, agent.bb)


def test_same_task_success_clears_old_failure_history():
    agent = _agent(RecordingCtrl())
    task = T.ReturnPot(resources=('a',))
    agent._note_failure(task)
    agent._note_failure(task)
    agent._note_success(task)
    agent._note_failure(task)
    assert not planner._is_abandoned(task, agent.bb)


def test_old_failure_history_expires_and_cooldown_eventually_retries():
    agent = _agent(RecordingCtrl())
    task = T.ReturnPot(resources=('a',))
    agent._note_failure(task)
    agent.tick += 201
    agent.bb.tick = agent.tick
    agent._note_failure(task)
    agent._note_failure(task)
    assert not planner._is_abandoned(task, agent.bb)
    agent._note_failure(task)
    assert planner._is_abandoned(task, agent.bb)
    agent.bb.tick += 201
    assert not planner._is_abandoned(task, agent.bb)


def test_motion_failure_does_not_blacklist_other_actions_on_same_hob():
    agent = _agent(RecordingCtrl())
    fill = T.FetchToCooker('1', ('hob', ('Rice',)), 'BoiledRice')
    restore = T.ReturnPot(resources=('hob',))
    agent._note_motion_failure(fill, {'motion_failure': {'reason': 'no_progress'}})
    assert planner._is_abandoned(fill, agent.bb)
    assert not planner._is_abandoned(restore, agent.bb)


def test_stationary_work_does_not_suppress_new_fire():
    w = _make_world(cookers=[_make_cooker('hob', on_fire=True)],
        extinguishers=[_make_extinguisher('ext')],
        chefs=[_make_chef('Player 2', 3, 3), _make_chef('Player 1', 10, 10)])
    bb = _make_bb(scratch=StuckScratch(last_cell=w.to_grid(3, 3), same_count=10))
    assert arbiter.select_mode(w, bb)[0] == 'Firefighting'


def test_stationary_firefighter_exits_when_fire_is_gone():
    w = _make_world(cookers=[_make_cooker('hob', on_fire=False)],
                   chefs=[_make_chef('Player 2', 3, 3)])
    bb = _make_bb(mode='Firefighting',
                 scratch=StuckScratch(last_cell=w.to_grid(3, 3), same_count=10))
    assert arbiter.select_mode(w, bb)[0] == 'Normal'


def test_new_level_discards_old_resource_cooldowns_and_orders():
    agent = _agent(RecordingCtrl())
    frame = _snapshot()
    with patch('ai.agent.planner.plan', return_value=(None, {})):
        agent._tick_once(World(frame))
        agent.bb.doomed_orders['old_order'] = 1000
        agent.bb.scratch = StuckScratch(abandoned={'old_hob': 1000})
        frame['level'] = 'Balloon_1_5_2P'
        agent._tick_once(World(frame))
    assert not agent.bb.doomed_orders
    assert not getattr(agent.bb.scratch, 'abandoned', {})


def test_pause_does_not_erase_recovery_but_menu_reentry_does():
    agent = _agent(RecordingCtrl())
    frame = _snapshot()
    with patch('ai.agent.planner.plan', return_value=(None, {})):
        agent._tick_once(World(frame))
        agent.bb.scratch = StuckScratch(abandoned={'old_hob': 1000})
        frame['paused'] = True
        agent._tick_once(World(frame))
        assert agent.bb.scratch.abandoned == {'old_hob': 1000}
        agent._tick_once(World(dict(frame, phase='menu')))
        agent._tick_once(World(dict(frame, paused=False)))
    assert not getattr(agent.bb.scratch, 'abandoned', {})


def test_failed_task_releases_own_claims():
    agent = _agent(RecordingCtrl())
    task = T.ReturnPot(resources=('hob',))
    record = {'candidates': []}
    with patch('ai.agent.planner.plan', return_value=(task, record)), \
         patch.object(agent.executor_inst, 'tick', return_value='failure'):
        agent._tick_once(World(_snapshot()))
    assert all(key not in agent.bb.claims for key in task.claims())


def test_waiting_reason_is_written_to_trace():
    from ai.output import trace
    agent = _agent(RecordingCtrl())
    record = {'candidates': [(T.ReturnPot(resources=('hob',)), 0, {}, False, 'platform_moving', False)]}
    with patch('ai.agent.planner.plan', return_value=(None, record)):
        agent._tick_once(World(_snapshot()))
    entry = trace.read_log()[-1]
    assert entry['mode_reason'] == 'waiting_for_platform'
    assert entry['waiting']['veto_counts'] == {'platform_moving': 1}


def test_preempted_task_does_not_keep_obsolete_resource_claims():
    agent = _agent(RecordingCtrl())
    a, b = T.ReturnPot(resources=('a',)), T.ReturnPot(resources=('b',))
    with patch('ai.agent.planner.plan', side_effect=[(a, {}), (b, {})]), \
         patch.object(agent.executor_inst, 'tick', return_value='running'):
        agent._tick_once(World(_snapshot()))
        agent.tick += 1
        agent._tick_once(World(_snapshot()))
    assert set(agent.bb.claims) == set(b.claims())

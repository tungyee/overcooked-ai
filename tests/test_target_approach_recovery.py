"""目标附近卡住的反馈回归；不向游戏发送输入。"""
import gzip
import json
import pytest
from pathlib import Path
from unittest.mock import patch

from ai import primitives as PR
from ai.exec.bt import Status
from ai.output import trace
from ai.primitives.motion import MotionControl
from ai.query import predicates as P
from ai.space import navigation as N
from ai.state.world import World
from tests.test_primitives import FakeWorld, make_ctx, _inter
from tests.test_agent_lifecycle import _agent, RecordingCtrl
from ai.state.blackboard import StuckScratch


def frame_at(tick):
    path = Path(__file__).parent / 'fixtures/motion/sushi12-docking-20261019.ndjson.gz'
    with gzip.open(path, 'rt', encoding='utf8') as f:
        return next(s for s in map(json.loads, f) if s['_trace_source']['tick'] == tick)


def timed(ctx, tick):
    ctx.update(tick=tick, now=tick * .05, frame_seq=tick // 4 + 1,
               frame_received_at=(tick // 4) * .2)
    ctx['hands'].tick()


def test_path_backtracking_preserves_wall_edges():
    cells = {(0, 0), (0, 1), (1, 0), (1, 1)}
    blocked = lambda a, b: {a, b} == {(0, 1), (1, 1)}
    f = N.distance_field((0, 0), lambda x, z: (x, z) in cells, {}, edge_blocked=blocked)
    path = N.trace_back(f, (1, 1), {}, edge_blocked=blocked)
    assert path == [(0, 0), (1, 0), (1, 1)]


def test_live_delivery_faces_nearest_occupied_part_not_anchor_diagonal():
    w = World(frame_at(2777))
    chef = w.chef('Player 2')
    st = next(s for s in w.stations if s['t'] == 'plate_station')
    ctx, _ = make_ctx(w=w, chef=chef)
    assert PR.Face(st).tick(ctx) == Status.RUNNING
    assert ctx['hands'].move_dir == 'up', '右半边出餐口在正前方，不应向左追锚点'


def test_align_contact_with_correct_handle_does_not_wait_full_stall_timeout():
    w = FakeWorld(walkable=lambda x, z: (x, z) in {(0, 0), (-1, 0)})
    ctx, ctrl = make_ctx(w=w)
    ctx['chef'].update(x=.4, interactions=_inter(gx=0, gz=1, placement_gx=0, placement_gz=1))
    target = {'n': 'counter', 'gx': 0, 'gz': 1}
    node = PR.AlignToCell(target, 'placement')
    statuses = []
    for tick in range(13):
        timed(ctx, tick)
        statuses.append(node.tick(ctx))
        if statuses[-1] == Status.SUCCESS:
            break
    assert statuses[0] == Status.RUNNING, '保留至少一次收紧尝试，不盲信句柄'
    assert Status.SUCCESS in statuses, '贴墙不可能到格心时，不应每次白等 1.6 秒'
    assert ('press', 'left') in ctrl.calls


def test_approach_resumes_after_drifting_out_of_goal():
    ctx, _ = make_ctx()
    mc = MotionControl()
    timed(ctx, 0)
    ctx['hands'].set_move('right')
    mc.tick(ctx, None, None, False, True, replan_after_drift=True)
    # 松键后持续被推离目标，不应等满 observation_timeout 才重新计划。
    for tick, x in ((4, .3), (8, .7)):
        timed(ctx, tick)
        ctx['chef']['x'] = x
        status = mc.tick(ctx, 'left', None, False, False, final=False,
                         replan_after_drift=True)
    assert status == Status.RUNNING
    assert ctx['hands'].move_dir == 'left'


def test_live_valid_handle_can_finish_settling_while_pushed_within_goal_cell():
    w = World(frame_at(1228))
    chef = w.chef('Player 2')
    st = next(s for s in w.stations if s.get('n') == 'DispenserCrate 3 (1)')
    ctx, _ = make_ctx(w=w, chef=chef, start=P.chef_cell(chef, w.to_grid))
    assert P.interact_ready(chef, st, 'pickup', w.to_grid)
    node = PR.Approach(st, 'pickup')
    ctx['hands'].set_move('up')
    timed(ctx, 0)
    assert node.tick(ctx) == Status.RUNNING
    # 仍在交互格且新帧句柄正确，但有外力横向挤动，不应等绝对静止。
    chef['x'] -= .2
    chef['rel_x'] -= .2
    timed(ctx, 4)
    assert node.tick(ctx) == Status.SUCCESS
    assert ctx['hands'].move_dir is None


def test_live_balloon_blocked_stance_recovers_without_blind_interaction():
    path = Path(__file__).parent / 'fixtures/motion/balloon15-docking-20261019.ndjson.gz'
    with gzip.open(path, 'rt', encoding='utf8') as f:
        w = World(next(map(json.loads, f)))
    chef = w.chef('Player 2')
    cell = P.chef_cell(chef, w.to_grid)
    ctx, ctrl = make_ctx(w=w, chef=chef, start=cell)
    ctx['field'] = N.distance_field(cell, w.walkable, w.belt_dir, edge_blocked=w.edge_blocked)
    st = next(s for s in w.stations if s.get('n') == 'workstation_cooker_01 (4)')
    node = PR.InteractWith(st, 'placement', lambda c: False)
    recoveries = []
    # 冻结实机位姿模拟真实碰撞；必须结束/换位，而非永远在原位等。
    for tick in range(100):
        timed(ctx, tick)
        status = node.tick(ctx)
        recovery = ctx.pop('interaction_recovery', None)
        if recovery:
            recoveries.append(recovery)
        if status == Status.FAILURE:
            break
    assert status == Status.FAILURE
    assert recoveries and recoveries[0]['cell'] == cell
    assert ctx['motion']['goal'] != list(cell)
    assert ('tap', 'pickup') not in ctrl.calls


def test_timer_zero_releases_input_without_planning_live_frame():
    agent = _agent(RecordingCtrl())
    with patch('ai.agent.planner.plan') as plan:
        agent._tick_once(World(frame_at(3954)))
    plan.assert_not_called()
    assert ('release_all',) in agent.ctrl.calls
    assert trace.read_log()[-1]['mode_reason'] == 'round_finished'


def test_interaction_refaces_after_alignment_changes_facing():
    ctx, ctrl = make_ctx()
    ctx['chef']['interactions'] = _inter(gx=1, gz=0, placement_gx=1, placement_gz=0)
    node = PR.InteractWith({'n': 'counter', 'gx': 1, 'gz': 0}, 'placement', lambda c: False)
    class Nudged:
        def tick(self, c):
            c['chef']['interactions'] = _inter(gx=0, gz=1, placement_gx=1, placement_gz=0)
            return Status.SUCCESS
        def reset(self): pass
        def halt(self): pass
    node._align = Nudged()
    assert node.tick(ctx) == Status.RUNNING
    assert ('tap', 'pickup') not in ctrl.calls, '侧移后应重新朝向目标，再按拾放'
    assert ctx['hands'].move_dir == 'right'


def station_ctx():
    st = {'n': 'counter', 't': 'counter', 'gx': 0, 'gz': 1, 'x': 0., 'z': 1.2}
    w = FakeWorld(stations=[st], walkable=lambda x, z: -3 <= x <= 4 and -2 <= z <= 3 and (x, z) != (0, 1))
    ctx, ctrl = make_ctx(w=w)
    ctx['chef']['held'] = 'Fish'
    ctx['chef']['interactions'] = _inter(gx=0, gz=1, placement_gx=0, placement_gz=1)
    return ctx, ctrl, st


def test_failed_confirmation_changes_stance_on_new_observation_only():
    ctx, ctrl, st = station_ctx()
    node = PR.InteractWith(st, 'placement', lambda c: False, retries=0)
    timed(ctx, 0)
    assert node.tick(ctx) == Status.RUNNING
    assert ctrl.calls.count(('tap', 'pickup')) == 1
    timed(ctx, 4)
    assert node.tick(ctx) == Status.RUNNING
    assert ctx['interaction_recovery']['phase'] == 'confirm'
    scratch = ctx['bb'].scratch
    assert (P.resource_id(st), (0, 0)) in scratch.bad_pairs
    assert not scratch.bad_cells, '交互失败不能全局封路'
    timed(ctx, 5)  # 同一新帧不能立刻重按 toggle
    assert node.tick(ctx) == Status.RUNNING
    assert ctx['hands'].move_dir is None
    timed(ctx, 8)
    assert node.tick(ctx) == Status.RUNNING
    assert ctx['motion']['goal'] != [0, 0]
    assert ctx['hands'].move_dir is not None
    assert ctrl.calls.count(('tap', 'pickup')) == 1


class FailedFace:
    def tick(self, ctx): return Status.FAILURE
    def reset(self): pass
    def halt(self): pass


def test_local_recovery_has_finite_budget_and_marks_last_bad_stance():
    ctx, ctrl, st = station_ctx()
    node = PR.InteractWith(st, 'placement', lambda c: False)
    node._face = FailedFace()
    statuses = []
    for i, cell in enumerate(((0, 0), (-1, 1), (0, 2))):
        ctx['chef'].update(x=cell[0]*1.2, z=cell[1]*1.2)
        ctx['field'] = N.distance_field(cell, ctx['w'].walkable, {})
        timed(ctx, i * 4)
        statuses.append(node.tick(ctx))
    assert statuses == [Status.RUNNING, Status.RUNNING, Status.FAILURE]
    assert len(ctx['bb'].scratch.bad_pairs) == 3
    assert ('tap', 'pickup') not in ctrl.calls
    assert ctx['hands'].move_dir is None


def test_confirming_hand_change_does_not_repeat_toggle_after_timeout():
    ctx, ctrl, st = station_ctx()
    node = PR.InteractWith(st, 'placement', lambda c: False, retries=0)
    timed(ctx, 0)
    node.tick(ctx)
    ctx['chef']['held'] = 'Plate'  # 手变了但后置条件不成立：交还规划，不能重按
    timed(ctx, 4)
    assert node.tick(ctx) == Status.FAILURE
    assert ctrl.calls.count(('tap', 'pickup')) == 1


def test_after_tap_target_disappearance_waits_for_confirmation_without_walking():
    ctx, ctrl, st = station_ctx()
    target = st
    node = PR.InteractWith(lambda c: target, 'placement', lambda c: False)
    timed(ctx, 0)
    node.tick(ctx)
    target = None
    ctx['chef'].update(x=4., interactions=_inter())
    timed(ctx, 4)
    assert node.tick(ctx) == Status.RUNNING
    assert ctx['interaction']['phase'] == 'confirm'
    assert ctx['hands'].move_dir is None
    assert ctrl.calls.count(('tap', 'pickup')) == 1


def test_ensure_hands_free_refreshes_moving_station_and_reapproaches_before_tap():
    ctx, ctrl, st = station_ctx()
    node = PR.EnsureHandsFree(pick=lambda c: c['w'].stations[0])
    # 首次接近后暂未完成朝向。
    class WaitingFace(FailedFace):
        def tick(self, ctx): return Status.RUNNING
    node._face = WaitingFace()
    timed(ctx, 0)
    assert node.tick(ctx) == Status.RUNNING
    moved = dict(st, x=3.6, gx=3)
    ctx['w'].stations = [moved]
    timed(ctx, 4)
    assert node.tick(ctx) == Status.RUNNING
    assert ctx['interaction_recovery']['reason'] == 'lost_stance'
    timed(ctx, 8)
    assert node.tick(ctx) == Status.RUNNING
    assert ctx['motion']['target'] == [3, 1]
    assert ctx['hands'].move_dir == 'right'
    assert ('tap', 'pickup') not in ctrl.calls


def test_bad_interaction_stance_remains_available_as_transit_cell():
    w = FakeWorld(walkable=lambda x, z: z == 0 and 0 <= x <= 3)
    ctx, _ = make_ctx(w=w)
    st = {'n': 'counter', 'gx': 2, 'gz': 1}
    ctx['bb'].scratch = StuckScratch()
    ctx['bb'].scratch.bad_pairs[(P.resource_id(st), (1, 0))] = 200
    assert PR.Approach(st).tick(ctx) == Status.RUNNING
    assert ctx['hands'].move_dir == 'right'


@pytest.mark.parametrize('start', [-6.3, -.8, .8, 6.3])
@pytest.mark.parametrize('speed', [4., 6., 8.])
@pytest.mark.parametrize('coast', [0., .35, .5])
def test_near_goal_converges_with_delayed_frames_and_inertia(start, speed, coast):
    w = FakeWorld(walkable=lambda x, z: z == 0 and -8 <= x <= 8)
    ctx, _ = make_ctx(w=w)
    node = PR.Approach({'n': 'counter', 'gx': 0, 'gz': 1})
    x, velocity, seq = start, 0., 0
    for tick in range(200):
        if tick % 4 == 0:
            seq += 1
            ctx['chef']['x'] = x
            cur = w.to_grid(x, 0.)
            ctx['field'] = N.distance_field(cur, w.walkable, {})
            ctx.update(frame_seq=seq, frame_received_at=tick*.05)
        ctx['now'] = tick*.05
        status = node.tick(ctx)
        direction = ctx['hands'].move_dir
        velocity = speed if direction == 'right' else -speed if direction == 'left' else velocity*coast
        x += velocity*.05
        if status != Status.RUNNING:
            break
    assert status == Status.SUCCESS, (start, speed, coast, x, ctx.get('motion'))
    for _ in range(8):
        velocity *= coast
        x += velocity*.05
    assert w.to_grid(x, 0.) == (0, 0), '宣告到位后仍应留在交互格'
    assert ctx['hands'].move_dir is None

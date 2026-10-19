"""1-4 台面盘误作地面物与失败站位未进入规划的真实帧回归。"""
import gzip
import json
from pathlib import Path

from ai import primitives as PR, tasks as T
from ai.core import planner
from ai.primitives.base import resolve
from ai.query import predicates as P
from ai.query.recipes import RecipeBook
from ai.space import reachability as R
from ai.state.blackboard import Blackboard, StuckScratch
from ai.state.world import World
from tests.test_primitives import make_ctx
from tests.test_agent_lifecycle import _agent, RecordingCtrl
import pytest


def context(tick=4715):
    path = Path(__file__).parent / 'fixtures/motion/sushi14-plate-stall-20261019.ndjson.gz'
    with gzip.open(path, 'rt', encoding='utf8') as f:
        w = World(next(r for r in map(json.loads, f) if r['_trace_source']['tick'] == tick))
    chef = w.chef('Player 2')
    ctx, ctrl = make_ctx(w=w, chef=chef, start=P.chef_cell(chef, w.to_grid))
    ctx['field'] = R.field_for(w, ctx['bb'])
    ctx['book'] = RecipeBook(w.recipes, w.stations)
    plate = next(p for p in w.plates if p['n'] == '-57568')
    return ctx, ctrl, plate


def test_table_plate_has_surface_interaction_coordinates_without_changing_identity():
    ctx, _, plate = context()
    target = resolve(plate, ctx)
    assert target.get('interaction_surface') == 'countertop_01_standard_wood (1)'
    assert (target['interaction_gx'], target['interaction_gz']) == (-1, 0)
    assert P.resource_id(target) == P.resource_id(plate)
    assert (target['x'], target['z'], target['n']) == (plate['x'], plate['z'], plate['n'])
    assert 'interaction_surface' not in plate, '不得污染观测'


def test_live_plate_on_counter_faces_left_instead_of_skipping_face():
    ctx, ctrl, plate = context()
    assert PR.Face(plate).tick(ctx) == 'running'
    assert ctx['hands'].move_dir == 'left'
    assert ('tap', 'pickup') not in ctrl.calls


def test_live_placing_cucumber_refaces_plate_on_upper_counter():
    ctx, ctrl, plate = context(5700)
    assert PR.Face(plate).tick(ctx) == 'running'
    assert ctx['hands'].move_dir == 'up'
    assert ('tap', 'pickup') not in ctrl.calls


def test_bad_only_stance_is_rejected_by_planner_even_for_emergency_plating():
    ctx, _, plate = context()
    bb = ctx['bb']
    bb.scratch = StuckScratch()
    cell = P.chef_cell(ctx['chef'], ctx['w'].to_grid)
    bb.scratch.bad_pairs[(P.resource_id(plate), cell)] = 200
    task = T.TakeCookedToPlate('2', (('GridManager', 'workstation_cooker_01 (2)'), plate['n']), 'BoiledSushiRice')
    assert task.NO_ABANDON
    assert not R.can_interact(ctx, plate), '站位失败必须进入规划，而非每拍选同一个急单'
    assert R.task_veto(task, ctx) == 'unreachable'
    candidates = [planner.Candidate(task, [], 20)]
    assert planner.filter_feasible(ctx['w'], bb, candidates, ctx['book']) == []


def test_other_plate_and_cooker_rescue_stay_available_when_one_plate_has_no_stance():
    ctx, _, plate = context()
    ctx['bb'].scratch = StuckScratch()
    cell = P.chef_cell(ctx['chef'], ctx['w'].to_grid)
    ctx['bb'].scratch.bad_pairs[(P.resource_id(plate), cell)] = 200
    other = next(p for p in ctx['w'].plates if p['n'] == '-57570')
    assert R.can_interact(ctx, other)
    cooker = next(s for s in ctx['w'].stations if s.get('n') == 'workstation_cooker_01 (2)')
    assert R.can_interact(ctx, cooker), '坏盘位置不能阻止端锅急救'


def test_bad_stance_ttl_expiration_allows_retry():
    ctx, _, plate = context()
    ctx['bb'].scratch = StuckScratch()
    cell = P.chef_cell(ctx['chef'], ctx['w'].to_grid)
    ctx['bb'].scratch.bad_pairs[(P.resource_id(plate), cell)] = 200
    assert not R.can_interact(ctx, plate)
    ctx['bb'].tick = 200
    assert R.can_interact(ctx, plate)


@pytest.mark.parametrize('tick', [4719, 4800])
def test_agent_replay_moves_to_rescue_instead_of_repeating_bad_plate(tick):
    ctx, _, plate = context(tick)
    agent = _agent(RecordingCtrl())
    agent.tick = tick
    agent.bb.scratch = StuckScratch()
    cell = P.chef_cell(ctx['chef'], ctx['w'].to_grid)
    agent.bb.scratch.bad_pairs[(P.resource_id(plate), cell)] = tick + 200
    agent._tick_once(ctx['w'])
    assert agent.executor_inst.active_task.TYPE == 'RescueCooked'
    assert agent.hands.move_dir is not None


def test_actual_plate_turn_pick_confirm_sequence_finishes_without_blacklisting():
    ctx, ctrl, plate = context()
    node = PR.InteractWith(plate, 'pickup', lambda c: P.holds_plate(c['chef'], plate))
    def observe(tick):
        ctx.update(tick=tick, now=tick*.05, frame_seq=tick//4+1,
                   frame_received_at=(tick//4)*.2)
        ctx['hands'].tick()
    observe(0)
    assert node.tick(ctx) == 'running'
    assert ctx['hands'].move_dir == 'left'
    assert ('tap', 'pickup') not in ctrl.calls
    observe(1)
    node.tick(ctx)
    # 下一帧游戏确认已经面朝盘所在台面。
    inter = ctx['chef']['interactions']
    inter.update(grid='countertop_01_standard_wood (1)', gx=-1, gz=0,
                 pickup='countertop_01_standard_wood (1)', pickup_gx=-1, pickup_gz=0,
                 placement='countertop_01_standard_wood (1)', placement_gx=-1, placement_gz=0)
    ctx['chef']['yaw'] = 270
    for tick in range(4, 12):
        observe(tick)
        assert node.tick(ctx) == 'running'
        if ('tap', 'pickup') in ctrl.calls:
            break
    assert ctrl.calls.count(('tap', 'pickup')) == 1
    plate['held_by'] = 'Player 2'
    ctx['chef']['held'] = plate['name']
    observe(12)
    assert node.tick(ctx) == 'success'
    assert not getattr(ctx['bb'].scratch, 'bad_pairs', {})


@pytest.mark.parametrize('held,offset', [(None, .8), ('Player 1', 0.)])
def test_surface_hint_does_not_bind_neighbour_or_held_item(held, offset):
    ctx, _, plate = context()
    plate.update(held_by=held, x=plate['x'] + offset)
    assert resolve(plate, ctx) is plate


def test_surface_hint_uses_world_position_when_two_grids_share_local_coordinates():
    ctx, _, _ = context()
    ctx['w'].stations = [
        dict(t='counter', n='main', grid='main', gx=0, gz=0, x=0., z=0.),
        dict(t='counter', n='basket', grid='basket', gx=0, gz=0, x=20., z=0.)]
    plate = dict(n='plate', name='equipment_plate_01', x=20., z=0., held_by=None)
    target = resolve(plate, ctx)
    assert target['interaction_grid'] == 'basket'
    assert target['interaction_surface'] == 'basket'
    assert P.resource_id(target) == ('', 'plate')

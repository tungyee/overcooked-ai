"""真实有界拓扑中的规划过滤：不拿存在性替代可达性。"""
from unittest.mock import patch

from ai import tasks as T
from ai.core import planner
from ai.query.recipes import RecipeBook, find_ingredient
from ai.state.blackboard import Blackboard
from ai.tasks.base import ingredient_available
from tests.test_primitives import FakeWorld


def world(connected=False):
    w = FakeWorld(cell=1, chefs=[dict(name='Player 2', x=0, z=0, held=None)],
        stations=[dict(t='chopping', n='local', x=1, z=1, gx=1, gz=1),
                  dict(t='chopping', n='remote', x=7, z=1, gx=7, gz=1),
                  dict(t='crate', n='crate', ing='Tomato', chop='ChoppedTomato', chop_stages=8,
                       x=7, z=-1, gx=7, gz=-1)],
        workstations=[dict(n='local', item='Tomato'), dict(n='remote', item='Tomato')],
        walkable=lambda x,z: z==0 and (0<=x<=8 if connected else 0<=x<=2 or 6<=x<=8))
    w.terrain = {9: [(-.4,8.4,-.4,.4)]}  # 声明有当前地形，缺拓扑的旧帧另测。
    w.edge_blocked = lambda a,b: False
    return w


def test_planner_drops_unreachable_high_priority_work_before_layer_selection():
    w = world()
    tasks = [T.Chop('1', ('remote',), 'Tomato'), T.Chop('1', ('local',), 'Tomato')]
    candidates = [planner.Candidate(t, [], 20) for t in tasks]
    kept = planner.filter_feasible(w, Blackboard(), candidates, RecipeBook([], w.stations))
    assert [c.task for c in kept] == [tasks[1]]


def test_unreachable_source_does_not_make_fetch_feasible():
    w = world()
    w.workstations = []
    ctx = planner._make_context(w, Blackboard(), RecipeBook([], w.stations))
    assert not ingredient_available(ctx, 'Tomato')


def test_unreachable_loose_item_does_not_hide_reachable_crate():
    w = world()
    w.workstations = []
    w.items = [dict(n='Tomato', x=7, z=0)]
    w.stations[-1].update(x=1, z=-1, gx=1)
    assert find_ingredient(w, 'Tomato', exclude_plate=True)['kind'] == 'crate'


def test_already_held_ingredient_does_not_require_returning_to_source():
    w = world()
    w.workstations = []
    w.chefs[0]['held'] = 'Tomato'
    ctx = planner._make_context(w, Blackboard(), RecipeBook([], w.stations))
    assert ingredient_available(ctx, 'Tomato')


def test_platform_reconnection_is_rechecked_in_next_snapshot():
    for connected in (False, True, False):
        w = world(connected)
        w.workstations = []
        ctx = planner._make_context(w, Blackboard(), RecipeBook([], w.stations))
        assert ingredient_available(ctx, 'Tomato') is connected


def test_cross_grid_local_coordinates_are_not_used_as_global_path_coordinates():
    w = world()
    w.workstations = []
    w.stations[-1].update(grid='basket', x=1, z=-1, gx=100, gz=100)
    ctx = planner._make_context(w, Blackboard(), RecipeBook([], w.stations))
    assert ingredient_available(ctx, 'Tomato')


def test_thin_wall_edge_blocks_source_even_if_floor_is_contiguous():
    w = world(True)
    w.workstations = []
    w.edge_blocked = lambda a,b: {a,b}=={(3,0),(4,0)}
    ctx = planner._make_context(w, Blackboard(), RecipeBook([], w.stations))
    assert not ingredient_available(ctx, 'Tomato')


def test_same_snapshot_reuses_one_distance_field_for_source_queries():
    from ai.space import reachability
    w = world()
    w.workstations = []
    with patch('ai.space.reachability.navigation.distance_field',
               wraps=reachability.navigation.distance_field) as build:
        for _ in range(12):
            find_ingredient(w, 'Tomato', exclude_plate=True)
        assert build.call_count == 1


def test_legacy_without_topology_keeps_offline_compatibility():
    w = world()
    w.terrain = {}
    w.workstations = []
    assert find_ingredient(w, 'Tomato', exclude_plate=True)['kind'] == 'crate'


def test_local_approach_replan_keeps_moving_platform_mask():
    from ai import primitives as PR
    from ai.exec.bt import Status
    from tests.test_primitives import make_ctx
    w = world(True)
    ctx, ctrl = make_ctx(w=w, chef=w.chefs[0], start=(0,0))
    ctx['navigation_blocked'] = {(3,0)}
    result = PR.Approach(dict(n='target', x=7, z=1)).tick(ctx)
    assert result == Status.FAILURE
    assert not any(call[0] == 'press' for call in ctrl.calls)


def test_discard_resolves_logical_bin_claim_by_type():
    from ai.space import reachability
    w = world()
    w.cookers = [dict(n='hob', x=1, z=1)]
    w.stations.append(dict(t='bin', n='workstation_bin_01', x=1, z=-1))
    ctx = planner._make_context(w, Blackboard())
    assert reachability.task_veto(T.Discard(resources=('hob',)), ctx) == ''


def test_discard_with_only_remote_bin_reports_unreachable_destination():
    from ai.space import reachability
    w = world()
    w.cookers = [dict(n='hob', x=1, z=1)]
    w.stations.append(dict(t='bin', n='workstation_bin_01', x=7, z=-1))
    ctx = planner._make_context(w, Blackboard())
    assert reachability.task_veto(T.Discard(resources=('hob',)), ctx) == 'unreachable_destination'

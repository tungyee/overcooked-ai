# -*- coding: utf-8 -*-
"""3_6_Dynamic_Lvl_02：S 形走廊夹在两排格中心之间（trace-20261101-184314）。

真实帧 tests/fixtures/dynamic/dyn36-20261101-184314-lane.ndjson.gz（`_trace_t`
为 trace 拍号，state 已还原成插件原始帧形状）：
  [0] t=8040：上层木筏 Raft One 停在中间平台右侧，同一行横接，P2 可达出餐口；
  [1] t=9700：Raft One 停在平台正上方，P1 正站在平台中间走廊 (11.15,-7.83)；
  [2] t=9772：P2 端着空盘，鸡肉卷盘 -61738（队首单）料齐，DeliverPlate 被判
      unreachable_destination；
  [3] t=9800：同 [2]，P2 在 Raft Two 上。

平台上两只料箱错开半格（x 10.2~11.4 / 11.4~12.6），中间 z∈(-8.4,-7.2) 一整行
是全开的走廊，但主网格（Raft Two）格中心 z=-8.4/-7.2 正好压在料箱边上：
gz=3 只有右半边可走、gz=4 只有左半边可走，上下错开不相邻，S 弯被切断。
"""
import gzip
import json
import os

import pytest

from ai import tasks as T
from ai.core import planner
from ai.query import predicates as P
from ai.query.recipes import RecipeBook
from ai.space import lanes, navigation, portals, reachability as R
from ai.state import world as W
from ai.state.bb_factory import make_bb

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "dynamic",
                       "dyn36-20261101-184314-lane.ndjson.gz")
CHICKEN_PLATE = "-61738"


@pytest.fixture(scope="module")
def frames():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _hatch(w):
    return next(s for s in w.stations if s.get("t") == "plate_station")


def _hatch_reachable(w, name=P.P2_NAME):
    field = R.field_for(w, None, name)
    ctx = dict(w=w, bb=None, chef=P.chef_by_name(w.chefs, name), field=field)
    return R.can_interact(ctx, _hatch(w))


def _cell(w, name):
    return P.chef_cell(P.chef_by_name(w.chefs, name), w.to_grid)


# ---------------------------------------------------------------- 根因复现


def test_grid_rows_offset_around_corridor(frames):
    """[3] 格中心采样：走廊两侧的可走格上下错开，普通四邻连不上。"""
    w = W.World(frames[3])
    assert [gx for gx in range(-8, 4) if w.walkable(gx, 3)] == [-3, -2]
    assert [gx for gx in range(-8, 4) if w.walkable(gx, 4)] == [-5, -4]
    old = navigation.distance_field(_cell(w, P.P2_NAME), w.walkable, w.belt_dir,
                                    edge_blocked=w.edge_blocked)
    assert (-4, 4) not in old


# ---------------------------------------------------------------- 修复


@pytest.mark.parametrize("idx", [2, 3])
def test_hatch_reachable_through_corridor(frames, idx):
    w = W.World(frames[idx])
    assert _hatch_reachable(w)


def test_corridor_links_both_rafts_from_p1(frames):
    """[1] P1 站在走廊里（真实走得过去）：从它出发，下层 Raft Two 与出餐口都可达。

    （这一帧 P2 自己站在 Raft Two 边缘 x=15.8，格中心 16.0 越出地板 15.98，
    起点格四邻全不可走——另一个问题，不在本用例范围。）
    """
    w = W.World(frames[1])
    field = R.field_for(w, None, P.P1_NAME)
    assert (3, 0) in field                     # Raft Two 上、P2 斜邻那格
    assert _hatch_reachable(w, P.P1_NAME)


def test_side_dock_unchanged(frames):
    """[0] 侧边横接本来就通：可达场不因走廊边而变（只补断开的地方）。"""
    w = W.World(frames[0])
    field = R.field_for(w, None)
    old = navigation.distance_field(_cell(w, P.P2_NAME), belt_dir=w.belt_dir,
                                    **dict(portals.nav_args(w, None),
                                           jumps={}, jump_cost={}))
    assert set(old) <= set(field)
    assert _hatch_reachable(w)


def test_deliver_not_vetoed_unreachable(frames):
    w = W.World(frames[2])
    bb = make_bb(frames[2]["_trace_t"])
    ctx = planner._make_context(w, bb, RecipeBook(w.recipes, w.stations))
    assert R.task_veto(T.DeliverPlate("1", (CHICKEN_PLATE,)), ctx) == ""


@pytest.mark.parametrize("idx", [2, 3])
def test_planner_delivers_head_chicken_plate(frames, idx):
    """最后几秒：队首鸡肉卷盘料齐，P2 选送餐（修复前被 unreachable_destination 否决）。"""
    w = W.World(frames[idx])
    task, _ = planner.plan(w, make_bb(frames[idx]["_trace_t"]), None,
                           RecipeBook(w.recipes, w.stations))
    assert task.TYPE == "DeliverPlate" and task.plate_id == CHICKEN_PLATE


def test_route_turns_inside_corridor(frames):
    """move 路点在走廊中线拐弯，不斜穿料箱。"""
    w = W.World(frames[3])
    field = R.field_for(w, None)
    goal = (-4, 4)
    path = navigation.trace_back(field, goal, w.belt_dir)
    assert path and path[-1] == goal
    points, gates, hops, _ = portals.route(w, path, field, None)
    assert not hops
    a = path.index((-3, 3)) if (-3, 3) in path else path.index((-2, 3))
    ax, az = w.to_world(*path[a])
    bx, bz = w.to_world(*goal)
    mid_z = (az + bz) / 2.0
    corner = [p for p in points if abs(p[1] - mid_z) < 1e-6]
    assert len(corner) == 2
    assert {round(corner[0][0], 4), round(corner[1][0], 4)} == {round(ax, 4), round(bx, 4)}
    # 相邻路点都是轴向直线段
    for p, q in zip(points, points[1:]):
        assert abs(p[0] - q[0]) < 1e-6 or abs(p[1] - q[1]) < 1e-6


# ---------------------------------------------------------------- 合成：不误连


def _synthetic(mid_wall=False):
    """Platform 那段几何的最小复刻：两只错开的料箱，中间一行全开。"""
    crates = [[10.2, 11.4, -9.6, -8.4], [11.4, 12.6, -7.2, -6.0]]
    if mid_wall:
        crates.append([11.2, 11.6, -8.4, -7.2])   # 走廊中段一道墙
    return {
        "level": "Synthetic",
        "grid": {"size": "(1.2, 1.0, 1.2)", "mapOffset": "0,0", "nodeMap": None},
        "grids": [{"id": "GridManager", "ox": 15.8, "oz": -12.0, "cell": 1.2,
                   "main": True, "moving": False}],
        "terrain": [{"layer": 9, "boxes": [[9.05, 13.9, -9.68, -5.95]]},
                    {"layer": 26, "boxes": crates}],
        "chefs": [{"name": P.P2_NAME, "x": 12.2, "z": -8.4},
                  {"name": P.P1_NAME, "x": 11.0, "z": -6.0}],
        "stations": [{"n": "Counter", "t": "counter", "x": 9.8, "z": -9.6,
                      "gx": -5, "gz": 2, "grid": "GridManager"}],
    }


def test_synthetic_corridor_connects():
    w = W.World(_synthetic())
    assert w.walkable(-3, 3) and w.walkable(-4, 4)
    assert not w.walkable(-3, 4) and not w.walkable(-4, 3)
    assert (-4, 4) in R.field_for(w, None)


def test_wall_in_corridor_blocks_lane():
    w = W.World(_synthetic(mid_wall=True))
    assert (-4, 4) not in R.field_for(w, None)


def test_open_floor_has_no_lanes():
    """普通开阔地：直走邻格可走，一条走廊边都不加。"""
    frame = _synthetic()
    frame["terrain"] = [{"layer": 9, "boxes": [[9.05, 13.9, -9.68, -5.95]]}]
    w = W.World(frame)
    lm = w.lane_map
    for gx in range(-6, 0):
        for gz in range(1, 6):
            if w.walkable(gx, gz):
                assert not lm.hops_from((gx, gz), w.walkable)

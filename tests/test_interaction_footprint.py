"""交互占地：移动篮亚格偏移时，台面上的盘/站点不因格量化判不可达。

真实帧 tests/fixtures/balloon/balloon23-20261030-002104.ndjson.gz
（trace-20261030-002104，Balloon_2_3_2P）：
  [0] t=852  盘 -16202 量化在 (13,5)，前方 (13,4) 可走（对照组）
  [1] t=853  篮子上漂，盘心越过格界量化到 (13,6)；承载台面仍压着 (13,5)
             → 旧实现单格四邻全不可走、判 unreachable，P2 改去 RescueCooked
  [2] t=947  同一现象，台面溢进邻格仅 0.26（可走性与占地阈值须同口径）
"""
import gzip
import json
import os

import pytest

from ai.primitives.move import _station_interact_cells
from ai.space import occupancy as O
from ai.space import reachability as R
from ai.state import world as W
from ai.state.bb_factory import make_bb

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "balloon",
                       "balloon23-20261030-002104.ndjson.gz")
PLATE = "-16202"


@pytest.fixture(scope="module")
def frames():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _plate_ctx(frame):
    w = W.World(frame)
    bb = make_bb(frame["_trace_t"])
    ctx = R.context(w, bb)
    return w, ctx, R.resource(w, PLATE)


@pytest.mark.parametrize("idx", [0, 1, 2])
def test_plate_on_straddling_counter_is_reachable(frames, idx):
    w, ctx, plate = _plate_ctx(frames[idx])
    assert R.can_interact(ctx, plate)


def test_planner_and_executor_agree_on_interact_cells(frames):
    """规划层判可达的那一拍，执行层 Approach 必须也有同一组交互位。"""
    w, ctx, plate = _plate_ctx(frames[1])
    cell = R.cell_of(w, plate)
    assert cell == (13, 6)
    goals = _station_interact_cells(plate, cell, w, "pickup")
    assert goals == [(13, 4)]
    assert all(g in ctx["field"] for g in goals)


def test_aligned_target_keeps_single_cell(frames):
    """单格四邻有可走格时不扩占地：静态/对齐站点行为不变。"""
    w, _ctx, plate = _plate_ctx(frames[0])
    cell = R.cell_of(w, plate)
    assert O.interaction_footprint(plate, cell, w.stations, w.to_grid,
                                   w.to_world, w.walkable) == {cell}


def test_straddling_chopping_board_is_reachable(frames):
    """站点自带 AABB 时用自己的占地（砧板同样会跨格）。"""
    w, ctx, _plate = _plate_ctx(frames[1])
    boards = [s for s in w.stations if s.get("t") == "chopping"]
    assert boards and all(R.can_interact(ctx, b) for b in boards)


def test_footprint_without_support_falls_back_to_cell():
    """无 AABB、也不在任何站点上（地上的盘）：退回单格。"""
    entry = dict(n="p", x=5.0, z=5.0)
    fp = O.interaction_footprint(entry, (5, 5), [], lambda x, z: (round(x), round(z)),
                                 lambda gx, gz: (float(gx), float(gz)),
                                 lambda gx, gz: False)
    assert fp == {(5, 5)}


def test_footprint_uses_support_station_box():
    """盘借用承载台面的 AABB：台面跨 (0,0)/(0,1) 两格，盘量化在 (0,1)。"""
    counter = dict(t="counter", n="c", x=0.0, z=0.6,
                   minx=-0.5, maxx=0.5, minz=0.1, maxz=1.1)
    plate = dict(n="p", x=0.0, z=0.6)
    walk = lambda gx, gz: gz == -1           # 只有台面前方一排可站
    fp = O.interaction_footprint(plate, (0, 1), [counter],
                                 lambda x, z: (round(x), round(z)),
                                 lambda gx, gz: (float(gx), float(gz)), walk)
    assert fp == {(0, 0), (0, 1)}

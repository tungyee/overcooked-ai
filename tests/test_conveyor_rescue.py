# -*- coding: utf-8 -*-
"""传送带上的盘快到末端垃圾桶时先抢救下来（4-2）。

真实帧 tests/fixtures/rapids/rapids42-bin-rescue.ndjson.gz
（trace-20261110-234204 t=340 / 420）：装生菜的盘 -19528 在 Raft Two 台面
传送带上，末端就是 Bin 1；旧逻辑没有任务接它，t=421 进桶销毁。
"""
import gzip
import json
import os

from ai import tasks as T
from ai.core import planner
from ai.exec.bt import Status
from ai.query.recipes import RecipeBook
from ai.state.bb_factory import make_bb
from ai.state.world import World
from tests.test_exec_primitives import FakeExec

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                   "rapids42-bin-rescue.ndjson.gz")
PID = "-19528"


def _frames():
    with gzip.open(FIX, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _ctx(frame):
    world = World(json.loads(json.dumps(frame)))
    book = RecipeBook(world.recipes, world.stations, world.items)
    bb = make_bb(frame["_trace_t"])
    return world, book, bb, planner._make_context(world, bb, book)


def test_conveyor_plate_is_rescued_before_bin():
    for t in (340, 420):
        frame = next(f for f in _frames() if f["_trace_t"] == t)
        world, book, bb, ctx = _ctx(frame)
        task = T.FetchConveyorPlate(resources=(PID,))
        assert task.feasible(ctx) and not task.done(ctx)
        chosen, record = planner.plan(world, bb, None, book)
        assert chosen.TYPE == "FetchConveyorPlate", (t, chosen)
        assert record["top_layer"] == 4


def test_rescue_task_picks_the_plate_up():
    frame = next(f for f in _frames() if f["_trace_t"] == 420)  # 手里空
    world, _book, _bb, ctx = _ctx(frame)
    task = T.FetchConveyorPlate(resources=(PID,))
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert task.build().tick(ctx) == Status.RUNNING
    assert ctx["interaction"]["kind"] == "pickup"
    assert ctx["interaction"]["target"][1] == PID

# -*- coding: utf-8 -*-
"""传送带上的盘快到末端垃圾桶时先抢救下来（4-2）。

真实帧 tests/fixtures/rapids/rapids42-bin-rescue.ndjson.gz
（trace-20261110-234204 t=340 / 420）：装生菜的盘 -19528 在 Raft Two 台面
传送带上，末端就是 Bin 1；旧逻辑没有任务接它，t=421 进桶销毁。

抢救只针对「对面够不着」的带子：玩家那侧的传送带本来就是交给玩家的通道，
P2 抢回来会变成「放上去 → 抢下来 → 再放上去」的死循环
（trace-20261110-235726 t=491~528，盘 -20214）。
"""
import gzip
import json
import os

from ai import tasks as T
from ai.core import planner
from ai.exec.bt import Status
from ai.query.recipes import RecipeBook
from ai.revenue import handover as HO
from ai.space import channels as CH
from ai.state.bb_factory import make_bb
from ai.state.world import World
from tests.test_exec_primitives import FakeExec

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                   "rapids42-bin-rescue.ndjson.gz")
FIX_SHARED = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                          "rapids42-shared-belt.ndjson.gz")
FIX_OWN = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                       "rapids42-own-belt.ndjson.gz")
PID = "-19528"
PID_SHARED = "-20214"
PID_OWN = "-18776"


def _frames():
    with gzip.open(FIX, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _frames_shared():
    with gzip.open(FIX_SHARED, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _frames_own():
    with gzip.open(FIX_OWN, "rt", encoding="utf8") as fh:
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


def test_plate_on_player_side_belt_is_not_rescued():
    """玩家够得着的传送带段是交给玩家的通道，P2 不该抢回来。"""
    for t in (491, 510):
        frame = next(f for f in _frames_shared() if f["_trace_t"] == t)
        world, book, bb, ctx = _ctx(frame)
        plate = next(p for p in world.all_plates
                     if str(p.get("n")) == PID_SHARED)
        assert CH.plate_on_conveyor(world, plate), t
        assert CH.plate_on_player_side_conveyor(world, plate, bb), t
        task = T.FetchConveyorPlate(resources=(PID_SHARED,))
        assert not task.feasible(ctx), t
        chosen, _record = planner.plan(world, bb, None, book)
        assert chosen is None or chosen.TYPE != "FetchConveyorPlate", (t, chosen)


def test_own_side_belt_is_not_a_send_channel():
    """自己这侧的带子末端是本侧垃圾桶，不能当送件通道。

    trace-20261111-000723 t=227~375：两筏漂开，P2 只够得着 Raft Two 带子，
    SendAcross 把成品盘 -18776 放到带子开头 (4,0)，盘一路走向本侧 Bin 1，
    FetchConveyorPlate 每 10 拍把它抢回来、再放上去。修法：这种带子不进
    @conveyor 通道，等漂近到能碰到玩家那侧再送。
    """
    frame = next(f for f in _frames_own() if f["_trace_t"] == 234)  # 盘子在手
    world, book, bb, ctx = _ctx(frame)
    assert CH.conveyor_slots(world, bb) == []
    assert CH.channel_keys(world, bb) == []
    assert [r for r in HO.send_requests(world, book, bb)
            if r.kind == "plated_dish"] == []
    task = T.SendAcross("2", (PID_OWN, CH.CONVEYOR), "plated_dish")
    assert not task.feasible(ctx)


def test_plate_on_own_side_belt_is_still_rescued():
    """玩家够不着的带子（自己这侧）上真有盘时，还是要抢在进桶前接住。"""
    frame = next(f for f in _frames_own() if f["_trace_t"] == 227)  # 盘在带上
    world, _book, _bb, ctx = _ctx(frame)
    plate = next(p for p in world.all_plates if str(p.get("n")) == PID_OWN)
    assert CH.plate_on_conveyor(world, plate)
    assert not CH.plate_on_player_side_conveyor(world, plate, ctx.get("bb"))
    assert T.FetchConveyorPlate(resources=(PID_OWN,)).feasible(ctx)


def test_rescue_task_picks_the_plate_up():
    frame = next(f for f in _frames() if f["_trace_t"] == 420)  # 手里空
    world, _book, _bb, ctx = _ctx(frame)
    task = T.FetchConveyorPlate(resources=(PID,))
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert task.build().tick(ctx) == Status.RUNNING
    assert ctx["interaction"]["kind"] == "pickup"
    # 锁段等料：目标是盘所在的传送带段（固定），不是盘本身。
    assert ctx["interaction"]["target"][1].startswith("ConveyorStation")

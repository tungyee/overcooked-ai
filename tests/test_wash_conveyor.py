# -*- coding: utf-8 -*-
"""4-2 传送带送来的脏盘要能接住并洗。

真实帧 tests/fixtures/rapids/rapids42-wash.ndjson.gz（trace-20261110-230514）：

  - t=900：Raft One 回盘台有一摞脏盘（P2 够不着），Raft Two 传送带上也有一摞；
    洗碗池在竹筏边缘，主网格交互格为空，旧逻辑 WashPlate 恒 infeasible，
    传送带把脏盘一路送进 Bin 1。
  - t=1595：洗碗池可达、传送带上的脏盘也可达，但 WashPlate 在 L7 清理层，
    排不过备菜，脏盘最终也进桶。
"""
import gzip
import json
import os

from ai import primitives as PR
from ai.core import planner
from ai.exec.bt import Status
from ai.query import predicates as P
from ai.query.recipes import RecipeBook
from ai.space import reachability, shuttle
from ai.state.bb_factory import make_bb
from ai.state.world import World
from ai.tasks import plate as PL
from tests.test_exec_primitives import FakeExec

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                   "rapids42-wash.ndjson.gz")


def _frames():
    with gzip.open(FIX, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _ctx(frame):
    world = World(json.loads(json.dumps(frame)))
    book = RecipeBook(world.recipes, world.stations, world.items)
    bb = make_bb(frame.get("_trace_t", 0))
    return world, book, bb, planner._make_context(world, bb, book)


def _sink(world):
    return next(st for st in world.stations if st.get("t") == "washing")


def test_dirty_stack_entry_prefers_reachable_conveyor_stack():
    """两边各一摞脏盘：别拿 P1 那侧够不着的那摞当目标。"""
    frame = next(f for f in _frames() if f["_trace_t"] == 900)
    world, _book, _bb, ctx = _ctx(frame)
    entry = PL.dirty_stack_entry(ctx)
    assert entry is not None
    assert entry.get("grid") == "Raft Two"
    assert str(entry.get("on_station") or "").startswith("attach:ConveyorStation")


def test_edge_sink_gets_a_wash_stance():
    """竹筏边缘洗碗池没有格内交互位时，用池前 0.9m 的格外站位。"""
    frame = next(f for f in _frames() if f["_trace_t"] == 900)
    world, _book, _bb, ctx = _ctx(frame)
    sink = _sink(world)
    stances = shuttle.wash_stances(world, sink)
    assert stances
    assert stances[0]["cell"] in (ctx["field"] or {})
    assert reachability.can_interact(ctx, sink)


def test_wash_plate_outranks_prep_when_dirty_is_on_conveyor():
    """脏盘已经上传送带：WashPlate 进 L4 解卡层，别排在备菜后面。"""
    for t in (900, 1595):
        frame = next(f for f in _frames() if f["_trace_t"] == t)
        world, book, bb, _ctx_ = _ctx(frame)
        task = PL.WashPlate(resources=(_sink(world).get("n"),))
        assert task.feasible(planner._make_context(world, bb, book))
        chosen, record = planner.plan(world, bb, None, book)
        assert chosen.TYPE == "WashPlate", (t, chosen)
        assert record["top_layer"] == 4


def test_approach_uses_wash_stance_goal():
    """走到洗碗池时用格外站位点，而不是量化后离池子 2m 的格心。"""
    frame = next(f for f in _frames() if f["_trace_t"] == 900)
    world, _book, bb, ctx = _ctx(frame)
    sink = _sink(world)
    approach = PR.Approach(sink, "placement")
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert approach.tick(ctx) == Status.RUNNING
    cmd = ctx["exec"].issued[-1]
    goal = cmd["goal"]
    assert abs(goal[0] - sink["x"]) < 0.2
    assert abs(goal[1] - 1.2) < 0.2


def test_wash_stance_keeps_searching_past_unlinkable_points():
    """池前最近的能站点挂不到可走格时，继续往外找，别直接判没有站位。

    trace-20261110-232403 t=2300：竹筏边缘的 WashingPart 最近的能站点
    (z=0.85) 离可走格 >1m，旧 _edge_stances 在这里 break 掉，wash_stances
    返回空，WashPlate 全程 infeasible，脏盘被传送带送进 Bin 1。
    """
    frame = json.loads(gzip.open(
        os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                     "rapids42-wash-edge.ndjson.gz"), "rt").readline())
    world, book, bb, ctx = _ctx(frame)
    sink = _sink(world)
    stances = shuttle.wash_stances(world, sink)
    assert stances
    assert stances[0]["cell"] in (ctx["field"] or {})
    assert reachability.can_interact(ctx, sink)
    chosen, record = planner.plan(world, bb, None, book)
    assert chosen.TYPE == "WashPlate"
    assert record["top_layer"] == 4


def test_holding_dirty_plate_keeps_wash_task():
    """拿起脏盘的下一拍不能把它放到旁边柜台。

    trace-20261110-234204 t=3363~3371：WashPlate approach 洗碗池，下一拍
    dirty_stack_entry 只查 items[]（手持物在 held_items），拿到 Raft One
    够不着的另一摞 → 判 infeasible，planner 改选 Chop，EnsureHandsFree
    把脏盘放到 countertop (6)。修后 dirty_stack_entry 先返回手里的脏盘，
    WashPlate 保持 L4。
    """
    frame = json.loads(gzip.open(
        os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                     "rapids42-wash-hold.ndjson.gz"), "rt").readline())
    world, book, bb, ctx = _ctx(frame)
    chef = next(c for c in world.chefs if c["name"] == P.P2_NAME)
    assert chef["held"] == "DirtyPlateStack"
    entry = PL.dirty_stack_entry(ctx)
    assert entry is not None and entry.get("held_by") == P.P2_NAME
    task = PL.WashPlate(resources=(_sink(world).get("n"),))
    assert task.feasible(ctx) and not task.done(ctx)
    chosen, record = planner.plan(world, bb, None, book)
    assert chosen.TYPE == "WashPlate", chosen
    assert record["top_layer"] == 4

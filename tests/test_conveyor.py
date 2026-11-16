# -*- coding: utf-8 -*-
"""传送带通道（Rapids_4_2_2P）：双竹筏之间只能靠台面传送带运输。

真实帧 tests/fixtures/rapids/rapids42-conveyor.ndjson.gz（trace-20261110-211743，
从 Sushi_4_1 切进 4-2 后的第 12290 / 12863 拍）：

  - 两边各一条 L 形 `ConveyorStation` 台面传送带，人上不去；
  - P2 放**自己这侧带子的开头**；两筏带子的最近段距离 ≤3m 时算接上，
    接不上才不送（放上去会进本侧桶：trace-20261111-000723 t=227~375）；
  - 一块切好的生菜挂在传送带段上，AI 够得着（接收侧）。

这组测试钉住：主网格原点来自 `grids[].main`、竹筏不被当成渡运通道、
移动网格站点不参与「上下/左右半场」、成品盘走 `SendAcross(@conveyor)`
且只放到玩家够得着的带子段上。
"""
import gzip
import json
import os

from ai import tasks as T
from ai.core import planner
from ai.exec.bt import Status
from ai.query import predicates as P
from ai.query import recipes as R
from ai.query.recipes import RecipeBook
from ai.revenue import handover as HO
from ai.revenue import throw_supply as TS
from ai.space import channels as CH
from ai.space import shuttle
from ai.space import zoning
from ai.state.bb_factory import make_bb
from ai.state.world import World
from tests.test_exec_primitives import FakeExec

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                   "rapids42-conveyor.ndjson.gz")
FIX3 = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                    "rapids42-hold.ndjson.gz")
FIX_SHARED = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                          "rapids42-shared-belt.ndjson.gz")
PID = "-205166"
PID_SHARED = "-20214"


def _frames():
    with gzip.open(FIX, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _frames3():
    with gzip.open(FIX3, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _frame_shared(t):
    with gzip.open(FIX_SHARED, "rt", encoding="utf8") as fh:
        return next(json.loads(line) for line in fh
                    if line.strip() and json.loads(line)["_trace_t"] == t)


def _world(frame):
    return World(json.loads(json.dumps(frame)))


def _ctx(world):
    book = RecipeBook(world.recipes, world.stations, world.items)
    bb = make_bb(100)
    return planner._make_context(world, bb, book), book


def test_main_grid_origin_comes_from_grids_not_default():
    """4-2 没有主网格站点；原点必须取 grids[].main，不能落到 Sushi_1_4 默认值。"""
    world = _world(_frames()[0])
    main = next(g for g in world.raw["grids"] if g.get("main") is True)
    assert (world.mapping.origin_x, world.mapping.origin_z) == (main["ox"], main["oz"])


def test_rafts_are_not_shuttle_channels():
    """竹筏有砧板/灶/料箱，人能站上去，不是「隔着围栏够」的渡运台面。"""
    for frame in _frames():
        world = _world(frame)
        assert shuttle.shuttle_grids(world) == set()
        assert "Raft One" not in CH.channel_keys(world)


def test_moving_raft_stations_do_not_split_the_kitchen():
    """站点都在会动的竹筏上时，不报「上/下两半」——否则 layout_review 会
    随竹筏漂移反复翻转，把模型逼去把活全塞给玩家（voice-20261110-211743）。"""
    tracker = zoning.ZoneTracker()
    for frame in _frames():
        report = tracker.observe(_world(frame))
        assert report.district == ""
        assert report.p2_label == "" and report.p1_label == ""


def test_ingredient_on_belt_is_receivable():
    """切好的生菜挂在传送带上，AI 这侧够得着（接收侧走普通取料）。"""
    world = _world(_frames()[0])
    loc = R.find_ingredient(world, "ChoppedLettuce")
    assert loc is not None and loc["kind"] == "loose"
    st = CH.conveyor_station_of(world, loc["st"])
    assert st is not None and st.get("grid") == "Raft Two"


def test_plated_dish_uses_conveyor_channel():
    """出餐口在玩家那侧、P2 够不着：成品盘应生成 SendAcross(@conveyor)。"""
    world = _world(_frame_shared(499))      # P2 端着生菜+番茄盘，玩家那侧带子够得着
    ctx, book = _ctx(world)
    reqs = [r for r in HO.send_requests(world, book, ctx.get("bb"))
            if r.kind == "plated_dish"]
    assert [(str(r.source.source_id), r.channel) for r in reqs] == [
        (PID_SHARED, CH.CONVEYOR)]
    task, _rec = planner.plan(world, ctx["bb"], None, book)
    assert task.TYPE == "SendAcross" and task.channel == CH.CONVEYOR
    assert task.source_id == PID_SHARED


def test_own_side_belt_is_send_channel_when_connected():
    """两筏带子接上时，P2 放自己这侧的带子开头（不是对面那段）。"""
    world = _world(_frames()[1])            # 最近段 2.4m，已接上
    ctx, book = _ctx(world)
    slots = CH.conveyor_slots(world, ctx.get("bb"))
    assert slots and all(s.get("grid") == "Raft Two" for s in slots)
    assert CH.CONVEYOR in CH.channel_keys(world, ctx.get("bb"))
    assert [r for r in HO.send_requests(world, book, ctx.get("bb"))
            if r.kind == "plated_dish"]


def test_send_holding_plate_targets_a_belt_segment():
    """端着成品盘：走到自己这侧带子的开头，放置句柄指向该段。"""
    world = _world(_frame_shared(499))
    ctx, _book = _ctx(world)
    task = T.SendAcross("1", (PID_SHARED, CH.CONVEYOR), "plated_dish")
    assert task.feasible(ctx) and not task.done(ctx)
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert task.build().tick(ctx) == Status.RUNNING
    cmd = ctx["exec"].issued[-1]
    assert cmd["cmd"] == "move" and cmd["kind"] == "placement"
    assert cmd["target"]["name"].startswith("ConveyorStation")
    assert cmd["target"]["grid"] == "Raft Two"
    assert (cmd["target"]["gx"], cmd["target"]["gz"]) == (4, 0)


def test_send_done_when_plate_is_on_belt():
    """放上传送带就算送出（不再追它漂到哪），缺口随之释放。"""
    frame = _frames()[1]
    plate = next(p for p in frame["plates"] if str(p.get("n")) == PID)
    slot = next(s for s in frame["stations"]
                if s.get("t") == "conveyor" and s.get("grid") == "Raft Two")
    plate.update(x=slot["x"], z=slot["z"], held_by=None,
                 on_station="attach:%s" % slot["n"])
    world = _world(frame)
    ctx, _book = _ctx(world)
    task = T.SendAcross("2", (PID, CH.CONVEYOR), "plated_dish")
    assert task.done(ctx) and not task.feasible(ctx)
    assert CH.channel_of_slot(world, CH.slot_of(world, plate, ctx.get("bb")),
                              ctx.get("bb")) == CH.CONVEYOR


# ---------------------------------------------------------------- 切好的沙拉料按「对面有干净盘」门控 / 带子开头


def test_chopped_salad_thrown_only_when_far_has_clean_plate():
    """切好的生菜/番茄可以投给对面，但只在对面有干净盘能装沙拉时才投。

    旧实现把切好料的「下一站」写成砧板（CONSUMER_BOARD），对面两块砧板恒在 →
    没盘也一直扔，料堆在 P1 那边没人接（voice-20261111-224619）。现在下一站是
    CONSUMER_PLATE（装盘），由 gap 里「对面有干净盘」的门控决定投不投。
    """
    frame = _frames3()[0]                   # t=710：P2 正拿着 ChoppedLettuce
    world = _world(frame)
    ctx, book = _ctx(world)
    # 下一站是「装盘」而非「砧板」。
    assert TS.stages(book, "Lettuce") == [
        ("Lettuce", TS.CONSUMER_BOARD, "Lettuce"),
        ("ChoppedLettuce", TS.CONSUMER_PLATE, "Lettuce"),
    ]
    board = next(st for st in world.stations
                 if st.get("n") == "countertop_01_chopping_board_swamp (1)")
    task = T.ThrowBatch(None, (P.resource_id(board), TS.CONSUMER_PLATE,
                              "Lettuce", "Lettuce"), "ChoppedLettuce")
    # 对面有干净盘（fixture 里 2 块在 Raft One）→ 可投。
    assert task.feasible(ctx)

    # 对面没有干净盘 → 缺口归零、不可投，沙拉留在 P2 这侧装盘。
    farless = json.loads(json.dumps(frame))
    farless["plates"] = []
    ctx2, _ = _ctx(_world(farless))
    assert not task.feasible(ctx2)


def test_send_uses_own_belt_start_not_far_end():
    """放自己这侧带子开头（4,0），不要放到远端死胡同（0,2 / -1,2）。"""
    frame = _frame_shared(353)
    plate = next(p for p in frame["plates"] if str(p.get("n")) == PID_SHARED)
    plate["held_by"] = P.P2_NAME
    chef = next(c for c in frame["chefs"] if c.get("name") == P.P2_NAME)
    chef["held"] = plate.get("name")
    world = _world(frame)
    ctx, _book = _ctx(world)
    task = T.SendAcross("1", (PID_SHARED, CH.CONVEYOR), "plated_dish")
    assert task.feasible(ctx) and not task.done(ctx)
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert task.build().tick(ctx) == Status.RUNNING
    cmd = ctx["exec"].issued[-1]
    assert cmd["kind"] == "placement"
    assert cmd["target"]["grid"] == "Raft Two"
    assert (cmd["target"]["gx"], cmd["target"]["gz"]) == (4, 0)


def _switch_world(vx):
    # 按钮（switch 站点）在 (1,1)/(-1,-1)，随按钮翻转的开关格 conveyor 在 (0,0)。
    class W(object):
        stations = [
            {"t": "switch", "n": "Switch (1)", "gx": 1, "gz": 1},
            {"t": "switch", "n": "Switch", "gx": -1, "gz": -1},
            {"t": "conveyor", "n": "ConveyorStation (85)", "gx": 0, "gz": 0,
             "vx": vx, "vz": 0.0},
        ]
    return W()


def test_switch_direction_reads_conveyor_vx():
    assert CH.switch_direction(_switch_world(0.6)) == ">"
    assert CH.switch_direction(_switch_world(-0.6)) == "<"


def test_switch_direction_none_without_switch():
    class W(object):
        stations = [{"t": "conveyor", "n": "c", "gx": 0, "gz": 0,
                     "vx": 0.6, "vz": 0.0}]
    assert CH.switch_direction(W()) is None


def test_conveyor_direction_ok_heuristic():
    assert CH.conveyor_direction_ok(_switch_world(0.6)) is True
    assert CH.conveyor_direction_ok(_switch_world(-0.6)) is False


def test_conveyor_direction_ok_defaults_true_without_switch():
    class W(object):
        stations = []
    assert CH.conveyor_direction_ok(W()) is True

"""Wizard_3_1（游戏内 3-1）：静态分割厨房 + 中间共享台面 + 整列滑动的砧板。

真实帧 tests/fixtures/wizard/wizard31-20261101-002050.ndjson.gz
（trace-20261101-002050 的 state 快照还原，`_trace_t` 为 trace 拍号）：
  [0] t=140 / [1] t=300 / [2] t=600：阶段 A。P2 被关在左边小区（番茄箱、3 块
      干净盘、灭火器），P1 在右边大区（面团/芝士箱、两台烤箱）。两场交集为空，
      中间列台面 (-3,2) `countertop_01_standard_gold (20)` 两边都够得着；
  [3] t=972：阶段 B。砧板整列滑到 x=13.2，P2 的区扩到烤箱和三块板，P1 只剩
      面团/芝士箱一侧，两边已没有共享的普通台面。
当局 P2 从没碰过盘子：SendAcross 只认渡运篮子，静态分割无通道。
"""
import gzip
import json
import os

import pytest

from ai import tasks as T
from ai.core import planner
from ai.exec.bt import Status
from ai.query.recipes import RecipeBook
from ai.revenue import cross_supply as XS
from ai.space import channels as CH
from ai.state import world as W
from ai.state.bb_factory import make_bb

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "wizard",
                       "wizard31-20261101-002050.ndjson.gz")
SHARED = "countertop_01_standard_gold (20)"


@pytest.fixture(scope="module")
def frames():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _ctx(frame):
    w = W.World(frame)
    book = RecipeBook(w.recipes, w.stations)
    bb = make_bb(100)
    return w, book, bb, planner._make_context(w, bb, book)


def _hold_plate(frame, pid):
    f = json.loads(json.dumps(frame))
    p = next(x for x in f["plates"] if x["n"] == pid)
    p["held_by"] = "Player 2"
    next(c for c in f["chefs"] if c["name"] == "Player 2")["held"] = p["name"]
    return f


def _plate_on(frame, pid, station_name):
    f = json.loads(json.dumps(frame))
    st = next(s for s in f["stations"] if s["n"] == station_name)
    p = next(x for x in f["plates"] if x["n"] == pid)
    p.update(x=st["x"], z=st["z"])
    for it in f["items"]:
        if it["n"] == p["name"]:
            it.update(x=st["x"], z=st["z"], on_station="attach:" + station_name)
    return f


# ---------------------------------------------------------------- 通道识别


@pytest.mark.parametrize("idx", [0, 1, 2])
def test_phase_a_has_static_channel(frames, idx):
    w = W.World(frames[idx])
    assert CH.is_split(w)
    assert CH.channel_keys(w) == [CH.STATIC]
    assert [s["n"] for s in CH.static_slots(w)] == [SHARED]


def test_phase_b_boards_slid_no_shared_counter(frames):
    """砧板整列滑走后两边不再共享普通台面：通道每帧现算，自动消失。"""
    w = W.World(frames[3])
    assert CH.is_split(w)
    assert CH.static_slots(w) == []
    assert CH.channel_keys(w) == []


# ---------------------------------------------------------------- 缺盘账


def test_far_needs_plates_when_ovens_only_on_far_side(frames):
    """披萨整份进炉（dish 本身要烤，成分不用）：灶只在 P1 那边 → 对面要盘。"""
    w, book, _bb, _c = _ctx(frames[1])
    acc = XS.far_plate_need(w, book)
    assert acc == {"need": 2, "far": 0, "near_spare": 3}
    assert XS.send_deficit(w, XS.KIND_CLEAN_PLATE, book) == 2


def test_plate_on_shared_counter_counts_as_in_transit(frames):
    f = _plate_on(frames[1], "-16284", SHARED)
    w, book, _bb, _c = _ctx(f)
    acc = XS.far_plate_need(w, book)
    assert acc["far"] == 1 and acc["near_spare"] == 2
    # 唯一共享格已被占：静态通道不留回程格，但满了就不能再送
    assert not XS.has_send_capacity(w, CH.STATIC)


def test_phase_b_no_send(frames):
    w, book, _bb, _c = _ctx(frames[3])
    assert XS.send_deficit(w, XS.KIND_CLEAN_PLATE, book) == 0


# ---------------------------------------------------------------- 规划与执行


def test_planner_sends_plate_through_shared_counter(frames):
    from tests.test_exec_primitives import FakeExec
    w, book, bb, ctx = _ctx(frames[1])
    task, rec = planner.plan(w, bb, None, book)
    assert task.TYPE == "SendAcross" and task.channel == CH.STATIC
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert task.build().tick(ctx) == Status.RUNNING
    cmd = ctx["exec"].issued[-1]
    assert cmd["cmd"] == "move" and ctx["interaction"]["kind"] == "pickup"


def test_send_holding_plate_targets_shared_counter(frames):
    from tests.test_exec_primitives import FakeExec
    f = _hold_plate(frames[1], "-16284")
    w, book, bb, ctx = _ctx(f)
    task = T.SendAcross(item="clean_plate", resources=("-16284", CH.STATIC))
    assert task.feasible(ctx) and not task.done(ctx)
    assert task.wait_targets(ctx) == []
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert task.build().tick(ctx) == Status.RUNNING
    cmd = ctx["exec"].issued[-1]
    assert cmd["cmd"] == "move" and cmd["target"]["name"] == SHARED
    assert ctx["interaction"]["kind"] == "placement"


def test_send_done_when_plate_on_shared_counter(frames):
    f = _plate_on(frames[1], "-16284", SHARED)
    w, book, bb, ctx = _ctx(f)
    assert T.SendAcross(item="clean_plate", resources=("-16284", CH.STATIC)).done(ctx)
    # 其余两块盘：通道满了，候选不再可行
    task = T.SendAcross(item="clean_plate", resources=("-16286", CH.STATIC))
    assert not task.feasible(ctx)


# ---------------------------------------------------------------- 披萨流程（协议 8）
# 当局插件还是 0.23.5：没有 cookers[].station_type / crate.chop_cookable /
# items[].cookable。下面在阶段 B 帧（P2 够得着两台烤箱和三块板）上按反编译
# 补出 0.24.0 会报的字段，验证整条链：切饼 → 放料 → 进炉 → 端盘取 → 出餐。

BASE = "Pizza_Base_Raw"
CAP = {"station": "Oven", "step": "OvenTray", "prep": True, "vessel": False, "time": 15}
COUNTER = "countertop_01_standard_gold (20)"      # P2 够得着的空台面 (-3,2)
PIZZA_COOKED = "CookedCompositeAssembledNode(OvenTray){ ing:Tomato, ing:Cheese, ing:Dough }"


def _proto8(frame):
    f = json.loads(json.dumps(frame))
    f["protocol_version"] = 8
    for c in f["cookers"]:
        c["station_type"] = "Oven"
    for s in f["stations"]:
        if s.get("t") == "crate" and s.get("ing") == "Dough":
            s["chop"] = BASE
            s["chop_cookable"] = dict(CAP)
    return f


def _base_on(frame, station, contents=()):
    """台面/板上放一张饼，饼上有 contents（["ing:Tomato", ...]）。"""
    f = json.loads(json.dumps(frame))
    st = next(s for s in f["stations"] if s["n"] == station)
    f["items"].append({"n": BASE, "x": st["x"], "z": st["z"], "y": 0.5,
                       "has_contents": bool(contents), "cooked": "Raw",
                       "detail": list(contents), "throwable": not contents,
                       "cookware": False, "cookable": dict(CAP), "flying": False,
                       "node": None, "grid": "GridManager",
                       "on_station": "attach:" + station, "held_by": None})
    return f


def _clear_boards(frame):
    """砧板上的切好料清掉（避免干扰：只测饼这一条）。"""
    f = json.loads(json.dumps(frame))
    f["items"] = [i for i in f["items"]
                  if not str(i.get("on_station") or "").startswith("attach:countertop_01_chopping")]
    for ws in f["workstations"]:
        ws["item"] = None
    return f


def _oven_with_pizza(frame, cooked):
    f = json.loads(json.dumps(frame))
    c = next(c for c in f["cookers"] if c["n"] == "Oven")
    c.update(isCooking=not cooked, isTurnedOn=True, cooked=cooked, burning=False,
             cookware=BASE, cook_type="OvenTray", pot_contents=[PIZZA_COOKED])
    f["items"].append({"n": BASE, "x": c["x"], "z": c["z"], "y": 0.5,
                       "has_contents": True, "cooked": "Cooked" if cooked else "Raw",
                       "detail": ["ing:Tomato", "ing:Cheese"], "throwable": False,
                       "cookware": False, "cookable": dict(CAP), "flying": False,
                       "node": None, "grid": "GridManager",
                       "on_station": "cooking:Oven", "held_by": None})
    return f


def _cands(frame):
    w, book, bb, ctx = _ctx(frame)
    task, rec = planner.plan(w, bb, None, book)
    return w, book, ctx, task, [c[0] for c in rec["candidates"]]


def test_book_knows_pizza_is_carrier_cooked(frames):
    w = W.World(_proto8(frames[3]))
    book = RecipeBook(w.recipes, w.stations)
    assert book.carrier_base("MargheritaPizza") == ("Dough", BASE, "Oven")
    assert set(book.carrier_toppings("MargheritaPizza")) == {"Tomato", "Cheese"}
    # 整份进炉：进盘的是一个熟料节点，订单需求按它判
    order = w.orders[0]
    assert book.requirements(order) == ("MargheritaPizza",)
    assert book.rich_token("MargheritaPizza") == "cooked:cheese+dough+tomato:oventray"


def test_old_frame_without_capabilities_has_no_carrier_flow(frames):
    """0.23.5 帧没有能力字段：不猜，不发载体 task（升级插件才有这条链）。"""
    w = W.World(frames[3])
    book = RecipeBook(w.recipes, w.stations)
    assert book.carrier_base("MargheritaPizza") is None


def test_base_on_counter_gets_toppings(frames):
    f = _base_on(_proto8(frames[3]), COUNTER)
    w, book, ctx, task, cands = _cands(f)
    tops = {t.item for t in cands if t.TYPE == "FetchToCarrier"}
    assert tops == {"Tomato", "Cheese"}           # 板上各有一份切好的
    assert task.TYPE == "FetchToCarrier"
    assert ctx and task.feasible(ctx)


def test_fetch_to_carrier_tree_picks_then_places_on_base(frames):
    from tests.test_exec_primitives import FakeExec
    f = _base_on(_proto8(frames[3]), COUNTER)
    w, book, bb, ctx = _ctx(f)
    task = T.FetchToCarrier("1", (BASE,), "Tomato")
    assert task.feasible(ctx) and not task.done(ctx)
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert task.build().tick(ctx) == Status.RUNNING
    assert ctx["interaction"]["kind"] == "pickup"
    # 手里拿着切好的番茄：下一步把它放到饼所在的台面上
    f2 = json.loads(json.dumps(f))
    f2["items"] = [i for i in f2["items"]
                   if not (i["n"] == "ChoppedTomato" and i["on_station"].endswith("(2)"))]
    next(c for c in f2["chefs"] if c["name"] == "Player 2")["held"] = "ChoppedTomato"
    w, book, bb, ctx = _ctx(f2)
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert task.build().tick(ctx) == Status.RUNNING
    cmd = ctx["exec"].issued[-1]
    assert ctx["interaction"]["kind"] == "placement" and cmd["target"]["name"] == COUNTER


def test_topping_done_when_on_base(frames):
    f = _base_on(_proto8(frames[3]), COUNTER, ["ing:Tomato"])
    w, book, bb, ctx = _ctx(f)
    assert T.FetchToCarrier("1", (BASE,), "Tomato").done(ctx)
    assert not T.FetchToCarrier("1", (BASE,), "Cheese").done(ctx)


def test_full_base_goes_into_free_oven(frames):
    f = _clear_boards(_base_on(_proto8(frames[3]), COUNTER, ["ing:Tomato", "ing:Cheese"]))
    w, book, ctx, task, cands = _cands(f)
    place = [t for t in cands if t.TYPE == "PlaceCarrierInCooker"]
    assert {t.cooker_id[1] for t in place} == {"Oven", "Oven (1)"}
    assert task.TYPE == "PlaceCarrierInCooker"
    assert not [t for t in cands if t.TYPE == "FetchToCarrier"]


def test_baking_pizza_takes_plate_to_oven_then_delivers(frames):
    f = _clear_boards(_oven_with_pizza(_proto8(frames[3]), cooked=True))
    w, book, ctx, task, cands = _cands(f)
    take = [t for t in cands if t.TYPE == "TakeCookedToPlate"]
    assert take and all(t.cooker_id[1] == "Oven" and t.item == "MargheritaPizza" for t in take)
    assert task.TYPE == "TakeCookedToPlate"
    # 只有一张饼：另一单不再在灶上配饼，也不发第二个进炉
    assert not [t for t in cands if t.TYPE == "PlaceCarrierInCooker"]
    # 出炉进盘后：盘里一个熟披萨节点 → 料齐 → 出餐
    g = json.loads(json.dumps(f))
    p = next(x for x in g["plates"] if x["n"] == task.plate_id)
    p.update(count=1, contents=["CookedCompositeAssembledNode"], detail=[PIZZA_COOKED])
    w, book, ctx, task2, cands = _cands(g)
    assert book.missing(w.orders[0], p) == ()
    assert any(t.TYPE == "DeliverPlate" for t in cands)


def _holding_cooked_pizza(frame):
    """RescueCooked 刚把熟披萨端出烤箱：灶空了，P2 手里是烤熟的饼（名字仍是
    Pizza_Base_Raw，只有 cooked="Cooked"）。"""
    f = _clear_boards(_oven_with_pizza(_proto8(frame), cooked=True))
    c = next(c for c in f["cookers"] if c["n"] == "Oven")
    c.update(isCooking=False, isTurnedOn=False, cooked=None, burning=None,
             cookware=None, cook_type=None, pot_contents=[])
    # items[] 混合散落与手持物，World 按 held_by 拆出 held_items
    pizza = next(i for i in f["items"] if i.get("on_station") == "cooking:Oven")
    pizza.update(on_station=None, grid=None, held_by="Player 2")
    next(ch for ch in f["chefs"] if ch["name"] == "Player 2")["held"] = BASE
    return f


def test_cooked_pizza_in_hand_is_not_put_back_in_oven(frames):
    """熟饼不是「装齐料待进炉」的载体：不许 PlaceCarrierInCooker 把它塞回去
    （trace-20261101-164146：RescueCooked ↔ PlaceCarrierInCooker 拿进拿出）。"""
    from ai.query import carriers as CR
    f = _holding_cooked_pizza(frames[3])
    w, book, ctx, task, cands = _cands(f)
    assert CR.match_prep_carriers(w, book) == {}
    assert not [t for t in cands if t.TYPE == "PlaceCarrierInCooker"]
    assert not T.PlaceCarrierInCooker(
        "1", (("GridManager", "Oven"), BASE), "MargheritaPizza").feasible(ctx)
    assert XS.stuck_carriers(w, book, bb=None) == []


def _pizza_on(frame, station, cooked):
    """台面上一张烤过的饼（出炉后 RescueCooked 放下的形态，node=底料节点）。"""
    f = json.loads(json.dumps(frame))
    st = next(s for s in f["stations"] if s["n"] == station)
    f["items"].append({"n": BASE, "x": st["x"], "z": st["z"], "y": 0.5,
                       "has_contents": True, "cooked": cooked,
                       "detail": ["ing:Cheese", "ing:Tomato"], "throwable": False,
                       "cookware": False, "cookable": dict(CAP), "flying": False,
                       "node": "Dough", "grid": "GridManager",
                       "on_station": "attach:" + station, "held_by": None})
    return f


PIZZA_TOKEN = "cooked:cheese+dough+tomato:oventray"


def test_cooked_pizza_on_counter_is_cooked_source(frames):
    """出炉放在台面的熟披萨 = 散熟料源：端盘去盛，不再进炉、不再另烤一张。"""
    from ai.revenue import cook_supply as CS
    from ai.tasks.base import cooked_sources
    f = _clear_boards(_pizza_on(_proto8(frames[3]), COUNTER, "Cooked"))
    w, book, ctx, task, cands = _cands(f)
    srcs = [s for s in cooked_sources(w, book) if s["kind"] == "loose"]
    assert [(s["id"], s["tokens"]) for s in srcs] == [(BASE, {PIZZA_TOKEN})]
    assert CS.cooked_supply(w, book, "MargheritaPizza") == 1
    take = [t for t in cands if t.TYPE == "TakeCookedToPlate"]
    assert take and all(t.cooker_id == BASE for t in take)
    assert not [t for t in cands if t.TYPE == "PlaceCarrierInCooker"]
    assert task.TYPE == "TakeCookedToPlate" and task.feasible(ctx)


def test_take_cooked_pizza_skips_same_named_raw_base(frames):
    """生饼和熟饼同名 Pizza_Base_Raw：生饼排在前面也要找到熟的那张。"""
    raw_spot = "countertop_01_standard_gold (5)"
    f = _proto8(frames[3])
    if not any(s["n"] == raw_spot for s in f["stations"]):
        raw_spot = next(s["n"] for s in f["stations"]
                        if s.get("t") == "counter" and s["n"] != COUNTER)
    f = _base_on(f, raw_spot, ["ing:Tomato"])
    f = _clear_boards(_pizza_on(f, COUNTER, "Cooked"))
    w, book, bb, ctx = _ctx(f)
    pid = w.plates[0]["n"]
    task = T.TakeCookedToPlate("1", (BASE, pid), "MargheritaPizza")
    src = task._source(ctx)
    assert src is not None and src["target"]["cooked"] == "Cooked"
    assert src["target"]["on_station"] == "attach:" + COUNTER


def test_burnt_pizza_is_not_cooked_source(frames):
    """糊饼游戏不让进盘（CanTransferToContainer 拒 Burnt）：不算源、不算供给。"""
    from ai.revenue import cook_supply as CS
    from ai.tasks.base import cooked_sources
    f = _clear_boards(_pizza_on(_proto8(frames[3]), COUNTER, "Burnt"))
    w, book, ctx, task, cands = _cands(f)
    assert not [s for s in cooked_sources(w, book) if s["kind"] == "loose"]
    assert CS.cooked_supply(w, book, "MargheritaPizza") == 0
    assert not [t for t in cands if t.TYPE == "TakeCookedToPlate"]


def test_phase_b_base_left_on_shared_board_by_p1(frames):
    """阶段 B 面团只在 P1 那边：P1 切好的饼留在共享砧板上，P2 接着放料。"""
    board = "countertop_01_chopping_board_gold (2)"
    f = _proto8(frames[3])
    f["items"] = [i for i in f["items"] if i.get("on_station") != "attach:" + board]
    for ws in f["workstations"]:
        if ws["n"] == board:
            ws["item"] = BASE
    f = _base_on(f, board)
    w, book, ctx, task, cands = _cands(f)
    assert task.TYPE == "FetchToCarrier"
    assert {t.item for t in cands if t.TYPE == "FetchToCarrier"} == {"Tomato", "Cheese"}


# ---------------------------------------------------------------- 饼卡在 P2 这边（trace-20261101-130851，插件 0.24.0）
# [0] t≈18.8s 饼（已放番茄）在共享砧板 #633008 上；[1] t≈33.2s 料齐的饼被 StashHeld
# 放到 P2 台面 (21)，烤箱在 P1 那边；[2] t≈158s P2 端着料齐的饼。当局 P2 先拿
# ThrowBatch 把饼从共享砧板拿走想投进烤箱（有料的饼游戏不让扔），超时放到自己这边，
# 之后再没有 task 能把它交出去。

CARRIER_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "wizard",
                               "wizard31-20261101-130851-carrier.ndjson.gz")


@pytest.fixture(scope="module")
def carrier_frames():
    with gzip.open(CARRIER_FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_no_throw_into_oven_for_whole_dish(carrier_frames):
    """整份进炉的菜没有「投进灶」这一站：只剩配料/面团投砧板。"""
    from ai.revenue import throw_supply as TS
    w, book, bb, ctx = _ctx(carrier_frames[0])
    assert all(c != TS.CONSUMER_COOKER for _f, c, _r in TS.stages(book, "MargheritaPizza"))
    task, rec = planner.plan(w, bb, None, book)
    throws = [c[0] for c in rec["candidates"] if c[0].TYPE == "ThrowBatch"]
    assert throws and all(t.consumer != TS.CONSUMER_COOKER for t in throws)
    assert task.TYPE != "ThrowBatch" or task.item != BASE


def test_unthrowable_item_is_not_a_throw_source(carrier_frames):
    from ai.revenue import throw_supply as TS
    f = json.loads(json.dumps(carrier_frames[0]))
    for it in f["items"]:
        if it["n"] == "ChoppedTomato":
            it["throwable"] = False
    w = W.World(f)
    dest = next(s for s in w.stations if s.get("t") == "chopping")
    assert not [s for s in TS.sources(w, "ChoppedTomato", "Tomato", dest, TS.MODE_SPLIT)
                if s["kind"] == "loose"]


def test_stranded_full_base_is_sent_back_to_shared_spot(carrier_frames):
    from tests.test_exec_primitives import FakeExec
    w, book, bb, ctx = _ctx(carrier_frames[1])
    stuck = XS.stuck_carriers(w, book)
    assert [c["name"] for c in stuck] == [BASE]
    task, _rec = planner.plan(w, bb, None, book)
    assert task.TYPE == "SendAcross" and task.item == XS.KIND_CARRIER
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert task.build().tick(ctx) == Status.RUNNING
    assert ctx["interaction"]["kind"] == "pickup"
    assert ctx["exec"].issued[-1]["target"]["name"] == "countertop_01_standard_gold (21)"


def test_holding_full_base_places_it_on_free_shared_board(carrier_frames):
    """共享台面 (20) 被开局送过去的盘占着：退到空的共享砧板。"""
    from tests.test_exec_primitives import FakeExec
    w, book, bb, ctx = _ctx(carrier_frames[2])
    task, _rec = planner.plan(w, bb, None, book)
    assert task.TYPE == "SendAcross" and task.item == XS.KIND_CARRIER
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert task.build().tick(ctx) == Status.RUNNING
    target = ctx["exec"].issued[-1]["target"]["name"]
    assert ctx["interaction"]["kind"] == "placement"
    assert target.startswith("countertop_01_chopping_board_gold")
    assert next(ws for ws in w.workstations if ws["n"] == target)["item"] is None


def test_base_on_shared_board_is_not_stuck_and_send_done(carrier_frames):
    f = json.loads(json.dumps(carrier_frames[1]))
    board = "countertop_01_chopping_board_gold#633008"
    st = next(s for s in f["stations"] if s["n"] == board)
    it = next(i for i in f["items"] if i["n"] == BASE)
    it.update(x=st["x"], z=st["z"], on_station="attach:" + board)
    next(ws for ws in f["workstations"] if ws["n"] == board)["item"] = BASE
    w, book, bb, ctx = _ctx(f)
    assert XS.stuck_carriers(w, book) == []
    task = T.SendAcross(item=XS.KIND_CARRIER, resources=(BASE, CH.STATIC))
    assert task.done(ctx) and not task.feasible(ctx)


def test_base_missing_reachable_topping_is_topped_before_sending(carrier_frames):
    """饼上缺番茄、番茄箱在 P2 这边：先补料，不急着送。"""
    f = json.loads(json.dumps(carrier_frames[1]))
    next(i for i in f["items"] if i["n"] == BASE)["detail"] = ["ing:Cheese"]
    w, book, bb, ctx = _ctx(f)
    assert XS.stuck_carriers(w, book) == []


# ---------------------------------------------------------------- 楼梯（SlopedGround，插件 0.24.1）
# 0.24.0 只收 9/10/20/22/26/30 层，Left Side Stairs 在 28 层漏收：一楼与二楼
# 出餐口断开，P2 端着满盘 DeliverPlate 恒 unreachable_destination
# （trace-20261101-130851 t=13767）。盒子取自 0.24.1 前的 [TERRAIN-DIAG] 日志。
STAIRS = [[7.67, 10.19, -3.74, -3.41, 0.08, 0.46], [7.67, 10.19, -3.45, -2.82, 0.13, 0.45],
          [7.67, 10.19, -2.94, -2.61, 0.39, 0.77], [7.67, 10.19, -2.65, -2.02, 0.45, 0.76],
          [7.67, 10.19, -2.14, -1.81, 0.83, 1.21], [7.67, 10.19, -1.85, -1.22, 0.89, 1.2]]


def _with_stairs(frame):
    f = json.loads(json.dumps(frame))
    f["terrain"].append({"layer": 28, "boxes": STAIRS})
    return f


def _p2_reaches_pass(frame):
    from ai.space import reachability as R
    w = W.World(frame)
    ctx = R.context(w, make_bb(100))
    return any(R.can_interact(ctx, s) for s in w.stations if s.get("t") == "plate_station")


def test_sloped_ground_merged_into_ground():
    from ai.space import occupancy as O
    t = O.parse_terrain([{"layer": 9, "boxes": [[0, 1, 0, 1]]},
                         {"layer": 28, "boxes": [STAIRS[-1]]}])
    assert t[O.LAYER_SLOPED_GROUND] == [tuple(STAIRS[-1][:4])]
    assert len(t[O.LAYER_GROUND]) == 2
    # 外扩补顶阶与二楼地板的接缝（顶阶 maxz=-1.22，二楼 minz=-1.05）
    assert O._point_in_any_box(8.4, -1.2, t[O.LAYER_GROUND])


def test_phase_b_p2_reaches_pass_via_stairs(frames):
    """阶段 B（P2 区扩到烤箱一侧）：有楼梯才走得到二楼出餐口。"""
    assert not _p2_reaches_pass(frames[3])
    assert _p2_reaches_pass(_with_stairs(frames[3]))


def test_stairs_keep_phase_a_split(frames):
    """楼梯连的是右侧大区与二楼，不打通阶段 A 的左右分割。"""
    w = W.World(_with_stairs(frames[0]))
    assert CH.is_split(w)
    assert CH.channel_keys(w) == [CH.STATIC]

"""跨区送件规格表（ai/revenue/handover.py）：四种 kind 的统一取放/触发。

真实帧 tests/fixtures/wizard/wizard32-20261101-173006-board-base.ndjson.gz
（Wizard_3_2_2P = UI 3-3，中间桌子把两边分开）：
  [2] t≈54s：P2 够不着出餐口、P1 够得着；有若干 P2 单侧台面可放成品盘。
  [3] t≈113s：P2 够得着脏盘回盘台、够不着洗碗池；P1 反过来。
"""
import copy
import gzip
import json
import os

import pytest

from ai import tasks as T
from ai.core import planner
from ai.dialog.commands import build_scope
from ai.exec.bt import Status
from ai.query.recipes import RecipeBook
from ai.revenue import cross_supply as XS
from ai.space import channels as CH
from ai.space import reachability as R
from ai.state import world as W
from ai.state.bb_factory import make_bb

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "wizard",
                       "wizard32-20261101-173006-board-base.ndjson.gz")
PIZZA_COOKED = "CookedCompositeAssembledNode(OvenTray){ ing:Tomato, ing:Cheese, ing:Dough }"


@pytest.fixture(scope="module")
def frames():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _ctx(frame):
    w = W.World(frame)
    book = RecipeBook(w.recipes, w.stations)
    bb = make_bb(100)
    return w, book, bb, planner._make_context(w, bb, book)


def _p2_only_counter(w, bb):
    for st in w.stations:
        if not isinstance(st, dict) or st.get("t") != "counter":
            continue
        p2 = R.can_interact(R.context(w, bb, "Player 2"), st)
        p1 = R.can_interact(R.context(w, bb, "Player 1"), st)
        if p2 and not p1:
            return st
    return None


def _move_item(frame, name, station):
    f = json.loads(json.dumps(frame))
    it = next(i for i in f["items"] if i.get("n") == name)
    it.update(x=station["x"], z=station["z"],
              on_station="attach:" + station["n"], held_by=None)
    next(c for c in f["chefs"]
         if c["name"] == "Player 2")["held"] = None
    return f


def _complete_plate_on(frame, station):
    f = json.loads(json.dumps(frame))
    free = [x for x in f["plates"]
            if not x.get("held_by") and (x.get("count") or 0) == 0]
    if free:
        p = free[0]
    else:
        p = {"n": "-99999", "name": "equipment_plate_01"}
        f["plates"].append(p)
    p.update(x=station["x"], z=station["z"], count=1,
             contents=["CookedCompositeAssembledNode"],
             detail=[PIZZA_COOKED], held_by=None)
    return f, p["n"]


def _holding_plate(frame, pid):
    f = json.loads(json.dumps(frame))
    p = next(x for x in f["plates"] if x["n"] == pid)
    p["held_by"] = "Player 2"
    next(c for c in f["chefs"]
         if c["name"] == "Player 2")["held"] = p.get("name") or "equipment_plate_01"
    return f


# ---------------------------------------------------------------- 规格表覆盖


def test_send_specs_cover_all_kinds():
    from ai.revenue import handover as HO
    assert set(HO.SEND_SPECS) == set(XS.KINDS)
    assert set(XS.KINDS) == {XS.KIND_CLEAN_PLATE, XS.KIND_CARRIER,
                             XS.KIND_DIRTY_PLATE, XS.KIND_PLATED_DISH,
                             XS.KIND_CHOPPED_INGREDIENT}


def test_request_objects_expose_kind_source_and_channel(frames):
    from ai.revenue import handover as HO
    w, book, bb, _ctx_ = _ctx(frames[2])
    st = _p2_only_counter(w, bb)
    f, pid = _complete_plate_on(frames[2], st)
    w, book, bb, _ctx_ = _ctx(f)
    reqs = HO.send_requests(w, book, bb)
    plated = [r for r in reqs if r.kind == XS.KIND_PLATED_DISH]
    assert plated
    assert str(plated[0].source.source_id) == pid
    assert plated[0].source.order_id
    assert plated[0].channel == CH.STATIC


# ---------------------------------------------------------------- 脏盘


def test_dirty_plate_sent_when_sink_only_on_far_side(frames):
    from ai.revenue import handover as HO
    w, book, bb, ctx = _ctx(frames[3])
    assert [i["n"] for i in XS.dirty_plate_sources(w, bb)] == ["DirtyPlateStack"]
    reqs = HO.send_requests(w, book, bb)
    dirty = [r for r in reqs if r.kind == XS.KIND_DIRTY_PLATE]
    assert dirty and dirty[0].channel == CH.STATIC

    task = T.SendAcross(item=XS.KIND_DIRTY_PLATE,
                        resources=("dirty_plate", CH.STATIC))
    assert task.feasible(ctx) and not task.done(ctx)
    # 洗碗池在对面：P2 直接洗不可行，只能走中转。
    assert not T.WashPlate(resources=("WashingPart",)).feasible(ctx)


def test_dirty_plate_build_picks_it_up(frames):
    from tests.test_exec_primitives import FakeExec
    f = json.loads(json.dumps(frames[3]))
    next(c for c in f["chefs"]
         if c["name"] == "Player 2")["held"] = None
    w, book, bb, ctx = _ctx(f)
    task = T.SendAcross(item=XS.KIND_DIRTY_PLATE,
                        resources=("dirty_plate", CH.STATIC))
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert task.build().tick(ctx) == Status.RUNNING
    assert ctx["interaction"]["kind"] == "pickup"


def test_dirty_plate_done_when_on_shared_counter(frames):
    w, book, bb, ctx = _ctx(frames[3])
    slot = CH.static_slots(w, bb)[0]
    w, book, bb, ctx = _ctx(_move_item(frames[3], "DirtyPlateStack", slot))
    task = T.SendAcross(item=XS.KIND_DIRTY_PLATE,
                        resources=("dirty_plate", CH.STATIC))
    assert task.done(ctx) and not task.feasible(ctx)


def test_no_dirty_send_when_p2_can_reach_sink(frames):
    w, book, bb, _ctx_ = _ctx(frames[2])
    st = _p2_only_counter(w, bb)
    f = json.loads(json.dumps(frames[2]))
    f["items"].append({"n": "DirtyPlateStack", "x": st["x"], "z": st["z"],
                       "on_station": "attach:" + st["n"], "held_by": None})
    w, book, bb, _ctx_ = _ctx(f)
    assert XS.dirty_plate_sources(w, bb) == []


# ---------------------------------------------------------------- 成品盘


def test_plated_dish_sent_when_hatch_only_on_far_side(frames):
    from ai.revenue import handover as HO
    w, book, bb, _ctx_ = _ctx(frames[2])
    st = _p2_only_counter(w, bb)
    f, pid = _complete_plate_on(frames[2], st)
    w, book, bb, ctx = _ctx(f)
    srcs = XS.plated_dish_sources(w, book, bb)
    assert [str(p["n"]) for p, _o in srcs] == [pid]
    reqs = HO.send_requests(w, book, bb)
    plated = [r for r in reqs if r.kind == XS.KIND_PLATED_DISH]
    order_id = plated[0].source.order_id
    task = T.SendAcross(order_id, (pid, CH.STATIC), XS.KIND_PLATED_DISH)
    assert task.feasible(ctx) and not task.done(ctx)
    # 出餐口在对面：P2 直接送餐不可行，只能走中转。
    assert not T.DeliverPlate(order_id, (pid,)).feasible(ctx)


def test_plated_dish_build_places_on_shared_counter(frames):
    from tests.test_exec_primitives import FakeExec
    w, book, bb, _ctx_ = _ctx(frames[2])
    st = _p2_only_counter(w, bb)
    f, pid = _complete_plate_on(frames[2], st)
    f = _holding_plate(f, pid)
    w, book, bb, ctx = _ctx(f)
    order_id = XS.plated_dish_sources(w, book, bb)[0][1]["id"]
    task = T.SendAcross(order_id, (pid, CH.STATIC), XS.KIND_PLATED_DISH)
    assert task.feasible(ctx) and not task.done(ctx)
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert task.build().tick(ctx) == Status.RUNNING
    assert ctx["interaction"]["kind"] == "placement"
    target = ctx["exec"].issued[-1]["target"]["name"]
    assert target.startswith("countertop_01_standard")


def test_plated_dish_done_when_on_shared_counter(frames):
    from ai.revenue import handover as HO
    w, book, bb, _ctx_ = _ctx(frames[2])
    slot = CH.static_slots(w, bb)[0]
    f, pid = _complete_plate_on(frames[2], slot)
    w, book, bb, ctx = _ctx(f)
    reqs = HO.send_requests(w, book, bb)
    assert not [r for r in reqs if r.kind == XS.KIND_PLATED_DISH]
    task = T.SendAcross(None, (pid, CH.STATIC), XS.KIND_PLATED_DISH)
    assert task.done(ctx)


def test_no_plated_send_when_p2_can_reach_hatch(frames):
    w, book, bb, _ctx_ = _ctx(frames[3])
    st = _p2_only_counter(w, bb)
    f, _pid = _complete_plate_on(frames[3], st)
    w, book, bb, _ctx_ = _ctx(f)
    assert XS.plated_dish_sources(w, book, bb) == []


# ---------------------------------------------------------------- 命令 scope


def test_wash_and_deliver_scopes_include_relay_kinds():
    wash, _ = build_scope(None, "wash", None, None)
    deliver, _ = build_scope(None, "deliver", None, None)
    assert ("SendAcross", XS.KIND_DIRTY_PLATE) in wash
    assert ("SendAcross", XS.KIND_PLATED_DISH) in deliver


def test_send_across_dirty_plate_blocked_while_holding_raw_carrier(frames):
    """手里端着待进炉的披萨底时，别为了送脏盘把它放下。

    实机 trace-20261109-212622 t=94~99：SendAcross(dirty_plate) 为清手把披萨底
    放到台面，PlaceCarrierInCooker 又捡起来，0.4 秒一次来回。
    """
    from ai.tasks.plate import holding_valuable

    f = json.loads(json.dumps(frames[3]))
    base = next(i for i in f["items"]
                if isinstance(i.get("cookable"), dict)
                and i["cookable"].get("prep")
                and not i.get("held_by"))
    base["held_by"] = "Player 2"
    for chef in f["chefs"]:
        if chef["name"] == "Player 2":
            chef["held"] = base["n"]
    w, book, bb, ctx = _ctx(f)

    assert holding_valuable(ctx) is True
    task = T.SendAcross(item=XS.KIND_DIRTY_PLATE,
                        resources=("dirty_plate", CH.STATIC))
    assert task.feasible(ctx) is False

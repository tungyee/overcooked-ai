# -*- coding: utf-8 -*-
"""4-5 传送带方向开关（Sushi_Level_4_5_2P）：方向设定器 + 收/送两侧判据。

真实帧 tests/fixtures/sushi/sushi45-switch-20261112.ndjson.gz（从
trace-20261112-131340 t=300 提取，P2 卡在按开关那一拍）：

  - 两个 `ServerSwitchStation` 开关：(-1,-1) 是 P2 的按钮、只能设「朝 P2」，
    (1,1) 是 P1 的按钮、只能设「朝 P1」；按钮红 = 方向已朝自己（按了没用），
    没有冷却；
  - 开关格 (0,0) 的 raw vx<0 = 实际向右 = 右桶 gx=6 = P2 的接收桶；raw vx>0
    = 实际向左 = 左桶 gx=-6 = P1 的接收桶。

这组测试钉住：`conveyor_direction_ok`（朝 P1）与 `conveyor_direction_toward_p2`
（朝 P2）互为相反；`dirty_plate_to_receive` 只在「P1 侧有脏盘 + 池子只在 P2 侧」
时成立；`ReceiveConveyor` 只在「方向朝 P1 且 P1 侧有脏盘」时 feasible。
"""
import copy
import gzip
import json
import os

from ai import tasks as T
from ai.core import planner
from ai.query.recipes import RecipeBook
from ai.revenue import cross_supply as XS
from ai.space import channels as CH
from ai.state.bb_factory import make_bb
from ai.state.world import World

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sushi",
                   "sushi45-switch-20261112.ndjson.gz")


def _frame():
    with gzip.open(FIX, "rt", encoding="utf8") as fh:
        return json.loads(fh.readline())


def _ctx(frame):
    w = World(copy.deepcopy(frame))
    book = RecipeBook(w.recipes, w.stations, w.items)
    bb = make_bb(100)
    return w, book, bb, planner._make_context(w, bb, book)


def _chef(world, name):
    return next(c for c in world.chefs if c.get("name") == name)


def _put_dirty_on_far_side(frame):
    """往 P1 那侧（far）放一摞脏盘，模拟 P1 要把它送过来洗。"""
    f = copy.deepcopy(frame)
    p1 = next(c for c in f["chefs"] if c.get("name") == "Player 1")
    f.setdefault("items", []).append({
        "n": "DirtyPlateStack", "name": "equipment_plate_dirty",
        "x": p1["x"], "z": p1["z"], "held_by": None,
        "throwable": False, "flying": False, "on_station": None,
    })
    return f


def _set_center_vx(frame, vx):
    """把开关格 (0,0) 的 raw vx 设成 vx，模拟方向开关翻到另一侧。"""
    f = copy.deepcopy(frame)
    for st in f["stations"]:
        if st.get("t") == "conveyor" and st.get("gx") == 0 and st.get("gz") == 0:
            st["vx"] = vx
            st["vz"] = 0.0
    return f


def test_fixture_direction_is_toward_p2():
    frame = _frame()
    w = World(frame)
    assert CH.switch_direction(w) == "<"
    assert CH.conveyor_direction_ok(w) is False       # 朝 P1？否
    assert CH.conveyor_direction_toward_p2(w) is True  # 朝 P2？是


def test_direction_ok_and_toward_p2_are_opposites():
    """方向翻到朝 P1（vx>0）后，两个判据应该互换。"""
    w = World(_set_center_vx(_frame(), 0.6))
    assert CH.switch_direction(w) == ">"
    assert CH.conveyor_direction_ok(w) is True
    assert CH.conveyor_direction_toward_p2(w) is False


def test_dirty_plate_to_receive_false_without_dirty_plate():
    w, _book, bb, _ctx_ = _ctx(_frame())
    assert XS.dirty_plate_to_receive(w, bb) is False


def test_dirty_plate_to_receive_true_when_dirty_on_far_side():
    w, _book, bb, _ctx_ = _ctx(_put_dirty_on_far_side(_frame()))
    assert XS.dirty_plate_to_receive(w, bb) is True


def test_receive_conveyor_done_when_direction_toward_p2():
    w, _book, bb, ctx = _ctx(_frame())
    task = T.ReceiveConveyor()
    assert task.done(ctx) is True
    assert task.feasible(ctx) is False  # 已朝 P2，无需再按


def test_receive_conveyor_feasible_when_dirty_and_direction_toward_p1():
    # 方向翻到朝 P1 + P1 侧有脏盘：P2 该按开关翻到朝 P2。
    frame = _set_center_vx(_put_dirty_on_far_side(_frame()), 0.6)
    w, _book, bb, ctx = _ctx(frame)
    assert CH.conveyor_direction_toward_p2(w, bb) is False
    task = T.ReceiveConveyor()
    assert task.done(ctx) is False
    assert task.feasible(ctx) is True


def test_belt_reached_by_player_follows_switch_direction():
    """4-5 单环：盘归不归 P1，看方向开关，不看「P1 够得着任意一段」。"""
    w, _book, bb, _ctx_ = _ctx(_frame())
    st = next(s for s in w.stations if s.get("t") == "conveyor")
    # 方向朝 P2（fixture 默认 vx<0）：不是玩家侧，P2 该接/抢救
    assert CH.belt_reached_by_player(w, st, bb) is False
    # 翻到朝 P1（vx>0）：变成玩家侧，P2 不抢
    w2 = World(_set_center_vx(_frame(), 0.6))
    st2 = next(s for s in w2.stations if s.get("t") == "conveyor")
    assert CH.belt_reached_by_player(w2, st2, bb) is True


def _put_clean_plate_on_conveyor(frame):
    """把一块干净空盘挪到传送带段上（模拟刚被 SendAcross 放上去）。"""
    f = copy.deepcopy(frame)
    seg = next(s for s in f["stations"] if s.get("t") == "conveyor")
    p = next(p for p in f.get("plates", [])
             if not p.get("held_by") and (p.get("count") or 0) == 0)
    p["x"] = seg["x"]
    p["z"] = seg["z"]
    p["on_station"] = "attach:" + seg["n"]
    return f


def test_place_clean_plate_infeasible_when_plate_on_conveyor():
    """盘已经在通道上（在送）时，PlaceCleanPlate 不该抢回来摆盘。"""
    frame = _put_clean_plate_on_conveyor(_frame())
    w, _book, bb, ctx = _ctx(frame)
    p = next(p for p in w.plates if (p.get("count") or 0) == 0)
    counter = next(s for s in w.stations if s.get("t") == "counter")
    task = T.PlaceCleanPlate(resources=(p.get("n"), (counter.get("gx"), counter.get("gz"))))
    assert CH.slot_of(w, p, bb) is not None
    assert task.feasible(ctx) is False


def _set_partial_plate(frame):
    """把一块空盘设成「黄瓜+鱼」的半成品寿司，并放到 P2 这侧。"""
    f = copy.deepcopy(frame)
    p = next(p for p in f["plates"] if (p.get("count") or 0) == 0)
    p2 = next(c for c in f["chefs"] if c.get("name") == "Player 2")
    p["contents"] = ["Cucumber", "SushiFish"]
    p["detail"] = ["ing:Cucumber", "ing:SushiFish"]
    p["count"] = 2
    p["held_by"] = None
    p["x"] = p2["x"]
    p["z"] = p2["z"]
    return f, p["n"]


def test_partial_plate_missing_rice_is_sent():
    """半成品盘缺米饭（灶只在 P1 侧）→ 该送过去补。"""
    # 方向翻到朝 P1（否则传送带通道关闭，plated_dish 不生成）
    frame, pid = _set_partial_plate(_set_center_vx(_frame(), 0.6))
    w, book, bb, _ctx_ = _ctx(frame)
    got = [(p.get("n"), o.get("dish"))
           for p, o in XS.plated_dish_sources(w, book, bb)]
    assert (pid, "Sushi_All") in got


_PLATE_MATCH_FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sushi",
                                "sushi45-plate-match-20261112.ndjson.gz")


def test_complete_plate_binds_to_its_matching_order():
    """已完整的 Sushi_PlainFish 盘别被「能马上加米饭」抢去绑给 Sushi_Fish。

    trace-20261112-230754 t=2769：盘 -44018（['SushiFish']）已经是完整的
    Sushi_PlainFish（order#3），但灶上有熟米饭，旧匹配第一轮把它绑去 Sushi_Fish
    （order#4），导致它既不能被当成品送、又被往上面补海苔。
    """
    with gzip.open(_PLATE_MATCH_FIX, "rt", encoding="utf8") as fh:
        frame = json.loads(fh.readline())
    w = World(copy.deepcopy(frame))
    book = RecipeBook(w.recipes, w.stations, w.items)
    bb = make_bb(100)
    pm = planner._match_plates_to_orders(w, bb, book)
    assert pm.get("-44018") == "3"   # Sushi_PlainFish，不是 "4"（Sushi_Fish）

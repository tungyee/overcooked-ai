# -*- coding: utf-8 -*-
"""已装料载体（散落的、本身带内容的非盘非锅物品）参与拼装。

真实帧 tests/fixtures/mine/mine24-20261031-233010-carrier.ndjson.gz
（trace-20261031-233010，Mine_2_4_2P）：
  [0] t=1790 盘 -16194 已有熟米（绑 order#4 Meat_Burrito）；台面 (8) 上一张
      卷饼 detail 里已包熟肉 —— 卷饼放进盘即齐
  [1] t=2790 同一局面，P2 手里拿着米
旧行为：规划器把「带料卷饼」当普通 Tortilla：完成度只算 1/3，FillCooker
压过它；还在给 order#4 备肉/端肉。整局到结束都没去拿那张卷饼。

载体不按菜名特判（卷饼/汉堡胚/……一律走 detail 解析）。
"""
import copy
import gzip
import json
import os

import pytest

from ai.core import planner
from ai.query import carriers as C
from ai.query import recipe_graph
from ai.query.recipes import RecipeBook
from ai.revenue import cook_supply as CS
from ai.state import world as W
from ai.state.bb_factory import make_bb
from ai import tasks as T

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mine",
                       "mine24-20261031-233010-carrier.ndjson.gz")
PLATE = "-16194"


@pytest.fixture(scope="module")
def frames():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _wb(frame):
    # 深拷贝：World 与帧共享 dict，测试里改盘/物品不能污染 module 级 fixture
    w = W.World(copy.deepcopy(frame))
    return w, RecipeBook(w.recipes, w.stations)


def _order(w, oid):
    return next(o for o in w.orders if str(o["id"]) == oid)


MEAT = ("cooked:burritomeat:fryingpan",)
CARRIER_TASK = "FetchToPlate(Tortilla, %s, %s, order#4)" % (PLATE, MEAT)
PLAIN_TASK = "FetchToPlate(Tortilla, %s, order#4)" % PLATE


# ---------- 纯函数：载体识别 / 匹配 ----------

def test_carrier_signature(frames):
    w, _ = _wb(frames[0])
    cs = C.carriers(w)
    assert len(cs) == 1
    c = cs[0]
    assert (c["name"], c["base"], c["sig"], c["held_by"]) == \
        ("Tortilla", "tortilla", MEAT, None)


def test_plain_items_cookware_plates_are_not_carriers():
    class _W(object):
        items = [
            {"n": "Tortilla", "x": 0, "z": 0, "detail": []},           # 空卷饼 = 普通料
            {"n": "utensil_pot_01", "x": 0, "z": 0, "cookware": True,
             "detail": ["CookedCompositeAssembledNode(Pot){ ing:Rice }"]},
            {"n": "equipment_plate_01 (1)", "x": 0, "z": 0,
             "detail": ["ing:Bun"]},
            {"n": "Bun", "x": 1, "z": 1, "flying": True, "detail": ["ing:Lettuce"]},
            {"n": "Bun", "x": 2, "z": 2, "detail": ["ing:Lettuce"]},     # 通用：汉堡胚
        ]
        held_items = [{"n": "Bun", "held_by": "Player 1",
                       "detail": ["ing:Tomato", "ing:Lettuce"]}]
        recipes = []
    cs = C.carriers(_W())
    assert [(c["base"], c["sig"], c["held_by"]) for c in cs] == [
        ("bun", ("lettuce",), None), ("bun", ("lettuce", "tomato"), "Player 1")]
    assert len(C.carriers(_W(), include_held=False)) == 1


def test_carrier_matches_compatible_order_only(frames):
    w, book = _wb(frames[0])
    plate_match = planner._match_plates_to_orders(w, make_bb(100), book)
    cm = C.match_carriers_to_orders(w, book, plate_match)
    # 只有一张载体，只配给一单；蘑菇卷不能拿包肉的卷饼
    assert list(cm) == ["4"]


def test_find_ingredient_matches_contents(frames):
    """普通取料只认空的；带签名时只认包了这些料的那件。"""
    from ai.query.recipes import find_ingredient
    w, _ = _wb(frames[0])
    loc = find_ingredient(w, "Tortilla", exclude_plate=True, can_access=lambda st: True)
    assert loc["kind"] == "crate"
    loc = find_ingredient(w, "Tortilla", exclude_plate=True, can_access=lambda st: True,
                          contents=MEAT)
    assert loc["kind"] == "loose" and loc["st"]["n"] == "Tortilla"
    assert find_ingredient(w, "Tortilla", exclude_plate=True, can_access=lambda st: True,
                           contents=("cooked:mushroom:fryingpan",)) is None


def _hold_tortilla(w, detail):
    p2 = next(c for c in w.chefs if c["name"] == "Player 2")
    p2["held"] = "Tortilla"
    w.held_items = [{"n": "Tortilla", "held_by": "Player 2", "detail": detail}]


def test_held_carrier_found_by_contents(frames):
    from ai.query.recipes import find_ingredient
    w, _ = _wb(frames[0])
    w.items = [it for it in w.items if it.get("n") != "Tortilla"]
    _hold_tortilla(w, ["CookedCompositeAssembledNode(FryingPan){ ing:BurritoMeat }"])
    acc = lambda st: True
    assert find_ingredient(w, "Tortilla", can_access=acc, contents=MEAT)["kind"] == "held"
    # 手里是包肉的：普通取料不算「已拿着空卷饼」
    assert find_ingredient(w, "Tortilla", can_access=acc)["kind"] == "crate"


# ---------- 供给 / DAG ----------

def test_cooked_supply_counts_carrier(frames):
    w, book = _wb(frames[0])
    with_carrier = CS.cooked_supply(w, book, "FriedBurritoMeat")
    w.items = [it for it in w.items if str(it.get("n")) != "Tortilla"]
    assert CS.cooked_supply(w, book, "FriedBurritoMeat") == with_carrier - 1
    _hold_tortilla(w, ["CookedCompositeAssembledNode(FryingPan){ ing:BurritoMeat }"])
    assert CS.cooked_supply(w, book, "FriedBurritoMeat") == with_carrier


def test_dag_emits_carrier_and_plain_and_no_meat_prep(frames):
    w, book = _wb(frames[0])
    bb = make_bb(100)
    plate_match = planner._match_plates_to_orders(w, bb, book)
    cooker_match = planner._match_cookers_to_orders(w, bb, book, plate_match)
    assert "4" not in cooker_match.values()
    tasks = recipe_graph.emit(w, bb, book, _order(w, "4"), plate_match, cooker_match)
    keys = [str(t) for t in tasks]
    assert CARRIER_TASK in keys and PLAIN_TASK in keys
    assert not [k for k in keys if "BurritoMeat" in k and "FetchToPlate" not in k], keys


# ---------- task ----------

def test_carrier_task_feasible_and_targets_carrier_counter(frames):
    w, book = _wb(frames[0])
    ctx = planner._make_context(w, make_bb(100), book)
    task = T.FetchToPlate("4", (PLATE, MEAT), "Tortilla")
    assert task.contents == MEAT
    assert task.feasible(ctx)
    src = T.ingredient_source(ctx, "Tortilla", MEAT)
    assert src is not None and src.get("n") == "countertop_01_standard_old (8)"
    # 蘑菇卷的盘不能放包肉卷饼
    assert not T.FetchToPlate("4", (PLATE, ("cooked:mushroom:fryingpan",)),
                              "Tortilla").feasible(ctx)


def test_carrier_task_done_needs_contents_in_plate(frames):
    w, book = _wb(frames[0])
    ctx = planner._make_context(w, make_bb(100), book)
    plate = next(p for p in w.plates if p["n"] == PLATE)
    task = T.FetchToPlate("4", (PLATE, MEAT), "Tortilla")
    plate["detail"] = ["CookedCompositeAssembledNode(Pot){ ing:Rice }", "ing:Tortilla"]
    assert not task.done(ctx)        # 放了空卷饼：底料在、肉不在
    plate["detail"] = ["CompositeAssembledNode{ CookedCompositeAssembledNode(Pot){ ing:Rice }, "
                       "CookedCompositeAssembledNode(FryingPan){ ing:BurritoMeat }, ing:Tortilla }"]
    assert task.done(ctx)


# ---------- 端到端：规划选中 ----------

@pytest.mark.parametrize("idx", [0, 1])
def test_planner_picks_carrier_to_plate(frames, idx):
    w, book = _wb(frames[idx])
    task, rec = planner.plan(w, make_bb(100), None, book)
    scores = {str(c[0]): c[1] for c in rec["candidates"] if c[3]}
    assert str(task) == CARRIER_TASK, sorted(scores.items(), key=lambda kv: -kv[1])[:6]
    assert scores[CARRIER_TASK] > scores.get(PLAIN_TASK, -99)


def test_plate_match_prefers_order_with_complete_carrier():
    """卷已包齐（饼+肉+饭）只差一只盘：唯一的空盘给这单，而不是别的单。"""
    from ai.query.recipes import RecipeBook as RB
    w, _ = _wb_from_fixture_first()
    book = RB(w.recipes, w.stations)
    w.orders = [
        {"id": "1", "dish": "Mushroom_Burrito", "remaining": 50, "lifetime": 130,
         "composition": ["Tortilla", "BoiledRice", "FriedMushrooms"], "optional": []},
        {"id": "2", "dish": "Meat_Burrito", "remaining": 40, "lifetime": 130,
         "composition": ["Tortilla", "BoiledRice", "FriedBurritoMeat"], "optional": []},
    ]
    # 场上只留一只空盘、一张包齐的卷
    keep = next(p for p in w.plates if p["n"] == PLATE)
    keep["detail"], keep["contents"], keep["count"] = [], [], 0
    w.plates = [keep]
    w.held_plates = []
    for it in w.items:
        if it.get("n") == "Tortilla":
            it["detail"] = ["CookedCompositeAssembledNode(Pot){ ing:Rice }",
                            "CookedCompositeAssembledNode(FryingPan){ ing:BurritoMeat }"]
    pm = planner._match_plates_to_orders(w, make_bb(100), book)
    assert pm == {PLATE: "2"}


def _wb_from_fixture_first():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        frame = json.loads(fh.readline())
    return _wb(frame)


# ---------- 不同内容的载体：饭+肉 / 只有饭 ----------

RICE_D = "CookedCompositeAssembledNode(Pot){ ing:Rice }"
MEAT_D = "CookedCompositeAssembledNode(FryingPan){ ing:BurritoMeat }"
RICE_T = "cooked:rice:pot"
MEAT_T = "cooked:burritomeat:fryingpan"


def _plan_with(frame, tortilla_detail, plate_detail):
    f = copy.deepcopy(frame)
    for it in f["items"]:
        if it["n"] == "Tortilla":
            it["detail"] = tortilla_detail
    for p in f["plates"]:
        if p["n"] == PLATE:
            p["detail"] = plate_detail
            p["count"] = len(plate_detail)
            p["contents"] = ["CookedCompositeAssembledNode"] * len(plate_detail)
    w, book = _wb(f)
    task, rec = planner.plan(w, make_bb(100), None, book)
    return str(task), [str(c[0]) for c in rec["candidates"]]


@pytest.mark.parametrize("idx", [0, 1])
@pytest.mark.parametrize("tortilla,plate,sig", [
    ([RICE_D, MEAT_D], [], (MEAT_T, RICE_T)),      # 包齐的卷 + 空盘
    ([RICE_D], [MEAT_D], (RICE_T,)),                # 包饭的卷 + 有肉的盘
    ([RICE_D], [], (RICE_T,)),                      # 包饭的卷 + 空盘（只凑到 2/3）
])
def test_planner_picks_carrier_variants(frames, idx, tortilla, plate, sig):
    chosen, _ = _plan_with(frames[idx], tortilla, plate)
    assert chosen == "FetchToPlate(Tortilla, %s, %s, order#4)" % (PLATE, sig)


@pytest.mark.parametrize("idx", [0, 1])
def test_conflicting_carrier_not_offered(frames, idx):
    """盘里已有饭，包饭的卷放进去就多一份饭：不发载体候选。"""
    chosen, cands = _plan_with(frames[idx], [RICE_D], [RICE_D])
    assert not [c for c in cands if "('%s',)" % RICE_T in c and "FetchToPlate" in c]
    assert "('%s',)" % RICE_T not in chosen

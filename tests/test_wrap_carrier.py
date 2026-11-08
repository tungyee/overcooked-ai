# -*- coding: utf-8 -*-
"""wrap 类载体（卷饼直接装熟料、不进炉）：检测 / DAG / FillCarrier task / 发射。

真实帧 tests/fixtures/mine/mine24-20261031-233010-carrier.ndjson.gz
（trace-20261031-233010，Mine_2_4_2P）：卷饼 recipe.plate 是 CompositeBurrito，
是唯一 Composite* 叶子 → 判定为包裹底料；熟料（饭/蘑菇/肉）直接装进卷饼皮，
不占盘。寿司里海苔/黄瓜/鱼三个叶子都是 CompositeSushi → 不唯一、退回盘装。
"""
import copy
import gzip
import json
import os

import pytest

from ai import tasks as T
from ai.core import planner, planner_layers
from ai.query import carriers as CR
from ai.query import recipe_graph
from ai.query.recipes import RecipeBook
from ai.state import world as W
from ai.state.bb_factory import make_bb

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mine",
                       "mine24-20261031-233010-carrier.ndjson.gz")

RICE_D = "CookedCompositeAssembledNode(Pot){ ing:Rice }"
MEAT_D = "CookedCompositeAssembledNode(FryingPan){ ing:BurritoMeat }"
RICE_T = "cooked:rice:pot"
MEAT_T = "cooked:burritomeat:fryingpan"


@pytest.fixture(scope="module")
def frames():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _wb(frame):
    w = W.World(copy.deepcopy(frame))
    return w, RecipeBook(w.recipes, w.stations)


def _ctx(frame, tick=100):
    w, book = _wb(frame)
    bb = make_bb(tick)
    return w, book, bb, planner._make_context(w, bb, book)


# ---------------------------------------------------------------- 检测

def test_wrap_base_detects_burrito(frames):
    w, book = _wb(frames[0])
    assert book.wrap_base("Mushroom_Burrito") == "Tortilla"
    assert book.wrap_base("Meat_Burrito") == "Tortilla"
    assert book.wrap_fillings("Mushroom_Burrito") == ("BoiledRice", "FriedMushrooms")
    assert book.wrap_fillings("Meat_Burrito") == ("BoiledRice", "FriedBurritoMeat")


def test_wrap_base_none_for_leaf_or_ambiguous(frames):
    w, book = _wb(frames[0])
    assert book.wrap_base("Tortilla") is None          # 叶子，不是拼装菜
    # 寿司：海苔/黄瓜/鱼三个叶子都是 CompositeSushi → 不唯一 → 退回盘装
    book2 = RecipeBook([
        {"name": "Sushi", "comp": ["Seaweed", "Cucumber", "SushiFish", "BoiledSushiRice"],
         "cook": "null", "plate": "CompositeSushi"},
        {"name": "Seaweed", "comp": [], "cook": "null", "plate": "CompositeSushi"},
        {"name": "Cucumber", "comp": [], "cook": "null", "plate": "CompositeSushi"},
        {"name": "SushiFish", "comp": [], "cook": "null", "plate": "CompositeSushi"},
        {"name": "BoiledSushiRice", "comp": ["SushiRice"], "cook": "Pot",
         "plate": "CompositeSushi"},
    ], stations=[])
    assert book2.wrap_base("Sushi") is None


def test_carrier_unified_kind(frames):
    w, book = _wb(frames[0])
    assert book.carrier("Mushroom_Burrito")[0] == "wrap"
    assert book.carrier("Mushroom_Burrito")[1] == "Tortilla"
    # 披萨仍是 cook 类（carrier_base 向后兼容），卷饼不进炉
    assert book.carrier_base("Mushroom_Burrito") is None


# ---------------------------------------------------------------- DAG

def test_wrap_node_chain(frames):
    """最后一样馅在链顶：FillCarrier(肉) 依赖 FillCarrier(饭) + 肉的煮料链。"""
    w, book = _wb(frames[0])
    node = recipe_graph._wrap_node(book, "Meat_Burrito")
    assert node["type"] == "FetchToPlate"
    assert node["item"] == "Tortilla"
    assert tuple(node["contents"]) == CR.signature((MEAT_T, RICE_T))

    meat = node["children"][0]
    assert meat["type"] == "FillCarrier"
    assert meat["item"] == "FriedBurritoMeat"
    assert tuple(meat["pre_contents"]) == (RICE_T,)
    assert [c["type"] for c in meat["children"]] == ["FetchToCooker", "FillCarrier"]

    rice = meat["children"][1]
    assert rice["item"] == "BoiledRice" and tuple(rice["pre_contents"]) == ()
    assert [c["type"] for c in rice["children"]] == ["FetchToCooker"]


# ---------------------------------------------------------------- 载体存在

def test_wrap_carrier_exists(frames):
    w, book = _wb(frames[0])
    assert CR.wrap_carrier_exists(w, "Tortilla", (MEAT_T,)) is True
    assert CR.wrap_carrier_exists(w, "Tortilla", (RICE_T,)) is False
    assert CR.wrap_carrier_exists(w, "Tortilla", ()) is False   # 空卷饼不是载体


# ---------------------------------------------------------------- FillCarrier task

def _fill(frame, order_id, cooker, item, pre=()):
    return T.FillCarrier(order_id, (cooker, "Tortilla", tuple(pre)), item)


def test_fill_carrier_done_when_wrapper_has_contents(frames):
    w, book, bb, ctx = _ctx(frames[0])
    meat = _fill(frames[0], "4", ("GridManager", "workstation_cooker_01 (2)"),
                 "FriedBurritoMeat")
    assert meat.done(ctx) is True       # 场上已有包熟肉的卷饼
    assert meat.feasible(ctx) is False  # done 了就不发
    rice = _fill(frames[0], "4", ("GridManager", "workstation_cooker_01 (6)"),
                 "BoiledRice")
    assert rice.done(ctx) is False


def test_fill_carrier_feasible_when_source_cooking(frames):
    """锅里有熟料、且有空卷饼可拿（箱子）→ 可行。"""
    frame = copy.deepcopy(frames[1])
    # 把包肉卷饼改成空卷饼（场上没有已装好肉的载体）
    for it in frame["items"]:
        if it.get("n") == "Tortilla":
            it["detail"] = []
    # cooker 6 有一锅熟饭
    for c in frame["cookers"]:
        if c.get("n") == "workstation_cooker_01 (6)":
            c["pot_contents"] = [RICE_D]
            c["cooked"] = True
            c["isCooking"] = True
    w, book, bb, ctx = _ctx(frame)
    rice = _fill(frame, "4", ("GridManager", "workstation_cooker_01 (6)"),
                 "BoiledRice")
    assert rice.done(ctx) is False
    assert rice.feasible(ctx) is True


def test_fill_carrier_feasible_false_without_source(frames):
    w, book, bb, ctx = _ctx(frames[0])
    rice = _fill(frames[0], "4", ("GridManager", "workstation_cooker_01 (6)"),
                 "BoiledRice")
    # 锅 6 是空的：没有熟饭可盛
    assert rice.feasible(ctx) is False


# ---------------------------------------------------------------- 发射

def test_emit_includes_fill_carrier_for_burrito(frames):
    """卷饼订单在「有熟料锅 + 有空卷饼」时，发射 FillCarrier 候选。"""
    frame = copy.deepcopy(frames[1])
    frame["orders"] = [o for o in frame["orders"] if o["id"] == "4"]   # 单订单，匹配无歧义
    for it in frame["items"]:
        if it.get("n") == "Tortilla":
            it["detail"] = []            # 空卷饼
    for p in frame["plates"]:
        p["detail"] = []                 # 盘里没有现成料：饭/肉只能从锅里盛
        p["contents"] = []
        p["count"] = 0
    for c in frame["cookers"]:
        if c.get("n") == "workstation_cooker_01 (6)":
            c["pot_contents"] = [RICE_D]
            c["cooked"] = True
            c["isCooking"] = True
        if c.get("n") == "workstation_cooker_01 (2)":
            c["pot_contents"] = [MEAT_D]
            c["cooked"] = True
            c["isCooking"] = True
    w, book = _wb(frame)
    bb = make_bb(100)
    pm = planner._match_plates_to_orders(w, bb, book)
    cm = planner._match_cookers_to_orders(w, bb, book, pm)
    order = next(o for o in w.orders if o["id"] == "4")
    tasks = recipe_graph.emit(w, bb, book, order, pm, cm)
    fills = [t for t in tasks if t.TYPE == "FillCarrier"]
    assert fills, [str(t) for t in tasks]
    # 第一样馅（饭）前置内容为空
    assert any(t.item == "BoiledRice" and t.pre_contents == () for t in fills)


def test_fill_carrier_gets_time_pressure_like_take_cooked(frames):
    """锅里熟了/快糊了，FillCarrier 要与 TakeCookedToPlate 同层竞争（救料层）。

    否则 FillCarrier 恒落 L_PREP，被「端盘盛熟」结构性压住、永不选中
    （trace-20261108-195728）。
    """
    frame = copy.deepcopy(frames[0])
    for c in frame["cookers"]:
        if c.get("n") == "workstation_cooker_01 (6)":
            c["pot_contents"] = [RICE_D]
            c["cooked"] = True
            c["cooking_progress"] = 24.0
            c["cooking_time"] = 12.0
    w, book = _wb(frame)
    fill = _fill(frame, "4", ("GridManager", "workstation_cooker_01 (6)"),
                 "BoiledRice")
    take = T.TakeCookedToPlate(
        "4", (("GridManager", "workstation_cooker_01 (6)"), "-16194"),
        "BoiledRice")
    assert planner._time_pressure(fill, w) == planner._time_pressure(take, w) > 0
    assert planner._is_rescue_action(fill) is True
    assert planner_layers.layer_of_task(fill, w) == \
        planner_layers.layer_of_task(take, w)


def test_planner_prefers_fill_carrier_over_plate_when_tied(frames):
    """锅已熟、盘和空卷饼都在：FillCarrier 与 TakeCookedToPlate 同分时，装馅优先。

    两者同为「盛熟料救锅」，L1/L2 只比 pressure（同分），靠候选顺序定胜负；
    wrap 节点排在盘装路径之前，所以装馅压过装盘（trace-20261108-195728）。
    """
    frame = copy.deepcopy(frames[1])
    frame["orders"] = [o for o in frame["orders"] if o["id"] == "4"]
    for it in frame["items"]:
        if it.get("n") == "Tortilla":
            it["detail"] = []
    for p in frame["plates"]:
        p["detail"] = []; p["contents"] = []; p["count"] = 0
    for c in frame["cookers"]:
        if c.get("n") == "workstation_cooker_01 (6)":
            c["pot_contents"] = [RICE_D]
            c["cooked"] = True
            c["isCooking"] = True
            c["cooking_progress"] = 24.0
            c["cooking_time"] = 12.0
    w, book = _wb(frame)
    task, _rec = planner.plan(w, make_bb(100), None, book)
    assert task is not None and task.TYPE == "FillCarrier", task


def test_planner_keeps_plate_path_as_fallback(frames):
    """盘装路径不因 wrap 路径而消失：普通 FetchToPlate(Tortilla) 仍在候选里。"""
    w, book = _wb(frames[0])
    bb = make_bb(100)
    pm = planner._match_plates_to_orders(w, bb, book)
    cm = planner._match_cookers_to_orders(w, bb, book, pm)
    order = next(o for o in w.orders if o["id"] == "4")
    tasks = recipe_graph.emit(w, bb, book, order, pm, cm)
    strs = [str(t) for t in tasks]
    assert any("FetchToPlate(Tortilla, -16194, order#4)" == s for s in strs)

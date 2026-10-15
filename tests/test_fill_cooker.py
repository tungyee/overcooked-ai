# -*- coding: utf-8 -*-
"""FillCooker（订单无关的填灶 task）回归（第 1 组 1.1）。

为什么要一个新 task 而不是调权重：`FetchToCooker` 绑订单，只能由
recipe_graph 按单发射，`_in_flight_cook_claim` 压掉同单第二个下锅候选，
`_match_cookers_to_orders` 又是严格 1:1。结构上「5 单要米」就不可能变成
「3 口灶在烧」——实测 trace-20261014-120406 的 1700~3270 窗口 59% 的拍里
三口灶一个都没在煮，order#3/4/5 三单全在这窗口过期。

FillCooker 的谓词是纯世界谓词（照 Prefetch 的形态）：有空灶 + 缺口 > 0。
不绑订单，所以绕开上述三道结构限制。
"""
import pytest

from ai import tasks as T
from ai.recipes import RecipeBook
from tests.test_cook_supply import RECIPES, W, cooker, order, plate


STATIONS = [
    {"t": "crate", "ing": "SushiRice", "n": "crate_rice", "gx": 1, "gz": 1},
    {"t": "cooking", "n": "c1", "gx": 5, "gz": 5},
    {"t": "cooking", "n": "c2", "gx": 6, "gz": 5},
]


def ctx(orders=(), cookers=(), plates=(), chefs=None, items=()):
    w = W(orders=orders, cookers=cookers, plates=plates,
          chefs=chefs if chefs is not None else [EMPTY_CHEF], items=items)
    w.stations = list(STATIONS)
    return {"w": w, "chef": w.chefs[0] if w.chefs else EMPTY_CHEF,
            "book": RecipeBook(RECIPES, STATIONS), "bb": None, "tick": 0}


EMPTY_CHEF = {"name": "Player 2", "held": None, "x": 0.0, "z": 0.0}


def task(item="BoiledSushiRice", cooker_id="c1"):
    return T.FillCooker(None, (cooker_id, ("SushiRice",)), item)


# ---------- 身份 ----------

def test_fill_cooker_is_order_independent():
    """order_id 必须是 None——这是绕开按单发射/1:1 匹配的全部意义。"""
    t = task()
    assert t.order_id is None
    assert t.TYPE == "FillCooker"


def test_registered_in_task_types():
    assert T.TASK_TYPES.get("FillCooker") is T.FillCooker


def test_claims_the_cooker_fetch_slot():
    """认领粒度与 FetchToCooker 一致，两者不会同时往一口灶下料。"""
    assert task().claims() == (("c1", "fetch_to"),)


def test_identity_distinguishes_cookers():
    assert task(cooker_id="c1") != task(cooker_id="c2")
    assert task(cooker_id="c1") == task(cooker_id="c1")


# ---------- 可行性 ----------

def test_feasible_when_shortfall_and_idle_cooker():
    """本局病灶形态：5 单要米、灶全空 → 必须可行。"""
    c = ctx(orders=[order("3", "Sushi_Cucumber"), order("4", "Sushi_Fish"),
                    order("5", "Sushi_Fish")],
            cookers=[cooker("c1"), cooker("c2")])
    assert task().feasible(c) is True


def test_infeasible_when_supply_covers_demand():
    """供给够了就别再下锅——否则灶全占满，反而挡住别的料。"""
    c = ctx(orders=[order("3", "Sushi_Cucumber")],
            cookers=[cooker("c1"),
                     cooker("c2", cooking=True, contents=["ing:SushiRice"])])
    assert task().feasible(c) is False


def test_infeasible_when_cooker_already_busy():
    c = ctx(orders=[order("3", "Sushi_Cucumber"), order("4", "Sushi_Fish")],
            cookers=[cooker("c1", cooking=True, contents=["ing:SushiRice"])])
    assert task().feasible(c) is False


def test_infeasible_when_cooker_has_no_pot():
    c = ctx(orders=[order("3", "Sushi_Cucumber"), order("4", "Sushi_Fish")],
            cookers=[cooker("c1", pot=False)])
    assert task().feasible(c) is False


def test_infeasible_when_cooker_burning():
    cc = cooker("c1")
    cc["onFire"] = True
    c = ctx(orders=[order("3", "Sushi_Cucumber"), order("4", "Sushi_Fish")],
            cookers=[cc])
    assert task().feasible(c) is False


def test_infeasible_without_orders():
    """没单就不囤料：囤出来的米没人要，还占着灶。"""
    c = ctx(cookers=[cooker("c1")])
    assert task().feasible(c) is False


def test_infeasible_when_ingredient_unavailable():
    c = ctx(orders=[order("3", "Sushi_Cucumber"), order("4", "Sushi_Fish")],
            cookers=[cooker("c1")])
    c["w"].stations = [s for s in STATIONS if s["t"] != "crate"]
    c["book"] = RecipeBook(RECIPES, c["w"].stations)
    assert task().feasible(c) is False


# ---------- 完成判定 ----------

def test_done_when_pot_started():
    c = ctx(cookers=[cooker("c1", cooking=True, contents=["ing:SushiRice"])])
    assert task().done(c) is True


def test_done_when_raw_in_pot():
    c = ctx(cookers=[cooker("c1", contents=["ing:SushiRice"])])
    assert task().done(c) is True


def test_not_done_when_pot_empty():
    c = ctx(cookers=[cooker("c1")])
    assert task().done(c) is False


# ---------- 树 ----------

def test_build_returns_a_tree():
    assert task().build() is not None

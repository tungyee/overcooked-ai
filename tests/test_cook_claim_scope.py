# -*- coding: utf-8 -*-
"""在途烹饪认领的作用域（第 1 组 1.3）。

`_in_flight_cook_claim` 原本只问「这单有没有**任意** fetch_to/take_cooked/
rescue 认领」，于是一单要两份熟料（或多料菜）时，第一份一下锅就把第二份
的候选全压掉——AI 站着等，灶空着。认领本是为了「同一份熟料别重复下锅」，
作用域应该是**具体的熟料**，不是整单。
"""
from ai import recipe_graph, tasks as T
from ai.blackboard import Blackboard
from ai.recipes import RecipeBook
from ai.world import World


RECIPES = [
    # 一单要两份熟料：米 + 熟鱼（构造场景，验证作用域而非本关配方）
    {"name": "Combo", "comp": ["BoiledSushiRice", "GrilledFish"]},
    {"name": "BoiledSushiRice", "comp": ["SushiRice"], "cook": "Pot"},
    {"name": "GrilledFish", "comp": ["RawFish"], "cook": "FryingPan"},
    {"name": "SushiRice"},
    {"name": "RawFish"},
]

STATIONS = [
    {"t": "crate", "n": "crate_rice", "ing": "SushiRice", "gx": 1, "gz": 1},
    {"t": "crate", "n": "crate_fish", "ing": "RawFish", "gx": 2, "gz": 1},
    {"t": "cooking", "n": "c1", "gx": 5, "gz": 5},
    {"t": "cooking", "n": "c2", "gx": 6, "gz": 5},
]


def _world():
    return World({
        "orders": [{"id": "o1", "dish": "Combo", "remaining": 100, "lifetime": 130,
                    "composition": ["BoiledSushiRice", "GrilledFish"]}],
        "stations": STATIONS, "recipes": RECIPES,
        "chefs": [{"name": "Player 2", "x": 1.0, "z": 1.0, "held": None}],
        "plates": [], "workstations": [],
        "cookers": [{"n": "c1", "x": 5.0, "z": 5.0, "cooked": False,
                     "isCooking": False, "burning": False, "onFire": False,
                     "pot_contents": []},
                    {"n": "c2", "x": 6.0, "z": 5.0, "cooked": False,
                     "isCooking": False, "burning": False, "onFire": False,
                     "pot_contents": []}],
        "items": [], "extinguishers": [], "grid": {"nodeMap": None},
    })


def _emit(w, bb):
    book = RecipeBook(w.recipes, w.stations)
    return recipe_graph.emit(w, bb, book, w.orders[0], {}, {})


def _cook_items(tasks):
    """发射出的 FetchToCooker 覆盖了哪几样熟料（每样会按空灶数各发一个）。"""
    return set(t.item for t in tasks if t.TYPE == "FetchToCooker")


def test_both_cooked_items_emitted_without_claims():
    assert _cook_items(_emit(_world(), Blackboard())) == \
        {"BoiledSushiRice", "GrilledFish"}


def test_claim_on_one_cooked_item_does_not_suppress_the_other():
    """米已下锅，熟鱼的候选必须还在——这才是本条修复的全部内容。"""
    w = _world()
    bb = Blackboard()
    bb.claim("c1", "fetch_to", "P2", "o1", 0, item="BoiledSushiRice")
    assert _cook_items(_emit(w, bb)) == {"GrilledFish"}


def test_claim_still_suppresses_the_same_cooked_item():
    """同一份熟料不能重复下锅——原有保护不能丢。"""
    w = _world()
    bb = Blackboard()
    bb.claim("c1", "fetch_to", "P2", "o1", 0, item="BoiledSushiRice")
    assert "BoiledSushiRice" not in _cook_items(_emit(w, bb))


def test_claim_without_item_falls_back_to_order_scope():
    """老认领（没记 item）退化为整单抑制：宁可少发也不要重复下锅。"""
    w = _world()
    bb = Blackboard()
    bb.claim("c1", "fetch_to", "P2", "o1", 0)
    assert _cook_items(_emit(w, bb)) == set()

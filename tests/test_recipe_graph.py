# -*- coding: utf-8 -*-
"""配方图（HTN 编译）候选发射的回归测试。

覆盖：切→煮链（阶段 2）、存在性 done 对逐板 Chop 的收敛（阶段 1 有意变更）。
"""
from ai.blackboard import Blackboard
from ai.recipes import RecipeBook
from ai.world import World
from ai import recipe_graph


def _world(orders, recipes, stations, plates=None, cookers=None,
           workstations=None, chefs=None, items=None):
    return World({
        "orders": orders,
        "recipes": recipes,
        "stations": stations,
        "plates": plates or [],
        "cookers": cookers or [],
        "workstations": workstations or [],
        "chefs": chefs or [{"name": "Player 2", "x": 0.0, "z": 0.0, "held": None}],
        "items": items or [],
        "extinguishers": [],
        "grid": {"nodeMap": None},
    })


def _emit_types(w, book, plate_match=None, cooker_match=None):
    return [t.TYPE for t in recipe_graph.emit(
        w, Blackboard(), book, w.orders[0],
        plate_match or {}, cooker_match or {})]


# ---------- 阶段 2：切→煮 ----------

_FRIED_MUSHROOM_RECIPES = [
    {"name": "FriedMushrooms", "t": "CookedCompositeOrderNode",
     "comp": ["Mushroom"], "cook": "FryingPan"},
]


def _fried_world(board_item=None, cooked=False):
    return _world(
        orders=[{"id": "order1", "dish": "FriedMushrooms",
                 "composition": ["FriedMushrooms"], "remaining": 60}],
        recipes=_FRIED_MUSHROOM_RECIPES,
        stations=[
            {"t": "crate", "n": "crate_mushroom", "ing": "Mushroom",
             "chop": "ChoppedMushroom", "chop_stages": 8, "gx": 1, "gz": 1},
            {"t": "chopping", "n": "board_01", "gx": 2, "gz": 2,
             "item": board_item},
            {"t": "counter", "n": "counter_01", "gx": 3, "gz": 3},
        ],
        plates=[{"n": "plate_01", "x": 6.0, "z": 6.0, "count": 0,
                 "contents": []}],
        cookers=[{"n": "cooker_01", "x": 4.0, "z": 4.0, "cooked": cooked}],
    )


def test_chop_then_cook_emits_fetch_first():
    """切→煮链的第一步：什么都没做 → 只发 FetchToBoard（取生料上板）。"""
    w = _fried_world()
    book = RecipeBook(w.recipes, w.stations)
    types = _emit_types(w, book, {"plate_01": "order1"})
    assert "FetchToBoard" in types
    assert "Chop" not in types
    assert "FetchToCooker" not in types


def test_chop_then_cook_emits_chop_when_raw_on_board():
    """生料在板上 → 发 Chop（下一步切）。"""
    w = _fried_world(board_item="Mushroom")
    book = RecipeBook(w.recipes, w.stations)
    types = _emit_types(w, book, {"plate_01": "order1"})
    assert "Chop" in types
    assert "FetchToBoard" not in types
    assert "FetchToCooker" not in types


def test_chop_then_cook_emits_fetch_to_cooker_when_chopped():
    """切好在板上 → 发 FetchToCooker（取切好形态下锅）。"""
    w = _fried_world(board_item="ChoppedMushroom")
    book = RecipeBook(w.recipes, w.stations)
    types = _emit_types(w, book, {"plate_01": "order1"})
    assert "FetchToCooker" in types
    assert "Chop" not in types
    assert "FetchToBoard" not in types


# ---------- 阶段 1：存在性 done 抑制逐板 Chop ----------

def test_chop_suppressed_when_chopped_exists_elsewhere():
    """别处已有切好的 → 不再对「还有生鱼的那块板」重复发 Chop（有意收敛）。"""
    w = _world(
        orders=[{"id": "order1", "dish": "Sushi_Fish",
                 "composition": ["SushiFish"], "remaining": 60}],
        recipes=[{"name": "SushiFish", "comp": []}],
        stations=[
            {"t": "crate", "n": "crate_fish", "ing": "SushiFish",
             "chop": "ChoppedSushiFish", "chop_stages": 8, "gx": 1, "gz": 1},
            {"t": "chopping", "n": "board_chopped", "gx": 2, "gz": 2,
             "item": "ChoppedSushiFish"},
            {"t": "chopping", "n": "board_raw", "gx": 3, "gz": 3,
             "item": "SushiFish"},
            {"t": "counter", "n": "counter_01", "gx": 4, "gz": 4},
        ],
        plates=[{"n": "plate_01", "x": 6.0, "z": 6.0, "count": 0,
                 "contents": []}],
    )
    book = RecipeBook(w.recipes, w.stations)
    types = _emit_types(w, book, {"plate_01": "order1"})
    assert "Chop" not in types, "切好的已存在，不该再发 Chop（哪怕生鱼板还有鱼）"
    assert "FetchToBoard" not in types


# ---------- 阶段 3：多料同锅 + token ----------

_PASTA_RECIPES = [
    {"name": "Pasta_TomatoOnly", "t": "CookedCompositeOrderNode",
     "comp": ["Pasta", "Tomato"], "cook": "Pot"},
    {"name": "Pasta", "comp": []},
    {"name": "Tomato", "comp": []},
]


def _pasta_world():
    return _world(
        orders=[{"id": "order1", "dish": "Pasta_TomatoOnly",
                 "composition": ["Pasta_TomatoOnly"], "remaining": 60}],
        recipes=_PASTA_RECIPES,
        stations=[
            {"t": "crate", "n": "crate_pasta", "ing": "Pasta", "gx": 1, "gz": 1},
            {"t": "crate", "n": "crate_tomato", "ing": "Tomato",
             "chop": "ChoppedTomato", "chop_stages": 8, "gx": 2, "gz": 1},
            {"t": "chopping", "n": "board_01", "gx": 3, "gz": 1,
             "item": "ChoppedTomato"},
            {"t": "counter", "n": "counter_01", "gx": 4, "gz": 1},
        ],
        plates=[{"n": "plate_01", "x": 6.0, "z": 6.0, "count": 0,
                 "contents": []}],
        cookers=[{"n": "cooker_01", "x": 5.0, "z": 1.0, "cooked": False}],
    )


def test_multi_raw_rich_token_joins_all_raws():
    book = RecipeBook(_PASTA_RECIPES, [])
    assert book.rich_token("Pasta_TomatoOnly") == "cooked:pasta+tomato:pot"


def test_multi_raw_cook_emits_fetch_to_cooker_with_raws():
    w = _pasta_world()
    book = RecipeBook(w.recipes, w.stations)
    tasks = recipe_graph.emit(w, Blackboard(), book, w.orders[0],
                              {"plate_01": "order1"}, {})
    f2c = [t for t in tasks if t.TYPE == "FetchToCooker"]
    assert f2c, "番茄已切好、意面可得 → 应发 FetchToCooker"
    assert f2c[0].raws == ("Pasta", "Tomato")


# ---------- 阶段 4：拌合 ----------

def test_mix_node_emits_fetch_first_then_mix():
    """Cake_Carrot 的拌合链：先切胡萝卜（FetchToBoard 前沿），Mix 依赖满足后才发。"""
    w = _world(
        orders=[{"id": "order1", "dish": "Cake_Carrot",
                 "composition": ["Cake_Carrot"], "remaining": 60}],
        recipes=[
            {"name": "Cake_Carrot", "t": "CookedCompositeOrderNode",
             "comp": ["MixedFlourEggHoneycombCarrot"], "cook": "OvenCakeTin"},
            {"name": "MixedFlourEggHoneycombCarrot",
             "t": "MixedCompositeOrderNode",
             "comp": ["Flour", "Egg", "Honeycomb", "Carrot"]},
            {"name": "Flour", "comp": []}, {"name": "Egg", "comp": []},
            {"name": "Honeycomb", "comp": []}, {"name": "Carrot", "comp": []},
        ],
        stations=[
            {"t": "mixing", "n": "mixer_01", "gx": 3, "gz": 3},
            {"t": "crate", "n": "crate_flour", "ing": "Flour", "gx": 1, "gz": 1},
            {"t": "crate", "n": "crate_egg", "ing": "Egg", "gx": 2, "gz": 1},
            {"t": "crate", "n": "crate_honey", "ing": "Honeycomb", "gx": 3, "gz": 1},
            {"t": "crate", "n": "crate_carrot", "ing": "Carrot",
             "chop": "ChoppedCarrot", "chop_stages": 8, "gx": 4, "gz": 1},
            {"t": "chopping", "n": "board_01", "gx": 5, "gz": 1, "item": None},
            {"t": "counter", "n": "counter_01", "gx": 6, "gz": 1},
        ],
        plates=[{"n": "plate_01", "x": 8.0, "z": 8.0, "count": 0,
                 "contents": []}],
        cookers=[{"n": "oven_01", "x": 7.0, "z": 1.0, "cooked": False}],
    )
    book = RecipeBook(w.recipes, w.stations)
    types = _emit_types(w, book, {"plate_01": "order1"})
    # 胡萝卜需切 → 前沿是 FetchToBoard（生胡萝卜上板）；Mix 依赖未齐，尚不发
    assert "FetchToBoard" in types
    assert "Mix" not in types

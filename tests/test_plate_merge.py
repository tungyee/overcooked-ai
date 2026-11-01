"""盘合并（plate merge）单测：RecipeBook.missing_union / MergePlates task /
_gen_merge 候选 / 并集完成度。

复用 tests/test_recipes.py 的实测帧配方数据（Sushi_1_4 的 RECIPES / STATIONS）。
"""
from ai.query import recipes as R

# 实测帧的真实数据（tests/fixtures/real_frame_sushi_1_4.json）
RECIPES = [
    {"name": "Sushi_Fish", "t": "CompositeOrderNode", "comp":
        ["Seaweed", "BoiledSushiRice", "SushiFish"], "cook": "null"},
    {"name": "Sushi_Cucumber", "t": "CompositeOrderNode", "comp":
        ["Seaweed", "BoiledSushiRice", "Cucumber"], "cook": "null"},
    {"name": "BoiledSushiRice", "t": "CookedCompositeOrderNode",
     "comp": ["SushiRice"], "cook": "Pot", "plate": "CompositeSushi"},
    {"name": "Seaweed", "t": "IngredientOrderNode", "comp": [],
     "cook": "null", "plate": "CompositeSushi"},
    {"name": "Cucumber", "t": "IngredientOrderNode", "comp": [],
     "cook": "null", "plate": "CompositeSushi"},
    {"name": "SushiFish", "t": "IngredientOrderNode", "comp": [],
     "cook": "null", "plate": "CompositeSushi"},
    {"name": "SushiRice", "t": "IngredientOrderNode", "comp": [],
     "cook": "null", "plate": "null"},
]

STATIONS = [
    {"t": "crate", "n": "DispenserCrate 3 (1)", "ing": "Seaweed",
     "chop": None, "chop_stages": None, "gx": -5, "gz": 4},
    {"t": "crate", "n": "DispenserCrate 3 (5)", "ing": "SushiFish",
     "chop": "ChoppedSushiFish", "chop_stages": 8, "gx": -1, "gz": 4},
    {"t": "crate", "n": "DispenserCrate 3 (3)", "ing": "SushiRice",
     "chop": None, "chop_stages": None, "gx": -5, "gz": 3},
    {"t": "crate", "n": "DispenserCrate 3 (6)", "ing": "Cucumber",
     "chop": "ChoppedCucumber", "chop_stages": 8, "gx": -1, "gz": 3},
]

ORDER_FISH = {"id": "1", "dish": "Sushi_Fish",
              "composition": ["Seaweed", "BoiledSushiRice", "SushiFish"]}


def book():
    return R.RecipeBook(RECIPES, STATIONS)


# ---------- Task 1: RecipeBook.missing_union ----------

def test_missing_union_empty_when_union_covers_order():
    b = book()
    seaweed = {"n": "-1", "detail": ["ing:Seaweed"]}
    fish_rice = {"n": "-2", "detail": ["ing:SushiFish",
                                       "CookedCompositeAssembledNode(Pot){ ing:SushiRice }"]}
    assert b.missing_union(ORDER_FISH, [seaweed, fish_rice]) == ()


def test_missing_union_only_counts_union():
    b = book()
    seaweed = {"n": "-1", "detail": ["ing:Seaweed"]}
    assert b.missing_union(ORDER_FISH, [seaweed]) == (
        "BoiledSushiRice", "SushiFish")

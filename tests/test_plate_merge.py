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


# ---------- Task 2: MergePlates task 契约 ----------

from ai import tasks as T  # noqa: E402


class W(object):
    def __init__(self, plates=(), orders=(), chefs=(), stations=()):
        self.plates = list(plates)
        self.orders = list(orders)
        self.chefs = list(chefs)
        self.stations = list(stations)
        self.recipes = RECIPES


P2 = {"name": "Player 2", "held": None, "x": 0.0, "z": 0.0}
P1 = {"name": "Player 1", "held": None, "x": 1.0, "z": 0.0}


def make_ctx(plates, orders=(), chefs=(), stations=()):
    w = W(plates, orders, chefs, stations)
    return {"w": w, "chef": w.chefs[0] if w.chefs else P2,
            "book": book()}


def plate(n, detail, held_by=None, x=1.0, z=1.0):
    return {"n": n, "detail": list(detail), "contents": list(detail),
            "held_by": held_by, "x": x, "z": z}


SEAWEED = plate("s1", ["ing:Seaweed"])
FISH_RICE = plate("d1", ["ing:SushiFish",
                         "CookedCompositeAssembledNode(Pot){ ing:SushiRice }"])


def test_merge_plates_claims_both_plates():
    t = T.MergePlates("1", ("s1", "d1"))
    assert t.claims() == (("s1", "merge"), ("d1", "merge"))


def test_merge_plates_feasible_when_union_completes_order():
    t = T.MergePlates("1", ("s1", "d1"))
    ctx = make_ctx([SEAWEED, FISH_RICE], orders=[ORDER_FISH],
                   chefs=[P2, P1])
    assert t.feasible(ctx) is True


def test_merge_plates_feasible_false_on_overlap_no_progress():
    # src 只有海苔，dst 已有海苔+鱼：并集不减少缺料（饭仍缺），严格进展不成立
    dst = plate("d1", ["ing:Seaweed", "ing:SushiFish"])
    t = T.MergePlates("1", ("s1", "d1"))
    ctx = make_ctx([SEAWEED, dst], orders=[ORDER_FISH], chefs=[P2, P1])
    assert t.feasible(ctx) is False


def test_merge_plates_feasible_false_on_union_superset():
    # 并集多了黄瓜，不是该单子集
    dst = plate("d1", ["ing:SushiFish", "ing:Cucumber",
                       "CookedCompositeAssembledNode(Pot){ ing:SushiRice }"])
    t = T.MergePlates("1", ("s1", "d1"))
    ctx = make_ctx([SEAWEED, dst], orders=[ORDER_FISH], chefs=[P2, P1])
    assert t.feasible(ctx) is False


def test_merge_plates_feasible_false_when_dst_held_by_p1():
    dst = plate("d1", ["ing:SushiFish",
                       "CookedCompositeAssembledNode(Pot){ ing:SushiRice }"],
                held_by="Player 1")
    t = T.MergePlates("1", ("s1", "d1"))
    ctx = make_ctx([SEAWEED, dst], orders=[ORDER_FISH], chefs=[P2, P1])
    assert t.feasible(ctx) is False


def test_merge_plates_done_when_src_empty():
    src = plate("s1", [], held_by="Player 2")
    t = T.MergePlates("1", ("s1", "d1"))
    ctx = make_ctx([src, FISH_RICE], orders=[ORDER_FISH], chefs=[P2, P1])
    assert t.done(ctx) is True


def test_merge_plates_done_false_when_src_still_has_contents():
    t = T.MergePlates("1", ("s1", "d1"))
    ctx = make_ctx([SEAWEED, FISH_RICE], orders=[ORDER_FISH], chefs=[P2, P1])
    assert t.done(ctx) is False

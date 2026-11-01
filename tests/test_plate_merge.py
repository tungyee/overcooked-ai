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


# ---------- Task 3: _gen_merge 候选生成 ----------

from ai.state.world import World  # noqa: E402
from ai.state.blackboard import Blackboard  # noqa: E402
from ai.core import planner  # noqa: E402


def _merge_world(plates, orders, chefs):
    return World({
        "orders": orders,
        "stations": [],
        "recipes": RECIPES,
        "chefs": chefs,
        "plates": plates,
        "workstations": [], "cookers": [],
        "items": [], "extinguishers": [], "grid": {"nodeMap": None},
    })


P2W = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None}
P1W = {"name": "Player 1", "x": 1.0, "z": 0.0, "held": None}


def _merge_tasks(cands):
    return [c.task for c in cands if c.task.TYPE == "MergePlates"]


def test_gen_merge_emits_complementary_pair_more_tokens_as_dst():
    w = _merge_world([SEAWEED, FISH_RICE], [ORDER_FISH], [P2W, P1W])
    cands = planner._gen_merge(w, Blackboard(), book())
    tasks = _merge_tasks(cands)
    assert len(tasks) == 1
    # d1 有 2 token、s1 只有 1 token → d1 作 dst
    assert (tasks[0].src_id, tasks[0].dst_id) == ("s1", "d1")
    assert tasks[0].order_id == "1"


def test_gen_merge_skips_overlap_pair():
    dst = plate("d1", ["ing:Seaweed", "ing:SushiFish"])
    w = _merge_world([SEAWEED, dst], [ORDER_FISH], [P2W, P1W])
    assert _merge_tasks(planner._gen_merge(w, Blackboard(), book())) == []


def test_gen_merge_skips_union_superset():
    dst = plate("d1", ["ing:SushiFish", "ing:Cucumber",
                       "CookedCompositeAssembledNode(Pot){ ing:SushiRice }"])
    w = _merge_world([SEAWEED, dst], [ORDER_FISH], [P2W, P1W])
    assert _merge_tasks(planner._gen_merge(w, Blackboard(), book())) == []


def test_gen_merge_skips_p1_held_plate():
    dst = plate("d1", ["ing:SushiFish",
                       "CookedCompositeAssembledNode(Pot){ ing:SushiRice }"],
                held_by="Player 1")
    w = _merge_world([SEAWEED, dst], [ORDER_FISH], [P2W, P1W])
    assert _merge_tasks(planner._gen_merge(w, Blackboard(), book())) == []


def test_gen_merge_equal_tokens_lower_id_as_dst_stable():
    a = plate("p1", ["ing:Seaweed"])
    b = plate("p2", ["ing:SushiFish"])
    w = _merge_world([a, b], [ORDER_FISH], [P2W, P1W])
    bb = Blackboard()
    got = _merge_tasks(planner._gen_merge(w, bb, book()))
    got2 = _merge_tasks(planner._gen_merge(w, bb, book()))
    assert len(got) == 1
    # 平手（各 1 token）→ id 小的 p1 作 dst；连跑两拍定向一致
    assert (got[0].src_id, got[0].dst_id) == ("p2", "p1")
    assert [(t.src_id, t.dst_id) for t in got2] == [("p2", "p1")]

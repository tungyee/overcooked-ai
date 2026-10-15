# -*- coding: utf-8 -*-
"""熟料供需计数回归（第 1 组 1.2）。

根因不是权重而是结构：`FetchToCooker` 只能由 recipe_graph 按单发射，
`_in_flight_cook_claim` 压掉同单第二个下锅候选，`_match_cookers_to_orders`
又是严格 1:1 —— 于是「5 单要米」永远不等于「3 口灶在烧」。实测
trace-20261014-120406 的 1700~3270 窗口（正是 order#3/4/5 三单过期的窗口）
**59% 的拍里三口灶一个都没在煮**，峰值也只有 2 口。

要让「填灶」成为订单无关的世界谓词，先得能回答两个纯计数问题：
这种熟料还缺几份（demand）、已经有几份在路上（supply）。
"""
from ai import cook_supply as CS
from ai.recipes import RecipeBook


RECIPES = [
    {"name": "Sushi_Fish", "comp": ["Seaweed", "BoiledSushiRice", "SushiFish"]},
    {"name": "Sushi_Cucumber", "comp": ["Seaweed", "BoiledSushiRice", "Cucumber"]},
    {"name": "BoiledSushiRice", "comp": ["SushiRice"], "cook": "Pot"},
    {"name": "SushiRice"},
    {"name": "SushiFish", "chop": "ChoppedSushiFish"},
    {"name": "Cucumber", "chop": "ChoppedCucumber"},
    {"name": "Seaweed"},
]


def book():
    return RecipeBook(RECIPES, [])


def order(oid, dish, remaining=100.0):
    comp = {"Sushi_Fish": ["Seaweed", "BoiledSushiRice", "SushiFish"],
            "Sushi_Cucumber": ["Seaweed", "BoiledSushiRice", "Cucumber"]}[dish]
    return {"id": oid, "dish": dish, "composition": comp,
            "remaining": remaining, "lifetime": 130}


def cooker(n, pot=True, cooking=False, cooked=False, contents=()):
    return {"n": n, "cooked": (cooked if pot else None),
            "isCooking": cooking, "burning": False, "onFire": False,
            "pot_contents": list(contents)}


def plate(n, detail=()):
    return {"n": n, "contents": list(detail), "detail": list(detail)}


class W(object):
    """只读世界替身：供需计数是纯谓词，不该依赖真 World。"""

    def __init__(self, orders=(), cookers=(), plates=(), chefs=(), items=()):
        self.orders = list(orders)
        self.cookers = list(cookers)
        self.plates = list(plates)
        self.chefs = list(chefs)
        self.items = list(items)
        self.recipes = RECIPES


# ---------- 需求 ----------

def test_demand_counts_every_order_needing_the_cooked_item():
    """本局实况：5 单全要米饭。"""
    w = W(orders=[order("1", "Sushi_Fish"), order("2", "Sushi_Cucumber"),
                  order("3", "Sushi_Cucumber"), order("4", "Sushi_Fish"),
                  order("5", "Sushi_Fish")])
    assert CS.cooked_demand(w, book(), "BoiledSushiRice") == 5


def test_demand_counts_requirements_not_outstanding_work():
    """demand 是「一共要几份」，**不扣除已装盘的**。

    已装盘那份由 cooked_supply 计入供给，两边只能扣一次。曾经两边都扣，
    于是缺口被重复减去（真实帧 t=3000：5 单要米、2 份已装盘，缺口应为 3
    却算成 1，填灶加分从 4.0 掉到 1.33、选不上）。
    """
    w = W(orders=[order("1", "Sushi_Fish"), order("2", "Sushi_Cucumber")],
          plates=[plate("p1", ["seaweed", "cooked:sushirice:pot"])])
    assert CS.cooked_demand(w, book(), "BoiledSushiRice") == 2


def test_plated_portion_is_subtracted_exactly_once():
    """同一份已装盘的米只能影响缺口一次——这条就是上面那个 bug 的卡口。"""
    b = book()
    w = W(orders=[order("1", "Sushi_Fish"), order("2", "Sushi_Cucumber")],
          plates=[plate("p1", ["seaweed", "cooked:sushirice:pot"])])
    assert CS.cooked_demand(w, b, "BoiledSushiRice") == 2
    assert CS.cooked_supply(w, b, "BoiledSushiRice") == 1
    assert CS.cooked_shortfall(w, b, "BoiledSushiRice") == 1


def test_demand_ignores_non_cooked_requirements():
    w = W(orders=[order("1", "Sushi_Fish")])
    assert CS.cooked_demand(w, book(), "Seaweed") == 0


def test_demand_is_zero_without_orders():
    assert CS.cooked_demand(W(), book(), "BoiledSushiRice") == 0


# ---------- 供给 ----------

def test_supply_counts_cooking_pots():
    w = W(cookers=[cooker("c1", cooking=True, contents=["ing:SushiRice"]),
                   cooker("c2")])
    assert CS.cooked_supply(w, book(), "BoiledSushiRice") == 1


def test_supply_counts_cooked_pots_not_yet_taken():
    w = W(cookers=[cooker("c1", cooked=True,
                          contents=["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"])])
    assert CS.cooked_supply(w, book(), "BoiledSushiRice") == 1


def test_supply_counts_plated_portions():
    """已装盘未送的也算供给：那份米已经不需要再煮一次。"""
    w = W(orders=[order("1", "Sushi_Fish")],
          plates=[plate("p1", ["cooked:sushirice:pot"])])
    assert CS.cooked_supply(w, book(), "BoiledSushiRice") == 1


def test_supply_counts_held_cooked_pot():
    """手里端着的熟锅是供给——否则会边端边再下一锅，灶全占满还装不下。"""
    w = W(chefs=[{"name": "Player 2", "held": "utensil_pot_01",
                  "detail": ["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"]}])
    assert CS.cooked_supply(w, book(), "BoiledSushiRice") == 1


def test_supply_counts_loose_cooked_pot_on_counter():
    w = W(items=[{"n": "utensil_pot_01", "x": 1.0, "z": 1.0, "cooked": "Cooked",
                  "detail": ["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"]}])
    assert CS.cooked_supply(w, book(), "BoiledSushiRice") == 1


def test_supply_excludes_burnt_pot():
    """糊锅不是供给：它要被倒掉，指望它就等于少煮一份。"""
    c = cooker("c1", cooked=True,
               contents=["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"])
    c["burning"] = True
    assert CS.cooked_supply(W(cookers=[c]), book(), "BoiledSushiRice") == 0


def test_supply_excludes_empty_pot():
    w = W(cookers=[cooker("c1"), cooker("c2")])
    assert CS.cooked_supply(w, book(), "BoiledSushiRice") == 0


# ---------- 缺口 ----------

def test_shortfall_is_demand_minus_supply():
    w = W(orders=[order("1", "Sushi_Fish"), order("2", "Sushi_Cucumber"),
                  order("3", "Sushi_Fish")],
          cookers=[cooker("c1", cooking=True, contents=["ing:SushiRice"])])
    assert CS.cooked_shortfall(w, book(), "BoiledSushiRice") == 2


def test_shortfall_never_negative():
    """供给过剩不该给出负缺口（会反向变成加分）。"""
    w = W(orders=[order("1", "Sushi_Fish")],
          cookers=[cooker("c1", cooking=True, contents=["ing:SushiRice"]),
                   cooker("c2", cooking=True, contents=["ing:SushiRice"])])
    assert CS.cooked_shortfall(w, book(), "BoiledSushiRice") == 0


def test_real_trace_window_shows_shortfall_while_cookers_idle():
    """复现本局 1700~3270 的病灶形态：5 单要米、一口都没烧 → 缺口 5。

    这正是 59% 灶空转窗口里 planner 看不到的那个量。
    """
    w = W(orders=[order("3", "Sushi_Cucumber", 60), order("4", "Sushi_Fish", 70),
                  order("5", "Sushi_Fish", 80), order("6", "Sushi_Cucumber", 100),
                  order("7", "Sushi_Cucumber", 120)],
          cookers=[cooker("c1"), cooker("c2"), cooker("c3")])
    assert CS.cooked_shortfall(w, book(), "BoiledSushiRice") == 5


def test_real_trace_t3000_shortfall_is_three():
    """真实帧 t=3000 的形态：5 单要米、2 份已装盘、三口灶全空 → 缺口 3。

    与 trace 回放实测一致（见 ai/cook_supply.py 的重复扣减注释）。
    """
    w = W(orders=[order("3", "Sushi_Cucumber"), order("4", "Sushi_Fish"),
                  order("5", "Sushi_Fish"), order("6", "Sushi_Cucumber"),
                  order("7", "Sushi_Cucumber")],
          plates=[plate("p1", ["seaweed", "cooked:sushirice:pot"]),
                  plate("p2", ["seaweed", "cooked:sushirice:pot"])],
          cookers=[cooker("c1"), cooker("c2"), cooker("c3")])
    assert CS.cooked_shortfall(w, book(), "BoiledSushiRice") == 3


# ---------- 空闲灶 ----------

def test_idle_cookers_lists_empty_pots_only():
    w = W(cookers=[cooker("c1"), cooker("c2", cooking=True, contents=["ing:SushiRice"]),
                   cooker("c3", pot=False)])
    assert [c["n"] for c in CS.idle_cookers(w)] == ["c1"]


def test_idle_cookers_excludes_burning():
    c = cooker("c1")
    c["onFire"] = True
    assert CS.idle_cookers(W(cookers=[c])) == []

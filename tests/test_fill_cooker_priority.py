# -*- coding: utf-8 -*-
"""填灶的优先级边界（第 1 组 1.4）——真实帧核对过的四条。

填灶要压过备菜（否则灶继续空转），但不能压过送餐和端熟锅（锅会烧糊、盘
不会）。这四条分别对应 trace-20261014-120406 的 t=1800 / t=2760 / t=3226
/ t=2279，离线复验的分数写在断言旁。
"""
import pytest

from ai.core import planner
from ai import tasks as T
from ai.state.blackboard import Blackboard
from ai.query.recipes import RecipeBook
from ai.state.world import World


RECIPES = [
    {"name": "Sushi_Cucumber", "comp": ["Seaweed", "BoiledSushiRice", "Cucumber"]},
    {"name": "BoiledSushiRice", "comp": ["SushiRice"], "cook": "Pot"},
    {"name": "SushiRice"},
    {"name": "Cucumber", "chop": "ChoppedCucumber"},
    {"name": "Seaweed"},
]

STATIONS = [
    {"t": "crate", "n": "crate_rice", "ing": "SushiRice", "gx": 1, "gz": 1},
    {"t": "crate", "n": "crate_weed", "ing": "Seaweed", "gx": 2, "gz": 1},
    {"t": "cooking", "n": "c1", "gx": 5, "gz": 5},
    {"t": "cooking", "n": "c2", "gx": 6, "gz": 5},
    {"t": "serving", "n": "hatch", "gx": 9, "gz": 9},
]


def _cooker(n, cooked=None, cooking=False, contents=()):
    return {"n": n, "x": 5.0, "z": 5.0, "cooked": cooked, "isCooking": cooking,
            "burning": False, "onFire": False, "pot_contents": list(contents)}


def _world(cookers, plates=(), orders=None, held=None):
    return World({
        "orders": orders if orders is not None else [
            {"id": "1", "dish": "Sushi_Cucumber", "remaining": 60, "lifetime": 130,
             "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]},
            {"id": "2", "dish": "Sushi_Cucumber", "remaining": 80, "lifetime": 130,
             "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]},
            {"id": "3", "dish": "Sushi_Cucumber", "remaining": 100, "lifetime": 130,
             "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]},
        ],
        "stations": STATIONS, "recipes": RECIPES,
        "chefs": [{"name": "Player 2", "x": 1.0, "z": 1.0, "held": held}],
        "plates": list(plates), "workstations": [], "cookers": list(cookers),
        "items": [], "extinguishers": [], "grid": {"nodeMap": None},
    })


def _score(w, tasks):
    bb = Blackboard()
    book = RecipeBook(w.recipes, w.stations)
    cands = [planner.Candidate(t, [], 15) for t in tasks]
    scored = planner._score_tasks(tasks, cands, w, bb, book)
    return {t.TYPE: s for t, s, _ in scored}


def test_fill_outranks_prep_when_cookers_idle():
    """t=1800 形态：3 单要米、灶全空 → 填灶必须压过摆海苔。

    这条是跨组权重冲突的卡口。第 3 组把 urgency 改成读 slack 后，备菜候选在
    中段能拿到 3~4 分 urgency（旧曲线恒 ~0.1），一度把 W_COOKER_IDLE=4.0 的
    填灶顶下去，第 1 组在真实帧上验证过的七个"灶空转该填灶"的拍全部回退。
    单组测试发现不了这种冲突，只有重放真实帧才看得见——所以把量级关系在这里
    钉死。
    """
    w = _world([_cooker("c1"), _cooker("c2")])
    by = _score(w, [T.FillCooker(None, ("c1", ("SushiRice",)), "BoiledSushiRice"),
                    T.FetchToPlate("1", ("p1",), "Seaweed")])
    assert by["FillCooker"] > by["FetchToPlate"], by


def test_delivery_still_outranks_fill():
    """t=3226 形态：料齐的盘要先送出去，填灶不能抢（实测 3.35 vs 2.50）。"""
    plate = {"n": "p1", "x": 1.0, "z": 1.0,
             "contents": ["seaweed", "cooked:sushirice:pot", "choppedcucumber"],
             "detail": ["ing:Seaweed",
                        "CookedCompositeAssembledNode(Pot){ ing:SushiRice }",
                        "ing:ChoppedCucumber"]}
    w = _world([_cooker("c1"), _cooker("c2")], plates=[plate])
    by = _score(w, [T.FillCooker(None, ("c1", ("SushiRice",)), "BoiledSushiRice"),
                    T.DeliverPlate("1", ("p1",))])
    assert by["DeliverPlate"] > by["FillCooker"], by


def test_hot_pot_still_outranks_fill():
    """t=2760 形态：锅已熟，端锅装盘必须压过填另一口灶（锅会烧糊、盘不会）。"""
    hot = _cooker("c1", cooked=True,
                  contents=["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"])
    plate = {"n": "p1", "x": 1.0, "z": 1.0, "contents": ["seaweed"],
             "detail": ["ing:Seaweed"]}
    w = _world([hot, _cooker("c2")], plates=[plate])
    by = _score(w, [T.FillCooker(None, ("c2", ("SushiRice",)), "BoiledSushiRice"),
                    T.TakeCookedToPlate("1", ("c1", "p1"), "BoiledSushiRice")])
    assert by["TakeCookedToPlate"] > by["FillCooker"], by


def test_fill_is_not_gated_on_having_a_plate():
    """一张盘都没有时填灶照样要做（第 1 组 1.5）。

    `async_resource` 对按单下锅有「订单已有盘才给加分」的门（它修的是另一个
    实机 bug：洗好碗后 P2 一直煮饭不去端盘）。那道门曾把灶空转钉死：
    没盘 → 不煮 → 米不够 → 单过期。FillCooker 订单无关、不经过那一项，
    所以无需放宽原门就解开了死锁——这条断言钉住这个性质。
    """
    w = _world([_cooker("c1"), _cooker("c2")], plates=[])
    bb = Blackboard()
    book = RecipeBook(w.recipes, w.stations)
    t = T.FillCooker(None, ("c1", ("SushiRice",)), "BoiledSushiRice")
    score, terms = planner._score_candidate(
        w, bb, planner.Candidate(t, [], 15), book, plate_match={})
    assert terms["cooker_idle"] > 0
    assert score > 0


def _fill_task():
    return T.FillCooker(None, ("c1", ("SushiRice",)), "BoiledSushiRice")


def _plate(n):
    # stack_top/clean 让空盘被算作「已就位」：盘匹配只认台面盘、手持盘、
    # 带料散盘和干净盘堆顶层（见 planner._match_plates_to_orders）。
    return {"n": n, "x": 1.0, "z": 1.0, "contents": [], "detail": [],
            "stack_top": True, "stack_clean": True}


def test_fill_damped_when_no_order_has_a_plate():
    """没有可上盘的订单时，填灶只保留地板优先级。

    2~3 单要米但全都没有盘 → 煮了也上不了桌；不能按满额 5.6 压过备菜/盘链。
    仍保留 `FILL_PLATE_DISCOUNT_FLOOR`，避免「没盘 → 不煮 → 单过期」死锁。
    """
    w = _world([_cooker("c1"), _cooker("c2")], plates=[])
    bb = Blackboard()
    book = RecipeBook(w.recipes, w.stations)
    _, terms = planner._score_candidate(
        w, bb, planner.Candidate(_fill_task(), [], 15), book, plate_match={})
    full = planner._cooker_idle_bonus(3)
    assert 0 < terms["cooker_idle"] < full
    assert terms["cooker_idle"] == pytest.approx(
        full * planner.FILL_PLATE_DISCOUNT_FLOOR)


def test_fill_damped_proportional_to_plated_orders():
    """3 单要米、只有 1 单有盘 → 填灶按 1/3 打折。"""
    w = _world([_cooker("c1"), _cooker("c2")], plates=[_plate("p1")])
    bb = Blackboard()
    book = RecipeBook(w.recipes, w.stations)
    _, terms = planner._score_candidate(
        w, bb, planner.Candidate(_fill_task(), [], 15), book,
        plate_match={"p1": "1"})
    assert terms["cooker_idle"] == pytest.approx(
        planner._cooker_idle_bonus(3) / 3.0)


def test_fill_full_when_all_orders_have_plates():
    """3 单都有盘 → 不打折，仍是满额填灶。"""
    plates = [_plate("p1"), _plate("p2"), _plate("p3")]
    w = _world([_cooker("c1"), _cooker("c2")], plates=plates)
    bb = Blackboard()
    book = RecipeBook(w.recipes, w.stations)
    _, terms = planner._score_candidate(
        w, bb, planner.Candidate(_fill_task(), [], 15), book,
        plate_match={"p1": "1", "p2": "2", "p3": "3"})
    assert terms["cooker_idle"] == pytest.approx(
        planner._cooker_idle_bonus(3))


def test_fill_not_generated_when_supply_covers_demand():
    """供给够了就别占灶：1 单要米、已有一锅在煮 → 填灶不可行。"""
    w = _world([_cooker("c1"),
                _cooker("c2", cooking=True, contents=["ing:SushiRice"])],
               orders=[{"id": "1", "dish": "Sushi_Cucumber", "remaining": 60,
                        "lifetime": 130,
                        "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]}])
    book = RecipeBook(w.recipes, w.stations)
    ctx = {"w": w, "chef": w.chefs[0], "book": book, "bb": Blackboard(), "tick": 0}
    t = T.FillCooker(None, ("c1", ("SushiRice",)), "BoiledSushiRice")
    assert t.feasible(ctx) is False

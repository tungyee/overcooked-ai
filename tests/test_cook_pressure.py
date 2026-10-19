# -*- coding: utf-8 -*-
"""时间压力曲线 + "该单需熟料" 的回归测试（实机两个 bug）。

- 米饭熟了也不去拿 → `_time_pressure` 曾是空实现（恒 0），熟料 task 结构性垫底。
- 反复换灶 → `_gen_cook` 漏了 spec §6.1 第 3 号的"该单需熟料"子句。
"""
import pytest

from ai.state.blackboard import Blackboard, StuckScratch
from ai.core import planner
from ai.query import recipe_graph
from ai import tasks as T
from ai.query.recipes import RecipeBook
from ai.state.world import World


def _world(cookers=None, stations=None, orders=None, recipes=None):
    return World({
        "orders": orders or [],
        "stations": stations or [],
        "recipes": recipes or [],
        "chefs": [{"name": "Player 2", "x": 1.0, "z": 1.0, "held": None}],
        "plates": [], "workstations": [], "cookers": cookers or [],
        "items": [], "extinguishers": [], "grid": {"nodeMap": None},
    })


def _cooker(name="cooker_01", cooked=None, is_cooking=None, burning=None,
            x=1.0, z=1.0):
    return {"n": name, "x": x, "z": z, "cooked": cooked,
            "isCooking": is_cooking, "burning": burning, "onFire": burning}


# ---------- 火情不受 L3 拉黑（spec §7.2 的例外）----------

def test_plan_firefighting_ignores_abandon_blacklist():
    """着火的那口灶被别的 task 拉黑了，也照样要去灭。

    拉黑是按资源 id 记的：Normal 里 ReturnPot / RescueCooked 在同一口灶上
    反复失败，会把**着火的那口灶**写进 scratch.abandoned。火不能因此没人
    管——plan_firefighting 忽略这条否决。
    """
    stations = [{"t": "cooking", "n": "cooker_01", "gx": 1, "gz": 1}]
    w = _world(cookers=[_cooker(burning=True)], stations=stations)
    bb = Blackboard()
    bb.scratch = StuckScratch()
    bb.scratch.abandoned["cooker_01"] = bb.tick + 200
    book = RecipeBook([], stations)

    chosen, record = planner.plan_firefighting(w, bb, book)

    assert chosen is not None, "火不能被拉黑挡住"
    assert chosen.TYPE == "Extinguish"
    assert chosen.resources == ()


# ---------- _time_pressure（spec §6.4）----------

def test_time_pressure_max_when_cooked():
    """锅熟了 → 取它的时间压力拉满。"""
    w = _world(cookers=[_cooker(cooked=True, is_cooking=True)])
    task = T.TakeCookedToPlate("order1", ("cooker_01", "plate_01"),
                              "BoiledSushiRice")
    assert planner._time_pressure(task, w) == 1.0


def test_time_pressure_zero_when_not_cooked():
    w = _world(cookers=[_cooker(cooked=False, is_cooking=True)])
    task = T.TakeCookedToPlate("order1", ("cooker_01", "plate_01"),
                              "BoiledSushiRice")
    assert planner._time_pressure(task, w) == 0.0


def test_time_pressure_ignores_unrelated_tasks():
    w = _world(cookers=[_cooker(cooked=True, is_cooking=True)])
    task = T.FetchToBoard("order1", ("board_01",), "Cucumber")
    assert planner._time_pressure(task, w) == 0.0


def test_time_pressure_ramps_up_toward_burning():
    """有连续进度时，压力随「快糊了」上升，帮 TakeCookedToPlate 跨过承诺惯性。

    游戏语义：cooked = progress>=time，burning = progress>2*time。
    ratio = progress/(2*time)：0.4 刚熟(策略)、1.0 快糊；压力 1.0 → 2.0。
    """
    w = _world(cookers=[{"n": "cooker_01", "x": 1.0, "z": 1.0,
                         "cooking_progress": 8.0, "cooking_time": 10.0}])
    task = T.TakeCookedToPlate("order1", ("cooker_01", "plate_01"),
                               "BoiledSushiRice")

    assert planner._time_pressure(task, w) == 1.0        # 刚熟 ratio 0.4

    w.cookers[0]["cooking_progress"] = 14.0
    assert planner._time_pressure(task, w) == 1.5        # ratio 0.7

    w.cookers[0]["cooking_progress"] = 20.0
    assert planner._time_pressure(task, w) == 2.0        # 快糊 ratio 1.0

    w.cookers[0]["cooking_progress"] = 2.0
    assert planner._time_pressure(task, w) == 0.0        # 还没到快熟段 ratio 0.1


def test_time_pressure_ramps_up_before_cooked():
    """煮到约三成（ratio 0.15）压力就开始爬：提前端盘去等熟（0.15→0.4 对应 0→1.0）。"""
    w = _world(cookers=[{"n": "cooker_01", "x": 1.0, "z": 1.0,
                         "cooking_progress": 3.6, "cooking_time": 12.0}])
    task = T.TakeCookedToPlate("order1", ("cooker_01", "plate_01"),
                               "BoiledSushiRice")

    assert planner._time_pressure(task, w) == 0.0        # ratio 0.15 起点

    w.cookers[0]["cooking_progress"] = 6.6               # ratio 0.275 → 0.5
    assert planner._time_pressure(task, w) == pytest.approx(0.5)

    w.cookers[0]["cooking_progress"] = 9.6               # ratio 0.4 刚熟(策略) → 1.0
    assert planner._time_pressure(task, w) == pytest.approx(1.0)


def test_cooked_rice_outranks_fetch_when_scored():
    """米饭熟了：TakeCookedToPlate 应盖过 FetchToBoard（症状二回归）。"""
    w = _world(
        cookers=[_cooker(cooked=True, is_cooking=True)],
        stations=[
            {"t": "crate", "n": "crate_fish", "ing": "SushiFish",
             "chop": "ChoppedSushiFish", "chop_stages": 8, "gx": 1, "gz": 1},
            {"t": "chopping", "n": "board_01", "gx": 2, "gz": 2, "item": None},
        ],
        orders=[{"id": "order1",
                 "composition": ["BoiledSushiRice", "SushiFish"],
                 "remaining": 60}],
        recipes=[{"name": "BoiledSushiRice", "comp": ["SushiRice"], "cook": "Pot"},
                 {"name": "SushiFish", "comp": []}],
    )
    bb = Blackboard()
    take = T.TakeCookedToPlate("order1", ("cooker_01", "plate_01"),
                              "BoiledSushiRice")
    fetch = T.FetchToBoard("order1", ("board_01",), "SushiFish")
    scored = planner._score_tasks(
        [take, fetch],
        [planner.Candidate(take, [], 15), planner.Candidate(fetch, [], 10)],
        w, bb)
    by_type = {t.TYPE: s for t, s, _ in scored}
    assert by_type["TakeCookedToPlate"] > by_type["FetchToBoard"], by_type


# ---------- "该单需熟料"（spec §6.1 第 3 号）----------

def _rice_world():
    return _world(
        cookers=[_cooker(name="cooker_02", cooked=False, is_cooking=False),
                 _cooker(name="cooker_03", cooked=False, is_cooking=False, x=9.0)],
        stations=[{"t": "crate", "n": "crate_rice", "ing": "SushiRice",
                   "gx": 1, "gz": 1}],
        orders=[{"id": "order1", "composition": ["BoiledSushiRice"],
                 "remaining": 60}],
        recipes=[{"name": "BoiledSushiRice", "comp": ["SushiRice"],
                  "cook": "Pot"}],
    )


def _emit_types(w, bb, book, order, plate_match=None, cooker_match=None):
    # type: (...) -> list
    return [t.TYPE for t in recipe_graph.emit(
        w, bb, book, order, plate_match or {}, cooker_match or {})]


def test_gen_cook_generates_when_nothing_in_flight():
    w = _rice_world()
    book = RecipeBook(w.recipes, w.stations)
    assert "FetchToCooker" in _emit_types(w, Blackboard(), book, w.orders[0])


def test_gen_cook_suppressed_when_order_already_producing():
    """该单已有一口灶在产这份熟料 → 不再为它生成 FetchToCooker（症状一回归）。"""
    w = _rice_world()
    book = RecipeBook(w.recipes, w.stations)
    types = _emit_types(w, Blackboard(), book, w.orders[0], {},
                        {"cooker_01": "order1"})
    assert "FetchToCooker" not in types


def test_gen_cook_suppressed_when_in_flight_claim():
    """该单已有在途认领（fetch_to）→ 不再重复生成。"""
    w = _rice_world()
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    bb.claim("cooker_09", "fetch_to", "P2", "order1", 0)
    assert "FetchToCooker" not in _emit_types(w, bb, book, w.orders[0])


# ---------- 取熟料侧也要吃"该单需熟料" ----------

def _take_world(plate_contents):
    w = _world(
        cookers=[_cooker(cooked=True, is_cooking=True)],
        stations=[{"t": "counter", "n": "counter_01", "gx": 5, "gz": 5}],
        orders=[{"id": "order1", "composition": ["BoiledSushiRice"],
                 "remaining": 60}],
        recipes=[{"name": "BoiledSushiRice", "comp": ["SushiRice"],
                  "cook": "Pot"}],
    )
    x, z = w.to_world(5, 5)
    w.plates = [{"n": "plate_01", "x": x, "z": z,
                 "count": len(plate_contents), "contents": list(plate_contents)}]
    return w


def _take_tasks(w, plate_match, cooker_match):
    book = RecipeBook(w.recipes, w.stations)
    return recipe_graph.emit(w, Blackboard(), book, w.orders[0],
                             plate_match, cooker_match)


def test_gen_take_cooked_generates_when_plate_lacks_it():
    w = _take_world([])
    tasks = _take_tasks(w, {"plate_01": "order1"}, {"cooker_01": "order1"})
    assert any(t.TYPE == "TakeCookedToPlate" for t in tasks), \
        "盘缺料时应能生成 TakeCookedToPlate"


def test_gen_take_cooked_generates_while_cooking():
    """煮中也生成 TakeCookedToPlate：提前端盘去等熟（不能等熟了才拿盘）。"""
    w = _take_world([])
    w.cookers = [{"n": "cooker_01", "x": 1.0, "z": 1.0,
                  "cooked": False, "isCooking": True,
                  "cooking_progress": 8.0, "cooking_time": 12.0,
                  "pot_contents": ["ing:SushiRice"]}]
    tasks = _take_tasks(w, {"plate_01": "order1"}, {"cooker_01": "order1"})
    assert any(t.TYPE == "TakeCookedToPlate" for t in tasks), \
        "煮中也应能生成 TakeCookedToPlate（提前准备）"


def test_gen_take_cooked_suppressed_when_order_plate_has_it():
    """该单的盘已经有这份熟料 → 不再为它生成取熟料（实机重复倒盘回归）。"""
    w = _take_world(["CookedCompositeAssembledNode"])
    tasks = _take_tasks(w, {"plate_01": "order1"}, {"cooker_01": "order1"})
    assert not any(t.TYPE == "TakeCookedToPlate" for t in tasks)


def test_gen_take_cooked_skips_cooker_assigned_to_other_order():
    """灶在匹配里归了别的单 → 不为该单生成取熟料（每拍 1:1 分配）。"""
    w = _take_world([])
    tasks = _take_tasks(w, {"plate_01": "order1"}, {"cooker_01": "order2"})
    assert not any(t.TYPE == "TakeCookedToPlate" for t in tasks)


# ---------- 取熟料可行性分两段：启动看灶、执行看手 ----------

def _ctx(w, held):
    chef = {"name": "Player 2", "x": 1.0, "z": 1.0, "held": held}
    return {"w": w, "chef": chef, "book": RecipeBook(w.recipes, w.stations),
            "bb": Blackboard(), "tick": 0}


def _take_task():
    return T.TakeCookedToPlate("order1", ("cooker_01", "plate_01"),
                              "BoiledSushiRice")


def test_take_cooked_feasible_when_cooker_cooked_empty_hand():
    w = _take_world([])
    assert _take_task().feasible(_ctx(w, None)) is True


def _ctx_with_pot(w, has_contents, cooked):
    """手持锅：World 已按 held_by 把手持物拆到 held_items（内容 has_contents/cooked）。"""
    ctx = _ctx(w, "utensil_pot_01")
    w.held_items = [{"n": "utensil_pot_01", "x": 1.0, "z": 1.0,
                     "has_contents": has_contents, "cooked": cooked}]
    return ctx


def test_take_cooked_not_feasible_while_holding_cooked_pot_with_empty_cooker():
    """端盘盛饭：手持熟锅不算"执行中"，灶空时没源可盛 → 不可行。

    旧实现端锅离灶后靠"手持熟锅"续命；现在全程端盘、不端锅，所以手持锅
    反而是"手不对"（需先放下、换盘），不该判可行。
    """
    w = _take_world([])
    w.cookers = [{"n": "cooker_01", "x": 1.0, "z": 1.0, "cooked": None}]
    task = _take_task()
    assert task.feasible(_ctx_with_pot(w, True, "Cooked")) is False
    assert task.feasible(_ctx(w, None)) is False      # 空手 + 灶空 → 不可行


def test_take_cooked_infeasible_while_holding_empty_pot():
    """手持空锅/生料锅（非目标盘）且灶空 → 不可行。

    端盘盛饭只看"手持目标盘"为执行中；手持锅不是目标盘、又没源可盛，
    交给 Planner 换别的 task（放下锅 / 去端盘）。
    """
    w = _take_world([])
    w.cookers = [{"n": "cooker_01", "x": 1.0, "z": 1.0, "cooked": None}]
    task = _take_task()
    assert task.feasible(_ctx_with_pot(w, False, "Raw")) is False   # 空锅
    assert task.feasible(_ctx_with_pot(w, True, None)) is False     # 生料锅


def test_take_cooked_pickup_expect_holds_target_plate():
    """树里 pickup 步的 expect 与 feasible 同口径：手持目标盘才算"拿起盘"。"""
    w = _take_world([])
    w.plates[0]["name"] = "equipment_plate_01"   # holding_plate 按盘名匹配手
    tree = _take_task().build()                 # Timeout(Sequence([...]))
    pickup = tree.child.children[1]             # 0=EnsureHandsFree, 1=pickup
    assert pickup.expect(_ctx(w, "equipment_plate_01")) is True
    assert pickup.expect(_ctx(w, "utensil_pot_01")) is False
    assert pickup.expect(_ctx(w, None)) is False


def test_take_cooked_not_feasible_when_plate_already_has_it():
    w = _take_world(["CookedCompositeAssembledNode"])
    assert _take_task().feasible(_ctx(w, "utensil_pot_01")) is False


# ---------- 盘→单 持久归属 ----------

def _two_plate_world(p1_contents, p2_contents):
    w = _world(
        cookers=[_cooker(cooked=True, is_cooking=True)],
        stations=[{"t": "counter", "n": "c1", "gx": 5, "gz": 5},
                  {"t": "counter", "n": "c2", "gx": 6, "gz": 5}],
        orders=[{"id": "order1", "composition": ["BoiledSushiRice"],
                 "remaining": 60},
                {"id": "order2", "composition": ["BoiledSushiRice"],
                 "remaining": 60}],
        recipes=[{"name": "BoiledSushiRice", "comp": ["SushiRice"],
                  "cook": "Pot"}],
    )
    x1, z1 = w.to_world(5, 5)
    x2, z2 = w.to_world(6, 5)
    w.plates = [
        {"n": "plate_01", "x": x1, "z": z1,
         "count": len(p1_contents), "contents": list(p1_contents)},
        {"n": "plate_02", "x": x2, "z": z2,
         "count": len(p2_contents), "contents": list(p2_contents)},
    ]
    return w


def test_take_cooked_reads_plate_match():
    """"该单的盘已有该料"按每拍 1:1 匹配判：匹配到的盘有饭 → 不发；盘空 → 发。"""
    book = RecipeBook(_take_world([]).recipes, _take_world([]).stations)
    # 盘有饭 + 匹配到 order1 → 不发
    w = _take_world(["CookedCompositeAssembledNode"])
    tasks = _take_tasks(w, {"plate_01": "order1"}, {"cooker_01": "order1"})
    assert not any(t.TYPE == "TakeCookedToPlate" for t in tasks)
    # 盘空 + 匹配到 order1 → 发
    w2 = _take_world([])
    tasks = _take_tasks(w2, {"plate_01": "order1"}, {"cooker_01": "order1"})
    assert any(t.TYPE == "TakeCookedToPlate" for t in tasks)


def test_gen_take_cooked_order_with_its_own_plate_has_rice_but_others_still_can():
    """order1 匹配到的盘已有饭 → order1 不再取；order2 盘空 → 仍能取（实机回归）。"""
    w = _two_plate_world(["CookedCompositeAssembledNode"], [])
    book = RecipeBook(w.recipes, w.stations)
    plate_match = {"plate_01": "order1", "plate_02": "order2"}
    cooker_match = {"cooker_01": "order2"}  # order1 盘已有饭，锅配给缺饭的 order2

    t1 = recipe_graph.emit(w, Blackboard(), book, w.orders[0],
                           plate_match, cooker_match)
    assert not any(t.TYPE == "TakeCookedToPlate" for t in t1)
    t2 = recipe_graph.emit(w, Blackboard(), book, w.orders[1],
                           plate_match, cooker_match)
    assert any(t.TYPE == "TakeCookedToPlate" and t.plate_id == "plate_02"
               for t in t2)


# ---------- RescueCooked：没人要的熟料也要端走 ----------

def test_rescue_cooked_feasible_when_no_placed_plate_needs_it():
    """没有已就位的盘缺这份料 → 端锅离灶（实机：多煮的锅没人取）。"""
    w = _take_world(["CookedCompositeAssembledNode"])
    task = T.RescueCooked(resources=("cooker_01",))
    assert task.feasible(_ctx(w, None)) is True


def test_rescue_cooked_feasible_when_placed_plate_lacks_it():
    """兜底不排除「有盘要它」：已就位空盘缺饭，RescueCooked 依然可行。"""
    w = _take_world([])                       # 已就位盘是空的，缺饭
    task = T.RescueCooked(resources=("cooker_01",))
    assert task.feasible(_ctx(w, None)) is True


def test_rescue_cooked_feasible_while_holding_cooked_pot_even_if_cooker_empty():
    """端锅离灶后该灶 cooked=None，但**带料的锅**在手上 → 仍可行。

    （这是 `RescueCooked ↔ ReturnPot` 死循环的回归：可行性不能在它自己
    动手那一刻翻脸。）
    """
    w = _take_world(["CookedCompositeAssembledNode"])    # 没有盘缺这份料
    task = T.RescueCooked(resources=("cooker_01",))
    assert task.feasible(_ctx(w, None)) is True           # 启动：灶 cooked
    w.cookers = [{"n": "cooker_01", "x": 1.0, "z": 1.0, "cooked": None}]
    assert task.feasible(_ctx_with_pot(w, True, "Cooked")) is True   # 端在手上
    assert task.feasible(_ctx(w, None)) is False          # 空手 + 灶空


def test_rescue_cooked_infeasible_while_holding_empty_pot():
    """手持**空锅**不算"锅已在我手上"。

    只看"手持锅具"会坏：树首 pickup 的 expect 同款短路——根本没去端熟锅，
    本 task 却一路"成功"到底，灶上的熟料继续烧糊。
    """
    w = _take_world(["CookedCompositeAssembledNode"])
    task = T.RescueCooked(resources=("cooker_01",))
    w.cookers = [{"n": "cooker_01", "x": 1.0, "z": 1.0, "cooked": None}]
    assert task.feasible(_ctx_with_pot(w, False, "Raw")) is False

    # 手持空锅 + 灶上无锅，依然不可行（与有没有盘无关）
    w2 = _take_world([])
    w2.cookers = [{"n": "cooker_01", "x": 1.0, "z": 1.0, "cooked": None}]
    assert task.feasible(_ctx_with_pot(w2, False, "Raw")) is False


def test_rescue_cooked_pickup_expect_needs_cooked_contents():
    """树里 pickup 的 expect 与 feasible 同口径：锅里有熟料才算端到手。"""
    w = _take_world(["CookedCompositeAssembledNode"])
    tree = T.RescueCooked(resources=("cooker_01",)).build()
    pickup = tree.child.children[0]               # 0=pickup, 1=EnsureHandsFree
    assert pickup.expect(_ctx_with_pot(w, True, "Cooked")) is True
    assert pickup.expect(_ctx_with_pot(w, False, "Raw")) is False
    assert pickup.expect(_ctx(w, None)) is False


# ---------- B1：取熟料的树首先清手 ----------

def test_take_cooked_tree_head_is_ensure_hands_free():
    """端盘盛饭前先清手（spec §6.1(b)/§7.4）——手里有东西时不再被短路骗过。"""
    from ai.primitives.interact import EnsureHandsFree
    seq = _take_task().build().child          # Timeout -> Sequence
    assert isinstance(seq.children[0], EnsureHandsFree)


# ---------- B2：手部前置的腾手代价 ----------

def test_chop_declares_empty_hands_and_clears_first():
    """切菜要空手：树首 EnsureHandsFree + 声明 HANDS=empty（实机卡砧板回归）。"""
    from ai.primitives.interact import EnsureHandsFree
    assert T.Chop.HANDS == "empty"
    seq = T.Chop("order1", ("board_01",), "Cucumber").build().child
    assert isinstance(seq.children[0], EnsureHandsFree)


def test_take_cooked_score_penalized_when_holding_cookware():
    w = _take_world([])
    task = _take_task()
    cand = planner.Candidate(task, [], 15)
    bb = Blackboard()

    w.chefs[0]["held"] = None
    free_score, free_terms = planner._score_candidate(w, bb, cand)

    w.chefs[0]["held"] = "utensil_pot_01"
    busy_score, busy_terms = planner._score_candidate(w, bb, cand)

    assert free_terms["hands"] == 0.0
    assert busy_terms["hands"] < 0
    assert busy_score < free_score


# ---------- B3：loose_cookware 只认地上的锅 ----------

def test_loose_cookware_excludes_only_cooker_pots():
    """灶台上的锅（在用）不算散落；台面 / 地上的锅算（该捡回灶台）。"""
    from ai.tasks.cleanup import loose_cookware
    w = _world(stations=[{"t": "cooking", "n": "cooker_01", "gx": 5, "gz": 5},
                         {"t": "counter", "n": "c1", "gx": 6, "gz": 5}])
    xc, zc = w.to_world(5, 5)          # 灶台格
    xt, zt = w.to_world(6, 5)          # 台面格
    xf, zf = w.to_world(9, 9)          # 地上
    w.items = [
        {"n": "utensil_pot_01", "x": xc, "z": zc,
         "on_station": "cooking:cooker_01"},                    # 灶台上
        {"n": "utensil_pot_02", "x": xt, "z": zt,
         "on_station": "attach:c1"},                            # 台面上
        {"n": "utensil_pot_03", "x": xf, "z": zf,
         "on_station": None},                                   # 地上
    ]
    ctx = {"w": w, "book": RecipeBook(w.recipes, w.stations)}
    names = [it["n"] for it in loose_cookware(ctx)]
    assert "utensil_pot_01" not in names, "灶台上的锅在用，不算散落"
    assert "utensil_pot_02" in names, "台面上的锅算散落"
    assert "utensil_pot_03" in names, "地上的锅算散落"


def test_loose_cookware_excludes_cooked_pot_on_counter():
    """柜台上的熟锅是「留料待用」，不是该捡回灶台的散锅。

    否则 RescueCooked 刚把熟锅救到台面，下一拍就被 RecoverLoosePot 端回
    灶台重新点火烧糊。判据与 cooked_sources 同源：items[].detail 含
    cooked:* token 即熟锅。
    """
    from ai.tasks.cleanup import loose_cookware
    w = _world(stations=[{"t": "cooking", "n": "cooker_01", "gx": 5, "gz": 5},
                         {"t": "counter", "n": "c1", "gx": 6, "gz": 5}])
    xc, zc = w.to_world(5, 5)          # 灶台格
    xt, zt = w.to_world(6, 5)          # 台面格
    w.items = [
        {"n": "utensil_pot_01", "x": xc, "z": zc,
         "on_station": "cooking:cooker_01"},                    # 灶上：在用
        {"n": "utensil_pot_02", "x": xt, "z": zt,              # 台面熟锅：留待装盘
         "detail": ["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"],
         "on_station": "attach:c1"},
        {"n": "utensil_pot_03", "x": xt, "z": zt,
         "on_station": "attach:c1"},                            # 台面空锅：应回收
    ]
    ctx = {"w": w, "book": RecipeBook(w.recipes, w.stations)}
    names = [it["n"] for it in loose_cookware(ctx)]
    assert "utensil_pot_01" not in names, "灶台上的锅在用，不算散落"
    assert "utensil_pot_02" not in names, "熟锅留台面等盘，不算散锅"
    assert "utensil_pot_03" in names, "空锅仍算散锅，该回收"


def test_loose_pots_excludes_cooked_pot_without_cooked_token():
    """熟锅 detail 只报原始料名（无 cooked: token）时，靠 cooked 字段兜底排除。

    实机复现：散熟锅 detail=['ing:SushiRice']、cooked="Cooked"。只看 detail 会把
    它当散空锅生成 RecoverLoosePot，与 TakeCookedToPlate 抢同一口锅 → 每拍横跳。
    """
    w = _world()
    w.items = [
        {"n": "utensil_pot_01", "x": 1.0, "z": 1.0,
         "has_contents": True, "cooked": "Cooked",
         "detail": ["ing:SushiRice"]},
    ]
    assert planner._loose_pots(w) == []

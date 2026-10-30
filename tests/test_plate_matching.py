# -*- coding: utf-8 -*-
"""盘/锅内容 token 解析 + 每拍 1:1 匹配的新功能回归。

插件新输出（Oc2AIPlugin.cs 的 DescribeNode）带来 detail / pot_contents 两个
字段，这里钉住它们的解析与下游匹配行为。
"""
from ai.state.blackboard import Blackboard
from ai.core import planner
from ai.query import recipe_graph
from ai import tasks as T
from ai.query.plate_tokens import parse_pot_contents, parse_plate_detail
from ai.query.recipes import RecipeBook
from ai.tasks.base import cooked_sources
from ai.state.world import World


# ---------- 解析 ----------

def test_parse_pot_contents_strips_prefix():
    assert parse_pot_contents(["ing:SushiRice"]) == ("sushirice",)
    assert parse_pot_contents(["item:Plate", "ing:Fish"]) == ("plate", "fish")
    assert parse_pot_contents([]) == ()
    assert parse_pot_contents(None) == ()


def test_parse_pot_contents_expands_cooked_composite():
    assert parse_pot_contents(
        ["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"]
    ) == ("sushirice",)
    assert parse_pot_contents(
        ["CookedCompositeAssembledNode(Pot){ ing:Pasta, ing:Tomato }"]
    ) == ("pasta", "tomato")


def test_parse_plate_detail_expands_cooked():
    got = parse_plate_detail([
        "ing:SushiFish",
        "CookedCompositeAssembledNode(Pot){ ing:SushiRice }",
        "ing:Seaweed",
    ])
    assert got == {"sushifish", "cooked:sushirice:pot", "seaweed"}


def test_parse_plate_detail_empty():
    assert parse_plate_detail([]) == set()
    assert parse_plate_detail(None) == set()


# ---------- 配方侧对齐 ----------

def _book():
    return RecipeBook(
        recipes=[{"name": "BoiledSushiRice", "comp": ["SushiRice"], "cook": "Pot"},
                 {"name": "SushiFish", "comp": []},
                 {"name": "Seaweed", "comp": []}],
        stations=[])


def test_cooked_of_raw_reverse_lookup():
    assert _book().cooked_of_raw("SushiRice") == ["BoiledSushiRice"]
    assert _book().cooked_of_raw("Nonexistent") == []
    assert _book().cooked_of_raw(None) == []


def test_order_want_tokens_and_plate_tokens_share_vocabulary():
    book = _book()
    order = {"id": "o1", "composition": ["Seaweed", "BoiledSushiRice", "SushiFish"]}
    want = book.order_want_tokens(order)
    # 熟料同时给 rich + opaque 两个 token（兼容新/旧帧）
    assert "cooked:sushirice:pot" in want
    assert "cookedcompositeassemblednode" in want
    assert "seaweed" in want and "sushifish" in want

    # 新帧 detail
    plate = {"detail": ["CookedCompositeAssembledNode(Pot){ ing:SushiRice }",
                        "SushiFish", "Seaweed"]}
    toks = book.plate_tokens(plate)
    assert toks == {"cooked:sushirice:pot", "sushifish", "seaweed"}
    assert toks <= want, "新帧 detail 应能匹配订单需求"

    # 旧帧 contents（无 detail）退回不透明 token
    plate_old = {"contents": ["CookedCompositeAssembledNode",
                              "SushiFish", "Seaweed"]}
    toks_old = book.plate_tokens(plate_old)
    assert "cookedcompositeassemblednode" in toks_old
    assert toks_old <= want, "旧帧 opaque token 也应能匹配（单熟料关卡）"


def test_plate_has_distinguishes_cooked_ingredients_via_detail():
    """多熟料一盘：detail 帧里 plate_has/missing 能区分不同熟料。

    旧帧 contents 里所有熟料都是同一个不透明 token，区分不了；新帧 detail
    带 rich token（cooked:{raw}:{cookware}），同一盘里放了两份不同熟料也
    能各自判定。
    """
    book = RecipeBook(
        recipes=[
            {"name": "BoiledPasta", "comp": ["Pasta"], "cook": "Pot"},
            {"name": "PanFriedPrawns", "comp": ["Prawn"], "cook": "FryingPan"},
            {"name": "PanFriedFish", "comp": ["Fish"], "cook": "FryingPan"},
        ],
        stations=[])
    order = {"id": "o1",
             "composition": ["BoiledPasta", "PanFriedPrawns", "PanFriedFish"]}
    plate = {"detail": [
        "CookedCompositeAssembledNode(Pot){ ing:Pasta }",
        "CookedCompositeAssembledNode(FryingPan){ ing:Prawn }",
    ]}
    assert book.plate_has(plate, "BoiledPasta") is True
    assert book.plate_has(plate, "PanFriedPrawns") is True
    assert book.plate_has(plate, "PanFriedFish") is False
    assert book.missing(order, plate) == ("PanFriedFish",)


# ---------- 灶↔订单匹配 ----------

def _cooker_world():
    return World({
        "orders": [{"id": "order1", "composition": ["BoiledSushiRice"],
                    "remaining": 30},
                   {"id": "order2", "composition": ["BoiledSushiRice"],
                    "remaining": 60}],
        "stations": [],
        "recipes": [{"name": "BoiledSushiRice", "comp": ["SushiRice"], "cook": "Pot"}],
        "chefs": [{"name": "Player 2", "x": 1.0, "z": 1.0, "held": None}],
        "plates": [{"n": "plate_01", "x": 1.0, "z": 1.0, "count": 0, "contents": []},
                   {"n": "plate_02", "x": 2.0, "z": 1.0, "count": 0, "contents": []}],
        "workstations": [], "cookers": [],
        "items": [], "extinguishers": [], "grid": {"nodeMap": None},
    })


def test_match_cookers_assigns_to_most_urgent():
    w = _cooker_world()
    w.cookers = [{"n": "cooker_01", "x": 3.0, "z": 1.0,
                  "isCooking": True, "cooked": False,
                  "pot_contents": ["ing:SushiRice"]}]
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    plate_match = planner._match_plates_to_orders(w, bb, book)
    got = planner._match_cookers_to_orders(w, bb, book, plate_match)
    # 只有一口锅 → 给更紧的 order1。灶上源的 id 现在是 (grid,name) 元组
    # （§5.2），本场景的灶没有 grid 字段，归一化为空字符串。
    assert got == {("", "cooker_01"): "order1"}, got


def test_match_cookers_ignores_empty_pots():
    w = _cooker_world()
    w.cookers = [{"n": "cooker_01", "x": 3.0, "z": 1.0,
                  "isCooking": True, "cooked": False,
                  "pot_contents": []}]
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    plate_match = planner._match_plates_to_orders(w, bb, book)
    assert planner._match_cookers_to_orders(w, bb, book, plate_match) == {}


def test_match_cookers_prioritizes_cooked_to_plated_order():
    """熟且未糊的灶应优先配给「有盘就位」的单（实机：熟饭配给没盘单干烧糊）。

    否则贪心按灶名/剩余时间分，会把熟灶分给没盘的单、生灶分给有盘的单——
    熟饭既生成不了 TakeCookedToPlate（单没盘），RescueCooked 又判「有盘要它
    别救」，两头堵死。
    """
    w = _cooker_world()
    w.cookers = [
        {"n": "cooker_cooked", "x": 3.0, "z": 1.0,
         "isCooking": False, "cooked": True, "burning": False,
         "pot_contents": ["ing:SushiRice"]},
        {"n": "cooker_cooking", "x": 4.0, "z": 1.0,
         "isCooking": True, "cooked": False, "burning": False,
         "pot_contents": ["ing:SushiRice"]},
    ]
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    # 只有 order2 有盘就位（plate_01），order1 没盘
    plate_match = {"plate_01": "order2"}
    got = planner._match_cookers_to_orders(w, bb, book, plate_match)
    # 灶上源的 id 是 (grid,name) 元组（§5.2），灶没有 grid 字段时归一化为 ""
    assert got.get(("", "cooker_cooked")) == "order2", got
    assert got.get(("", "cooker_cooking")) == "order1", got


def test_match_cookers_prioritizes_burning_cooker_over_loose_pot():
    """快糊的灶上锅要比散熟锅更优先配给有盘的单。

    实机：散熟锅（danger 0）抢走有盘的单，灶上锅（danger 高）配给没盘的单，
    TakeCookedToPlate 发不出来，灶上锅活活烧糊。
    """
    w = _cooker_world()
    w.cookers = [
        {"n": "cooker_burning", "x": 3.0, "z": 1.0,
         "isCooking": True, "cooked": True, "burning": False,
         "cooking_progress": 22.0, "cooking_time": 12.0,   # 22/24≈0.92 快糊
         "pot_contents": ["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"]},
    ]
    w.items = [{"n": "utensil_pot_01", "x": 2.0, "z": 1.0,
                "has_contents": True, "cooked": "Cooked",
                "detail": ["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"]}]
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    # 只有 order1 有盘（plate_01）
    plate_match = {"plate_01": "order1"}
    got = planner._match_cookers_to_orders(w, bb, book, plate_match)
    # 灶上源 id 是 (grid,name) 元组（§5.2）；散锅（loose）id 仍是裸 item 名。
    assert got.get(("", "cooker_burning")) == "order1", got
    assert got.get("utensil_pot_01") == "order2", got


def _loose_cooked_world():
    """一帧：拼装台上有口熟饭散锅 + 一张已就位的空盘。"""
    return World({
        "orders": [{"id": "order1", "composition": ["BoiledSushiRice"],
                    "remaining": 30}],
        # 盘在 counter 拼装格上（实机帧里盘必须就位才能倒料）
        "stations": [{"t": "counter", "n": "assembly", "gx": 1, "gz": 1,
                      "x": 1.2, "z": 1.2}],
        "recipes": [{"name": "BoiledSushiRice", "comp": ["SushiRice"],
                     "cook": "Pot"}],
        "chefs": [{"name": "Player 2", "x": 1.0, "z": 1.0, "held": None}],
        "plates": [{"n": "plate_01", "x": 1.0, "z": 1.0, "count": 0,
                    "contents": []}],
        "workstations": [], "cookers": [],
        "items": [{"n": "utensil_pot_01", "x": 2.0, "z": 1.0,
                   "has_contents": True, "cooked": "Cooked",
                   "detail": ["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"]}],
        "extinguishers": [], "grid": {"nodeMap": None},
    })


def test_cooked_sources_finds_loose_cooked_pot():
    """散熟锅进熟料源：items[].detail 的 cooked:* token 被精确识别。"""
    w = _loose_cooked_world()
    book = RecipeBook(w.recipes, w.stations)
    srcs = cooked_sources(w, book)
    assert any(s["kind"] == "loose" and s["id"] == "utensil_pot_01"
               and "cooked:sushirice:pot" in s["tokens"] for s in srcs), srcs


def test_match_cookers_includes_loose_cooked_pot():
    """散熟锅参与熟料源↔订单匹配。"""
    w = _loose_cooked_world()
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    plate_match = planner._match_plates_to_orders(w, bb, book)
    got = planner._match_cookers_to_orders(w, bb, book, plate_match)
    assert got == {"utensil_pot_01": "order1"}, got


def test_match_plates_ignores_drying_rack_and_held_plates():
    """旧帧（无 stack 字段）沥碗架里的空盘、P1 手里端着的空盘不参与盘↔订单匹配。

    旧帧分不出盘堆顶层，直接匹配会误绑中间层；空盘该走 PlaceCleanPlate。
    新帧的堆顶盘见 test_match_plates_binds_clean_stack_top。
    地上的**带料**盘现在会参与匹配，见 test_match_plates_includes_ground_plate_with_contents。
    """
    w = _loose_cooked_world()
    # 沥碗架上的盘（plate_return 站，不是 counter）
    w.stations.append({"t": "plate_return", "n": "DryingPart",
                       "gx": 3, "gz": 1, "x": 3.6, "z": 1.2})
    w.plates.append({"n": "plate_dry", "x": 3.6, "z": 1.2,
                     "count": 0, "contents": []})
    # P1 手里端着的盘（位置跟着 P1，在非 counter 的地板格）
    w.chefs.append({"name": "Player 1", "x": 4.0, "z": 4.0,
                    "held": "equipment_plate_01"})
    w.plates.append({"n": "plate_held", "x": 4.0, "z": 4.0,
                     "count": 0, "contents": []})
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    got = planner._match_plates_to_orders(w, bb, book)
    assert got == {"plate_01": "order1"}, got


def test_match_plates_includes_p2_held_plate():
    """P2 手里正端着的盘仍参与盘↔订单匹配（端盘盛饭/送餐中盘离台面，绑定不能丢）。

    实机：端盘盛饭第一步把盘从台面端起，若下一拍匹配不再算它，订单被重绑到
    另一块空盘 → task 身份变化 → 树重建 → 手里这块被放下（端起又放下）。
    """
    w = _loose_cooked_world()
    # 盘离开 counter 格（counter 在 1,1），名字 = P2 手持盘名
    w.plates[0]["name"] = "equipment_plate_01"
    w.plates[0]["x"] = 4.0
    w.plates[0]["z"] = 4.0
    w.chefs[0]["held"] = "equipment_plate_01"
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    got = planner._match_plates_to_orders(w, bb, book)
    assert got == {"plate_01": "order1"}, got


def _stack_world(plates, counter_plate=False):
    # 取盘站在 (5,1)，拼装台在 (1,1)；counter_plate=True 时台面上再摆一块空盘
    ps = list(plates)
    if counter_plate:
        ps.append({"n": "plate_counter", "x": 1.0, "z": 1.0,
                   "count": 0, "contents": [], "held_by": None,
                   "stack": None, "stack_clean": False, "stack_top": False})
    return World({
        "orders": [{"id": "order1", "composition": ["BoiledSushiRice"],
                    "remaining": 30},
                   {"id": "order2", "composition": ["BoiledSushiRice"],
                    "remaining": 60}],
        "stations": [{"t": "counter", "n": "assembly", "gx": 1, "gz": 1,
                      "x": 1.2, "z": 1.2},
                     {"t": "plate_return", "n": "DryingPart", "gx": 5, "gz": 1,
                      "x": 5.0, "z": 1.0}],
        "recipes": [{"name": "BoiledSushiRice", "comp": ["SushiRice"],
                     "cook": "Pot"}],
        "chefs": [{"name": "Player 2", "x": 3.0, "z": 3.0, "held": None}],
        "plates": ps,
        "workstations": [], "cookers": [], "items": [], "extinguishers": [],
        "grid": {"nodeMap": None},
    })


def _stacked(pid, top, clean=True, count=0):
    return {"n": pid, "x": 5.0, "z": 1.0, "count": count,
            "contents": ["Seaweed"] * count, "held_by": None,
            "stack": "CleanPlateStack", "stack_clean": clean, "stack_top": top}


def test_match_plates_binds_clean_stack_top():
    """新帧：干净盘堆的顶层盘直接参与匹配，中间层不参与。"""
    w = _stack_world([_stacked("plate_top", True), _stacked("plate_mid", False),
                      _stacked("plate_bot", False)])
    book = RecipeBook(w.recipes, w.stations)
    got = planner._match_plates_to_orders(w, Blackboard(), book)
    assert got == {"plate_top": "order1"}, got


def test_match_plates_ignores_dirty_stack_top():
    w = _stack_world([_stacked("plate_dirty", True, clean=False)])
    book = RecipeBook(w.recipes, w.stations)
    assert planner._match_plates_to_orders(w, Blackboard(), book) == {}


def test_match_plates_prefers_counter_plate_over_empty_stack_top():
    """台面空盘与堆顶空盘同等缺料时，台面盘先配给队首单。"""
    w = _stack_world([_stacked("plate_top", True)], counter_plate=True)
    book = RecipeBook(w.recipes, w.stations)
    got = planner._match_plates_to_orders(w, Blackboard(), book)
    assert got == {"plate_counter": "order1", "plate_top": "order2"}, got


def test_match_plates_ignores_buried_stack_plate_with_contents():
    """堆里中间层的盘即使报了内容也不当散落盘匹配（只有顶层能交互）。"""
    w = _stack_world([_stacked("plate_top", True),
                      _stacked("plate_mid", False, count=1)])
    book = RecipeBook(w.recipes, w.stations)
    got = planner._match_plates_to_orders(w, Blackboard(), book)
    assert "plate_mid" not in got, got


def test_stack_top_order_skips_place_clean_plate_and_takes_cooked():
    """单配到堆顶盘 → 不再发 PlaceCleanPlate，直接发端堆顶盘去盛锅。"""
    w = _stack_world([_stacked("plate_top", True), _stacked("plate_mid", False)])
    w.orders = w.orders[:1]
    w.cookers = [{"n": "cooker_01", "x": 3.0, "z": 1.0,
                  "isCooking": True, "cooked": False,
                  "pot_contents": ["ing:SushiRice"]}]
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    pm = planner._match_plates_to_orders(w, bb, book)
    cm = planner._match_cookers_to_orders(w, bb, book, pm)
    cands = recipe_graph.emit(w, bb, book, w.orders[0], pm, cm)
    types = [c.TYPE for c in cands]
    assert "PlaceCleanPlate" not in types, types
    take = [c for c in cands if c.TYPE == "TakeCookedToPlate"]
    assert take and take[0].plate_id == "plate_top", cands


def test_emit_plate_skips_buried_stack_plates():
    """没盘的单摆盘时，只能绑盘堆顶层，不能绑中间层（pickup 只拿顶层）。"""
    w = _stack_world([_stacked("plate_top", True), _stacked("plate_mid", False)])
    bb = Blackboard()
    cands = recipe_graph._emit_plate(w, bb, "order2")
    pids = {c.plate_id for c in cands}
    assert pids == {"plate_top"}, cands


def test_match_plates_includes_ground_plate_with_contents():
    """掉在地上的带料盘参与匹配（实机：盘被碰掉成孤儿，见 trace-20261018-104804）。

    带料盘（count>0）非手持且不在台面 = 散落在地上；中间层/底层的空盘
    （count==0）永远不满足，所以堆叠里只有顶层已倒料的那只会被匹配。
    """
    w = World({
        "orders": [
            {"id": "order1",
             "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"],
             "remaining": 30},
            {"id": "order2",
             "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"],
             "remaining": 60},
        ],
        "stations": [{"t": "counter", "n": "assembly", "gx": 1, "gz": 1,
                      "x": 1.2, "z": 1.2}],
        "recipes": [{"name": "BoiledSushiRice", "comp": ["SushiRice"],
                     "cook": "Pot"}],
        "chefs": [{"name": "Player 2", "x": 4.0, "z": 4.0, "held": None}],
        "plates": [
            {"n": "plate_ground", "name": "equipment_plate_01",
             "x": 4.0, "z": 4.0, "count": 1, "contents": ["Seaweed"]},
            {"n": "plate_counter", "name": "equipment_plate_01 (1)",
             "x": 1.0, "z": 1.0, "count": 1, "contents": ["Cucumber"]},
        ],
        "workstations": [], "cookers": [], "items": [], "extinguishers": [],
        "grid": {"nodeMap": None},
    })
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    got = planner._match_plates_to_orders(w, bb, book)
    assert got == {"plate_counter": "order1", "plate_ground": "order2"}, got


def test_match_plates_still_ignores_p1_held_plate_with_contents():
    """P1 手里端着的盘（即使带料）仍排除：P2 没法往 P1 手里的盘倒料/端走。"""
    w = World({
        "orders": [
            {"id": "order1",
             "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"],
             "remaining": 30},
        ],
        "stations": [{"t": "counter", "n": "assembly", "gx": 1, "gz": 1,
                      "x": 1.2, "z": 1.2}],
        "recipes": [{"name": "BoiledSushiRice", "comp": ["SushiRice"],
                     "cook": "Pot"}],
        "chefs": [
            {"name": "Player 2", "x": 4.0, "z": 4.0, "held": None},
            {"name": "Player 1", "x": 5.0, "z": 5.0,
             "held": "equipment_plate_01 (2)"},
        ],
        "plates": [
            {"n": "plate_held", "name": "equipment_plate_01 (2)",
             "x": 5.0, "z": 5.0, "count": 1, "contents": ["Seaweed"]},
            {"n": "plate_counter", "name": "equipment_plate_01 (1)",
             "x": 1.0, "z": 1.0, "count": 1, "contents": ["Cucumber"]},
        ],
        "workstations": [], "cookers": [], "items": [], "extinguishers": [],
        "grid": {"nodeMap": None},
    })
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    got = planner._match_plates_to_orders(w, bb, book)
    assert got == {"plate_counter": "order1"}, got


def test_match_plates_pins_committed_plate_to_its_order():
    """P2 承诺绑定的盘钉死在所属订单，不因贪心 tiebreak 被改绑。

    实机 trace-20261017-172221 t=663：P1 往另一块盘放料 → 两块盘 missing 并列
    → 字符串 tiebreak 翻盘 → P2 手里的盘被改绑 → 丢盘、熟饭烧糊。
    """
    w = World({
        "orders": [
            {"id": "order1",
             "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"],
             "remaining": 30},
            {"id": "order2",
             "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"],
             "remaining": 60},
        ],
        "stations": [{"t": "counter", "n": "assembly", "gx": 1, "gz": 1,
                      "x": 1.2, "z": 1.2}],
        "recipes": [{"name": "BoiledSushiRice", "comp": ["SushiRice"],
                     "cook": "Pot"}],
        "chefs": [{"name": "Player 2", "x": 4.0, "z": 4.0,
                   "held": "equipment_plate_01 (3)"}],
        "plates": [
            {"n": "plate_18", "name": "equipment_plate_01 (3)",
             "x": 4.0, "z": 4.0, "count": 1, "contents": ["Cucumber"]},
            {"n": "plate_16", "name": "equipment_plate_01 (2)",
             "x": 1.0, "z": 1.0, "count": 1, "contents": ["Seaweed"]},
        ],
        "workstations": [], "cookers": [],
        "items": [], "extinguishers": [], "grid": {"nodeMap": None},
    })
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    # P2 承诺：端 plate_18 去给 order1 盛熟饭
    bb.commitment.active_task = T.TakeCookedToPlate(
        "order1", ("cooker_x", "plate_18"), "BoiledSushiRice")

    got = planner._match_plates_to_orders(w, bb, book)
    # 无钉时贪心会按字符串 tiebreak 给成 {plate_16: order1, plate_18: order2}
    assert got == {"plate_18": "order1", "plate_16": "order2"}, got


def test_match_plates_prefers_head_order_over_complete_tail():
    """队首优先：生菜盘对队尾已齐、对队首还缺一样，仍配给队首。

    旧口径（完成度感知）会把这块生菜盘配给缺 0 样的队尾单，队首反而没盘 →
    跳单送餐断 combo。新口径第一键是「是否队首」，队首优先拿盘继续补料。
    """
    w = World({
        "orders": [
            {"id": "order1", "composition": ["Tomato", "Lettuce"], "remaining": 60},
            {"id": "order2", "composition": ["Lettuce"], "remaining": 90},
        ],
        "stations": [{"t": "counter", "n": "assembly", "gx": 1, "gz": 1,
                      "x": 1.2, "z": 1.2}],
        "recipes": [],
        "chefs": [{"name": "Player 2", "x": 4.0, "z": 4.0, "held": None}],
        "plates": [
            {"n": "plate_lettuce", "name": "equipment_plate_01",
             "x": 1.0, "z": 1.0, "count": 1, "contents": ["Lettuce"]},
        ],
        "workstations": [], "cookers": [], "items": [], "extinguishers": [],
        "grid": {"nodeMap": None},
    })
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    got = planner._match_plates_to_orders(w, bb, book)
    assert got == {"plate_lettuce": "order1"}, got


def _pasta_world(cookers):
    """一块空盘在拼装格上；队首 order1=MeatOnly，order2=Marinara。"""
    return World({
        "orders": [
            {"id": "order1", "dish": "Pasta_MeatOnly_New",
             "composition": ["BoiledPasta", "FriedBurritoMeat"],
             "remaining": 60},
            {"id": "order2", "dish": "Pasta_Marinara_New",
             "composition": ["BoiledPasta", "PanFriedPrawns", "PanFriedFish"],
             "remaining": 90},
        ],
        "stations": [{"t": "counter", "n": "assembly", "gx": 1, "gz": 1,
                      "x": 1.2, "z": 1.2}],
        "recipes": [
            {"name": "BoiledPasta", "comp": ["Pasta"], "cook": "Pot"},
            {"name": "FriedBurritoMeat", "comp": ["BurritoMeat"],
             "cook": "FryingPan"},
            {"name": "PanFriedPrawns", "comp": ["Prawn"], "cook": "FryingPan"},
            {"name": "PanFriedFish", "comp": ["UncookedFish"],
             "cook": "FryingPan"},
        ],
        "chefs": [{"name": "Player 2", "x": 4.0, "z": 4.0, "held": None}],
        "plates": [{"n": "plate_01", "name": "equipment_plate_01",
                    "x": 1.0, "z": 1.0, "count": 0, "contents": [],
                    "detail": []}],
        "workstations": [], "cookers": cookers, "items": [],
        "extinguishers": [], "grid": {"nodeMap": None},
    })


def _cooked_pan(name, raw, x=3.0):
    # 同坐标的灶会被 dedupe_cookers 合并，多口锅要给不同 x
    return {"n": name, "x": x, "z": 1.0, "cook_type": "FryingPan",
            "isCooking": False, "cooked": True, "burning": False,
            "cooking_progress": 13.0, "cooking_time": 12.0,
            "pot_contents": ["CookedCompositeAssembledNode(FryingPan){ ing:%s }"
                             % raw]}


def test_match_plates_cooked_order_takes_plate_head_cannot_use():
    """队首这拍用不上盘（肉没在煮）、非队首的熟鱼等着装盘 → 盘让给非队首。

    实机 2-2（trace-20261030-224658 t=660 等）：缺料数键让 MeatOnly 永远赢
    共用意面的盘，Marinara 的熟鱼没盘只能 RescueCooked。
    """
    w = _pasta_world([_cooked_pan("pan_fish", "UncookedFish")])
    book = RecipeBook(w.recipes, w.stations)
    got = planner._match_plates_to_orders(w, Blackboard(), book)
    assert got == {"plate_01": "order2"}, got


def test_match_plates_head_keeps_plate_when_it_can_use_it():
    """队首也有熟料等装盘时，照旧队首优先（combo）。"""
    w = _pasta_world([_cooked_pan("pan_fish", "UncookedFish"),
                      _cooked_pan("pan_meat", "BurritoMeat", x=5.0)])
    book = RecipeBook(w.recipes, w.stations)
    got = planner._match_plates_to_orders(w, Blackboard(), book)
    assert got == {"plate_01": "order1"}, got


def test_match_plates_head_keeps_plate_when_nobody_can_use_it():
    """都没有熟料可装 → 退回原序，队首拿盘。"""
    w = _pasta_world([])
    book = RecipeBook(w.recipes, w.stations)
    got = planner._match_plates_to_orders(w, Blackboard(), book)
    assert got == {"plate_01": "order1"}, got


def test_order_needs_cooked_false_with_loose_cooked_pot():
    """散熟锅已匹配给该单 → 不再生成下锅候选（从煮的范围排出）。"""
    w = _loose_cooked_world()
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    plate_match = planner._match_plates_to_orders(w, bb, book)
    cooker_match = planner._match_cookers_to_orders(w, bb, book, plate_match)
    types = [t.TYPE for t in recipe_graph.emit(
        w, bb, book, w.orders[0], plate_match, cooker_match)]
    assert "FetchToCooker" not in types


def test_gen_take_cooked_for_loose_cooked_pot():
    """散熟锅生成 TakeCookedToPlate 候选（纳入取的范围）。"""
    w = _loose_cooked_world()
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    plate_match = planner._match_plates_to_orders(w, bb, book)
    cooker_match = planner._match_cookers_to_orders(w, bb, book, plate_match)
    tasks = recipe_graph.emit(w, bb, book, w.orders[0], plate_match, cooker_match)
    takes = [t for t in tasks if t.TYPE == "TakeCookedToPlate"]
    assert len(takes) == 1, takes
    assert takes[0].cooker_id == "utensil_pot_01"


def test_take_cooked_to_plate_feasible_for_loose_pot():
    """TakeCookedToPlate 对散熟锅源可行（feasible 泛化源解析）。"""
    w = _loose_cooked_world()
    book = RecipeBook(w.recipes, w.stations)
    chef = w.chefs[0]
    task = T.TakeCookedToPlate("order1", ("utensil_pot_01", "plate_01"),
                               "BoiledSushiRice")
    ctx = {"w": w, "chef": chef, "book": book, "bb": Blackboard(), "tick": 0}
    assert task.feasible(ctx) is True


def test_take_cooked_feasible_for_loose_pot_without_cooked_token():
    """散熟锅 detail 只报原始料名（ing:SushiRice）时，靠 cooked 字段兜底仍可行。

    实机：散熟锅 detail=['ing:SushiRice']、cooked="Cooked"。只看 detail 会漏掉
    cooked:* token，_source 返回 None → feasible False → 有熟饭取不动（AI 卡死）。
    """
    w = _loose_cooked_world()
    for it in w.items:
        if it["n"] == "utensil_pot_01":
            it["detail"] = ["ing:SushiRice"]
            it["cooked"] = "Cooked"
    book = RecipeBook(w.recipes, w.stations)
    chef = w.chefs[0]
    task = T.TakeCookedToPlate("order1", ("utensil_pot_01", "plate_01"),
                               "BoiledSushiRice")
    ctx = {"w": w, "chef": chef, "book": book, "bb": Blackboard(), "tick": 0}
    assert task.feasible(ctx) is True


def test_filter_feasible_keeps_cooked_pot_task_despite_abandoned():
    """停火关键任务不受 abandoned 拉黑约束（NO_ABANDON）。

    散熟锅即使被写进 scratch.abandoned，TakeCookedToPlate（盛饭）也仍然
    可行——拉黑期（200 拍）比烧糊倒计时还长，过滤掉它熟锅必糊必着火
    （实机 trace-20261018-153802：盛饭失败把端锅一起拉黑 → 熟锅烧糊起火）。
    """
    from ai.state.blackboard import StuckScratch
    w = _loose_cooked_world()
    book = RecipeBook(w.recipes, w.stations)
    bb = Blackboard()
    bb.scratch = StuckScratch()
    bb.tick = 100
    plate_match = planner._match_plates_to_orders(w, bb, book)
    cooker_match = planner._match_cookers_to_orders(w, bb, book, plate_match)
    tasks = recipe_graph.emit(w, bb, book, w.orders[0], plate_match, cooker_match)
    cands = [planner.Candidate(t, [], 10) for t in tasks]
    assert cands, "前置：应能生成取熟候选"
    assert planner.filter_feasible(w, bb, cands, book), "前置：未拉黑时应可行"
    # 拉黑散熟锅资源 → 盛饭是 NO_ABANDON 停火任务，仍应可行
    bb.scratch.abandoned["utensil_pot_01"] = 300
    assert planner.filter_feasible(w, bb, cands, book), \
        "盛饭是停火关键任务，拉黑不该过滤它"
    # 过期前后行为一致（拉黑对它本就不生效）
    bb.scratch.abandoned["utensil_pot_01"] = 50
    assert planner.filter_feasible(w, bb, cands, book)

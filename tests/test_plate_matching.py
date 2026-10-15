# -*- coding: utf-8 -*-
"""盘/锅内容 token 解析 + 每拍 1:1 匹配的新功能回归。

插件新输出（Oc2AIPlugin.cs 的 DescribeNode）带来 detail / pot_contents 两个
字段，这里钉住它们的解析与下游匹配行为。
"""
from ai.blackboard import Blackboard
from ai import planner, recipe_graph
from ai import tasks as T
from ai.plate_tokens import parse_pot_contents, parse_plate_detail
from ai.recipes import RecipeBook
from ai.tasks.base import cooked_sources
from ai.world import World


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
    # 只有一口锅 → 给更紧的 order1
    assert got == {"cooker_01": "order1"}, got


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
    assert got.get("cooker_cooked") == "order2", got
    assert got.get("cooker_cooking") == "order1", got


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
    assert got.get("cooker_burning") == "order1", got
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
    """只有落在 counter 拼装格上的盘才参与盘↔订单匹配。

    沥碗架（plate_return+DryingPart）里的盘、P1 手里端着的盘不能倒料，
    也不能算作某张单的拼装盘（实机：P2 往沥碗架的盘倒料）。
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


def test_filter_feasible_drops_abandoned_resource():
    """L3 兜底：资源在 abandoned 拉黑期 → 候选被过滤，过期后恢复。"""
    from ai.blackboard import StuckScratch
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
    # 拉黑散熟锅资源 → 过滤后为空
    bb.scratch.abandoned["utensil_pot_01"] = 300
    assert planner.filter_feasible(w, bb, cands, book) == []
    # 过期 → 恢复
    bb.scratch.abandoned["utensil_pot_01"] = 50
    assert planner.filter_feasible(w, bb, cands, book)

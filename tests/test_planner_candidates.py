"""Planner 候选生成单测（Plan 3 Task 3）。

测试 generate_candidates() 的核心逻辑：
  1. 遍历订单配方生成对应任务
  2. 前置条件字符串正确
  3. 估计步数正确
  4. 认领过滤生效
  5. 双手状态过滤生效
"""
import pytest

from ai.state.blackboard import Blackboard, Commitment
from ai.core.planner import Candidate, generate_candidates
from ai.query.recipes import RecipeBook
from ai.state.world import World


def _make_order(dish, composition):
    # type: (str, list) -> dict
    """构造订单 dict。"""
    return {"dish": dish, "composition": composition, "id": "order1"}


def _make_world(orders=None, stations=None, recipes=None, chefs=None,
                plates=None, workstations=None, cookers=None):
    # type: (list, list, list, list, list, list, list) -> World
    """构造最小 World。"""
    state = {
        "orders": orders or [],
        "stations": stations or [],
        "recipes": recipes or [],
        "chefs": chefs or [],
        "plates": plates or [],
        "workstations": workstations or [],
        "cookers": cookers or [],
        "grid": {"nodeMap": None}
    }
    return World(state)


def _make_bb(claims=None, hands_empty=True):
    # type: (dict, bool) -> Blackboard
    """构造黑板。claims 是 {(resource_id, action): owner} 的简化映射。"""
    bb = Blackboard()
    bb.tick = 10
    if claims:
        for (res, act), owner in claims.items():
            bb.claim(res, act, owner, "order1", bb.tick)
    # 模拟双手状态：若 hands_empty=False，则设置 commitment 持有某物
    if not hands_empty:
        bb.commitment = Commitment()
        bb.commitment.active_task = "dummy_task"
    return bb


# ---------- 测试用例 ----------

def test_empty_order_returns_empty():
    """空订单配方返回空候选列表。"""
    w = _make_world(orders=[])
    bb = _make_bb()
    book = RecipeBook([], [])

    candidates = generate_candidates(w, bb, None, book)

    assert candidates == []


def test_simple_order_generates_fetch_chop_serve():
    """简单订单（Tomato，需装盘）生成 取盘 / 取料上板 / 切 / 出餐。

    数据源用真实约定：板是 stations t=="chopping"、盘在 world.plates、
    拼装格是 stations t=="counter"、出餐口 t=="plate_station"。
    """
    orders = [
        _make_order("TomatoSalad", ["Tomato"]),                     # 需切番茄
        {"dish": "SeaweedSushi", "composition": ["Seaweed"],
         "id": "order2"},                                           # 出餐用
    ]
    w = _make_world(
        orders=orders,
        stations=[
            {"t": "crate", "n": "crate_tomato", "ing": "Tomato",
             "chop": "ChoppedTomato", "chop_stages": 8, "gx": 1, "gz": 1},
            {"t": "chopping", "n": "board_01", "gx": 2, "gz": 2,
             "item": None},                              # 空板
            {"t": "chopping", "n": "board_02", "gx": 3, "gz": 3,
             "item": "Tomato"},                          # 有番茄的板
            {"t": "counter", "n": "counter_01", "gx": 5, "gz": 5},
            {"t": "plate_station", "n": "serve_01", "gx": 6, "gz": 6},
        ],
        plates=[
            {"n": "plate_clean", "x": 10.0, "z": 10.0,
             "count": 0, "contents": []},
            # 已装配好的盘（别放 Tomato，否则 find_ingredient 会在盘里
            # 找到番茄而不再从箱子取，FetchToBoard 就不生成了）
            {"n": "plate_full", "x": 11.0, "z": 11.0,
             "count": 1, "contents": ["Seaweed"]},
        ],
    )
    bb = _make_bb()
    book = RecipeBook([{"name": "Tomato", "comp": []}], w.stations)

    candidates = generate_candidates(w, bb, orders[0], book)

    # 番茄已在 board_02 上 → 不再生成 FetchToBoard（避免搬料循环），
    # 而是生成 Chop 去切它。
    assert len(candidates) >= 4
    types = [c.task.TYPE for c in candidates]
    assert "PlaceCleanPlate" in types
    assert "FetchToBoard" not in types
    assert "Chop" in types
    assert "DeliverPlate" in types

    # 检查前置条件
    for c in candidates:
        assert isinstance(c.preconditions, list)
        assert isinstance(c.estimated_steps, int)
        assert c.estimated_steps > 0


def test_hands_not_empty_filters_fetch():
    """双手非空时不生成 Fetch* 任务。"""
    order = _make_order("TomatoSalad", ["Tomato", "Plate"])
    w = _make_world(
        orders=[order],
        stations=[
            {"t": "crate", "n": "crate_tomato", "ing": "Tomato",
             "gx": 1, "gz": 1, "x": 1.0, "z": 1.0},
        ],
    )
    bb = _make_bb(hands_empty=False)
    book = RecipeBook([{"name": "Tomato", "comp": []}], w.stations)

    candidates = generate_candidates(w, bb, order, book)

    # 不应该有 Fetch 开头的任务
    types = [c.task.TYPE for c in candidates]
    fetch_types = [t for t in types if t.startswith("Fetch")]
    assert len(fetch_types) == 0


def test_hands_not_empty_still_generates_fetch_to_board():
    """手里拿着目标生料时仍生成 FetchToBoard（树首 pickup 短路，直接放置）。

    实机复现：拿着黄瓜、FetchToBoard 被 hands_empty 硬门挡掉、StashHeld 又因
    "黄瓜还有单要"不可行 → select_task 返回 None，AI 卡在砧板前。
    """
    order = _make_order("Sushi_Cucumber", ["Cucumber"])
    w = _make_world(
        orders=[order],
        stations=[
            {"t": "crate", "n": "crate_cucumber", "ing": "Cucumber",
             "chop": "ChoppedCucumber", "chop_stages": 8, "gx": 1, "gz": 1},
            {"t": "chopping", "n": "board_01", "gx": 2, "gz": 2, "item": None},
        ],
        chefs=[{"name": "Player 2", "held": "Cucumber", "x": 0.0, "z": 0.0}],
    )
    bb = _make_bb()
    book = RecipeBook([{"name": "Cucumber", "comp": []}], w.stations)

    candidates = generate_candidates(w, bb, order, book)

    types = [c.task.TYPE for c in candidates]
    assert "FetchToBoard" in types


def test_raw_on_board_does_not_generate_fetch_to_board():
    """生料已在砧板上时不生成 FetchToBoard（避免搬料循环）。

    实机复现：鱼片上板后，FetchToBoard 的来源变成"另一块板"，planner 反复
    选 FetchToBoard 把鱼片在两块板之间搬来搬去，永不切。
    """
    order = _make_order("Sushi_Cucumber", ["Cucumber"])
    w = _make_world(
        orders=[order],
        stations=[
            {"t": "crate", "n": "crate_cucumber", "ing": "Cucumber",
             "chop": "ChoppedCucumber", "chop_stages": 8, "gx": 1, "gz": 1},
        ],
        workstations=[
            {"n": "board_full", "item": "Cucumber"},
            {"n": "board_empty", "item": None},
        ],
        chefs=[{"name": "Player 2", "held": None, "x": 0.0, "z": 0.0}],
    )
    bb = _make_bb()
    book = RecipeBook([{"name": "Cucumber", "comp": []}], w.stations)

    candidates = generate_candidates(w, bb, order, book)

    types = [c.task.TYPE for c in candidates]
    assert "FetchToBoard" not in types
    assert "Chop" in types


def test_move_fetch_to_board_scores_below_chop():
    """打分兜底：FetchToBoard 来源是"另一块板"（搬料）被打低，Chop 分数更高。"""
    from ai import tasks as T
    from ai.core.planner import _score_candidate

    order = _make_order("Sushi_Cucumber", ["Cucumber"])
    w = _make_world(
        orders=[order],
        stations=[
            {"t": "crate", "n": "crate_cucumber", "ing": "Cucumber",
             "chop": "ChoppedCucumber", "chop_stages": 8, "gx": 1, "gz": 1},
        ],
        workstations=[
            {"n": "board_full", "item": "Cucumber"},
            {"n": "board_empty", "item": None},
        ],
        chefs=[{"name": "Player 2", "held": None, "x": 0.0, "z": 0.0}],
    )
    bb = _make_bb()
    book = RecipeBook([{"name": "Cucumber", "comp": []}], w.stations)

    move_task = T.FetchToBoard("order1", ("board_empty",), "Cucumber")
    move_cand = Candidate(move_task, ["hands_empty"], 10)
    chop_task = T.Chop("order1", ("board_full",), "Cucumber")
    chop_cand = Candidate(chop_task, ["chopping_station_free"], 20)

    move_score, move_terms = _score_candidate(w, bb, move_cand, book)
    chop_score, chop_terms = _score_candidate(w, bb, chop_cand, book)

    assert move_terms["move_penalty"] < 0
    assert chop_score > move_score


def test_claimed_station_filtered():
    """已认领的站点不生成对应任务。"""
    order = _make_order("TomatoSalad", ["Tomato", "Plate"])
    w = _make_world(
        orders=[order],
        stations=[
            {"t": "crate", "n": "crate_tomato", "ing": "Tomato",
             "gx": 1, "gz": 1, "x": 1.0, "z": 1.0},
            {"t": "board", "n": "board_01", "gx": 2, "gz": 2,
             "x": 2.0, "z": 2.0, "item": None},
        ],
    )
    # board_01 已被 P1 认领用于切菜
    bb = _make_bb(claims={(("board_01",), "chop"): "P1"})
    book = RecipeBook([{"name": "Tomato", "comp": []}], w.stations)

    candidates = generate_candidates(w, bb, order, book)

    # 检查没有针对 board_01 的 Chop 任务
    for c in candidates:
        if c.task.TYPE == "Chop":
            assert "board_01" not in str(c.task.resources)


def test_complex_order_with_cooked_meat():
    """复杂订单（CookedMeat + Lettuce）生成 烹饪 / 切菜 链。"""
    order = _make_order("MeatSalad", ["CookedMeat", "Lettuce"])
    w = _make_world(
        orders=[order],
        stations=[
            {"t": "crate", "n": "crate_meat", "ing": "RawMeat",
             "gx": 1, "gz": 1, "x": 1.0, "z": 1.0},
            {"t": "crate", "n": "crate_lettuce", "ing": "Lettuce",
             "chop": "ChoppedLettuce", "chop_stages": 8,
             "gx": 2, "gz": 2, "x": 2.0, "z": 2.0},
            {"t": "chopping", "n": "board_01", "gx": 4, "gz": 4,
             "x": 4.0, "z": 4.0, "item": None},
            {"t": "chopping", "n": "board_02", "gx": 6, "gz": 6,
             "x": 6.0, "z": 6.0, "item": "Lettuce"},
            {"t": "counter", "n": "counter_01", "gx": 5, "gz": 5,
             "x": 5.0, "z": 5.0},
        ],
        plates=[{"n": "plate_clean", "x": 10.0, "z": 10.0,
                 "count": 0, "contents": []}],
        cookers=[{"n": "cooker_01", "gx": 3, "gz": 3,
                  "x": 3.0, "z": 3.0, "cooked": False}],
    )
    bb = _make_bb()
    recipes = [
        {"name": "CookedMeat", "comp": ["RawMeat"], "cook": "pot"},
        {"name": "Lettuce", "comp": []},
    ]
    book = RecipeBook(recipes, w.stations)

    candidates = generate_candidates(w, bb, order, book)

    # 应该有多种任务类型
    types = set(c.task.TYPE for c in candidates)
    # 至少包含 取盘 / 下锅 / 取菜上板 / 切菜
    assert len(types) >= 4
    # 检查有烹饪相关任务
    assert any("Cooker" in t or "Cook" in t for t in types)

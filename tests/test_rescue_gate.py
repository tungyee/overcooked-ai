# -*- coding: utf-8 -*-
"""截止闸与层内 fallthrough（spec §4.6）。

规则回顾：

- `cushion = solo_slack`（剩余拍 − 该单完整 solo ETA），**不是**剩余时间；
- `margin = max(2.5 秒, min(0.15 × D, 5 秒))`，`D` = 这个动作 + 送餐的耗时；
- `0 <= cushion < margin` 且不在抢单冷却期 → 开闸，允许无视 claim/assign 负分；
- 本层全被认领且无闸 → 逐层向下掉，不是只掉一层。

这些测试不 mock 闸本身：世界、配方、黑板都是真的，候选也走 `plan()` 的
生产路径。闸的输入 `cushion` 由真实的 `ETA.solo_slack_by_order` 算出。
"""
import pytest

from ai import tasks as T
from ai.core import planner
from ai.core import planner_layers as PL
from ai.dialog.schema import Preference

# 账本作用域（与 ai/dialog/duties.py 展开结果同形）。
COOK_CLAIM = frozenset({("FetchToCooker", None), ("FillCooker", None),
                        ("TakeCookedToPlate", None)})
WASH_SCOPE = frozenset({("WashPlate", None), ("PlaceCleanPlate", None)})


def _type(task_type):
    """作用域：该 task 类型的任意 item（账本里的「只写类型」）。"""
    return frozenset({(task_type, None)})
from ai.query.recipes import RecipeBook
from ai.revenue import eta as ETA
from ai.state.blackboard import Blackboard
from ai.state.world import World


RECIPES = [
    {"name": "Sushi_A", "comp": ["Seaweed"]},
    {"name": "Sushi_B", "comp": ["Cucumber"]},
    {"name": "Seaweed"},
    {"name": "Cucumber"},
]

STATIONS = [
    {"t": "washing", "n": "sink_01", "gx": 7, "gz": 1},
    {"t": "plate_return", "n": "DryingPart", "gx": 8, "gz": 1},
    {"t": "serving", "n": "hatch", "gx": 9, "gz": 1},
]


@pytest.fixture
def bb():
    b = Blackboard()
    b.tick = 1000
    b.preferences = []
    return b


def _cand(task, book=None):
    """候选：branch 走生产路径的同一条归一函数。"""
    branch = planner.branch_of_task(task, book) if book is not None else None
    return planner.Candidate(task, [], 15, branch=branch)


def _order(oid, dish="Sushi_A", remaining=100.0):
    return {"id": oid, "dish": dish,
            "composition": ["Seaweed" if dish == "Sushi_A" else "Cucumber"],
            "remaining": remaining, "lifetime": 130.0}


def _world(orders, stations=None):
    """一块料齐的盘 + 一个脏盘摞。

    料齐的盘给出 L3 DeliverPlate；场上 0 块干净空盘 + 有脏盘摞给出 L4
    WashPlate。盘放在 (0,0)（没有 counter 站点），因此不算「已就位的盘」。
    """
    return World({
        "orders": orders,
        "stations": list(stations if stations is not None else STATIONS),
        "recipes": RECIPES,
        "chefs": [{"name": "Player 2", "x": 1.0, "z": 1.0, "held": None}],
        "plates": [
            {"n": "plate_a", "x": 0.0, "z": 0.0, "contents": ["seaweed"]},
            {"n": "plate_b", "x": 0.0, "z": 0.0, "contents": ["cucumber"]},
        ],
        "workstations": [],
        "cookers": [],
        "items": [{"n": "DirtyPlateStack", "x": 3.0, "z": 0.0}],
        "extinguishers": [],
        "grid": {"nodeMap": None},
    })


# ---------- margin / gate ----------

def test_margin_starts_at_two_and_a_half_seconds_for_short_actions():
    """短动作的 D 撑不满 0.15 的占比，2.5 秒（50 拍）起步兜底。"""
    assert planner._rescue_margin_ticks("FetchToCooker") == 50   # D=170 拍
    assert planner._rescue_margin_ticks("DeliverPlate") == 50    # D=50 拍


def test_margin_narrowed_below_old_value():
    """收窄后的余量窗口恒小于旧值：Chop 从 100 拍收到 50 拍。"""
    d = ETA.task_cost_ticks("Chop") + ETA.COST_DELIVER   # 30 + 50 = 80 拍
    new = planner._rescue_margin_ticks("Chop")
    assert new == max(50, min(int(0.15 * d), 100))       # 50
    assert new < max(100, min(int(0.3 * d), 200))        # 旧值 100，更大


def test_gate_opens_with_cushion_inside_margin(bb):
    """cushion=2s=40 拍 < margin 2.5s（50 拍）→ 开闸。"""
    task = T.FetchToCooker(order_id=1, resources=("cooker_1",),
                           item="BoiledSushiRice")
    bb.tick = 1000
    assert planner._rescue_gate_open(_cand(task), cushion=40, bb=bb) is True


def test_gate_closed_when_cushion_is_comfortable(bb):
    """9 秒余量还等得起，尊重认领。"""
    task = T.FetchToCooker(order_id=1, resources=("cooker_1",),
                           item="BoiledSushiRice")
    bb.tick = 1000
    assert planner._rescue_gate_open(_cand(task), cushion=180, bb=bb) is False


def test_gate_closed_when_cushion_negative(bb):
    """cushion < 0 已经来不及，不为了它抢。"""
    task = T.FetchToCooker(order_id=1, resources=("cooker_1",),
                           item="BoiledSushiRice")
    bb.tick = 1000
    assert planner._rescue_gate_open(_cand(task), cushion=-160, bb=bb) is False


def test_gate_requires_a_cushion(bb):
    """订单不在 slack_map 里（算不出 cushion）→ 闸关闭，不猜。"""
    task = T.FetchToCooker(order_id=1, resources=("cooker_1",),
                           item="BoiledSushiRice")
    assert planner._rescue_gate_open(_cand(task), cushion=None, bb=bb) is False


def test_cooldown_blocks_the_gate(bb):
    """抢过一次后 100 拍内不再因为同一单来回切。"""
    task = T.FetchToCooker(order_id=1, resources=("cooker_1",),
                           item="BoiledSushiRice")
    cand = _cand(task)
    bb.tick = 1000
    bb.rescue_cooldown_until = 1050
    assert planner._rescue_gate_open(cand, cushion=40, bb=bb) is False
    # 冷却到期那一拍仍算冷却内（严格大于才算过）
    bb.tick = 1050
    assert planner._rescue_gate_open(cand, cushion=40, bb=bb) is False
    bb.tick = 1051
    assert planner._rescue_gate_open(cand, cushion=40, bb=bb) is True


# ---------- 救援类免疫 / L2 仍可选 ----------

def test_l2_rescue_ignores_claim(bb):
    """RescueCooked 不吃任何 claim 负分（spec §4.6）。"""
    task = T.RescueCooked(resources=("cooker_1",))
    bb.preferences = [Preference(kind="claim", weight=-8.0,
                                     scope=_type("RescueCooked"))]
    assert planner._claim_penalty(_cand(task), bb, layer=2) == 0.0


def test_l2_take_cooked_stays_eligible_under_cook_claim(bb):
    """L2 的 TakeCookedToPlate 在饭链认领里，但它在 L1/L2 免疫负分。

    所以即使玩家说「我来煮」，这一层也不该因为认领而掉到 L3 以下。
    """
    task = T.TakeCookedToPlate(order_id="o1",
                               resources=("cooker_1", "plate_a"),
                               item="BoiledSushiRice")
    bb.preferences = [Preference(kind="claim", weight=-8.0,
                                     scope=COOK_CLAIM)]
    cand = _cand(task)
    assert planner._claim_penalty(cand, bb, layer=PL.L_RESCUE) == 0.0

    w = _world([_order("o1")])
    layer, eligible = planner._select_layer(
        [cand], w, {cand.task.key: PL.L_RESCUE}, bb, {}, bb.tick)
    assert layer == PL.L_RESCUE
    assert eligible == [cand]


# ---------- 层内 fallthrough ----------

def test_claimed_delivery_falls_through_to_unblock(bb):
    """L3 全被认领且闸没开 → 掉到 L4 洗盘，而不是硬选被认领的送餐。"""
    w = _world([_order("o1", remaining=100.0)])
    book = RecipeBook(w.recipes, w.stations)
    bb.preferences = [Preference(kind="claim", weight=-8.0,
                                     scope=_type("DeliverPlate"))]

    chosen, record = planner.plan(w, bb, None, book)

    assert chosen is not None
    assert chosen.TYPE == "WashPlate"
    assert record["top_layer"] == PL.L_UNBLOCK
    assert "rescue_override" not in record

    deliver_row = _row(record, "DeliverPlate")
    assert deliver_row is not None
    assert deliver_row[6] == PL.L_REDEEM       # layer
    assert deliver_row[7] is False             # in_top_layer：本拍没用这层


def test_fallthrough_is_iterative_not_one_layer(bb):
    """L3、L4 两层都被认领 → 继续掉到更低的清理层，只掉一层是不够的。

    本场景里 Prefetch 与 ResetPlate 都落 L7（Prefetch 不在 LAYER_PREP_TYPES
    里，这是既有分层行为），所以断言「选中层 = L7」同时证明跳过了 L3/L4。
    """
    stations = list(STATIONS) + [
        {"t": "crate", "n": "crate_weed", "ing": "Seaweed", "gx": 1, "gz": 1},
    ]
    w = _world([_order("o1", remaining=100.0),
                _order("o2", remaining=110.0)], stations=stations)
    book = RecipeBook(w.recipes, w.stations)
    bb.preferences = [
        Preference(kind="claim", weight=-8.0, scope=_type("DeliverPlate")),
        Preference(kind="claim", weight=-8.0, scope=WASH_SCOPE),
    ]

    chosen, record = planner.plan(w, bb, None, book)

    assert chosen is not None
    assert record["top_layer"] == PL.L_CLEANUP
    assert chosen.TYPE == "ResetPlate"
    assert _row(record, "DeliverPlate")[6] == PL.L_REDEEM
    assert _row(record, "DeliverPlate")[7] is False
    assert _row(record, "WashPlate")[6] == PL.L_UNBLOCK
    assert _row(record, "WashPlate")[7] is False


def test_gate_open_claimed_delivery_is_selected_and_announced(bb):
    """最后窗口：允许无视认领抢送餐，并写冷却 + 对话事件。"""
    # plate 已料齐 → solo_eta = 送餐 50 拍；remaining=3.5s=70 拍 → cushion=20
    w = _world([_order("o1", remaining=3.5)])
    book = RecipeBook(w.recipes, w.stations)
    bb.preferences = [Preference(kind="claim", weight=-8.0,
                                     scope=_type("DeliverPlate"))]

    chosen, record = planner.plan(w, bb, None, book)

    assert chosen is not None
    assert chosen.TYPE == "DeliverPlate"
    assert chosen.order_id == "o1"
    assert record["top_layer"] == PL.L_REDEEM
    assert record["rescue_override"] == "o1"
    assert _row(record, "DeliverPlate")[7] is True
    assert bb.rescue_cooldown_until == bb.tick + 100
    assert bb.dialog.pending_event == ("rescue", "o1")
    assert bb.dialog.pending_event == ("rescue", "o1")
    assert bb.dialog.rescue_task_key == chosen.key
    assert bb.dialog.rescue_hold_until == bb.tick + 200


def test_cooldown_blocks_a_second_gate_open(bb):
    """第一拍抢过之后，同一单在冷却期内不再开闸，层照常向下掉。"""
    w = _world([_order("o1", remaining=3.5)])
    book = RecipeBook(w.recipes, w.stations)
    bb.preferences = [Preference(kind="claim", weight=-8.0,
                                     scope=_type("DeliverPlate"))]

    first, _ = planner.plan(w, bb, None, book)
    assert first.TYPE == "DeliverPlate"
    assert bb.dialog.pending_event == ("rescue", "o1")
    bb.dialog.pending_event = None            # 主循环已转发

    second, record = planner.plan(w, bb, None, book)

    assert second is not None
    assert second.TYPE == "WashPlate"
    assert record["top_layer"] == PL.L_UNBLOCK
    assert "rescue_override" not in record
    assert bb.dialog.pending_event is None    # 没有第二次救援事件


def test_rescue_hold_keeps_claimed_delivery_on_next_tick(bb):
    """抢回的 task 必须活到做完，而不是下一拍就被同一条 claim 清掉。

    评审复现：T 拍开闸选中被认领的送餐，T+1 拍冷却让闸关闭，
    `_clear_claimed_commitment` 又被同一条 claim 命中 → active_task 被清空，
    Executor 重建树，"抢回"只维持一拍。保护期要挡住这条清理路径。
    """
    w = _world([_order("o1", remaining=3.5)])
    book = RecipeBook(w.recipes, w.stations)
    bb.preferences = [Preference(kind="claim", weight=-8.0,
                                     scope=_type("DeliverPlate"))]

    first, _ = planner.plan(w, bb, None, book)
    assert first.TYPE == "DeliverPlate"
    rescued_at = bb.tick
    bb.dialog.pending_event = None                  # 主循环已转发

    # Agent 在 plan() 之后登记承诺（ai/agent.py ⑪），下一拍才会看到 active_task
    bb.commitment.active_task = first
    bb.tick = rescued_at + 1

    second, record = planner.plan(w, bb, None, book)

    assert second == first
    assert record["top_layer"] == PL.L_REDEEM
    assert bb.commitment.active_task == first       # 没被同一条 claim 清掉
    assert bb.dialog.pending_event is None          # 不重复播报
    assert bb.rescue_cooldown_until == rescued_at + 100


def test_hold_release_lets_the_other_order_rescue(bb):
    """抢回的单做完后保护期释放；冷却期内不换单，冷却过后另一单正常开闸。"""
    w = _world([_order("o1", remaining=3.5),
                _order("o2", dish="Sushi_B", remaining=4.5)])
    book = RecipeBook(w.recipes, w.stations)
    bb.preferences = [Preference(kind="claim", weight=-8.0,
                                     scope=_type("DeliverPlate"))]

    first, _ = planner.plan(w, bb, None, book)
    assert first.order_id == "o1"
    assert bb.dialog.rescue_task_key == first.key

    bb.commitment.active_task = first
    bb.tick += 1
    held, _ = planner.plan(w, bb, None, book)
    assert held.order_id == "o1"          # 保护期内保持同一单

    # o1 做完：Agent 清承诺，单与盘也消失
    bb.commitment = None
    w.orders = [o for o in w.orders if o["id"] != "o1"]
    w.plates = [p for p in w.plates if p["n"] != "plate_a"]

    # 冷却期内不能改抢别的单，保护期也已释放
    bb.tick = 1010
    blocked, _ = planner.plan(w, bb, None, book)
    assert blocked.TYPE == "WashPlate"
    assert bb.dialog.rescue_task_key is None

    # 冷却过后 → o2 正常开闸抢回
    bb.tick = 1101
    rescued, record = planner.plan(w, bb, None, book)
    assert rescued.TYPE == "DeliverPlate"
    assert rescued.order_id == "o2"
    assert record["rescue_override"] == "o2"
    assert bb.dialog.rescue_task_key == rescued.key
    assert bb.dialog.rescue_hold_until == 1101 + 200


def test_hold_expiry_releases_the_rescue_task(bb):
    """保护期到期后认领重新生效：任务被清、层向下掉。"""
    w = _world([_order("o1", remaining=3.5)])
    book = RecipeBook(w.recipes, w.stations)
    bb.preferences = [Preference(kind="claim", weight=-8.0,
                                     scope=_type("DeliverPlate"))]

    first, _ = planner.plan(w, bb, None, book)
    assert first.TYPE == "DeliverPlate"
    bb.commitment.active_task = first

    # 保护期缩到 5 拍（正常 200 拍），让到期点落在冷却期内
    bb.dialog.rescue_hold_until = bb.tick + 5
    bb.tick += 6

    chosen, _ = planner.plan(w, bb, None, book)

    assert chosen.TYPE == "WashPlate"       # 认领重新生效 → 掉到 L4
    assert bb.commitment.active_task is None
    assert bb.dialog.rescue_task_key is None
    assert bb.dialog.rescue_hold_until == 0


def test_two_expiring_orders_select_a_stable_order(bb):
    """两单都在最后窗口时，选择要稳定（顺序 + 结果都不抖）。"""
    w = _world([_order("o1", remaining=3.5),
                _order("o2", dish="Sushi_B", remaining=4.5)])
    book = RecipeBook(w.recipes, w.stations)
    bb.preferences = [Preference(kind="claim", weight=-8.0,
                                     scope=_type("DeliverPlate"))]

    plate_match = planner._match_plates_to_orders(w, bb, book)
    cooker_match = planner._match_cookers_to_orders(w, bb, book, plate_match)
    slack = ETA.solo_slack_by_order(w, book, plate_match, cooker_match)
    assert slack == {"o1": 20.0, "o2": 40.0}

    cands = [
        _cand(T.DeliverPlate(order_id="o2", resources=("plate_b",)), book),
        _cand(T.DeliverPlate(order_id="o1", resources=("plate_a",)), book),
    ]
    layer_map = {c.task.key: PL.L_REDEEM for c in cands}

    first = planner._select_layer(cands, w, layer_map, bb, slack, bb.tick)
    second = planner._select_layer(cands, w, layer_map, bb, slack, bb.tick)

    assert first == second
    assert first[0] == PL.L_REDEEM
    assert [c.task.order_id for c in first[1]] == ["o2", "o1"]

    scored = planner._score_tasks([c.task for c in first[1]], first[1], w, bb,
                                  book, plate_match, slack, cooker_match,
                                  PL.L_REDEEM)
    chosen = planner._apply_commitment_inertia(scored, w, bb)
    assert chosen.order_id == "o1"       # cushion 更小（60 < 80）的那单


def _row(record, task_type):
    """在 decision_record["candidates"] 里找某类候选的第一行。"""
    for row in record["candidates"]:
        if row[0].TYPE == task_type:
            return row
    return None

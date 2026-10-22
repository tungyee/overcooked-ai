"""P2 的硬意图：放弃订单、stop 硬压制、认领清承诺（spec §4.4–4.6）。

这三件事都不是候选打分上的加法项，而是规划器层的硬过滤/硬重置：
  1. `abandon_order` 在 generate_candidates 阶段就整单不发候选；
  2. 账本 `stop` 让作用域内的候选在可行性层被否决；
  3. 玩家的 `claim`/`assign` 命中当前承诺时，立即放弃当前 task。
"""
import pytest

from ai import tasks as T
from ai.core import planner
from ai.dialog.schema import Preference
from ai.query.recipes import RecipeBook
from ai.state.blackboard import Blackboard
from ai.state.world import World


# 账本作用域（与 ai/dialog/duties.py 展开结果同形）。
CUCUMBER_CHAIN = frozenset({("FetchToBoard", "cucumber"), ("Chop", "cucumber"),
                            ("FetchToPlate", "cucumber")})
FISH_CHAIN = frozenset({("FetchToBoard", "sushifish"), ("Chop", "sushifish"),
                        ("FetchToPlate", "sushifish")})
COOK_CLAIM = frozenset({("FetchToCooker", None), ("FillCooker", None),
                        ("TakeCookedToPlate", None)})


def _type(task_type):
    """作用域：该 task 类型的任意 item（账本里的「只写类型」）。"""
    return frozenset({(task_type, None)})


# 「先别煮」= 账本 stop 条目：下锅/填灶/取料（饭链）的作用域。
COOK_SCOPE = frozenset({("FetchToCooker", None), ("FillCooker", None),
                        ("Prefetch", None), ("TakeCookedToPlate", None)})


def _stop(scope, expires):
    return Preference(kind="stop", owner="P2", scope=scope,
                      expires_tick=expires)


_RECIPES = [
    {"name": "FriedMushrooms", "t": "CookedCompositeOrderNode",
     "comp": ["Mushroom"], "cook": "FryingPan"},
    {"name": "Seaweed", "comp": []},
]


def _orders():
    """两单都要煎蘑菇 + 海苔：既发 FetchToCooker，也满足 Prefetch 的 ≥2 单。"""
    return [
        {"id": 1, "dish": "FriedMushrooms",
         "composition": ["FriedMushrooms", "Seaweed"], "remaining": 60},
        {"id": 2, "dish": "FriedMushrooms",
         "composition": ["FriedMushrooms", "Seaweed"], "remaining": 60},
    ]


@pytest.fixture
def world():
    stations = [
        {"t": "crate", "n": "crate_mushroom", "ing": "Mushroom",
         "chop": "ChoppedMushroom", "chop_stages": 8, "gx": 1, "gz": 1},
        {"t": "crate", "n": "crate_seaweed", "ing": "Seaweed",
         "gx": 2, "gz": 1},
        {"t": "chopping", "n": "board_01", "gx": 2, "gz": 2,
         "item": "ChoppedMushroom"},
        {"t": "counter", "n": "counter_01", "gx": 3, "gz": 3},
    ]
    w = World({
        "orders": _orders(),
        "recipes": _RECIPES,
        "stations": stations,
        # find_ingredient 从 workstations 认「板上有切好的料」。
        "workstations": [{"n": "board_01", "item": "ChoppedMushroom",
                          "gx": 2, "gz": 2}],
        "plates": [],
        "cookers": [{"n": "cooker_01", "x": 4.0, "z": 4.0,
                     "cooked": False, "cookware": "FryingPan"}],
        "chefs": [{"name": "Player 2", "x": 0.0, "z": 0.0, "held": None}],
        "items": [],
        "extinguishers": [],
        "grid": {"nodeMap": None},
    })
    # 干净盘落在拼装格上才会参与 1:1 匹配（盘↔订单）。
    px, pz = w.to_world(3, 3)
    w.plates = [{"n": "plate_01", "x": px, "z": pz, "count": 0,
                 "contents": []}]
    return w


@pytest.fixture
def bb():
    b = Blackboard()
    b.tick = 100
    return b


@pytest.fixture
def book(world):
    return RecipeBook(world.recipes, world.stations)


@pytest.fixture
def plate_world():
    """两单同菜（黄瓜寿司）+ 一张拼装格上的干净盘。

    dish 不在配方表 → 走 composition 兜底，每单都发 `FetchToPlate`。
    """
    recipes = [{"name": "Cucumber", "comp": []}]
    orders = [
        {"id": 1, "dish": "CucumberSushi", "composition": ["Cucumber"],
         "remaining": 60},
        {"id": 2, "dish": "CucumberSushi", "composition": ["Cucumber"],
         "remaining": 60},
    ]
    stations = [
        {"t": "crate", "n": "crate_cucumber", "ing": "Cucumber",
         "gx": 1, "gz": 1},
        {"t": "counter", "n": "counter_01", "gx": 3, "gz": 3},
    ]
    w = World({
        "orders": orders,
        "recipes": recipes,
        "stations": stations,
        "workstations": [],
        "plates": [],
        "cookers": [],
        "chefs": [{"name": "Player 2", "x": 0.0, "z": 0.0, "held": None}],
        "items": [],
        "extinguishers": [],
        "grid": {"nodeMap": None},
    })
    px, pz = w.to_world(3, 3)
    w.plates = [{"n": "plate_01", "x": px, "z": pz, "count": 0,
                 "contents": []}]
    return w


@pytest.fixture
def plate_book(plate_world):
    return RecipeBook(plate_world.recipes, plate_world.stations)


def _ctx(world, bb, book):
    return planner._make_context(world, bb, book)


# ---------- 放弃订单：整单不发候选 ----------

def test_abandoned_order_is_not_generated(bb, world, book):
    expiry = bb.tick + 1200
    bb.abandoned_orders = {2: expiry}

    cands = planner.generate_candidates(world, bb, None, book)

    assert cands, "其他订单的候选仍应生成"
    assert all(getattr(c.task, "order_id", None) != 2 for c in cands)


def test_abandoned_order_returns_at_expiry(bb, world, book):
    expiry = bb.tick + 1200
    bb.abandoned_orders = {2: expiry}
    bb.tick = expiry                     # 到期这一拍即恢复

    cands = planner.generate_candidates(world, bb, None, book)

    assert any(getattr(c.task, "order_id", None) == 2 for c in cands)


def test_abandoned_order_is_not_revived_by_commitment(bb, world, book):
    """当前承诺在放弃订单上时，不能靠 generate_candidates 的兜底重新入列。"""
    bb.abandoned_orders = {2: bb.tick + 1200}
    bb.commitment.active_task = T.FetchToCooker(
        order_id=2, resources=("cooker_01",), item="FriedMushrooms")

    cands = planner.generate_candidates(world, bb, None, book)

    assert all(getattr(c.task, "order_id", None) != 2 for c in cands)


def test_abandoned_order_does_not_consume_plate_match(bb, plate_world,
                                                      plate_book):
    """放弃的单不能占住 1:1 盘匹配，否则活着的那单永远拿不到盘。

    回归场景（评审复现）：两单同菜 + 一张干净盘，贪心匹配会把盘给 id 小的
    单；只过滤 generate_candidates 时，被放弃的单仍在原始 world.orders 里
    参与匹配，活单的 plate_match 为空 → 没有 FetchToPlate。
    """
    bb.abandoned_orders = {1: bb.tick + 1200}

    _, record = planner.plan(plate_world, bb, None, plate_book)

    assert record["plate_match"].get("plate_01") == "2"
    live_plate_cands = [
        entry for entry in record["candidates"]
        if getattr(entry[0], "order_id", None) == 2
        and "plate_01" in str(getattr(entry[0], "resources", ()))
    ]
    assert live_plate_cands, "活单 2 仍应有绑定 plate_01 的候选"


# ---------- stop：硬压制 ----------

def test_cook_suppression_vetoes_fetch_to_cooker_and_prefetch(bb, world, book):
    bb.preferences = [_stop(COOK_SCOPE, bb.tick + 160)]
    cands = planner.generate_candidates(world, bb, None, book)
    ctx = _ctx(world, bb, book)

    f2c = next(c for c in cands if c.task.TYPE == "FetchToCooker")
    prefetch = next(c for c in cands if c.task.TYPE == "Prefetch")

    assert planner._feasibility_of(f2c, ctx, bb) == (False, "p2_suppressed")
    assert planner._feasibility_of(prefetch, ctx, bb) == (False, "p2_suppressed")


def test_expired_suppression_does_not_veto(bb, world, book):
    expiry = bb.tick + 160
    bb.preferences = [_stop(COOK_SCOPE, expiry)]
    bb.tick = expiry                     # 到期这一拍即失效
    cands = planner.generate_candidates(world, bb, None, book)
    ctx = _ctx(world, bb, book)

    f2c = next(c for c in cands if c.task.TYPE == "FetchToCooker")
    prefetch = next(c for c in cands if c.task.TYPE == "Prefetch")

    assert planner._feasibility_of(f2c, ctx, bb) == (True, "")
    assert planner._feasibility_of(prefetch, ctx, bb) == (True, "")


def test_unrelated_family_suppression_does_not_veto_cook(bb, world, book):
    bb.preferences = [_stop(_type("DeliverPlate"), bb.tick + 160)]
    cands = planner.generate_candidates(world, bb, None, book)
    f2c = next(c for c in cands if c.task.TYPE == "FetchToCooker")

    assert planner._feasibility_of(f2c, _ctx(world, bb, book), bb) == (True, "")


# ---------- 分工偏好与承诺 ----------

def test_claim_keeps_matching_commitment(bb):
    task = T.Chop(order_id=1, resources=("board_1",), item="Cucumber")
    bb.commitment.active_task = task
    bb.commitment.infeasible_streak = 2
    original = bb.commitment
    bb.preferences = [Preference(kind="claim", weight=-8.0, scope=CUCUMBER_CHAIN)]

    planner._clear_claimed_commitment(bb)

    assert bb.commitment is original
    assert bb.commitment.active_task == task
    assert bb.commitment.infeasible_streak == 2


def test_non_matching_preference_keeps_commitment(bb):
    task = T.Chop(order_id=1, resources=("board_1",), item="Cucumber")
    bb.commitment.active_task = task
    bb.commitment.infeasible_streak = 2
    bb.preferences = [
        Preference(kind="claim", weight=-8.0, scope=FISH_CHAIN),
        Preference(kind="stop", weight=0.0, scope=FISH_CHAIN,
                   expires_tick=bb.tick + 600),
        Preference(kind="direct", weight=4.0, scope=CUCUMBER_CHAIN),
    ]

    planner._clear_claimed_commitment(bb)

    assert bb.commitment.active_task == task
    assert bb.commitment.infeasible_streak == 2


def test_expired_claim_does_not_clear_commitment(bb):
    task = T.Chop(order_id=1, resources=("board_1",), item="Cucumber")
    bb.commitment.active_task = task
    bb.commitment.infeasible_streak = 2
    bb.preferences = [Preference(kind="claim", weight=-8.0,
                                 expires_tick=bb.tick, scope=CUCUMBER_CHAIN)]

    planner._clear_claimed_commitment(bb)

    assert bb.commitment.active_task == task
    assert bb.commitment.infeasible_streak == 2


def test_suppression_clears_active_commitment(bb):
    """stop 是硬停手：当前承诺落在 stop 作用域里时立即清掉。"""
    task = T.FetchToCooker(order_id=1, resources=("cooker_1",),
                           item="FriedMushrooms")
    bb.commitment.active_task = task
    bb.commitment.infeasible_streak = 2
    bb.preferences = [_stop(COOK_SCOPE | frozenset({
        ("RescueCooked", None), ("ReturnPot", None),
        ("RecoverLoosePot", None), ("Extinguish", None)}), bb.tick + 160)]

    planner._clear_claimed_commitment(bb)

    assert bb.commitment.active_task is None
    assert bb.commitment.infeasible_streak == 0


def test_abandoned_order_clears_active_commitment(bb):
    """abandon_order 是整单硬排除：当前承诺在该单上也立即清掉。"""
    task = T.FetchToCooker(order_id=2, resources=("cooker_1",),
                           item="FriedMushrooms")
    bb.commitment.active_task = task
    bb.commitment.infeasible_streak = 2
    bb.abandoned_orders = {2: bb.tick + 1200}

    planner._clear_claimed_commitment(bb)

    assert bb.commitment.active_task is None
    assert bb.commitment.infeasible_streak == 0


def test_rescue_task_is_not_cleared_by_claim(bb):
    """L0–L2 救援动作不吃 claim：饭要糊了也得端。"""
    task = T.RescueCooked(resources=("cooker_1",))
    bb.commitment.active_task = task
    bb.commitment.infeasible_streak = 2
    bb.preferences = [
        Preference(kind="claim", weight=-8.0, scope=COOK_CLAIM),
        # 只写类型的 claim 才会真正命中救援 task；饭链 claim 本身不含
        # RescueCooked，只用它测不出免疫。
        Preference(kind="claim", weight=-8.0, scope=_type("RescueCooked")),
    ]

    planner._clear_claimed_commitment(bb)

    assert bb.commitment.active_task == task
    assert bb.commitment.infeasible_streak == 2


def test_rescue_task_is_not_cleared_by_suppression(bb):
    """TakeCookedToPlate 在 stop 作用域里，但它是救援动作，压制也不能清承诺。"""
    task = T.TakeCookedToPlate(order_id=1, resources=("cooker_1",),
                               item="BoiledSushiRice")
    bb.commitment.active_task = task
    bb.commitment.infeasible_streak = 2
    bb.preferences = [_stop(COOK_SCOPE | frozenset({
        ("RescueCooked", None), ("ReturnPot", None),
        ("RecoverLoosePot", None), ("Extinguish", None)}), bb.tick + 160)]

    planner._clear_claimed_commitment(bb)

    assert bb.commitment.active_task == task
    assert bb.commitment.infeasible_streak == 2


def test_plan_keeps_claimed_commitment_at_start(bb, world, book):
    task = T.Chop(order_id=1, resources=("board_1",), item="Cucumber")
    bb.commitment.active_task = task
    bb.preferences = [Preference(kind="claim", weight=-8.0, scope=CUCUMBER_CHAIN)]

    planner.plan(world, bb, None, book)

    assert bb.commitment.active_task == task


def test_suppression_does_not_veto_rescue_candidates(bb):
    """stop（先别煮）不能挡住刚熟/救援动作（spec §4.5 末段）。

    实机场景：玩家说「先别煮饭」，锅里的饭刚好熟了——TakeCookedToPlate
    必须仍然可行，否则饭只能烧糊。
    """
    bb.preferences = [_stop(COOK_SCOPE | frozenset({
        ("RescueCooked", None), ("ReturnPot", None),
        ("RecoverLoosePot", None), ("Extinguish", None)}), bb.tick + 160)]
    rescue = (
        T.TakeCookedToPlate(order_id=1, resources=("cooker_1",),
                            item="BoiledSushiRice"),
        T.RescueCooked(resources=("cooker_1",)),
        T.ReturnPot(resources=("cooker_1",)),
        T.RecoverLoosePot(resources=("cooker_1",), item="pot_1"),
        T.Extinguish(),
    )
    for task in rescue:
        assert planner._is_stopped(task, bb) is False

    # 普通下锅任务仍被压制。
    fetch = T.FetchToCooker(order_id=1, resources=("cooker_1",),
                            item="BoiledSushiRice")
    assert planner._is_stopped(fetch, bb) is True

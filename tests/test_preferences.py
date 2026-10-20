"""账本偏好 → 候选分（spec §4.5–4.6）。

覆盖三件事：
  1. 作用域匹配：`(task 类型, item)`，item 大小写不敏感，`(类型, None)` 通配；
     `exclude` 扣掉被更新条目接管的部分；
  2. 免疫规则：L0/L1/L2 与救援类不吃负分，但正分照常生效；
  3. 到期：`expires_tick` 到点即失效，0 为长期。
"""
import pytest

from ai import tasks as T
from ai.core import planner
from ai.dialog.schema import Preference
from ai.query.recipes import RecipeBook
from ai.state.blackboard import Blackboard


RECIPES = [
    {"name": "Cucumber", "comp": []},
    {"name": "SushiFish", "comp": []},
    {"name": "SushiRice", "comp": []},
    {"name": "BoiledSushiRice", "comp": ["SushiRice"], "cook": "pot"},
]


@pytest.fixture
def book():
    return RecipeBook(RECIPES, [])


@pytest.fixture
def bb():
    b = Blackboard()
    b.tick = 100
    return b


def _cand(task, book):
    """候选：branch 走生产路径的同一条归一函数。"""
    return planner.Candidate(task, [], 15,
                             branch=planner.branch_of_task(task, book))


def _pref(kind, weight, *pairs, **kw):
    return Preference(kind=kind, weight=weight, scope=frozenset(pairs), **kw)


CHOP_CUCUMBER = ("Chop", "cucumber")
CUCUMBER_CHAIN = (("FetchToBoard", "cucumber"), ("Chop", "cucumber"),
                  ("FetchToPlate", "cucumber"))


# ---------- 作用域匹配 ----------

def test_type_wildcard_matches_every_item(bb, book):
    bb.preferences = [_pref("claim", -8.0, ("Chop", None))]
    chop = _cand(T.Chop(order_id=1, resources=("board_1",),
                        item="Cucumber"), book)
    fish = _cand(T.Chop(order_id=1, resources=("board_1",),
                        item="SushiFish"), book)
    assert planner._preference_score(chop, bb, layer=6) == -8.0
    assert planner._preference_score(fish, bb, layer=6) == -8.0


def test_item_scope_covers_its_chain_only(bb, book):
    bb.preferences = [_pref("claim", -8.0, *CUCUMBER_CHAIN)]
    chop = _cand(T.Chop(order_id=1, resources=("board_1",),
                        item="Cucumber"), book)
    fetch = _cand(T.FetchToPlate(order_id=1, resources=("plate_1",),
                                 item="Cucumber"), book)
    fish = _cand(T.FetchToPlate(order_id=1, resources=("plate_1",),
                                item="SushiFish"), book)
    assert planner._preference_score(chop, bb, layer=6) == -8.0
    assert planner._preference_score(fetch, bb, layer=6) == -8.0
    assert planner._preference_score(fish, bb, layer=6) == 0.0


def test_item_match_is_case_insensitive(bb, book):
    bb.preferences = [_pref("claim", -8.0, CHOP_CUCUMBER)]
    cand = _cand(T.Chop(order_id=1, resources=("board_1",),
                        item="Cucumber"), book)
    assert planner._preference_score(cand, bb, layer=6) == -8.0


def test_exclude_carves_items_out_of_wildcard(bb, book):
    bb.preferences = [
        _pref("claim", -8.0, ("Chop", None), exclude=frozenset({CHOP_CUCUMBER})),
        _pref("direct", 4.0, CHOP_CUCUMBER),
    ]
    cucumber = _cand(T.Chop(order_id=1, resources=("board_1",),
                            item="Cucumber"), book)
    fish = _cand(T.Chop(order_id=1, resources=("board_1",),
                        item="SushiFish"), book)
    assert planner._preference_score(cucumber, bb, layer=6) == 4.0
    assert planner._claim_penalty(cucumber, bb, layer=6) == 0.0
    assert planner._preference_score(fish, bb, layer=6) == -8.0


def test_empty_scope_matches_nothing(bb, book):
    bb.preferences = [_pref("claim", -8.0)]
    cand = _cand(T.Chop(order_id=1, resources=("board_1",),
                        item="Cucumber"), book)
    assert planner._preference_score(cand, bb, layer=6) == 0.0


# ---------- 免疫规则 ----------

def test_l2_rescue_ignores_negative_claim_but_accepts_positive_direct(bb, book):
    rescue = _cand(T.RescueCooked(resources=("cooker_1",)), book)
    bb.preferences = [_pref("claim", -8.0, ("RescueCooked", None))]
    assert planner._preference_score(rescue, bb, layer=2) == 0.0
    bb.preferences = [_pref("direct", 4.0, ("RescueCooked", None))]
    assert planner._preference_score(rescue, bb, layer=2) == 4.0


def test_l1_ignores_negative_claim(bb, book):
    cand = _cand(T.TakeCookedToPlate(order_id=1, resources=("cooker_1",),
                                     item="BoiledSushiRice"), book)
    bb.preferences = [_pref("claim", -8.0,
                            ("TakeCookedToPlate", "boiledsushirice"))]
    assert planner._preference_score(cand, bb, layer=1) == 0.0


# ---------- 到期 ----------

def test_expired_preference_is_skipped(bb, book):
    cand = _cand(T.Chop(order_id=1, resources=("board_1",),
                        item="Cucumber"), book)
    bb.tick = 101
    bb.preferences = [_pref("claim", -8.0, CHOP_CUCUMBER, expires_tick=100)]
    assert planner._preference_score(cand, bb, layer=6) == 0.0


def test_preference_is_invalid_at_expires_tick(bb, book):
    """与 prune_expired_preferences 同界：tick == expires_tick 即失效。"""
    cand = _cand(T.Chop(order_id=1, resources=("board_1",),
                        item="Cucumber"), book)
    bb.preferences = [_pref("claim", -8.0, CHOP_CUCUMBER, expires_tick=100)]

    bb.tick = 99
    assert planner._preference_score(cand, bb, layer=6) == -8.0
    bb.tick = 100
    assert planner._preference_score(cand, bb, layer=6) == 0.0


def test_preference_with_zero_expiry_never_expires(bb, book):
    cand = _cand(T.Chop(order_id=1, resources=("board_1",),
                        item="Cucumber"), book)
    bb.preferences = [_pref("claim", -8.0, CHOP_CUCUMBER, expires_tick=0)]
    bb.tick = 10**6
    assert planner._preference_score(cand, bb, layer=6) == -8.0


def test_claim_penalty_counts_only_negative_claims(bb, book):
    cand = _cand(T.Chop(order_id=1, resources=("board_1",),
                        item="Cucumber"), book)
    bb.preferences = [
        _pref("claim", -8.0, CHOP_CUCUMBER),
        _pref("direct", 4.0, CHOP_CUCUMBER),
        _pref("stop", 0.0, CHOP_CUCUMBER),
    ]
    assert planner._claim_penalty(cand, bb, layer=6) == -8.0
    assert planner._preference_score(cand, bb, layer=6) == -4.0


# ---------- branch 归一 ----------

def test_branch_of_task_normalizes_cooked_and_chopped_names(book):
    cooked = T.FetchToCooker(order_id=1, resources=("cooker_1",),
                             item="BoiledSushiRice")
    raw = T.Prefetch(item="Cucumber")
    assert planner.branch_of_task(cooked, book) == "SushiRice"
    assert planner.branch_of_task(raw, book) == "Cucumber"


def test_branch_of_task_is_none_for_shared_and_missing_item(book):
    assert planner.branch_of_task(T.WashPlate(resources=("sink_1",)), book) is None
    assert planner.branch_of_task(T.Prefetch(), book) is None

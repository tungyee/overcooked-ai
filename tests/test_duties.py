# -*- coding: utf-8 -*-
"""分工账本：动作级作用域、op 应用与编译（ai/dialog/duties.py）。

目录与候选用入库真实帧 `dialog_scenarios.ndjson.gz`（寿司关：黄瓜/鱼/饭/海苔）。
"""
import gzip
import json
import os

import pytest

from ai import tasks as T
from ai.agent import _TASK_META
from ai.core import planner
from ai.dialog.actions import action_scope
from ai.dialog.duties import (HELP_REQUEST_COOLDOWN_TICKS, STOP_TTL_TICKS,
                              WEIGHT_NOW, Kitchen, Ledger, render_ledger,
                              tool_schemas)
from ai.dialog.schema import preference_matches_task
from ai.query.recipe_graph import _compile_node
from ai.query.recipes import RecipeBook
from ai.state.blackboard import Blackboard
from ai.state.world import World

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "dialog_scenarios.ndjson.gz")
MATERIALS = ["Seaweed", "BoiledSushiRice", "Cucumber", "SushiFish"]


def _stations(world):
    return {st.get("t") for st in world.stations if st.get("t")}


@pytest.fixture(scope="module")
def frame():
    with gzip.open(FIXTURE, "rt") as fh:
        return [json.loads(line) for line in fh][-1]


@pytest.fixture
def world(frame):
    return World(frame)


@pytest.fixture
def book(world):
    return RecipeBook(world.recipes, world.stations)


@pytest.fixture
def kitchen(world, book):
    return Kitchen.from_orders(book, world.orders, _compile_node, _TASK_META,
                               _stations(world))


def _candidates(world, book, bb=None):
    bb = bb or Blackboard()
    pm = planner._match_plates_to_orders(world, bb, book)
    cm = planner._match_cookers_to_orders(world, bb, book, pm)
    return planner.generate_candidates(world, bb, None, book,
                                       plate_match=pm, cooker_match=cm)


def _hits(pref, cands):
    return {(c.task.TYPE, c.task.item) for c in cands
            if preference_matches_task(pref, c.task)}


def _task(task_type, item=None):
    return type("T", (), {"TYPE": task_type, "item": item})()


def _kinds(ledger, task_type, item=None):
    return sorted(p.kind for p in ledger.preferences()
                  if preference_matches_task(p, _task(task_type, item)))


def _add(ledger, kitchen, mode, action, direction=None, item=None, tick=0):
    args = {"op": "add", "mode": mode, "action": action}
    if direction is not None:
        args["direction"] = direction
    if item is not None:
        args["item"] = item
    return ledger.apply(args, kitchen, tick)


# ------------------------------------------------------------ 目录

def test_targets_are_materials_then_steps_then_shared_actions(kitchen):
    t = kitchen.targets()
    assert t[:4] == MATERIALS
    assert set(t[-3:]) == {"WashPlate", "PlaceCleanPlate", "DeliverPlate"}
    steps = t[4:-3]
    assert all(":" in s for s in steps)
    assert "Chop:Cucumber" in steps
    assert "FetchToCooker:BoiledSushiRice" in steps


def test_kitchen_render_is_action_oriented(kitchen):
    text = kitchen.render()
    assert "Sushi_Cucumber = Seaweed + BoiledSushiRice + Cucumber" in text
    assert "切" in text and "下锅" in text and "装盘" in text
    assert "物料（target" not in text


def test_kitchen_render_single_dish(kitchen):
    text = kitchen.render(["Sushi_Fish"], shared=False)
    assert "Sushi_Fish" in text and "Sushi_Cucumber" not in text
    assert "切" in text and "下锅" in text


def test_every_registered_task_has_a_description():
    missing = [name for name, cls in T.TASK_TYPES.items()
               if not getattr(cls, "DESC", "") or not getattr(cls, "LABEL", "")]
    assert missing == []


def test_kitchen_without_sink_drops_wash(world, book):
    kitchen = Kitchen.from_orders(book, world.orders, _compile_node,
                                  _TASK_META, _stations(world) - {"washing"})
    assert "WashPlate" not in kitchen.targets()
    scope, reason = kitchen.expand("WashPlate")
    assert scope is None and "WashPlate" in reason


def test_expand_material_and_shared(kitchen):
    assert kitchen.expand("Cucumber")[0] == frozenset({(None, "cucumber")})
    assert kitchen.expand("DeliverPlate")[0] == frozenset({("DeliverPlate", None)})
    scope, reason = kitchen.expand("Chop")
    assert scope is None and "可选" in reason


def test_intermediate_items_join_the_material_scope():
    dag = {"type": "DeliverPlate", "item": "Pasta", "children": [{
        "type": "TakeCookedToPlate", "item": "Pasta_TomatoOnly", "children": [{
            "type": "FetchToCooker", "item": "Pasta_TomatoOnly", "children": [{
                "type": "Chop", "item": "Tomato", "children": [{
                    "type": "FetchToBoard", "item": "Tomato", "children": []}]}]}]}]}
    kitchen = Kitchen({"Pasta": dag}, _TASK_META)
    scope, _ = kitchen.expand("Pasta_TomatoOnly")
    assert scope == frozenset({(None, "pasta_tomatoonly"), (None, "tomato")})
    assert kitchen.processes("Pasta_TomatoOnly") == ["切", "煮"]


def test_every_material_is_hit_by_level_candidates(world, book):
    hit_items = {str(c.task.item).lower() for c in _candidates(world, book)
                 if c.task.item}
    for name in ("Seaweed", "Cucumber", "SushiFish"):
        assert name.lower() in hit_items


# ------------------------------------------------------------ 动作级账本 op

def test_apply_assign_p2_compiles_direct(kitchen):
    led = Ledger()
    r = _add(led, kitchen, "assign", "切", direction="P2")
    assert r["ok"] is True
    prefs = led.preferences()
    assert len(prefs) == 1 and prefs[0].kind == "direct" \
        and prefs[0].owner == "P2" and prefs[0].scope == action_scope("切")


def test_apply_assign_p1_compiles_claim_strong(kitchen):
    led = Ledger()
    _add(led, kitchen, "assign", "下锅", direction="P1")
    prefs = led.preferences()
    assert prefs[0].kind == "claim" and prefs[0].owner == "P1" \
        and prefs[0].weight == -6.0


def test_apply_cancel_removes_duty(kitchen):
    led = Ledger()
    _add(led, kitchen, "assign", "切", direction="P2")
    led.apply({"op": "cancel", "mode": "assign", "action": "切"}, kitchen, 0)
    assert led.preferences() == []


def test_apply_cancel_unknown_returns_reason(kitchen):
    led = Ledger()
    r = led.apply({"op": "cancel", "mode": "assign", "action": "切"}, kitchen, 0)
    assert r["ok"] is False and "没有" in r["reason"]


def test_apply_same_action_rewrites(kitchen):
    led = Ledger()
    _add(led, kitchen, "assign", "切", direction="P2")
    _add(led, kitchen, "assign", "切", direction="P1")
    prefs = led.preferences()
    assert len(prefs) == 1 and prefs[0].kind == "claim"
    assert led.ids() == ["a1"]


def test_apply_now_and_stop_compile(kitchen):
    led = Ledger()
    assert _add(led, kitchen, "now", "切", item="鸡块")["id"] == "a1"
    assert _add(led, kitchen, "stop", "下锅")["id"] == "a2"
    by_id = {p.duty_id: p for p in led.preferences()}
    assert (by_id["a1"].kind, by_id["a1"].owner, by_id["a1"].weight) == \
        ("now", "P2", WEIGHT_NOW)
    assert (by_id["a2"].kind, by_id["a2"].owner, by_id["a2"].weight) == \
        ("stop", "P2", 0.0)
    # now 带 item 的作用域精确到节点；stop 不带 item 是动作级
    assert by_id["a1"].scope == action_scope("切", "鸡块")
    assert by_id["a2"].scope == action_scope("下锅")


def test_apply_rejects_bad_calls(kitchen):
    led = Ledger()
    assert _add(led, kitchen, "assign", "切")["ok"] is False  # 缺 direction
    assert led.apply({"op": "add", "mode": "assign", "action": "不存在",
                      "direction": "P2"}, kitchen, 0)["ok"] is False
    assert led.apply({"op": "add", "mode": "dance", "action": "切"},
                     kitchen, 0)["ok"] is False
    assert led.apply("nonsense", kitchen, 0)["ok"] is False
    assert led.duties == []


def test_assign_action_scope_hits_its_tasks(kitchen):
    """assign(切) 的动作级作用域命中 Chop，不命中别的动作。"""
    led = Ledger()
    _add(led, kitchen, "assign", "切", direction="P2")
    assert _kinds(led, "Chop", "Cucumber") == ["direct"]
    assert _kinds(led, "FillCooker", "BoiledSushiRice") == []


# ------------------------------------------------------------ now 一次性消费

def test_consume_now_removes_matching_now(kitchen):
    led = Ledger()
    _add(led, kitchen, "now", "切", item="鸡块")
    assert led.consume_now(_task("Chop", "鸡块")) is True
    assert led.preferences() == []


def test_consume_now_keeps_non_matching_now(kitchen):
    led = Ledger()
    _add(led, kitchen, "now", "切", item="鸡块")
    _add(led, kitchen, "now", "切", item="土豆")
    assert led.consume_now(_task("Chop", "鸡块")) is True
    assert led.get("a2").mode == "now"       # 土豆未命中 → 保留


def test_consume_now_ignores_non_now_and_none(kitchen):
    led = Ledger()
    _add(led, kitchen, "assign", "切", direction="P2")
    assert led.consume_now(_task("Chop", "鸡块")) is False
    assert led.consume_now(None) is False
    assert len(led.duties) == 1


# ------------------------------------------------------------ planner 截止闸（不依赖账本）

def test_order_less_claim_can_still_be_rescued():
    cand = planner.Candidate(type("T", (), {"TYPE": "WashPlate",
                                            "order_id": None, "key": 1})(),
                             [], 1)
    slack = {"o1": 40.0, "o2": -5.0, "o3": 300.0}
    assert planner._candidate_cushion(cand, None, slack) == 40.0
    assert planner._tightest_order(slack) == "o1"
    assert planner._candidate_cushion(cand, None, {"o2": -5.0}) is None


# ------------------------------------------------------------ 渲染 / schema

def test_render_ledger_text_with_action():
    text = render_ledger([
        {"id": "a1", "mode": "assign", "action": "下锅", "direction": "P1",
         "source": "下锅我来", "impact": "挡住了你 3 个可做的活"},
        {"id": "a2", "mode": "stop", "action": "切", "item": "鸡块",
         "remaining_s": 12.0},
    ])
    assert "[a1] P1 负责 下锅  来源「下锅我来」" in text
    assert "此刻：挡住了你 3 个可做的活" in text
    assert "[a2] P2 先别做 切:鸡块（还剩 12.0 秒）" in text
    assert render_ledger([]) == "（空）"


def test_tool_schemas_action_enum_excludes_fetch_and_throw():
    tools = tool_schemas()
    props = tools[0]["function"]["parameters"]["properties"]
    enum = props["action"]["enum"]
    assert set(enum) == {"切", "下锅", "拌", "装盘", "送餐", "洗盘"}
    assert "FetchToBoard" not in enum and "ThrowBatch" not in enum
    assert props["mode"]["enum"] == ["now", "assign", "stop"]
    assert props["direction"]["enum"] == ["P2", "P1"]


def test_tool_schemas_drops_target_and_id():
    tools = tool_schemas()
    fn = tools[0]["function"]
    props = fn["parameters"]["properties"]
    assert "target" not in props and "id" not in props
    assert props["op"]["enum"] == ["add", "cancel"]
    assert set(fn["parameters"]["required"]) == {"op", "mode", "action"}


def test_ledger_help_request_compiles_to_action_claim():
    ledger = Ledger()
    ledger.add_help_request("Chop", "Cucumber", tick=0)
    claim = [p for p in ledger.preferences() if p.kind == "claim"]
    assert claim and claim[0].owner == "P1" and claim[0].weight == -6.0
    assert claim[0].scope == action_scope("切")  # 动作级，不是节点级


def test_ledger_help_request_fetch_keeps_node_scope():
    # 取料（FetchToBoard）无动作归属，保持节点级 claim
    ledger = Ledger()
    ledger.add_help_request("FetchToBoard", "Potato", tick=0)
    claim = [p for p in ledger.preferences() if p.kind == "claim"]
    assert claim and claim[0].scope == frozenset({("FetchToBoard", "potato")})


def test_ledger_help_request_expires_via_prune():
    ledger = Ledger()
    ledger.add_help_request("Chop", "Cucumber", tick=0, ttl_ticks=10)
    ledger.prune(10)
    assert ledger.help_request_ids() == []


def test_ledger_help_request_no_duplicate():
    ledger = Ledger()
    a = ledger.add_help_request("Chop", "Cucumber", tick=0)
    b = ledger.add_help_request("Chop", "Cucumber", tick=0)
    assert a == b and len(ledger.help_request_ids()) == 1


def test_ledger_help_request_cooldown_suppresses_repeat():
    ledger = Ledger()
    ledger.add_help_request("Chop", "Cucumber", tick=0)
    assert ledger.help_request_recent("Chop", "Cucumber", tick=10) is True
    assert ledger.help_request_recent("FetchToPlate", "Cucumber", tick=10) is False
    assert ledger.help_request_recent("Chop", "SushiFish", tick=10) is False
    assert ledger.help_request_recent(
        "Chop", "Cucumber", tick=HELP_REQUEST_COOLDOWN_TICKS) is False


def test_ledger_help_request_dedup_does_not_refresh_cooldown():
    ledger = Ledger()
    ledger.add_help_request("Chop", "Cucumber", tick=0)
    ledger.add_help_request("Chop", "Cucumber", tick=50)
    assert ledger.help_request_recent(
        "Chop", "Cucumber", tick=HELP_REQUEST_COOLDOWN_TICKS) is False


def test_step_target_expand(kitchen):
    scope, err = kitchen.expand("Chop:Cucumber")
    assert err == ""
    assert scope == frozenset({("Chop", "cucumber")})


def test_step_target_rejects_unknown_task(kitchen):
    _, err = kitchen.expand("NotATask:Cucumber")
    assert err != ""


# ------------------------------------------------------------ planner 硬/软分离（动作级 scope）

from ai.dialog.schema import Preference


def test_stop_never_vetoes_rescue():
    bb = Blackboard()
    bb.preferences = [Preference(kind="stop", owner="P2", weight=0.0,
                                 scope=action_scope("装盘"))]
    rescue = T.TakeCookedToPlate("9", (), "FriedChips")
    assert planner._is_stopped(rescue, bb) is False
    fetch_plate = T.FetchToPlate("9", (), "FriedChips")
    assert planner._is_stopped(fetch_plate, bb) is True


def test_item_level_now_scope_matches_only_that_item():
    pref = Preference(kind="now", owner="P2", weight=20.0,
                      scope=action_scope("切", "Chicken"))
    assert preference_matches_task(pref, _task("Chop", "Chicken")) is True
    assert preference_matches_task(pref, _task("Chop", "Potato")) is False


def test_claim_negative_weight_enters_scoring():
    bb = Blackboard()
    pref = Preference(kind="claim", owner="P1", weight=-6.0,
                      scope=action_scope("切"))
    bb.preferences = [pref]
    cand = planner.Candidate(_task("Chop", "Cucumber"), [], 1)
    matched = planner._matching_preferences(cand, bb, layer=6)
    assert any(w == -6.0 and p.kind == "claim" for p, w in matched)

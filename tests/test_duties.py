"""分工账本：物料目录、作用域、op 应用与编译（ai/dialog/duties.py）。

目录与候选用入库真实帧 `dialog_scenarios.ndjson.gz`（寿司关：黄瓜/鱼/饭/海苔）。
"""
import gzip
import json
import os

import pytest

from ai import tasks as T
from ai.agent import _TASK_META
from ai.core import planner
from ai.dialog.duties import (STOP_TTL_S, STOP_TTL_TICKS, WEIGHT_NOW, Kitchen,
                              Ledger, render_ledger, tool_schemas)
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


def _add(ledger, kitchen, mode, target, tick=0):
    return ledger.apply({"op": "add", "mode": mode, "target": target},
                        kitchen, tick)


# ------------------------------------------------------------ 目录


def test_targets_are_materials_then_shared_actions(kitchen):
    assert kitchen.targets()[:4] == MATERIALS
    assert set(kitchen.targets()[4:]) == {"WashPlate", "PlaceCleanPlate",
                                          "DeliverPlate"}


def test_render_lists_dishes_materials_steps_and_legend(kitchen):
    text = kitchen.render()
    assert "Sushi_Cucumber = Seaweed + BoiledSushiRice + Cucumber" in text
    # 加工方式标签与步骤短名都来自 task 类（PROCESS / LABEL），不写死
    assert "Seaweed（直接装盘）：%s" % (T.FetchToPlate.LABEL,) in text
    assert "BoiledSushiRice（要煮）：%s → %s" % (
        T.FetchToCooker.LABEL, T.TakeCookedToPlate.LABEL) in text
    assert "Cucumber（要切）：%s → %s → %s" % (
        T.FetchToBoard.LABEL, T.Chop.LABEL, T.FetchToPlate.LABEL) in text
    assert "WashPlate（%s）" % (T.WashPlate.LABEL,) in text
    # 救援动作不开放认领；在饭链里的盛盘随物料认领，不列进来
    auto = [l for l in text.splitlines() if l.startswith("不开放认领")][0]
    assert T.Extinguish.LABEL in auto
    assert T.TakeCookedToPlate.LABEL not in auto


def test_render_single_dish_without_shared(kitchen):
    text = kitchen.render(["Sushi_Fish"], shared=False)
    assert "Sushi_Fish" in text and "Sushi_Cucumber" not in text
    assert "SushiFish（要切）" in text and "Cucumber（" not in text
    assert "共享动作" not in text


def test_every_registered_task_has_a_description():
    """新增 task 类型必须写 DESC 与 LABEL，否则 LLM 只能猜英文类型名的语义。"""
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
    """熟料链上的中间产物（意面里的番茄）也属于这种物料。"""
    dag = {"type": "DeliverPlate", "item": "Pasta", "children": [{
        "type": "TakeCookedToPlate", "item": "Pasta_TomatoOnly", "children": [{
            "type": "FetchToCooker", "item": "Pasta_TomatoOnly", "children": [{
                "type": "Chop", "item": "Tomato", "children": [{
                    "type": "FetchToBoard", "item": "Tomato", "children": []}]}]}]}]}
    kitchen = Kitchen({"Pasta": dag}, _TASK_META)
    scope, _ = kitchen.expand("Pasta_TomatoOnly")
    assert scope == frozenset({(None, "pasta_tomatoonly"), (None, "tomato")})
    text = kitchen.render()
    assert "Pasta_TomatoOnly（要切、煮）：取料上砧板 Tomato → 切 Tomato → 下锅 → 盛盘" in text
    assert kitchen.processes("Pasta_TomatoOnly") == ["切", "煮"]
    # 这个 Kitchen 没给站点：洗盘做不了要明说
    assert "本关没有对应站点、做不了：洗盘（WashPlate）" in text


def test_every_material_is_hit_by_level_candidates(world, book):
    """物料作用域必须真的命中 planner 生成的候选（item 拼写与候选一致）。"""
    hit_items = {str(c.task.item).lower() for c in _candidates(world, book)
                 if c.task.item}
    for name in ("Seaweed", "Cucumber", "SushiFish"):
        assert name.lower() in hit_items


def test_material_scope_covers_prefetch_and_fill_cooker():
    kitchen = Kitchen({"S": {"type": "DeliverPlate", "item": "S", "children": [
        {"type": "TakeCookedToPlate", "item": "BoiledSushiRice", "children": [
            {"type": "FetchToCooker", "item": "BoiledSushiRice", "children": []}]},
        {"type": "FetchToPlate", "item": "Cucumber", "children": []}]}},
        _TASK_META)
    ledger = Ledger()
    _add(ledger, kitchen, "leave_to_p1", "BoiledSushiRice")
    _add(ledger, kitchen, "do", "Cucumber")
    assert _kinds(ledger, "FillCooker", "BoiledSushiRice") == ["claim"]
    assert _kinds(ledger, "TakeCookedToPlate", "BoiledSushiRice") == ["claim"]
    assert _kinds(ledger, "Prefetch", "Cucumber") == ["direct"]
    assert _kinds(ledger, "Chop", "SushiFish") == []


# ------------------------------------------------------------ 账本 op


def test_add_compiles_modes_to_preferences(kitchen):
    ledger = Ledger()
    assert _add(ledger, kitchen, "leave_to_p1", "Cucumber")["id"] == "a1"
    assert _add(ledger, kitchen, "do", "WashPlate")["id"] == "a2"
    by_id = {p.duty_id: p for p in ledger.preferences()}
    assert (by_id["a1"].kind, by_id["a1"].weight) == ("claim", -8.0)
    assert (by_id["a2"].kind, by_id["a2"].weight) == ("direct", 4.0)


def test_deliver_plate_accepts_only_now_and_do(kitchen):
    """送餐只开放 now/do：不能「交给 P1」，也不能「先别做」。"""
    ledger = Ledger()
    assert _add(ledger, kitchen, "now", "DeliverPlate")["ok"] is True
    assert _add(ledger, kitchen, "do", "DeliverPlate")["ok"] is True
    r1 = _add(ledger, kitchen, "leave_to_p1", "DeliverPlate")
    assert r1["ok"] is False and "DeliverPlate" in r1["reason"]
    r2 = _add(ledger, kitchen, "stop", "DeliverPlate")
    assert r2["ok"] is False and "DeliverPlate" in r2["reason"]
    # 被拒的调用不新增约定：仍只有 do 一条，且不产生 claim/stop
    assert [d.mode for d in ledger.duties] == ["do"]
    assert {p.kind for p in ledger.preferences()} == {"direct"}


def test_other_shared_actions_keep_all_modes(kitchen):
    """限制只针对送餐：洗盘 / 摆盘仍可交给 P1 或叫停。"""
    ledger = Ledger()
    assert _add(ledger, kitchen, "leave_to_p1", "WashPlate")["ok"] is True
    assert _add(ledger, kitchen, "stop", "PlaceCleanPlate")["ok"] is True


def test_render_marks_deliver_mode_restriction(kitchen):
    text = kitchen.render()
    assert "DeliverPlate（送餐）（只接受：P2 马上去、P2 优先做）" in text


def test_add_now_and_stop_compile_to_preferences(kitchen):
    ledger = Ledger()
    assert _add(ledger, kitchen, "now", "Cucumber")["id"] == "a1"
    assert _add(ledger, kitchen, "stop", "SushiFish")["id"] == "a2"
    by_id = {p.duty_id: p for p in ledger.preferences()}
    assert (by_id["a1"].kind, by_id["a1"].owner, by_id["a1"].weight) == \
        ("now", "P2", WEIGHT_NOW)
    assert (by_id["a2"].kind, by_id["a2"].owner, by_id["a2"].weight) == \
        ("stop", "P2", 0.0)


def test_add_on_same_target_rewrites_it(kitchen):
    ledger = Ledger()
    _add(ledger, kitchen, "leave_to_p1", "Cucumber")
    again = _add(ledger, kitchen, "leave_to_p1", "Cucumber")
    assert again["unchanged"] and ledger.ids() == ["a1"]
    flipped = _add(ledger, kitchen, "do", "Cucumber")
    assert flipped["id"] == "a1" and ledger.ids() == ["a1"]
    assert ledger.get("a1").mode == "do"


def test_edit_keeps_id_and_changes_given_fields(kitchen):
    ledger = Ledger()
    _add(ledger, kitchen, "leave_to_p1", "Cucumber")
    result = ledger.apply({"op": "edit", "id": "a1", "mode": "do"}, kitchen, 5)
    assert result["ok"] and result["id"] == "a1"
    assert (ledger.get("a1").mode, ledger.get("a1").target) == ("do", "Cucumber")
    moved = ledger.apply({"op": "edit", "id": "a1", "target": "SushiFish"},
                         kitchen, 6)
    assert moved["ok"] and ledger.get("a1").target == "SushiFish"


def test_edit_onto_existing_target_replaces_it(kitchen):
    ledger = Ledger()
    _add(ledger, kitchen, "do", "Cucumber")
    _add(ledger, kitchen, "leave_to_p1", "SushiFish")
    ledger.apply({"op": "edit", "id": "a1", "target": "SushiFish"}, kitchen, 0)
    assert ledger.ids() == ["a1"] and ledger.get("a1").mode == "do"


def test_remove_and_unknown_id(kitchen):
    ledger = Ledger()
    _add(ledger, kitchen, "do", "WashPlate")
    assert ledger.apply({"op": "remove", "id": "a1"}, kitchen, 0)["ok"]
    assert ledger.duties == []
    bad = ledger.apply({"op": "remove", "id": "a9"}, kitchen, 0)
    assert bad["ok"] is False and "a9" in bad["reason"]


def test_bad_calls_are_rejected_and_ledger_unchanged(kitchen):
    ledger = Ledger()
    assert ledger.apply({"op": "add", "mode": "do", "target": "Chop"},
                        kitchen, 0)["ok"] is False
    assert ledger.apply({"op": "add", "mode": "dance", "target": "Cucumber"},
                        kitchen, 0)["ok"] is False
    assert ledger.apply({"op": "add", "mode": "do"}, kitchen, 0)["ok"] is False
    assert ledger.apply("nonsense", kitchen, 0)["ok"] is False
    assert ledger.duties == []


def test_claiming_material_with_rescue_step_warns(kitchen):
    """L1/L2 免疫负分不改：认领饭链时提醒「锅刚熟/快糊时 P2 仍会去盛」。"""
    ledger = Ledger()
    result = _add(ledger, kitchen, "leave_to_p1", "BoiledSushiRice")
    assert result["ok"] and "TakeCookedToPlate" in result["warning"]
    assert "warning" not in _add(ledger, kitchen, "leave_to_p1", "Cucumber")
    assert "warning" not in _add(ledger, kitchen, "do", "BoiledSushiRice")


def test_newer_duty_wins_on_shared_items():
    """两种物料共用中间产物时，新条目优先（旧条目 exclude 掉重叠的 item）。"""
    dag = {"type": "DeliverPlate", "item": "D", "children": [
        {"type": "FetchToPlate", "item": "Salad", "children": [
            {"type": "Chop", "item": "Tomato", "children": []}]},
        {"type": "TakeCookedToPlate", "item": "Soup", "children": [
            {"type": "Chop", "item": "Tomato", "children": []}]}]}
    kitchen = Kitchen({"D": dag}, _TASK_META)
    ledger = Ledger()
    _add(ledger, kitchen, "leave_to_p1", "Salad")
    _add(ledger, kitchen, "do", "Soup")
    assert _kinds(ledger, "Chop", "Tomato") == ["direct"]
    assert _kinds(ledger, "FetchToPlate", "Salad") == ["claim"]


# ------------------------------------------------------------ now 一次性消费

def test_consume_now_downgrades_matching_now_to_do(kitchen):
    """「马上去」落实一次就降级成 do：命中的 now 不再长期存在。"""
    ledger = Ledger()
    _add(ledger, kitchen, "now", "Cucumber")
    assert ledger.get("a1").mode == "now"

    assert ledger.consume_now(_task("Chop", "Cucumber")) is True
    assert ledger.get("a1").mode == "do"


def test_consume_now_keeps_non_matching_now(kitchen):
    """只降级命中的 now，没命中的 now 保留。"""
    ledger = Ledger()
    _add(ledger, kitchen, "now", "Cucumber")
    _add(ledger, kitchen, "now", "Seaweed")

    assert ledger.consume_now(_task("Chop", "Cucumber")) is True
    assert ledger.get("a1").mode == "do"       # Cucumber 命中 → 降级
    assert ledger.get("a2").mode == "now"      # Seaweed 未命中 → 保留


def test_consume_now_ignores_non_now_and_none(kitchen):
    """do 不受影响；task 为 None 直接不改。"""
    ledger = Ledger()
    _add(ledger, kitchen, "do", "Cucumber")
    assert ledger.consume_now(_task("Chop", "Cucumber")) is False
    assert ledger.consume_now(None) is False
    assert ledger.get("a1").mode == "do"


# ------------------------------------------------------------ planner


def test_material_claim_hits_its_chain_not_others(world, book, kitchen):
    ledger = Ledger()
    _add(ledger, kitchen, "leave_to_p1", "Cucumber")
    hits = _hits(ledger.preferences()[0], _candidates(world, book))
    assert hits and all(item == "Cucumber" for _t, item in hits)
    assert ("FetchToBoard", "Cucumber") in hits
    assert ("Prefetch", "Cucumber") in hits


def test_ledger_changes_planner_choice(world, book, kitchen):
    """账本必须真的改变 P2 的选择：基线送餐 → 「黄瓜马上去」后取黄瓜。"""
    before, _ = planner.plan(world, Blackboard(), None, book)
    assert before.TYPE == "DeliverPlate"

    bb = Blackboard()
    ledger = Ledger()
    _add(ledger, kitchen, "now", "Cucumber")
    bb.preferences[:] = ledger.preferences()
    after, _ = planner.plan(world, bb, None, book)
    assert (after.TYPE, after.item) == ("FetchToBoard", "Cucumber")


def test_deliver_cannot_be_claimed_away(world, book, kitchen):
    """「送餐我来」被拒（送餐只接受 now/do）：不产生认领偏好，P2 仍可选送餐。"""
    before, _ = planner.plan(world, Blackboard(), None, book)
    assert before.TYPE == "DeliverPlate"

    ledger = Ledger()
    assert _add(ledger, kitchen, "leave_to_p1", "DeliverPlate")["ok"] is False
    assert ledger.preferences() == []

    bb = Blackboard()
    bb.preferences[:] = ledger.preferences()
    after, _ = planner.plan(world, bb, None, book)
    assert after.TYPE == "DeliverPlate"


def test_order_less_claim_can_still_be_rescued():
    """「我来洗碗」长期认领：最紧的单进入最后窗口时，截止闸仍能把洗盘抢回。"""
    cand = planner.Candidate(type("T", (), {"TYPE": "WashPlate",
                                            "order_id": None, "key": 1})(),
                             [], 1)
    slack = {"o1": 40.0, "o2": -5.0, "o3": 300.0}
    assert planner._candidate_cushion(cand, None, slack) == 40.0
    assert planner._tightest_order(slack) == "o1"
    assert planner._candidate_cushion(cand, None, {"o2": -5.0}) is None


# ------------------------------------------------------------ 渲染 / schema


def test_render_ledger_text_with_impact():
    text = render_ledger([
        {"id": "a1", "mode": "leave_to_p1", "target": "BoiledSushiRice",
         "source": "煮饭我来", "impact": "挡住了你 3 个可做的活"},
        {"id": "a2", "mode": "stop", "target": "Cucumber", "remaining_s": 12.0},
    ])
    assert "[a1] 交给 P1 BoiledSushiRice  来源「煮饭我来」" in text
    assert "此刻：挡住了你 3 个可做的活" in text
    assert "[a2] P2 先别做 Cucumber（还剩 12.0 秒）" in text
    assert render_ledger([]) == "（空）"


def test_tool_schemas_use_targets_from_level(kitchen):
    tools = tool_schemas(kitchen.targets(), ["a1"])
    assert [t["function"]["name"] for t in tools] == ["update_duties"]
    props = tools[0]["function"]["parameters"]["properties"]
    assert props["target"]["enum"] == kitchen.targets()
    assert props["id"]["enum"] == ["a1"]
    assert props["mode"]["enum"] == ["do", "leave_to_p1", "now", "stop"]
    assert "node" not in props and "upstream" not in props
    assert "now" in props["mode"]["description"]
    assert "stop" in props["mode"]["description"]


def test_tool_schemas_without_duties(kitchen):
    tools = tool_schemas(kitchen.targets(), [])
    assert "enum" not in tools[0]["function"]["parameters"]["properties"]["id"]

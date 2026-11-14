# -*- coding: utf-8 -*-
"""新工具协议：execute_action / cancel_action 与命令队列。"""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from ai.agent import Agent
from ai.dialog import llm
from ai.dialog.commands import (command_done, command_done_undecidable,
                               dirty_count)
from ai.query.recipes import RecipeBook
from ai.state.world import World


FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "full_level.ndjson")


def _world():
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        raw = json.loads(fh.readline())
    raw["orders"] = [{
        "id": "1", "dish": "Sushi_Cucumber",
        "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"],
        "remaining": 60, "lifetime": 130,
    }]
    return World(raw)


@pytest.fixture
def agent(world):
    with patch("ai.agent.StateClient", lambda *a, **k: MagicMock()):
        a = Agent(voice=None)
    a.bb.tick = 100
    a.book = RecipeBook(world.recipes, world.stations)
    return a


@pytest.fixture
def world():
    return _world()


def test_execute_action_creates_persistent_command(agent, world):
    out = agent._execute_action(world, {"action": "chop", "target": "Cucumber"})
    assert out["ok"] is True and out["status"] == "running"
    cmd = agent.commands.active()
    assert cmd is not None
    assert cmd.action == "chop" and cmd.resolved_target == "Cucumber"
    assert ("FetchToBoard", "cucumber") in cmd.scope
    assert ("Chop", "cucumber") in cmd.scope
    assert agent.bb.command_scope == cmd.scope


def test_execute_actions_queues_in_order(agent, world):
    """execute_actions 按数组顺序逐条校验/入队，返回逐条结果。"""
    out = agent._execute_actions(world, {"actions": [
        {"action": "chop", "target": "Cucumber"},
        {"action": "chop", "target": "Cucumber"},
    ]})
    assert out["ok"] is True
    assert len(out["results"]) == 2
    assert all(r.get("ok") for r in out["results"])
    assert out["results"][0]["action"] == "chop"
    assert out["results"][1]["action"] == "chop"
    assert len(agent.commands.commands) == 1   # 第二条 dedupe 到同一条命令


def test_execute_actions_rejects_empty_array(agent, world):
    out = agent._execute_actions(world, {"actions": []})
    assert out["ok"] is False
    assert "非空数组" in out["reason"]


def test_execute_action_dedupes_same_command(agent, world):
    first = agent._execute_action(world, {"action": "chop", "target": "Cucumber"})
    second = agent._execute_action(world, {"action": "chop", "target": "Cucumber"})
    assert second["deduped"] is True
    assert second["command_id"] == first["command_id"]
    assert len(agent.commands.commands) == 1


def test_cancel_action_clears_queue_and_holds(agent, world):
    agent._execute_action(world, {"action": "chop", "target": "Cucumber"})
    out = agent._cancel_action(world, {})
    assert out["ok"] is True and out["cancelled"]
    assert agent.commands.commands == []
    assert agent.bb.command_scope is None
    assert agent.bb.hold_until >= 100


def test_command_done_when_chopped_item_appears(agent, world):
    agent._execute_action(world, {"action": "chop", "target": "Cucumber"})
    cmd = agent.commands.active()
    assert command_done(world, agent.book, cmd) is False
    world.workstations = [{"n": "board_01", "item": "ChoppedCucumber"}]
    assert command_done(world, agent.book, cmd) is True


def test_wash_done_only_after_dirty_count_drops(agent, world):
    world.stations.append({"t": "washing", "n": "sink", "plate_count": 1})
    agent._execute_action(world, {"action": "wash"})
    cmd = agent.commands.active()
    assert cmd.start_dirty == 1
    assert dirty_count(world) == 1
    assert command_done(world, agent.book, cmd) is False
    world.stations[-1]["plate_count"] = 0
    assert command_done(world, agent.book, cmd) is True


def test_wash_not_done_while_holding_dirty_plate(agent, world):
    """捡起脏盘摞不等于洗完：dirty_count / command_done 要算手持物。

    trace-20261108-164955 t=1932：P2 刚 pick 起 DirtyPlateStack，items[]
    里的脏盘消失、脏盘计数 1→0，wash 命令被误标 succeeded 并移出队列。
    """
    world.items = [{"n": "DirtyPlateStack", "x": 0.0, "z": 0.0}]
    agent._execute_action(world, {"action": "wash"})
    cmd = agent.commands.active()
    assert cmd.start_dirty == 1
    world.items = []
    world.held_items = [{"n": "DirtyPlateStack", "held_by": "Player 2"}]
    assert dirty_count(world) == 1
    assert command_done(world, agent.book, cmd) is False
    # 洗完：手里没了、池子也空
    world.held_items = []
    assert command_done(world, agent.book, cmd) is True


def _cook_cmd(target):
    from ai.dialog.commands import Command
    return Command(id="cmd1", action="cook", target=target,
                   resolved_target=target, display_target=target,
                   scope=frozenset())


def test_cook_command_done_once_ingredient_is_in_the_pot(agent, world):
    """煮/煎命令按「下锅」判完成：料进锅就结束，不等熟、不等端盘。

    实机 trace-20261108-182610：熟意面 t=1959 已装盘，但盘内容只报
    `CookedCompositeAssembledNode`，旧的 item_present 认不出来，cmd1 永远
    不结束，独占执行槽把后面的「煮番茄」饿死。
    """
    world.cookers = [{"n": "cooker_01", "pot_contents": ["ing:Pasta"],
                      "isCooking": True}]
    assert command_done(world, agent.book, _cook_cmd("BoiledPasta")) is True


def test_cook_command_done_once_chopped_ingredient_is_in_the_pan(agent, world):
    """煎料：锅里是切好产物（ChoppedPastaTomato）也算下锅。"""
    world.cookers = [{"n": "cooker_02",
                      "pot_contents": ["ing:ChoppedPastaTomato"],
                      "isCooking": True}]
    assert command_done(world, agent.book,
                        _cook_cmd("PanFriedTomatoes")) is True


def test_cook_command_not_done_when_pot_has_other_ingredient(agent, world):
    """锅里有别的东西不算做完（不能「看到锅在煮」就报完成）。"""
    world.cookers = [{"n": "cooker_01", "pot_contents": ["ing:PastaTomato"],
                      "isCooking": True}]
    assert command_done(world, agent.book, _cook_cmd("BoiledPasta")) is False


def test_prompt_has_no_internal_codes(world):
    text = llm.system_message("Sushi_Cucumber = Seaweed + Cucumber")["content"]
    assert "P1" not in text and "P2" not in text
    state = llm.state_message("speech", {
        "orders": [{"id": "1", "dish": "Sushi_Cucumber", "remaining": 60}],
        "p1": {"holding": "Seaweed"}, "p2": {"doing": "切"},
    })["content"]
    assert "你" in state
    assert "P1" not in state and "P2" not in state


def test_expired_order_fails_and_pauses_queue(agent, world):
    agent.commands.add("deliver", "Sushi_Cucumber",
                       frozenset({("DeliverPlate", None)}), 100,
                       order_id="1", display_target="Sushi_Cucumber",
                       mode="dish")
    world.orders = []
    agent._service_commands(world)
    cmd = agent.commands.get("cmd1")
    assert cmd.status == "failed"
    assert "过期" in cmd.reason
    assert agent.commands.paused is True
    assert agent.bb.command_scope is None


def test_raw_item_resolves_to_material_for_balloon_level():
    """Balloon 关的物料名是熟料名（PanFriedPrawns），玩家/模型说生料名
    （Prawn / 虾）时必须能归一到对应 material。"""
    from ai.dialog.commands import (build_scope, resolve_done_item,
                                    resolve_target, valid_targets)
    from ai.dialog.catalog import Kitchen, Material

    kitchen = Kitchen({}, {})
    kitchen.materials = {
        "PanFriedPrawns": Material(
            "PanFriedPrawns", items={"prawn", "panfriedprawns"},
            types={"FetchToBoard", "Chop", "FetchToCooker"},
            steps=[("FetchToBoard", "Prawn"), ("Chop", "Prawn"),
                   ("FetchToCooker", "PanFriedPrawns")]),
        "FriedBurritoMeat": Material(
            "FriedBurritoMeat", items={"burritomeat", "friedburritomeat"},
            types={"FetchToBoard", "Chop", "FetchToCooker"},
            steps=[("FetchToBoard", "BurritoMeat"), ("Chop", "BurritoMeat"),
                   ("FetchToCooker", "FriedBurritoMeat")]),
    }
    kitchen.dish_materials = {
        "Pasta_Marinara_New": ["PanFriedPrawns"],
        "Pasta_MeatOnly_New": ["FriedBurritoMeat"],
    }

    targets = valid_targets(kitchen)
    assert "Prawn" in targets and "BurritoMeat" in targets

    mode, resolved, display, reason, _valid = resolve_target(
        kitchen, "chop", "Prawn")
    assert (mode, resolved, display, reason) == (
        "material", "PanFriedPrawns", "Prawn", "")
    scope, _ = build_scope(kitchen, "chop", resolved, mode)
    assert ("FetchToBoard", "prawn") in scope
    assert ("Chop", "prawn") in scope
    assert resolve_done_item(kitchen, "chop", resolved, "Prawn") == "Prawn"

    mode, resolved, _d, reason, _v = resolve_target(kitchen, "chop", "肉")
    assert (mode, resolved, reason) == ("material", "FriedBurritoMeat", "")

    # 模型偶尔输出复数 Prawns；单数兜底到同一 material。
    mode, resolved, _d, _r, _v = resolve_target(kitchen, "chop", "Prawns")
    assert (mode, resolved) == ("material", "PanFriedPrawns")


def test_scope_empty_keeps_active_task_in_command_scope(agent, world):
    """命令 scope 暂时没候选时，正在跑的同 scope 树不能切走。

    否则候选被自己的在途认领压掉 → 标 waiting → 改选 scope 外 → 下一拍
    认领释放候选又回来，每拍重建树，P2 原地抖动（trace-20261108-161828）。
    """
    scope = frozenset({("FetchToCooker", "friedmushrooms")})
    result = agent.commands.add("cook", "FriedMushrooms", scope, 100,
                                display_target="FriedMushrooms")
    cmd_id = result["command_id"]
    agent.bb.command_scope = scope
    agent.bb.command_id = cmd_id

    class _Active(object):
        TYPE = "FetchToCooker"
        item = "FriedMushrooms"

    active = _Active()
    agent.executor_inst.active_task = active

    chosen, record = agent._scope_empty_fallback(
        world, {"candidates": [], "command_scope_empty": True})

    assert chosen is active
    assert agent.bb.command_scope == scope
    assert agent.bb.command_id == cmd_id
    assert agent.commands.get(cmd_id).status != "waiting"


def test_scope_empty_yields_when_active_task_out_of_scope(agent, world):
    """在跑的树不在命令 scope 内：让位、标 waiting，按无 scope 重规划。"""
    scope = frozenset({("FetchToCooker", "friedmushrooms")})
    result = agent.commands.add("cook", "FriedMushrooms", scope, 100,
                                display_target="FriedMushrooms")
    cmd_id = result["command_id"]
    agent.bb.command_scope = scope
    agent.bb.command_id = cmd_id

    class _Active(object):
        TYPE = "DeliverPlate"
        item = "FriedMushrooms"

    agent.executor_inst.active_task = _Active()
    sentinel = object()
    with patch("ai.agent.planner.plan",
               return_value=(sentinel, {"candidates": []})):
        chosen, _record = agent._scope_empty_fallback(
            world, {"candidates": [], "command_scope_empty": True})

    assert chosen is sentinel
    assert agent.bb.command_scope is None
    assert agent.bb.command_id is None
    assert agent.commands.get(cmd_id).status == "waiting"


def test_pizza_chop_targets_are_item_scoped(monkeypatch):
    """同一道披萨的不同料不能被 material 级去重合并成一条命令。"""
    import gzip

    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "wizard",
                           "wizard32-20261101-170754-cooked-pizza.ndjson.gz")
    with gzip.open(fixture, "rt", encoding="utf8") as fh:
        world = World(json.loads(fh.readline()))
    monkeypatch.setattr("ai.agent.StateClient", lambda *a, **k: MagicMock())
    agent = Agent(voice=None)
    agent.bb.tick = 100
    agent.book = RecipeBook(world.recipes, world.stations)

    out = agent._execute_actions(world, {"actions": [
        {"action": "chop", "target": "Dough"},
        {"action": "chop", "target": "Tomato"},
        {"action": "chop", "target": "Cheese"},
    ]})

    ids = [row["command_id"] for row in out["results"]]
    assert len(set(ids)) == 3
    scopes = {c.id: c.scope for c in agent.commands.commands}
    assert ("Chop", "dough") in scopes["cmd1"]
    assert ("Chop", "tomato") in scopes["cmd2"]
    assert ("Chop", "cheese") in scopes["cmd3"]


def test_execute_actions_rejected_on_autonomous_event(agent, world):
    """自主事件（division/review 等）不能自己派活。"""
    from ai.dialog.worker import DutyBatch

    batch = DutyBatch(
        batch_id="b", origin="division",
        calls=[("execute_actions", {"actions": [
            {"action": "chop", "target": "Cucumber"}]})])
    out = agent._apply_duty_batch(world, batch)[0]
    assert out["ok"] is False and out["silent"] is True
    assert "planner" in out["reason"]
    assert agent.commands.commands == []


def test_execute_actions_allowed_on_player_speech(agent, world):
    from ai.dialog.worker import DutyBatch

    batch = DutyBatch(
        batch_id="b", origin="speech",
        calls=[("execute_actions", {"actions": [
            {"action": "chop", "target": "Cucumber"}]})])
    out = agent._apply_duty_batch(world, batch)[0]
    assert out["ok"] is True
    assert len(agent.commands.commands) == 1


def test_command_without_done_signal_finishes_after_one_task(agent, world):
    """判不出完成条件的命令（target 是菜名）做过一次就收工。

    实机 trace-20261109-124818：`execute_actions([{action:"chop"}])` 不带
    target，最后落在「给这道菜切所有料」的 dish scope，target=菜名 →
    `chop_target(菜名)` 是 None → command_done 永远 False，命令占着执行槽
    212/240 秒，planner 被锁在备料里，披萨组装不起来。
    """
    from ai.dialog.commands import command_done_undecidable

    scope = frozenset({("Chop", None)})
    agent.commands.add("chop", None, scope, 100)
    cmd = agent.commands.active()
    assert command_done_undecidable(cmd, agent.book) is True

    # 还没做过任何事：不能凭空成功
    agent._service_commands(world)
    assert cmd.status == "running"

    # 成功做过一次本 scope 的任务之后，这一拍收尾
    cmd.served = True
    agent._service_commands(world)
    assert cmd.status == "succeeded"


def test_targetless_chop_is_rejected_with_division_hint():
    """不带 target 的 chop / cook 直接拒，并提示改分工用 set_division。

    披萨关卡的 materials 以菜名为键，targetless chop 会被 resolve_target 折回
    整道菜，scope 变成「给这道菜备齐料」、target 是菜名 → command_done 永远
    False（实机 trace-20261109-124818 04:49:12 那条命令占了 212/240 秒）。
    玩家说「你也帮我切点东西吧」本来也是改分工，不是马上派活。
    """
    import gzip

    path = os.path.join(os.path.dirname(__file__), "fixtures", "wizard",
                        "wizard32-20261101-173006-board-base.ndjson.gz")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        raw = json.loads(fh.readline())
    world = World(raw)
    with patch("ai.agent.StateClient", lambda *a, **k: MagicMock()):
        a = Agent(voice=None)
    a.bb.tick = 100
    a.book = RecipeBook(world.recipes, world.stations)

    for action in ("chop", "cook"):
        out = a._execute_action(world, {"action": action})
        assert out["ok"] is False, out
        assert "set_division" in out["reason"]
    assert a.commands.active() is None


def test_running_command_expires_after_ttl(agent, world):
    """命令在跑也有一条寿命上限：卡住必须把执行槽还回来。

    实机 trace-20261109-124818：一条判不出完成条件的 `chop` 占了 212/240 秒，
    planner 被锁在备料 scope 里、两个灶全程空转。
    """
    from ai.dialog import commands as CM

    scope = frozenset({("Chop", None)})
    agent.commands.add("chop", None, scope, 100)
    cmd = agent.commands.active()
    assert cmd.status == "running"

    # 还没到上限：不动
    agent.bb.tick = 100 + CM.COMMAND_TTL_TICKS
    agent._service_commands(world)
    assert cmd.status in ("running", "waiting")

    # 超过上限：判失败，队列暂停，执行槽释放
    agent.bb.tick = 100 + CM.COMMAND_TTL_TICKS + 1
    agent._service_commands(world)
    assert cmd.status == "failed"
    assert "太久" in cmd.reason
    assert agent.bb.command_scope is None

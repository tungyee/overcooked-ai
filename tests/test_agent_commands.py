# -*- coding: utf-8 -*-
"""新工具协议：check_action / execute_action / cancel_action 与命令队列。"""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from ai.agent import Agent
from ai.dialog import llm
from ai.dialog.commands import command_done, dirty_count
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


def test_check_action_reports_real_targets(agent, world):
    out = agent._check_action(world, {"action": "chop", "target": "Cucumber"})
    assert out == {"ok": True, "action": "chop", "target": "Cucumber",
                   "resolved_target": "Cucumber"}

    out = agent._check_action(world, {"action": "chop", "target": "Seaweed"})
    assert out["ok"] is False
    assert "不用切" in out["reason"]


def test_check_action_chinese_alias(agent, world):
    out = agent._check_action(world, {"action": "chop", "target": "黄瓜"})
    assert out["ok"] is True
    assert out["resolved_target"] == "Cucumber"


def test_check_wash_without_dirty_plates_is_not_done(agent, world):
    out = agent._check_action(world, {"action": "wash"})
    assert out["ok"] is False
    assert "没有脏盘" in out["reason"]


def test_execute_action_creates_persistent_command(agent, world):
    out = agent._execute_action(world, {"action": "chop", "target": "Cucumber"})
    assert out["ok"] is True and out["status"] == "running"
    cmd = agent.commands.active()
    assert cmd is not None
    assert cmd.action == "chop" and cmd.resolved_target == "Cucumber"
    assert ("FetchToBoard", "cucumber") in cmd.scope
    assert ("Chop", "cucumber") in cmd.scope
    assert agent.bb.command_scope == cmd.scope


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


def test_prompt_has_no_internal_codes(world):
    text = llm.system_message("Sushi_Cucumber = Seaweed + Cucumber")["content"]
    assert "P1" not in text and "P2" not in text
    state = llm.state_message("speech", {
        "orders": [{"id": "1", "dish": "Sushi_Cucumber", "remaining": 60}],
        "p1": {"holding": "Seaweed"}, "p2": {"doing": "切"},
    })["content"]
    assert "队友" in state and "你" in state
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
    from ai.dialog.duties import Kitchen, Material

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

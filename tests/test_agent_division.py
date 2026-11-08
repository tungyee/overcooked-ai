# -*- coding: utf8 -*-
"""主循环分工接入：开局事件、set_division 落地、动态复核触发。"""
import queue
from unittest.mock import MagicMock

import pytest

from ai.agent import Agent
from ai.query.recipes import RecipeBook
from ai.state.world import World
from ai.dialog.worker import DutyBatch


class FakeWorker(object):
    def __init__(self):
        self.cmd_q = queue.Queue()
        self.ack_q = queue.Queue()
        self.ctx_q = queue.Queue(maxsize=1)
        self.event_q = queue.Queue(maxsize=1)
        self.contexts = []
        self.events = []
        self.level_events = []
        self.reset_count = 0

    def put_event(self, event):
        self.events.append(event)
        while True:
            try:
                self.event_q.get_nowait()
            except queue.Empty:
                break
        self.event_q.put_nowait(event)

    def publish_context(self, context):
        self.contexts.append(context)
        while True:
            try:
                self.ctx_q.get_nowait()
            except queue.Empty:
                break
        self.ctx_q.put_nowait(context)

    def reset_state(self):
        self.reset_count += 1

    def log_event(self, kind, **fields):
        if kind == "level_start":
            self.level_events.append((kind, fields.get("level")))


class FakeGuide(object):
    title = "测试攻略"

    @staticmethod
    def summary():
        return "打法：你煎，玩家装盘。"

    @staticmethod
    def section(name):
        return {"division": "你：煎。玩家：装盘。",
                "mechanics": "地图。", "traps": "别糊。",
                "recipes": "汉堡。"}.get(name, "")

    @staticmethod
    def division_text():
        return "你：煎。玩家：装盘。"


def make_world(level="Wizard_5_5_2P", scene_epoch=1):
    return World({
        "phase": "level",
        "level": level,
        "session_id": "s",
        "scene_epoch": scene_epoch,
        "orders": [{"id": 1, "dish": "Burger", "composition": ["Meat"],
                    "remaining": 60, "lifetime": 130}],
        "recipes": [{"name": "Meat", "comp": []}],
        "stations": [
            {"t": "chopping", "n": "board", "gx": 1, "gz": 1,
             "x": 1.2, "z": 1.2},
            {"t": "cooking", "n": "cooker", "gx": 2, "gz": 2,
             "x": 2.4, "z": 2.4},
            {"t": "plate_station", "n": "pass", "gx": 3, "gz": 3,
             "x": 3.6, "z": 3.6},
            {"t": "washing", "n": "sink", "gx": 4, "gz": 4,
             "x": 4.8, "z": 4.8},
        ],
        "chefs": [
            {"name": "Player 1", "x": 0.0, "z": 0.0, "held": None},
            {"name": "Player 2", "x": 1.0, "z": 1.0, "held": None},
        ],
        "items": [],
        "extinguishers": [],
        "plates": [],
        "cookers": [],
        "grid": {"size": [1.2, 1.0, 1.2], "nodeMap": None},
    })


@pytest.fixture
def worker():
    return FakeWorker()


@pytest.fixture
def agent(monkeypatch, worker):
    monkeypatch.setattr("ai.agent.StateClient", lambda *a, **k: MagicMock())
    monkeypatch.setattr("ai.dialog.help.cost_to",
                        lambda world, bb, name, source: 1)
    fake_book = MagicMock()
    fake_book.resolve.return_value = FakeGuide()
    monkeypatch.setattr("ai.agent.default_cookbook", lambda: fake_book)
    a = Agent(dialog_worker=worker, exec_client=MagicMock())
    a.bb.tick = 100
    return a


def _batch(*calls):
    return DutyBatch(batch_id="b1", calls=list(calls), source="test")


def test_initial_division_event_and_context(agent, worker):
    world = make_world()
    agent.book = RecipeBook(world.recipes, world.stations)
    agent._service_dialog(world)
    names = [event[0] for event in worker.events]
    assert "division" in names
    context = worker.contexts[-1]
    assert context["guide"] == "打法：你煎，玩家装盘。"
    assert context["division_text"] == ""
    tools = {t["function"]["name"]: t for t in context["tools"]}
    assert "set_division" in tools and "read_cookbook" in tools
    assert tools["set_division"]["function"]["parameters"]["properties"][
        "assignments"]["items"]["properties"]["action"]["enum"] == [
            "chop", "cook", "plate"]


def test_set_division_applies_and_weights(agent, worker):
    world = make_world()
    agent.book = RecipeBook(world.recipes, world.stations)
    result = agent._apply_duty_batch(world, _batch(
        ("set_division", {"assignments": [
            {"action": "cook", "owner": "you"},
            {"action": "plate", "owner": "player"},
        ]})
    ))[0]
    assert result["ok"] is True
    assert result["division_changed"] is True
    assert agent.bb.division.snapshot() == {
        "you": ["cook"], "player": ["plate"], "both": []}
    kinds = {(p.kind, p.owner, p.weight) for p in agent.bb.preferences}
    assert kinds == {("direct", "P2", 1.0), ("claim", "P1", -6.0)}


def test_set_division_is_resident_and_editable_mid_level(agent, worker):
    """set_division 不是开局一次性：后续对话轮仍带它，玩家中途可改。"""
    world = make_world()
    agent.book = RecipeBook(world.recipes, world.stations)
    agent._service_dialog(world)          # 开局事件
    first = [t["function"]["name"] for t in worker.contexts[-1]["tools"]]
    assert "set_division" in first

    result = agent._apply_duty_batch(world, _batch(
        ("set_division", {"assignments": [
            {"action": "cook", "owner": "player"}]})
    ))[0]
    assert result["division_changed"] is True

    ctx = agent._build_dialog_context(world)
    assert "set_division" in [t["function"]["name"] for t in ctx["tools"]]
    assert agent.bb.division.snapshot()["player"] == ["cook"]


def test_set_division_rejects_deliver_and_unknown_item(agent):
    world = make_world()
    agent.book = RecipeBook(world.recipes, world.stations)
    result = agent._apply_duty_batch(world, _batch(
        ("set_division", {"assignments": [
            {"action": "deliver", "owner": "you"},
            {"action": "chop", "owner": "you", "item": "Bun"},
        ]})
    ))[0]
    assert result["ok"] is False
    assert len(result["rejected"]) == 2


def test_read_cookbook(agent):
    world = make_world()
    agent._maybe_reset_dialog_level(world)
    result = agent._apply_duty_batch(world, _batch(
        ("read_cookbook", {"section": "division"})
    ))[0]
    assert result["ok"] is True and "煎" in result["text"]


def test_level_change_clears_division(agent, worker):
    world = make_world()
    agent.book = RecipeBook(world.recipes, world.stations)
    agent._maybe_reset_dialog_level(world)   # 首次观察只记账
    agent._apply_duty_batch(world, _batch(
        ("set_division", {"assignments": [{"action": "cook", "owner": "you"}]})
    ))
    assert agent.bb.division.entries
    agent._maybe_reset_dialog_level(make_world(scene_epoch=2))
    assert agent.bb.division.entries == []
    assert agent.bb.preferences == []
    assert worker.reset_count == 1


def test_review_only_when_reach_changes_and_stable(agent, worker, monkeypatch):
    world = make_world()
    agent.book = RecipeBook(world.recipes, world.stations)
    agent._service_dialog(world)          # 开局：记下签名
    worker.events.clear()

    sig_a = (("下锅", True, True), ("切", True, True))
    sig_b = (("下锅", False, True), ("切", True, True))
    station_a = ("a",)
    station_b = ("b",)
    monkeypatch.setattr("ai.agent.reach_signature", lambda w, b: sig_a)
    monkeypatch.setattr("ai.agent.station_signature", lambda w: station_a)
    agent._division_reach_sig = sig_a
    agent._division_station_sig = station_a

    # 可达性不变：再检查也不发 review。
    agent.bb.tick += 20
    agent._service_dialog(world)
    assert [e[0] for e in worker.events] == []

    # 站点移动 + 可达性变化：连续 2 次确认后发 review。
    monkeypatch.setattr("ai.agent.reach_signature", lambda w, b: sig_b)
    monkeypatch.setattr("ai.agent.station_signature", lambda w: station_b)
    agent.bb.tick += 20
    agent._service_dialog(world)
    assert [e[0] for e in worker.events] == []
    agent.bb.tick += 20
    agent._service_dialog(world)
    assert [e[0] for e in worker.events] == ["division_review"]


def test_review_slow_path_for_temporary_block(agent, worker, monkeypatch):
    world = make_world()
    agent.book = RecipeBook(world.recipes, world.stations)
    agent._service_dialog(world)
    worker.events.clear()

    sig_a = (("下锅", True, True), ("切", True, True))
    sig_b = (("下锅", False, True), ("切", True, True))
    agent._division_reach_sig = sig_a
    agent._division_station_sig = ("same",)
    monkeypatch.setattr("ai.agent.reach_signature", lambda w, b: sig_b)
    monkeypatch.setattr("ai.agent.station_signature", lambda w: ("same",))

    for _ in range(7):
        agent.bb.tick += 20
        agent._service_dialog(world)
        assert [e[0] for e in worker.events] == []
    agent.bb.tick += 20
    agent._service_dialog(world)
    assert [e[0] for e in worker.events] == ["division_review"]

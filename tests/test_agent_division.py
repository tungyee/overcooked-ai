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
    # 只给玩家那边扣分；分给你的动作不加分（避免自己的活压过更值钱的事）
    kinds = {(p.kind, p.owner, p.weight) for p in agent.bb.preferences}
    assert kinds == {("claim", "P1", -6.0)}


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


def test_set_division_partial_reject_is_failure(agent):
    """部分条目被拒也要按失败返回，worker 才不会先播乐观台词。"""
    world = make_world()
    agent.book = RecipeBook(world.recipes, world.stations)
    result = agent._apply_duty_batch(world, _batch(
        ("set_division", {"assignments": [
            {"action": "cook", "owner": "you"},
            {"action": "chop", "owner": "you", "item": "Bun"},
        ]})
    ))[0]
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["division_changed"] is True
    assert result["rejected"]


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


def test_review_only_when_station_topology_changes(agent, worker, monkeypatch):
    """转盘/梯子的纯路径变化不触发；站点真的移动才 review。"""
    world = make_world()
    agent.book = RecipeBook(world.recipes, world.stations)
    agent._service_dialog(world)          # 开局：记下站点签名
    worker.events.clear()

    station_a = (("切", "G", 1, 1, 1, 1),)
    station_b = (("切", "G", 2, 1, 2, 1),)
    monkeypatch.setattr("ai.agent.station_signature", lambda w: station_a)
    agent._division_station_sig = station_a

    # 站点没变：再检查也不发 review。
    agent.bb.tick += 20
    agent._service_dialog(world)
    assert [e[0] for e in worker.events] == []

    # 站点移动：连续 2 次确认后发 review。
    monkeypatch.setattr("ai.agent.station_signature", lambda w: station_b)
    agent.bb.tick += 20
    agent._service_dialog(world)
    assert [e[0] for e in worker.events] == []
    agent.bb.tick += 20
    agent._service_dialog(world)
    assert [e[0] for e in worker.events] == ["division_review"]


def test_command_result_does_not_wake_llm(agent, worker):
    """命令完成/失败只更新上下文，不再作为 command_result 事件叫 LLM。"""
    world = make_world()
    agent.book = RecipeBook(world.recipes, world.stations)
    agent.commands.add(
        "chop", "NoSuchMaterial", frozenset({("Chop", "x")}), 100,
        done_item="x")
    agent._service_commands(world)
    assert worker.events == []
    assert agent._last_command_result["status"] == "failed"


# ---------- 换布局：区域复核 / 换边 ----------

def make_split_world(kind="lr", p2=(1, 1), p1=(1, 3)):
    """切开的两半厨房：`lr` 左右切、`ud` 上下切、`open` 全连通（换布局窗口）。

    站点全程不动（3-3 的真实情况：只有料箱在转），所以三种布局共用同一组
    站点：砧板在上、灶和水槽在下。左右切时 P1（灶+水槽）待左半、你（砧板）
    避开左半去右半；上下切时 P1 待下半、你去上半。
    """
    world = make_world(level="Wizard_3_2_2P")
    if kind == "lr":
        blocked = {(5, gz) for gz in range(6)}
    elif kind == "ud":
        blocked = {(gx, 3) for gx in range(11)}
    else:
        blocked = set()
    cells = {(gx, gz) for gx in range(11) for gz in range(6)} - blocked
    world.walkable = lambda gx, gz: (gx, gz) in cells
    world.to_grid = lambda x, z: (int(round(x)), int(round(z)))
    world.stations = [
        {"t": "chopping", "n": "boardL", "gx": 1, "gz": 6},
        {"t": "chopping", "n": "boardR", "gx": 7, "gz": 6},
        {"t": "cooking", "n": "ovenL", "gx": 1, "gz": -1},
        {"t": "washing", "n": "sink", "gx": -1, "gz": 1},
    ]
    for chef in world.chefs:
        cell = p2 if chef["name"] == "Player 2" else p1
        chef["x"], chef["z"] = float(cell[0]), float(cell[1])
    return world


def _settle(agent, world, times=3):
    for _ in range(times):
        agent.bb.tick += 20
        agent._service_dialog(world)


def test_layout_review_fires_when_player_on_wrong_side(agent, worker):
    world = make_split_world("lr", p2=(7, 1), p1=(1, 1))
    agent.book = RecipeBook(world.recipes, world.stations)
    agent._service_dialog(world)            # 开局：只发 division
    worker.events.clear()
    _settle(agent, world)
    assert worker.events == []              # 布局没变，不复核

    world = make_split_world("ud", p2=(7, 5), p1=(7, 5))
    agent.bb.tick += 20
    agent._service_dialog(world)
    agent.bb.tick += 20
    agent._service_dialog(world)
    names = [event[0] for event in worker.events]
    assert names == ["layout_review"]
    payload = worker.events[0][1]
    assert payload["player_wrong"] is True
    assert payload["you_wrong"] is False
    assert "下" in payload["facts"]


def test_layout_review_states_sides_even_when_both_in_place(agent, worker):
    """两人都在对的位置也要报一句站位：玩家不知道自己该守哪边。"""
    world = make_split_world("lr", p2=(7, 1), p1=(1, 1))
    agent.book = RecipeBook(world.recipes, world.stations)
    agent._service_dialog(world)
    _settle(agent, world)
    worker.events.clear()

    world = make_split_world("ud", p2=(7, 5), p1=(1, 1))
    _settle(agent, world)
    names = [e[0] for e in worker.events]
    assert names == ["layout_review"]
    payload = worker.events[0][1]
    assert payload["you_wrong"] is False and payload["player_wrong"] is False
    assert "你在上半边" in payload["facts"] and "玩家在下半边" in payload["facts"]


def test_layout_review_only_on_change(agent, worker):
    """布局没变就不再发（每 34 秒一次，不是每秒一次）。"""
    world = make_split_world(p2=(7, 1), p1=(7, 1))
    agent.book = RecipeBook(world.recipes, world.stations)
    agent._service_dialog(world)
    _settle(agent, world)
    worker.events.clear()
    _settle(agent, world)
    assert worker.events == []


def test_reposition_armed_when_map_opens(agent, worker):
    """转场窗口打开 → 布置换边目标，且目标落在两种布局都算对的格里。"""
    lr = make_split_world("lr", p2=(7, 1), p1=(1, 1))
    agent.book = RecipeBook(lr.recipes, lr.stations)
    agent._service_dialog(lr)
    _settle(agent, lr)                      # 记下左右布局
    ud = make_split_world("ud", p2=(7, 1), p1=(1, 1))
    _settle(agent, ud)                      # 记下上下布局
    assert agent.bb.reposition is None

    opened = make_split_world("open", p2=(7, 1), p1=(1, 1))
    agent.bb.tick += 20
    agent._service_dialog(opened)

    intent = agent.bb.reposition
    assert intent is not None
    assert intent.goal in intent.safe
    # 两种布局都算对的是右上角（左右切在右、上下切在上）
    assert (7, 5) in intent.safe
    assert (1, 5) not in intent.safe


def test_reposition_cleared_when_window_closes(agent, worker):
    lr = make_split_world("lr", p2=(7, 1), p1=(1, 1))
    agent.book = RecipeBook(lr.recipes, lr.stations)
    agent._service_dialog(lr)
    _settle(agent, lr)
    ud = make_split_world("ud", p2=(7, 1), p1=(1, 1))
    _settle(agent, ud)

    opened = make_split_world("open", p2=(7, 1), p1=(1, 1))
    agent.bb.tick += 20
    agent._service_dialog(opened)
    assert agent.bb.reposition is not None

    closed = make_split_world("ud", p2=(7, 5), p1=(1, 1))
    agent.bb.tick += 20
    agent._service_dialog(closed)
    assert agent.bb.reposition is None


def test_service_record_exposes_zones(agent):
    world = make_split_world("lr", p2=(7, 1), p1=(1, 1))
    agent.book = RecipeBook(world.recipes, world.stations)
    record = agent._service_dialog(world)
    assert record["zones"]["district"] == "LR"
    assert record["zones"]["labels"] == ["左", "右"]
    assert record["reposition"] is None


def test_reposition_runs_without_dialog_worker(agent, monkeypatch):
    """没开语音（对话层缺席）也要换边：窗口不等人。"""
    quiet = Agent(dialog_worker=None, exec_client=MagicMock())

    def feed(world):
        quiet._service_dialog(world)

    lr = make_split_world("lr", p2=(7, 1), p1=(1, 1))
    feed(lr)
    quiet.bb.tick += 20
    feed(lr)
    quiet.bb.tick += 20
    feed(lr)
    ud = make_split_world("ud", p2=(7, 1), p1=(1, 1))
    quiet.bb.tick += 20
    feed(ud)
    quiet.bb.tick += 20
    feed(ud)
    assert quiet.bb.reposition is None

    opened = make_split_world("open", p2=(7, 1), p1=(1, 1))
    quiet.bb.tick += 20
    feed(opened)
    assert quiet.bb.reposition is not None


def test_layout_review_not_repeated_by_region_jitter(agent, worker):
    """同一种切法边界差一两格不算换布局（真机 240 秒曾发过 14 条）。"""
    lr = make_split_world("lr", p2=(7, 1), p1=(7, 1))
    agent.book = RecipeBook(lr.recipes, lr.stations)
    agent._service_dialog(lr)
    worker.events.clear()

    ud = make_split_world("ud", p2=(7, 5), p1=(7, 5))
    _settle(agent, ud)
    assert [e[0] for e in worker.events] == ["layout_review"]
    worker.events.clear()

    # 少一格可走格：连通块大小变了，但两个角色该待的半边没变
    jittered = make_split_world("ud", p2=(7, 5), p1=(7, 5))
    inner = jittered.walkable
    jittered.walkable = lambda gx, gz: inner(gx, gz) and (gx, gz) != (0, 0)
    _settle(agent, jittered)
    assert worker.events == []


def test_initial_division_facts_describe_layout(agent, worker):
    """开局就要说清「分成哪两半、谁该在哪」，不然模型只能按出生点猜。"""
    world = make_split_world("lr", p2=(7, 1), p1=(1, 1))
    agent.book = RecipeBook(world.recipes, world.stations)
    agent._service_dialog(world)
    facts = worker.events[0][1]["facts"]
    assert "分成左右两半" in facts
    assert "你该在右半边" in facts
    assert "玩家该在左半边" in facts


def test_initial_division_facts_say_single_block_when_open(agent, worker):
    world = make_split_world("open", p2=(7, 1), p1=(1, 1))
    agent.book = RecipeBook(world.recipes, world.stations)
    agent._service_dialog(world)
    facts = worker.events[0][1]["facts"]
    assert "一整块" in facts
    assert "两半" not in facts


def test_reposition_armed_within_one_tick(agent, worker):
    """开窗要当拍就布置：窗口只有 2~3 秒，等 1 秒的复核周期就废了。

    实测 trace-20261109-123544 t=118.0 开窗，120.18 才 armed，只剩 1 秒。
    """
    lr = make_split_world("lr", p2=(7, 1), p1=(1, 1))
    agent.book = RecipeBook(lr.recipes, lr.stations)
    agent._service_dialog(lr)
    _settle(agent, lr)
    ud = make_split_world("ud", p2=(7, 1), p1=(1, 1))
    _settle(agent, ud)
    assert agent.bb.reposition is None

    opened = make_split_world("open", p2=(7, 1), p1=(1, 1))
    agent.bb.tick += 1                 # 只过一拍
    agent._service_dialog(opened)
    assert agent.bb.reposition is not None


def test_reposition_armed_even_when_already_in_safe(agent, worker):
    """已经在安全区也要占住窗口：不然两秒后就被普通任务拽出去了。"""
    lr = make_split_world("lr", p2=(7, 5), p1=(1, 1))
    agent.book = RecipeBook(lr.recipes, lr.stations)
    agent._service_dialog(lr)
    _settle(agent, lr)
    ud = make_split_world("ud", p2=(7, 5), p1=(1, 1))
    _settle(agent, ud)
    assert agent.bb.reposition is None

    opened = make_split_world("open", p2=(7, 5), p1=(1, 1))
    agent.bb.tick += 1
    agent._service_dialog(opened)
    intent = agent.bb.reposition
    assert intent is not None
    assert intent.goal in intent.safe

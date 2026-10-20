# -*- coding: utf-8 -*-
"""对话层端到端集成：真实帧 + Agent + planner + worker（spec §4.6–4.7 / §10.2）。

全部离线、确定性：
- 真实帧优先来自入库的 `tests/fixtures/dialog_scenarios.ndjson.gz`，
  本地没有时回退到 `full_level.ndjson`；两者都缺失时 skip；
- 状态构造复用 `tests/test_agent_dialog.py` 的 World helper；
- LLM / TTS / 播放器 / VAD 都是 fake，不联网、不占音频设备。

与 `tests/test_agent_dialog.py` 的单测不同，这里走完整路径：
`DialogWorker` 拿到 LLM 工具调用 → 批次入队 → `Agent._service_dialog` 落
账本、编译偏好 → ack 回到 worker → 写历史并播报；规划器再在同一黑板上重新选。
"""
import gzip
import json
import os
import queue

import pytest
from unittest.mock import MagicMock, patch

from ai import tasks as T
from ai.agent import Agent
from ai.core import planner
from ai.core import planner_layers as PL
from ai.dialog.schema import preference_matches_task
from ai.dialog.worker import DutyBatch
from ai.query.recipes import RecipeBook
from ai.state.world import World
from tests.test_agent_dialog import make_plate_world, make_world
from tests.test_dialog_worker import (FakeLLM, duty, make_worker, reply,
                                      run_async, wait_until)


FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
SCENARIO_FIXTURE = os.path.join(FIXTURE_DIR, "dialog_scenarios.ndjson.gz")
FULL_FIXTURE = os.path.join(FIXTURE_DIR, "full_level.ndjson")


# --------------------------------------------------------------- 真实帧

def _fixture_path():
    """优先用入库的小样本帧；本地有完整录像时回退到它。"""
    if os.path.exists(SCENARIO_FIXTURE):
        return SCENARIO_FIXTURE
    if os.path.exists(FULL_FIXTURE):
        return FULL_FIXTURE
    pytest.skip(
        "缺少 %s（或本地 %s）；用 tools/record_frames.py 录制完整帧，"
        "或从主仓库复制 dialog_scenarios.ndjson.gz"
        % (SCENARIO_FIXTURE, FULL_FIXTURE))


def _find_frame(predicate, description):
    """按行扫描 fixture，返回第一个满足 predicate 的原始帧。

    缺 fixture 或缺该状态时明确 skip，绝不静默通过。
    """
    path = _fixture_path()
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            frame = json.loads(line)
            if predicate(frame):
                return frame
    pytest.skip("fixture 中没有满足条件的帧：%s（%s）" % (description, path))


def _has_complete_plate(frame):
    return any(len(p.get("contents") or []) >= 3
               for p in frame.get("plates") or [])


def _has_cooking_rice_and_cucumber(frame):
    cooking_rice = any(
        "SushiRice" in json.dumps(c.get("pot_contents") or [])
        for c in frame.get("cookers") or [] if c.get("isCooking"))
    cucumber_board = any(w.get("item") == "Cucumber"
                         for w in frame.get("workstations") or [])
    return cooking_rice and cucumber_board


def _has_cooked_rice(frame):
    return any(c.get("cooked") for c in frame.get("cookers") or [])


def _make_agent(monkeypatch, world, worker=None):
    monkeypatch.setattr("ai.agent.StateClient", lambda *a, **k: MagicMock())
    agent = Agent(ctrl=MagicMock(), voice=None, dialog_worker=worker)
    agent.bb.tick = 100
    agent.book = RecipeBook(world.recipes, world.stations)
    return agent


class _RecordingWorker(object):
    """只记录主循环转发的事件；不启动线程、不碰音频。"""

    def __init__(self):
        self.cmd_q = queue.Queue()
        self.ack_q = queue.Queue()
        self.ctx_q = queue.Queue(maxsize=1)
        self.event_q = queue.Queue(maxsize=1)
        self.events = []

    def start(self):
        pass

    def stop(self):
        pass

    def reset_state(self):
        pass

    def put_event(self, event):
        self.events.append(event)

    def publish_context(self, context):
        pass


def _apply(agent, world, **args):
    """落一条 update_duties；返回该条结果 dict。"""
    batch = DutyBatch(batch_id="t", calls=[("update_duties", args)])
    return agent._apply_duty_batch(world, batch)[0]


def _row(record, task):
    for row in record["candidates"]:
        if row[0] == task:
            return row
    return None


# --------------------------------------------------------- 场景 1：送餐

def test_player_claims_delivery_p2_drops_to_lower_layer(monkeypatch):
    """玩家说「我来送」：P2 本拍不再选 DeliverPlate，改选低层候选。"""
    frame = _find_frame(_has_complete_plate, "料齐的盘 / DeliverPlate 候选")
    world = World(frame)
    agent = _make_agent(monkeypatch, world)

    before, _ = planner.plan(world, agent.bb, None, agent.book)
    assert before is not None and before.TYPE == "DeliverPlate", (
        "fixture 的料齐盘帧应产生 DeliverPlate 候选，实际 %r" % (before,))

    result = _apply(agent, world, op="add", mode="leave_to_p1",
                    target="DeliverPlate")
    assert result["ok"], result

    after, record = planner.plan(world, agent.bb, None, agent.book)

    assert after is None or after.TYPE != "DeliverPlate"
    # 低层候选仍存在（送餐被认领后逐层向下掉，而不是停摆）。
    assert after is not None
    assert record["top_layer"] > PL.L_REDEEM
    deliver_rows = [row for row in record["candidates"]
                    if row[0].TYPE == "DeliverPlate"]
    assert deliver_rows
    assert all(row[7] is False for row in deliver_rows)  # 未进本拍打分


# --------------------------------------------------------- 场景 2：米饭链

def test_player_claims_rice_branch_penalizes_rice_not_cucumber(monkeypatch):
    """「我来煮饭」：米链负分、黄瓜链不受影响。"""
    frame = _find_frame(_has_cooking_rice_and_cucumber,
                        "正在煮饭 + 砧板黄瓜")
    world = World(frame)
    agent = _make_agent(monkeypatch, world)

    result = _apply(agent, world, op="add", mode="leave_to_p1",
                    target="BoiledSushiRice")
    assert result["ok"], result

    plate_match = planner._match_plates_to_orders(
        world, agent.bb, agent.book)
    cooker_match = planner._match_cookers_to_orders(
        world, agent.bb, agent.book, plate_match)
    cands = planner.generate_candidates(
        world, agent.bb, None, agent.book,
        plate_match=plate_match, cooker_match=cooker_match)

    rice_types = ("FetchToCooker", "FillCooker", "TakeCookedToPlate")
    rice = [c for c in cands
            if c.branch == "SushiRice" and c.task.TYPE in rice_types]
    assert rice, "fixture 帧应产生米链候选"

    # 非 L1/L2 的米链候选吃 -8；L1/L2 的免疫由场景 4 单独覆盖。
    penalized = [c for c in rice
                 if PL.layer_of_task(c.task, world) > PL.L_RESCUE]
    assert penalized
    for cand in penalized:
        layer = PL.layer_of_task(cand.task, world)
        assert planner._preference_score(cand, agent.bb, layer) == -8.0

    cucumbers = [c for c in cands
                 if c.branch == "Cucumber"
                 and c.task.TYPE in ("FetchToBoard", "Chop",
                                     "FetchToPlate")]
    assert cucumbers
    for cand in cucumbers:
        layer = PL.layer_of_task(cand.task, world)
        assert planner._preference_score(cand, agent.bb, layer) == 0.0


# --------------------------------------------------------- 场景 3：pending 洗盘

def _make_wash_world():
    """有洗碗池、但没有脏盘；盘堆为空（干净盘=0）。"""
    return World({
        "phase": "level",
        "level": "Sushi_1_4_2P",
        "orders": [],
        "recipes": [{"name": "Cucumber", "comp": []}],
        "stations": [
            {"t": "washing", "n": "WashingPart", "gx": 7, "gz": 1},
            {"t": "plate_return", "n": "DryingPart", "gx": 8, "gz": 1},
        ],
        "workstations": [],
        "plates": [],
        "cookers": [],
        "chefs": [
            {"name": "Player 1", "x": 0.0, "z": 0.0, "held": None},
            {"name": "Player 2", "x": 1.0, "z": 1.0, "held": None},
        ],
        "items": [],
        "extinguishers": [],
        "grid": {"size": [1.2, 1.0, 1.2], "nodeMap": None},
    })


def test_pending_wash_starts_when_dirty_plate_appears(monkeypatch):
    """没有脏盘时说「去洗碗」→ 账本保留 +4；盘子出现后去洗。"""
    world = _make_wash_world()
    agent = _make_agent(monkeypatch, world)

    result = _apply(agent, world, op="add", mode="do", target="WashPlate")

    assert result["ok"], result
    pref = agent.bb.preferences[0]
    assert pref.kind == "direct"
    assert pref.owner == "P2"
    assert pref.weight == 4.0

    # 盘子出现：同一份偏好必须立刻兑现（不是重新下命令）。
    world.items.append({"n": "DirtyPlateStack", "x": 3.0, "z": 0.0})
    chosen, record = planner.plan(world, agent.bb, None, agent.book)

    assert chosen is not None
    assert chosen.TYPE == "WashPlate"
    assert record["top_layer"] == PL.L_UNBLOCK
    row = _row(record, chosen)
    assert row is not None
    assert row[2].get("pref") == 4.0


# --------------------------------------------------------- 场景 4：L2 救援

def test_l2_take_cooked_not_blocked_by_cook_claim(monkeypatch):
    """「我来煮饭」不能挡住刚熟/快糊的锅：L2 仍选中 TakeCookedToPlate。"""
    frame = _find_frame(_has_cooked_rice, "刚熟/正在收汁的饭")
    world = World(frame)
    agent = _make_agent(monkeypatch, world)

    result = _apply(agent, world, op="add", mode="leave_to_p1",
                    target="BoiledSushiRice")
    assert result["ok"], result

    chosen, record = planner.plan(world, agent.bb, None, agent.book)

    assert chosen is not None
    assert chosen.TYPE in ("TakeCookedToPlate", "RescueCooked")
    # 锅的压力已经进入 L1/L2（否则这一帧不会返回刚熟候选）。
    assert record["top_layer"] in (PL.L_IRREVERSIBLE, PL.L_RESCUE)
    row = _row(record, chosen)
    assert row is not None
    assert row[2].get("pref", 0.0) == 0.0  # 认领负分被 L1/L2 免疫


# --------------------------------------------------------- 场景 5：截止闸

def _make_expiring_world():
    """料齐的盘 + 只剩 5.5 秒的订单 → cushion < margin。

    与 `tests/test_rescue_gate.py` 同一套真实 World/配方/ETA：不 mock 闸。
    """
    return World({
        "orders": [{"id": "o1", "dish": "Sushi_A",
                    "composition": ["Seaweed"], "remaining": 5.5,
                    "lifetime": 130.0}],
        "stations": [
            {"t": "washing", "n": "sink_01", "gx": 7, "gz": 1},
            {"t": "plate_return", "n": "DryingPart", "gx": 8, "gz": 1},
            {"t": "serving", "n": "hatch", "gx": 9, "gz": 1},
        ],
        "recipes": [
            {"name": "Sushi_A", "comp": ["Seaweed"]},
            {"name": "Seaweed"},
        ],
        "chefs": [{"name": "Player 2", "x": 1.0, "z": 1.0, "held": None}],
        "plates": [{"n": "plate_a", "x": 0.0, "z": 0.0,
                    "contents": ["seaweed"]}],
        "workstations": [],
        "cookers": [],
        "items": [{"n": "DirtyPlateStack", "x": 3.0, "z": 0.0}],
        "extinguishers": [],
        "grid": {"nodeMap": None},
    })


def test_rescue_gate_override_with_claim_and_pending_event(monkeypatch):
    """最后窗口内认领送餐：闸开、抢回并写 rescue_override（不再语音播报）。"""
    world = _make_expiring_world()
    worker = _RecordingWorker()
    agent = _make_agent(monkeypatch, world, worker)

    result = _apply(agent, world, op="add", mode="leave_to_p1",
                    target="DeliverPlate")
    assert result["ok"], result

    chosen, record = planner.plan(world, agent.bb, None, agent.book)

    assert chosen is not None
    assert chosen.TYPE == "DeliverPlate"
    assert record["top_layer"] == PL.L_REDEEM       # 没有 fallthrough
    assert record["rescue_override"] == "o1"
    assert agent.bb.dialog.pending_event == ("rescue", "o1")
    assert agent.bb.rescue_cooldown_until == agent.bb.tick + 100


# --------------------------------------------------------- 场景 6/7：worker

class _AgentWorkerBridge(object):
    """把真实 DialogWorker 的队列接到 Agent；不启动线程、不碰音频。"""

    def __init__(self, bundle):
        self.cmd_q = bundle.cmd_q
        self.ack_q = bundle.ack_q
        self.ctx_q = bundle.ctx_q
        self.event_q = bundle.event_q

    def start(self):
        pass

    def stop(self):
        pass

    def reset_state(self):
        pass

    def put_event(self, event):
        try:
            self.event_q.put_nowait(event)
        except queue.Full:
            pass

    def publish_context(self, context):
        try:
            self.ctx_q.put_nowait(context)
        except queue.Full:
            pass


def _make_dialog_agent(world, bundle):
    bridge = _AgentWorkerBridge(bundle)
    with patch("ai.agent.StateClient", lambda *a, **k: MagicMock()):
        agent = Agent(ctrl=MagicMock(), dialog_worker=bridge)
    agent.bb.tick = 100
    agent.book = RecipeBook(world.recipes, world.stations)
    return agent


def _run_worker_turn(agent, worker, bundle, world, text):
    """worker 处理一句 → Agent 落地批次并回 ack → worker 收尾。

    批次必须由 Agent 从 `cmd_q` 里 drain；提前 `get()` 会把它从主循环手里抢走。
    """
    context = agent._build_dialog_context(world)
    thread = run_async(worker._handle_transcript, text, context)
    assert wait_until(lambda: bundle.cmd_q.qsize() >= 1), "batch not enqueued"
    record = agent._service_dialog(world)
    thread.join(2)
    assert not thread.is_alive()
    return record


def test_spoken_division_reaches_planner_end_to_end():
    """「你切黄瓜，送餐我来」：两条工具调用都落地，P2 的选择随之改变。"""
    frame = _find_frame(_has_complete_plate, "料齐的盘 / DeliverPlate 候选")
    world = World(frame)
    llm = FakeLLM(reply("",
                        duty(op="add", mode="do", target="Cucumber"),
                        duty(op="add", mode="leave_to_p1",
                             target="DeliverPlate")),
                  reply("好，黄瓜我切，送餐交给你。"))
    worker, bundle = make_worker(llm=llm)
    agent = _make_dialog_agent(world, bundle)

    before, _ = planner.plan(world, agent.bb, None, agent.book)
    assert before.TYPE == "DeliverPlate"

    record = _run_worker_turn(agent, worker, bundle, world, "你切黄瓜，送餐我来")

    assert [r["ok"] for r in record["batches"][0]["results"]] == [True, True]
    assert bundle.tts.spoken == ["好，黄瓜我切，送餐交给你。"]
    # 工具结果写回历史，下一轮模型能看到 id
    assert [m["role"] for m in worker.state.history] == [
        "user", "assistant", "tool", "tool", "assistant"]

    after, _ = planner.plan(world, agent.bb, None, agent.book)
    assert after.TYPE != "DeliverPlate"
    assert (after.TYPE, after.item) in (("FetchToBoard", "Cucumber"),
                                        ("Prefetch", "Cucumber"),
                                        ("Chop", "Cucumber"))


def test_rejected_call_is_reported_back_and_rest_applies():
    frame = _find_frame(_has_complete_plate, "料齐的盘")
    world = World(frame)
    llm = FakeLLM(reply("",
                        duty(op="add", mode="do", target="Chop"),
                        duty(op="add", mode="do", target="Cucumber")),
                  reply("饭不用切，黄瓜我来切。"))
    worker, bundle = make_worker(llm=llm)
    agent = _make_dialog_agent(world, bundle)

    record = _run_worker_turn(agent, worker, bundle, world, "你去切")

    results = record["batches"][0]["results"]
    assert [r["ok"] for r in results] == [False, True]
    assert agent.ledger.ids() == ["a1"]
    assert "本关没有 Chop" in worker.state.history[2]["content"]
    # 第二段看到了拒绝原因，说的是真实结果
    assert bundle.tts.spoken == ["饭不用切，黄瓜我来切。"]

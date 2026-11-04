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
    agent = Agent(voice=None, dialog_worker=worker)
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



# ------------------------------------------------- 动作级端到端（重构后）

def test_action_division_reaches_planner(monkeypatch):
    """动作级分工走完整链：判 + apply + 编译偏好 + planner 在同一黑板重选。"""
    path = _fixture_path()
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        frame = json.loads(f.readlines()[-1])
    world = World(frame)
    agent = _make_agent(monkeypatch, world)

    # P2 负责切、P1 负责下锅
    assert _apply(agent, world, op="add", mode="assign", action="切",
                  direction="P2")["ok"] is True
    assert _apply(agent, world, op="add", mode="assign", action="下锅",
                  direction="P1")["ok"] is True

    # 偏好编译：切=direct(+1)、下锅=claim(-6)
    kinds = {p.kind: p.weight for p in agent.bb.preferences}
    assert kinds.get("direct") == 1.0
    assert kinds.get("claim") == -6.0

    # planner 在同一黑板上重新选，能出结果（偏好已就位、链路不崩）
    chosen, record = planner.plan(world, agent.bb, None, agent.book)
    assert chosen is not None

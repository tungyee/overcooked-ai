# -*- coding: utf-8 -*-
"""真实帧回归：Planner / Agent 在 Sushi_1_4 实测帧上必须真的产出可行任务。

背景（Plan 4 收尾核查发现）：候选生成器一度用错站点类型（board / cooker /
counter），且完全没有 FetchToPlate 生成器，导致在真实关卡上
select_task() 恒返回 None——AI 站着不动。这组测试把"真实帧上能选出可行
task 并持续执行"钉成回归网。
"""
import json
import os
from unittest.mock import patch

import pytest

from ai.agent import Agent
from ai.state.blackboard import Blackboard
from ai.core import planner
from ai.output import trace
from ai.query.recipes import RecipeBook
from ai.state.world import World

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "real_frame_sushi_1_4.json")


def _frame():
    if not os.path.exists(FIXTURE):
        pytest.skip("缺 tests/fixtures/real_frame_sushi_1_4.json")
    with open(FIXTURE) as f:
        return json.load(f)


def _world():
    return World(_frame())


def _book(world):
    return RecipeBook(world.recipes, world.stations)


def test_real_frame_has_feasible_candidates():
    """真实帧上候选非空、可行非空，且不再产出海量假 DeliverPlate。"""
    w = _world()
    bb = Blackboard()
    book = _book(w)

    cands = planner.generate_candidates(w, bb, None, book)
    feasible = planner.filter_feasible(w, bb, cands, book)

    assert cands, "真实帧上应能生成候选"
    assert feasible, "真实帧上应有可行候选"

    types = set(c.task.TYPE for c in feasible)
    # 至少能开始取料 / 切 / 下锅之一，而不是只会送餐
    assert types & {"FetchToPlate", "FetchToBoard", "FetchToCooker"}, types

    # 出餐候选不应按"每个 counter 一个"爆炸（旧 bug：245 个假 DeliverPlate）
    deliver = [c for c in cands if c.task.TYPE == "DeliverPlate"]
    assert len(deliver) <= len(w.orders), len(deliver)


def test_real_frame_select_task_returns_feasible_task():
    """select_task 在真实帧上必须返回一个可行的 task（不能是 None）。"""
    w = _world()
    bb = Blackboard()
    book = _book(w)

    task = planner.select_task(w, bb, None, book)
    assert task is not None, "真实帧上 Planner 不应返回 None（AI 会站着不动）"

    # 选中的 task 必须落在可行集合里（用与 select_task 相同的每拍匹配）
    plate_match = planner._match_plates_to_orders(w, bb, book)
    cooker_match = planner._match_cookers_to_orders(w, bb, book, plate_match)
    cands = planner.generate_candidates(w, bb, None, book, plate_match, cooker_match)
    feasible = planner.filter_feasible(w, bb, cands, book)
    assert any(c.task == task for c in feasible)


def test_real_frame_agent_runs_without_crash():
    """真实帧喂给完整主循环：不崩溃、持续执行、发 exec 命令。"""
    from ai.exec.client import NullExec

    frame = _frame()
    frame["paused"] = False
    ex = NullExec()

    trace.clear_log()
    with patch("ai.agent.StateClient", return_value=object()):
        agent = Agent(exec_client=ex)
    for _ in range(5):
        agent._tick_once(World(frame))
        agent.tick += 1

    logs = trace.read_log()
    assert len(logs) == 5
    # 至少有一拍在真正执行（running），并发了 exec 命令
    assert any(entry["exec"].get("status") == "running" for entry in logs)
    assert ex.issued, "真实帧上应发过 exec 命令"

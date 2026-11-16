# -*- coding: utf-8 -*-
"""碰撞回传 blocked（硬障碍）的拉黑与局部重绕回归。

插件在移动方向 SweepTest 命中 obstacle collider 后回 blocked（blocked_box +
blocked_kind），Python 侧要：把 AABB 投影成格、短暂拉黑整块、在 Approach 内
局部重绕（不整树 FAILURE），重试耗尽才升格。
"""
from ai import primitives as PR
from ai import tasks as T
from ai.core import planner
from ai.exec.bt import Status
from ai.primitives.base import Primitive
from ai.primitives.move import BLOCK_RETRIES
from ai.primitives.stuck import BLOCKED_BOX_TTL_TICKS
from tests.test_agent_lifecycle import _agent
from tests.test_exec_primitives import FakeWorld, FakeExec, make_ctx, W, CHEF


class _FixedExec(object):
    def __init__(self, result):
        self.result = result

    def poll(self):
        return self.result


def test_poll_exec_blocked_box_records_cells():
    p = Primitive()
    p._pending_cmd = 7
    p._pending_name = "move"
    ctx = {"w": FakeWorld(), "tick": 0}
    ex = _FixedExec({"cmd_id": 7, "ok": False, "why": "blocked",
                     "blocked_box": [3.1, 4.1, -0.5, 0.5],
                     "blocked_kind": "obstacle", "blocked_cell": None})
    assert p._poll_exec(ctx, ex) == Status.FAILURE
    assert ctx["motion_failure"]["reason"] == "blocked"
    assert ctx["motion_failure"]["blocked_cells"] == [(3, 0)]
    assert ctx["motion_failure"]["blocked_kind"] == "obstacle"


def test_blocked_motion_failure_writes_box_cells_and_does_not_abandon():
    agent = _agent()
    task = T.ReturnPot(resources=('hob',))
    agent._note_motion_failure(task, {
        'motion_failure': {'reason': 'blocked',
                           'blocked_cells': [(1, 2), (1, 3)],
                           'blocked_kind': 'obstacle'}})
    assert agent.bb.scratch.bad_cells[(1, 2)] == agent.tick + BLOCKED_BOX_TTL_TICKS
    assert agent.bb.scratch.bad_cells[(1, 3)] == agent.tick + BLOCKED_BOX_TTL_TICKS
    assert not planner._is_abandoned(task, agent.bb)


def test_blocked_motion_failure_ignores_pushable_kind():
    agent = _agent()
    task = T.ReturnPot(resources=('hob',))
    agent._note_motion_failure(task, {
        'motion_failure': {'reason': 'blocked',
                           'blocked_cells': [(1, 2)],
                           'blocked_kind': 'player'}})
    assert not agent.bb.scratch.bad_cells
    assert not planner._is_abandoned(task, agent.bb)


def _blocked_result(cmd_id):
    return {"cmd_id": cmd_id, "ok": False, "why": "blocked",
            "blocked_box": [3.1, 4.1, -0.5, 0.5],
            "blocked_kind": "obstacle", "blocked_cell": None}


def test_approach_blocked_does_local_reroute():
    ex = FakeExec()
    ctx = make_ctx(W, CHEF, ex)
    target = {"gx": 5, "gz": 0, "x": 6.0, "z": 0.0, "n": "counter (3)"}
    ap = PR.Approach(lambda c: target, kind=None)
    assert ap.tick(ctx) == Status.RUNNING
    ex.results.append(_blocked_result(ex.issued[0]["cmd_id"]))
    assert ap.tick(ctx) == Status.RUNNING        # 局部重绕，不整树 FAILURE
    assert ap._block_retries == 1
    assert ctx["bb"].scratch.bad_cells[(3, 0)] == BLOCKED_BOX_TTL_TICKS


def test_approach_blocked_exhausts_retries_then_fails():
    ex = FakeExec()
    ctx = make_ctx(W, CHEF, ex)
    target = {"gx": 5, "gz": 0, "x": 6.0, "z": 0.0, "n": "counter (3)"}
    ap = PR.Approach(lambda c: target, kind=None)
    assert ap.tick(ctx) == Status.RUNNING
    ap._block_retries = BLOCK_RETRIES
    ex.results.append(_blocked_result(ex.issued[0]["cmd_id"]))
    assert ap.tick(ctx) == Status.FAILURE        # 重试耗尽 → 升格整树 replan

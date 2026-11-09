# -*- coding: utf-8 -*-
"""插件报 ledge 时要像 stuck 一样拉黑落点，否则会反复重发同一条 move。"""
from ai.exec.bt import Status
from ai.primitives.base import Primitive


class FakeWorld(object):
    def to_grid(self, x, z):
        return (int(round(x)), int(round(z)))


class FakeExec(object):
    def __init__(self, result):
        self.result = result

    def poll(self):
        return self.result


def test_ledge_failure_records_no_progress_cell():
    p = Primitive()
    p._pending_cmd = 7
    p._pending_name = "move"
    ctx = {"w": FakeWorld(), "tick": 0}
    ex = FakeExec({"cmd_id": 7, "ok": False, "why": "ledge",
                   "blocked_cell": [3.6, 3.6], "held_after": None})
    assert p._poll_exec(ctx, ex) == Status.FAILURE
    assert ctx["motion_failure"]["reason"] == "no_progress"
    assert ctx["motion_failure"]["blocked_cell"] == (4, 4)

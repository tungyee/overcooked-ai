"""exec 命令失败原因进 trace（exec.exec_failure）。

插件回的 why 原先只拿来写 bad_cells，trace 里看不到；送餐 approach 连败
分不清门洞判关 / 卡墙 / 超时（trace-20261031-004647）。
"""
from ai.exec.bt import Status
from ai.exec.commands import build_command
from ai.output import trace
from ai.primitives.base import Primitive


class FakeExec(object):
    """issue 返回固定 id；poll 返回预置结果（None = 还没回）。"""

    def __init__(self, result=None, cmd_id=7):
        self.result = result
        self.cmd_id = cmd_id

    def issue(self, cmd):
        return self.cmd_id

    def poll(self):
        return self.result


class Probe(Primitive):
    def tick(self, ctx):
        return Status.RUNNING


def _issue(ctx, ex):
    p = Probe()
    assert p._begin_exec(ctx, ex, build_command("move", timeout_ms=1000)) == Status.RUNNING
    return p


def test_plugin_why_recorded_on_failure():
    ex = FakeExec({"cmd_id": 7, "ok": False, "why": "gate closed",
                   "held_after": None, "blocked_cell": [15.6, -12.0]})
    ctx = {"now": 0.0}
    p = _issue(ctx, ex)
    assert p._poll_exec(ctx, ex) == Status.FAILURE
    assert ctx["exec_failure"] == {"cmd": "move", "why": "gate closed",
                                   "blocked_cell": [15.6, -12.0]}
    rec = trace.execution_record("failure", None, ctx)
    assert rec["exec_failure"]["why"] == "gate closed"


def test_client_deadline_recorded():
    ex = FakeExec(None)
    ctx = {"now": 0.0}
    p = _issue(ctx, ex)
    ctx["now"] = 10.0
    assert p._poll_exec(ctx, ex) == Status.FAILURE
    assert ctx["exec_failure"]["why"] == "client_deadline"


def test_not_connected_recorded():
    ex = FakeExec(cmd_id=-1)
    ctx = {"now": 0.0}
    assert Probe()._begin_exec(ctx, ex, build_command("tap")) == Status.FAILURE
    assert ctx["exec_failure"] == {"cmd": "tap", "why": "not_connected",
                                   "blocked_cell": None}


def test_success_leaves_no_failure():
    ex = FakeExec({"cmd_id": 7, "ok": True, "why": None,
                   "held_after": None, "blocked_cell": None})
    ctx = {"now": 0.0}
    p = _issue(ctx, ex)
    assert p._poll_exec(ctx, ex) == Status.SUCCESS
    assert "exec_failure" not in ctx
    assert "exec_failure" not in trace.execution_record("success", None, ctx)

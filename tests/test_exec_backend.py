# -*- coding: utf-8 -*-
"""exec 通道接线测试：Agent 的 ctx 恒带 exec（唯一执行后端）。"""
from ai.agent import Agent
from ai.exec.client import NullExec


class FakeWorld(object):
    def __init__(self):
        self.chefs = [{"name": "Player 2", "x": 0.0, "z": 0.0}]
        self.belt_dir = {}
        self.edge_blocked = None

    def to_grid(self, x, z):
        return (0, 0)

    def walkable(self, gx, gz):
        return True

    def balloon_ground_box(self):
        return None

    def balloon_cells(self):
        return set()


class _FakeStateClient(object):
    def __init__(self, *a, **k):
        pass

    def read_latest(self):
        return None


def test_ctx_always_has_exec(monkeypatch):
    monkeypatch.setattr("ai.agent.StateClient", _FakeStateClient)
    monkeypatch.setattr("ai.agent.ExecClient", lambda: NullExec())
    a = Agent()
    ctx = a._exec_context(FakeWorld())
    assert "exec" in ctx
    assert isinstance(ctx["exec"], NullExec)


def test_injected_null_exec_used(monkeypatch):
    monkeypatch.setattr("ai.agent.StateClient", _FakeStateClient)
    ex = NullExec()
    a = Agent(exec_client=ex)
    assert a.exec_client is ex

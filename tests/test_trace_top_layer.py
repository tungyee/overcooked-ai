# -*- coding: utf-8 -*-
"""trace 的 top_layer / 实际执行 task 的可观测性回归。

实机 trace-20261015-165358 的 3637 行里 `top_layer` **全部是 None**：
`planner.plan()` 算好了放进 decision_record，但 `agent._tick` 组 record 时
没有转抄，`trace.emit` 读 `record.get("top_layer")` 自然拿不到。

后果：分层最大的可观测性收益（「这一拍选在哪层」，spec §6 的复盘第一步）
整个失效，只能靠逐候选的 layer 字段反推 min()。
"""
from ai.output import trace


def test_emit_writes_top_layer(tmp_path, monkeypatch):
    """record 里带 top_layer 就要落盘，不能丢。"""
    path = tmp_path / "t.jsonl"
    monkeypatch.setattr(trace, "TRACE_PATH", str(path))
    trace.emit({"top_layer": 5, "candidates": []}, "Normal", None,
               {"status": "running"}, [], 7)
    import json
    rec = json.loads(path.read_text().strip())
    assert rec["top_layer"] == 5


def test_agent_record_carries_top_layer():
    """Agent 组 record 时必须转抄 plan_record 的 top_layer。

    这条是真正的回归点：trace.emit 一直读得对，是上游没给。
    """
    import inspect

    from ai import agent
    src = inspect.getsource(agent.Agent._tick_once)
    assert "top_layer" in src, \
        "agent._tick_once 没转抄 top_layer，trace 里会永远是 None"

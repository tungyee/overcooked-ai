# -*- coding: utf-8 -*-
"""trace 的层信息（spec §6）。

「为什么没选救锅」要能一眼区分两种原因：被否决（ok=False + veto）还是
可行但层不够高（ok=True + in_top_layer=False）。这是分层最大的可观测性
收益，所以单独钉住。
"""
from ai.output import trace


class _Task(object):
    def __init__(self, name):
        # type: (str) -> None
        self._name = name

    def __repr__(self):
        return self._name


def test_candidate_carries_layer_and_in_top_layer():
    cands = [
        (_Task("FillCooker"), 4.8, {"cooker_idle": 4.8}, True, "", True, 5, True),
        (_Task("Chop"), 0.0, {"urgency": 3.0}, True, "", False, 6, False),
    ]
    out = trace._format_candidates(cands)
    assert out[0]["layer"] == 5
    assert out[0]["in_top_layer"] is True
    assert out[1]["layer"] == 6
    assert out[1]["in_top_layer"] is False
    # 「可行但层不够高」：ok 为真、veto 为空、in_top_layer 为假
    assert out[1]["ok"] is True and out[1]["veto"] == ""


def test_six_tuple_still_formats():
    """plan_firefighting 仍发 6 元组（它不分层，只有一个全局候选）。"""
    cands = [(_Task("Extinguish"), 0.0, {}, True, "", True)]
    out = trace._format_candidates(cands)
    assert out[0]["layer"] is None
    assert out[0]["in_top_layer"] is False
    assert out[0]["chosen"] is True


def test_three_tuple_still_formats():
    """旧测试夹具用的短元组。"""
    out = trace._format_candidates([(_Task("Chop"), 1.5, {"eta": -1.0})])
    assert out[0]["ok"] is True
    assert out[0]["layer"] is None


def test_top_layer_goes_into_log_entry(tmp_path, monkeypatch):
    """顶层也要记 top_layer：复盘时先看这一拍在哪层，再看候选。"""
    import json

    monkeypatch.setattr(trace, "TRACE_PATH", str(tmp_path / "t.jsonl"))
    record = {
        "mode_reason": "default",
        "p1": {},
        "candidates": [(_Task("FillCooker"), 4.8, {}, True, "", True, 5, True)],
        "top_layer": 5,
    }
    trace.emit(record, "Normal", None, {"status": "running"}, [], 100, None)
    line = json.loads(open(str(tmp_path / "t.jsonl")).read().strip())
    assert line["top_layer"] == 5

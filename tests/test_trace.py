"""Trace 决策日志单测。

重点验证：
  1. JSONL 格式正确（每行是合法 JSON）。
  2. 必需字段存在：t / mode / candidates / commit / exec / keys。
  3. candidates 数组每项含 task / score / ok / terms。
  4. 默认落盘位置在仓库内（不再写 /tmp）。
"""
import json
import os
import time
import pytest
from fnmatch import fnmatch

from ai import trace
from ai import tasks as T
from ai.blackboard import Blackboard, Commitment


@pytest.fixture(autouse=True)
def clean_log():
    """每个测试前清空日志。"""
    trace.clear_log()
    yield
    # 测试后也清理
    if os.path.exists(trace.TRACE_PATH):
        os.remove(trace.TRACE_PATH)


def test_default_path_is_repo_local_trace_jsonl():
    """默认落盘在仓库根目录的 trace-<时间戳>.jsonl，与 .gitignore 对应。"""
    path = trace.default_path()
    assert os.path.dirname(path) == trace.REPO_ROOT
    assert fnmatch(os.path.basename(path), "trace-*.jsonl")

    # /tmp 会被系统清理、且每局互相覆盖，正是实机复盘时日志凭空消失的原因
    assert not path.startswith("/tmp")
    assert "/tmp/" not in path


def test_default_path_uses_given_root_and_time():
    """落盘目录/时间戳可注入（测试与工具用）。"""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(0))
    path = trace.default_path(root="/somewhere/var", now=time.localtime(0))
    assert path == os.path.join("/somewhere/var", "trace-%s.jsonl" % stamp)


def test_trace_format_valid_jsonl():
    """每行是合法 JSON。"""
    record = {
        "mode_reason": "test",
        "p1": {},
        "candidates": []
    }
    trace.emit(record, "AI", {}, {}, [], 1)
    trace.emit(record, "AI", {}, {}, [], 2)

    # 读取日志文件，验证每行都是合法 JSON
    with open(trace.TRACE_PATH, "r") as f:
        lines = f.readlines()

    assert len(lines) == 2
    for line in lines:
        # 不应抛出异常
        obj = json.loads(line)
        assert isinstance(obj, dict)


def test_trace_contains_required_fields():
    """必需字段存在。"""
    record = {
        "mode_reason": "planning",
        "p1": {"holding": "Plate"},
        "candidates": [
            (T.Chop("order1", ("board_01",), "Tomato"), 5.2, {"urgency": 3.0, "eta": -1.0}, True, None, True)
        ]
    }
    bb = Blackboard()
    bb.commitment.active_task = T.Chop("order1", ("board_01",), "Tomato")
    bb.commitment.started_tick = 10

    trace.emit(
        record=record,
        mode="AI",
        commit=bb.commitment,
        exec_state={"node": "Approach", "status": "running"},
        keys=["w", "d"],
        tick=42
    )

    logs = trace.read_log()
    assert len(logs) == 1

    entry = logs[0]
    # 验证所有必需字段
    assert "t" in entry
    assert "mode" in entry
    assert "mode_reason" in entry
    assert "p1" in entry
    assert "candidates" in entry
    assert "commit" in entry
    assert "exec" in entry
    assert "keys" in entry

    # 验证具体值
    assert entry["t"] == 42
    assert entry["mode"] == "AI"
    assert entry["mode_reason"] == "planning"
    assert entry["keys"] == ["w", "d"]


def test_trace_candidates_structure():
    """candidates 数组结构正确。"""
    record = {
        "mode_reason": "",
        "p1": {},
        "candidates": [
            (
                T.Chop("order1", ("board_01",), "Tomato"),
                5.23,
                {"urgency": 3.14, "pressure": 0.0, "eta": -1.5},
                True,
                None,
                True
            ),
            (
                T.Chop("order2", ("board_02",), "Lettuce"),
                3.1,
                {"urgency": 2.0, "pressure": 0.5, "eta": -2.0},
                False,
                "resource_claimed",
                False
            )
        ]
    }

    trace.emit(record, "AI", {}, {}, [], 1)

    logs = trace.read_log()
    assert len(logs) == 1

    candidates = logs[0]["candidates"]
    assert len(candidates) == 2

    # 验证第一个候选
    cand1 = candidates[0]
    assert "task" in cand1
    assert "score" in cand1
    assert "ok" in cand1
    assert "veto" in cand1
    assert "terms" in cand1
    assert "chosen" in cand1

    assert "Chop" in cand1["task"]
    assert "Tomato" in cand1["task"]
    assert cand1["score"] == 5.23
    assert cand1["ok"] is True
    assert cand1["veto"] == ""
    assert cand1["chosen"] is True

    # 验证 terms 字段结构
    terms1 = cand1["terms"]
    assert "urgency" in terms1
    assert "pressure" in terms1
    assert "eta" in terms1
    assert terms1["urgency"] == 3.14
    assert terms1["eta"] == -1.5

    # 验证第二个候选
    cand2 = candidates[1]
    assert cand2["ok"] is False
    assert cand2["veto"] == "resource_claimed"
    assert cand2["chosen"] is False


def test_trace_empty_candidates():
    """候选列表为空时不崩溃。"""
    record = {
        "mode_reason": "no_candidates",
        "p1": {},
        "candidates": []
    }

    trace.emit(record, "P1", {}, {}, [], 5)

    logs = trace.read_log()
    assert len(logs) == 1
    assert logs[0]["candidates"] == []


def test_trace_commitment_format():
    """验证 commitment 格式化正确。"""
    bb = Blackboard()
    bb.commitment.active_task = T.Chop("order1", ("board_01",), "Tomato")
    bb.commitment.started_tick = 100

    record = {"mode_reason": "", "p1": {}, "candidates": []}
    trace.emit(record, "AI", bb.commitment, {}, [], 105)

    logs = trace.read_log()
    commit = logs[0]["commit"]

    assert "task" in commit
    assert "ticks" in commit
    assert "Chop" in commit["task"]
    assert commit["ticks"] == 100


def test_trace_empty_commitment():
    """无承诺时 commit 为空 dict。"""
    record = {"mode_reason": "", "p1": {}, "candidates": []}
    trace.emit(record, "AI", {}, {}, [], 1)

    logs = trace.read_log()
    assert logs[0]["commit"] == {}


def test_trace_exec_state():
    """验证执行状态正确记录。"""
    record = {"mode_reason": "", "p1": {}, "candidates": []}
    exec_state = {"node": "Sequence", "status": "running"}

    trace.emit(record, "AI", {}, exec_state, [], 1)

    logs = trace.read_log()
    assert logs[0]["exec"] == exec_state


def test_trace_multiple_entries():
    """验证多拍日志累积写入。"""
    for i in range(5):
        record = {"mode_reason": f"tick_{i}", "p1": {}, "candidates": []}
        trace.emit(record, "AI", {}, {}, [], i)

    logs = trace.read_log()
    assert len(logs) == 5

    for i, entry in enumerate(logs):
        assert entry["t"] == i
        assert entry["mode_reason"] == f"tick_{i}"


def test_trace_round_scores():
    """验证分数四舍五入到两位小数。"""
    record = {
        "mode_reason": "",
        "p1": {},
        "candidates": [
            (
                T.Chop("order1", ("board_01",), "Tomato"),
                5.23456789,
                {"urgency": 3.141592, "eta": -1.567},
                True,
                None,
                False
            )
        ]
    }

    trace.emit(record, "AI", {}, {}, [], 1)

    logs = trace.read_log()
    cand = logs[0]["candidates"][0]

    assert cand["score"] == 5.23  # 四舍五入
    assert cand["terms"]["urgency"] == 3.14
    assert cand["terms"]["eta"] == -1.57


def test_trace_unicode_handling():
    """验证 Unicode 字符正确处理（中文等）。"""
    record = {
        "mode_reason": "规划中",
        "p1": {"holding": "盘子"},
        "candidates": []
    }

    trace.emit(record, "AI", {}, {}, ["上", "下"], 1)

    logs = trace.read_log()
    entry = logs[0]

    assert entry["mode_reason"] == "规划中"
    assert entry["p1"]["holding"] == "盘子"
    assert entry["keys"] == ["上", "下"]


def test_trace_state_field():
    """state 参数落盘到 state 字段，默认是空 dict。"""
    record = {"mode_reason": "", "p1": {}, "candidates": []}
    state = {"plates": [{"n": "Plate_1"}], "map": {"p2_cell": [1, 2]}}

    trace.emit(record, "AI", {}, {}, [], 1, state=state)

    logs = trace.read_log()
    assert logs[0]["state"] == state


def test_trace_state_defaults_to_empty():
    """未传 state 时 state 字段为空 dict。"""
    record = {"mode_reason": "", "p1": {}, "candidates": []}
    trace.emit(record, "AI", {}, {}, [], 1)

    logs = trace.read_log()
    assert logs[0]["state"] == {}


def test_trace_bindings_field():
    """bindings 字段落盘（盘↔订单 / 灶↔订单 / 认领 / 分单归属）。"""
    record = {
        "mode_reason": "",
        "p1": {},
        "candidates": [],
        "bindings": {
            "plate_order": {"Plate_1": "order1"},
            "cooker_order": {"cooker_01": "order1"},
            "claims": {"cooker_01:fetch_to": {"owner": "P2", "order": "order1", "age": 0}},
            "order_assignment": {"order1": "P2"},
        },
    }

    trace.emit(record, "AI", {}, {}, [], 1)

    logs = trace.read_log()
    assert logs[0]["bindings"] == record["bindings"]


def test_trace_bindings_defaults_to_empty():
    """未传 bindings 时 bindings 字段为空 dict。"""
    record = {"mode_reason": "", "p1": {}, "candidates": []}
    trace.emit(record, "AI", {}, {}, [], 1)

    logs = trace.read_log()
    assert logs[0]["bindings"] == {}

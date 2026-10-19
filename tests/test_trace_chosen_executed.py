# -*- coding: utf-8 -*-
"""trace 要分得清「planner 挑了谁」与「实际跑了谁」。

实机 trace-20261015-165358 t=2582~2718：`WashPlate` 每拍都报
`veto=infeasible`，候选里 `chosen` 却在 WashPlate 与 FillCooker 之间 2:1
来回跳，而 `exec.task` 一直是 WashPlate。

机制：`_apply_commitment_inertia` 攒到 INFEASIBLE_THRESHOLD=3 决定改选，
`agent._tick_once` ⑨.5 的「后置条件已成立就让它把 SUCCESS 报完」又把旧
task 接回去 → streak 清零 → 下一拍重新计数，于是永远攒不满。

实际行为没错（WashPlate 最终在 t=2718 成功），但候选里的 `chosen` 记的是
planner 的**原始**选择，复盘时会读反：看着像在振荡，其实一直在跑同一棵树。
同样的 2:1 在 t=1617~1658（RescueCooked vs FetchToBoard）也有。

修法不是改行为，而是让 trace 把两者都记下来。
"""
import json

from ai.output import trace


def test_emit_records_executed_task_separately(tmp_path, monkeypatch):
    """planner 的选择与实际执行的 task 不同时，两者都要能从 trace 读出来。"""
    path = tmp_path / "t.jsonl"
    monkeypatch.setattr(trace, "TRACE_PATH", str(path))
    trace.emit(
        {"candidates": [], "chosen": "FillCooker(cooker_01)"},
        "Normal", None,
        {"status": "running", "task": "WashPlate(WashingPart)"},
        [], 2680)
    rec = json.loads(path.read_text().strip())
    assert rec["chosen"] == "FillCooker(cooker_01)", "planner 的原始选择"
    assert rec["exec"]["task"] == "WashPlate(WashingPart)", "实际跑的树"
    assert rec["chosen"] != rec["exec"]["task"], \
        "这一拍正是 ⑨.5 接管的情形，trace 必须能看出差异"


def test_agent_records_planner_choice():
    """Agent 要把 planner 的原始选择写进 record。

    ⑨.5 之后 chosen_task 已被改写成 active，所以必须在**覆盖之前**抓一份，
    否则两个字段会永远相等、这条可观测性就是假的。
    """
    import inspect

    from ai import agent
    src = inspect.getsource(agent.Agent._tick_once)
    assert '"chosen"' in src, "agent._tick_once 没记 planner 的原始选择"
    # **抓取**必须早于 ⑨.5 的覆盖点（record 字典本身在后面组装，无妨）。
    assert src.index("planner_choice = chosen_task") \
        < src.index("chosen_task = active"), \
        "要在 ⑨.5 覆盖前抓，否则记的是覆盖后的值、两个字段永远相等"

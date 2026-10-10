# -*- coding: utf-8 -*-
"""决策日志（Trace）。

职责：
  每拍输出一条 JSONL，记录模式 / 候选 / 打分 / 选中 task / 执行状态 / 按键。

落盘位置：仓库根目录 `trace-<时间戳>.jsonl`，每次启动一个新文件。
  - 不写 /tmp：系统会清理（实机复盘时文件凭空消失过），且每次跑都覆盖上
    一局的日志，事后无法回看；
  - 文件名与 .gitignore 里的 `trace-*.jsonl` 对应，不会误提交。

暂停 / 非关卡时不逐拍刷屏，只在该状态进入时写一条（见 agent 生命周期守
卫），这样"没有日志"和"AI 没决策"能分开。

Spec: §8.3 决策可观测性
"""
import json
import os
import time
from typing import Any, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_path(root=None, now=None):
    # type: (Optional[str], Optional[time.struct_time]) -> str
    """默认落盘路径：<root>/trace-<YYYYmmdd-HHMMSS>.jsonl。"""
    root = root or REPO_ROOT
    stamp = time.strftime("%Y%m%d-%H%M%S", now or time.localtime())
    return os.path.join(root, "trace-%s.jsonl" % stamp)


TRACE_PATH = default_path()


def execution_record(status, task, ctx, frame_seq=None, received_at=None):
    # type: (str, Any, Dict, Optional[int], Optional[float]) -> Dict
    """保留终态 task 与移动阶段，复盘时能区分旧帧等待 / 补步 / 真卡住。"""
    result = {"status": status}
    if task is not None:
        result["task"] = repr(task)
    if "motion" in ctx:
        result["motion"] = ctx["motion"]
    if frame_seq is not None and received_at is not None:
        result["observation"] = {"frame_seq": frame_seq,
                                 "age": round(time.monotonic() - received_at, 3)}
    return result


def emit(record, mode, commit, exec_state, keys, tick, state=None):
    # type: (Dict, str, Dict, Dict, List[str], int, Optional[Dict]) -> None
    """输出一拍的决策日志。

    Args:
        record: Planner 返回的 decision_record（含 candidates / p1 等）
        mode: 当前模式
        commit: bb.commitment
        exec_state: {"node": "...", "status": "..."}
        keys: 本拍按下的键列表
        tick: 当前拍号
        state: 可选。state_dump.snapshot(world) 的全量状态快照，
            落盘到 "state" 字段，用于复盘实体状态（盘/灶/锅/地图）。
    """
    log_entry = {
        "t": tick,
        "mode": mode,
        "mode_reason": record.get("mode_reason", ""),
        "p1": record.get("p1", {}),
        "candidates": _format_candidates(record.get("candidates", [])),
        "bindings": record.get("bindings", {}),
        "commit": _format_commitment(commit),
        "exec": exec_state,
        "keys": keys,
        "state": state or {},
    }

    with open(TRACE_PATH, "a") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")


def _format_candidates(candidates):
    # type: (List) -> List[Dict]
    """格式化候选列表。

    Args:
        candidates: List of (task, score, terms, ok, veto, chosen) tuples

    Returns:
        List of formatted candidate dicts
    """
    formatted = []
    for cand in candidates:
        # 解构候选元组：(task, score, terms, ok, veto, chosen)
        if len(cand) >= 6:
            task, score, terms, ok, veto, chosen = cand[:6]
        elif len(cand) >= 3:
            # 兼容不完整的候选格式（测试场景）
            task, score, terms = cand[:3]
            ok, veto, chosen = True, None, False
        else:
            continue

        formatted.append({
            "task": repr(task),  # 使用 __repr__，如 "Chop(Tomato, board_01, order#order1)"
            "score": round(float(score), 2),
            "ok": ok,
            "veto": veto if veto else "",
            "terms": {k: round(float(v), 2) for k, v in terms.items()} if terms else {},
            "chosen": chosen
        })
    return formatted


def _format_commitment(commit):
    # type: (Any) -> Dict
    """格式化承诺状态。

    Args:
        commit: Blackboard.Commitment 对象或 dict

    Returns:
        格式化的承诺 dict
    """
    # 处理 None 或空承诺
    if not commit:
        return {}

    # 处理 Commitment dataclass
    if hasattr(commit, "active_task"):
        if not commit.active_task:
            return {}
        return {
            "task": repr(commit.active_task),
            "ticks": commit.started_tick
        }

    # 处理 dict 形式
    if isinstance(commit, dict):
        if not commit.get("active_task"):
            return {}
        return {
            "task": str(commit["active_task"]),
            "ticks": commit.get("started_tick", 0)
        }

    return {}


def clear_log():
    # type: () -> None
    """清空日志文件（测试用）。"""
    with open(TRACE_PATH, "w") as f:
        f.write("")


def read_log():
    # type: () -> List[Dict]
    """读取并解析日志文件（测试用）。"""
    try:
        with open(TRACE_PATH, "r") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []

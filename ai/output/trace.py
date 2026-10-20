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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_path(root=None, now=None):
    # type: (Optional[str], Optional[time.struct_time]) -> str
    """默认落盘路径：<root>/trace-<YYYYmmdd-HHMMSS>.jsonl。"""
    root = root or REPO_ROOT
    stamp = time.strftime("%Y%m%d-%H%M%S", now or time.localtime())
    return os.path.join(root, "trace-%s.jsonl" % stamp)


TRACE_PATH = default_path()


def _score_of(state):
    # type: (Dict) -> Dict
    return state.get("score") or {}


def _orders_of(state):
    # type: (Dict) -> List[Dict]
    out = []
    for o in (state.get("orders") or []):
        if not isinstance(o, dict):
            continue
        out.append({"id": o.get("id"),
                    "remaining": o.get("remaining"),
                    "lifetime": o.get("lifetime")})
    return out


def _time_left(state):
    # type: (Dict) -> Any
    timer = state.get("timer") or {}
    for side in ("server", "client"):
        part = timer.get(side) or {}
        if part.get("left") is not None:
            return part["left"]
    return None


def _expired_ids(prev_orders, orders):
    # type: (List[Dict], List[Dict]) -> List[Any]
    """过期判定：同一 id 的 remaining 从低位跳回接近 lifetime。

    订单 id 会被复用（过期后同位置换新单），所以「id 消失」不是可靠信号，
    唯一可观测的事实是 remaining 回弹（实测 0.1 → 129.9）。
    """
    before = {o.get("id"): o for o in prev_orders if o.get("id") is not None}
    out = []
    for o in orders:
        oid = o.get("id")
        prev = before.get(oid)
        if prev is None:
            continue
        rem, old = o.get("remaining"), prev.get("remaining")
        if rem is None or old is None:
            continue
        try:
            if float(rem) > float(old) + _EXPIRY_REBOUND_SECONDS:
                out.append(oid)
        except (TypeError, ValueError):
            continue
    return out


# remaining 回弹多少秒算「换了新单」。单帧正常只会减少约 0.2s（5Hz 推帧），
# 留足余量以免丢帧时误判。
_EXPIRY_REBOUND_SECONDS = 5.0

# 上一拍的收益快照（模块级：trace.emit 本身是无状态函数式接口）
_prev_revenue = {}  # type: Dict[str, Any]


def reset_revenue_state():
    # type: () -> None
    """清空跨拍收益状态（测试/换局用）。"""
    _prev_revenue.clear()


def _revenue(state):
    # type: (Dict) -> Dict
    """一拍的收益摘要：分数 / 连击 / 剩余时间 / 订单，外加三类事件。

    事件（送达 / 连击归零 / 订单过期）都靠与上一拍比较得出。旧 trace 只落
    原始 state，复盘要自己 diff 4577 行才能看出 combo 是哪拍断的——而那正是
    tips 偏低的全部原因（超时实测不扣分，代价只在连击）。
    """
    score = _score_of(state)
    orders = _orders_of(state)
    rev = {
        "deliveries": score.get("deliveries"),
        "base": score.get("base"),
        "tips": score.get("tips"),
        "combo": score.get("combo"),
        "multiplier": score.get("multiplier"),
        "time_left": _time_left(state),
        "orders": orders,
    }  # type: Dict[str, Any]

    prev = dict(_prev_revenue)
    if prev:
        d_now, d_before = rev["deliveries"], prev.get("deliveries")
        if d_now is not None and d_before is not None and d_now > d_before:
            rev["delivered"] = True
            rev["base_gain"] = (rev["base"] or 0) - (prev.get("base") or 0)
            rev["tip_gain"] = (rev["tips"] or 0) - (prev.get("tips") or 0)
        c_now, c_before = rev["combo"], prev.get("combo")
        if c_now == 0 and c_before:
            rev["combo_broke"] = True
            rev["combo_was"] = c_before
        expired = _expired_ids(prev.get("orders") or [], orders)
        if expired:
            rev["expired"] = expired

    _prev_revenue.clear()
    _prev_revenue.update({"deliveries": rev["deliveries"], "base": rev["base"],
                          "tips": rev["tips"], "combo": rev["combo"],
                          "orders": orders})
    return rev


def execution_record(status, task, ctx, frame_seq=None, received_at=None):
    # type: (str, Any, Dict, Optional[int], Optional[float]) -> Dict
    """保留终态 task 与移动阶段，复盘时能区分旧帧等待 / 补步 / 真卡住。"""
    result = {"status": status}
    if task is not None:
        result["task"] = repr(task)
    if "motion" in ctx:
        result["motion"] = ctx["motion"]
    for name in ('interaction', 'interaction_failure', 'interaction_recovery', 'alignment'):
        if name in ctx:
            result[name] = ctx[name]
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
        "waiting": record.get("waiting", {}),
        "p1": record.get("p1", {}),
        "candidates": _format_candidates(record.get("candidates", [])),
        "bindings": record.get("bindings", {}),
        # 这一拍选在哪层。复盘先看它，再看候选明细（spec §6）。
        "top_layer": record.get("top_layer"),
        # planner 这一拍**原始**选的 task（字符串）。与 exec.task 可能不同：
        # agent ⑨.5 会把「后置条件已成立的旧树」接回去让它报完 SUCCESS。
        # 实机 trace-20261015-165358 t=2582~2718 的 WashPlate/FillCooker 2:1
        # 「振荡」就是这个——候选里的 chosen 在跳，实际一直跑同一棵树，
        # 不记这一项复盘必读反。
        "chosen": record.get("chosen"),
        # 对话分支（spec §6.4）：{task key: 生料名}。认领复盘的第一步；
        # `rescue_override` 是截止闸抢回的订单 id（没抢单时缺省）。
        "branches": record.get("branches") or {},
        "rescue_override": record.get("rescue_override"),
        # 对话层本拍做的事：命令/ack、事件、偏好条数（spec §6.3 第 5 条）。
        # 没有对话 worker 时是空 dict，复盘能直接看出语音是否接入。
        "dialog": record.get("dialog") or {},
        "commit": _format_commitment(commit),
        "exec": exec_state,
        "keys": keys,
        "state": state or {},
    }
    # 收益摘要只在有快照时给：暂停/非关卡那条没有 state，别凭空造字段
    # （"revenue 缺失" 本身就是"这拍没在玩"的信号）。
    if state:
        log_entry["revenue"] = _revenue(state)

    with open(TRACE_PATH, "a") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")


def _format_candidates(candidates):
    # type: (List) -> List[Dict]
    """格式化候选列表。

    Args:
        candidates: List of (task, score, terms, ok, veto, chosen[, layer,
            in_top_layer]) tuples

    Returns:
        List of formatted candidate dicts
    """
    formatted = []
    for cand in candidates:
        layer, in_top_layer = None, False
        # 解构候选元组。分层后尾部多两项（spec §6）；旧的 6 元组与 3 元组
        # 仍要能格式化——测试与 plan_firefighting 都还在用短元组。
        if len(cand) >= 8:
            task, score, terms, ok, veto, chosen, layer, in_top_layer = cand[:8]
        elif len(cand) >= 6:
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
            "chosen": chosen,
            # 「为什么没选它」：ok=False 是被否决，ok=True + in_top_layer=False
            # 是「可行但层不够高」。两者在旧 trace 里无法区分。
            "layer": layer,
            "in_top_layer": bool(in_top_layer),
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
    """清空日志文件（测试用）。跨拍收益状态一并复位，免得用例互相串。"""
    reset_revenue_state()
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

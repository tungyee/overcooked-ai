#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线测：真实 MIMO（游戏同款 ultraspeed）能否对 1-3 正确分工。

和游戏运行时走同一条 prompt / 工具协议：
  - system = 人设 + 本关 DAG + 攻略摘要（division=True, cookbook=有攻略）；
  - 事件 = "division"（本关开始，按攻略和站位 set_division + speak）；
  - 工具 = tool_schemas 按事件裁剪（非语音回合不带派活工具）；
  - LLM = ai.dialog.llm.LLMClient，payload 带 `thinking: {"type": "disabled"}`。

只读 trace 与 `.env`，不连游戏、不注入按键、不播音频。密钥只从环境变量读。

用法（仓库根执行，用 .venv）：
  .venv/bin/python tools/test_division_llm.py                 # 默认 1-3，跑 3 次
  .venv/bin/python tools/test_division_llm.py --runs 5        # 跑 5 次统计耗时
  .venv/bin/python tools/test_division_llm.py --dry-run --show-prompt  # 只打 prompt
  .venv/bin/python tools/test_division_llm.py --level Sushi_1_4_2P --trace trace-xxx.jsonl
"""
import argparse
import glob
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ai.dialog.env import load_env_file                  # noqa: E402
from ai.dialog.llm import (LLMClient, event_note,        # noqa: E402
                           state_message, system_message)
from ai.dialog.worker import _tools_for_event            # noqa: E402
from ai.exec.client import NullExec                      # noqa: E402
from ai.query.recipes import RecipeBook                  # noqa: E402
from ai.state.world import World                         # noqa: E402

DEFAULT_LEVEL = "Sushi_1_3_2P"

# 1-3 的正确分工（按站台几何，见 docs/map/Sushi_1_3.md 与 ai/output/map_render.py）：
#   砧板全在左、灶台全在右、出餐口在左；
#   虾/海苔/米箱在左，黄瓜箱在右但砧板在左。
# 因此：煮饭归你（右灶）；切虾、切黄瓜都归玩家（左砧板）。
_HARD_EXPECT = [
    ("cook", None, "you"),
    ("chop", "sushiprawn", "player"),
    ("chop", "cucumber", "player"),
]
# 装盘在左出餐口附近；允许 player 或 both，只报告不硬判。
_LOOSE_EXPECT = [
    ("plate", None, "player"),
]


def _parse_expect(spec):
    # type: (str) -> Tuple[str, Optional[str], str]
    """`action[:item]=owner` → (action, item_or_None, owner)。"""
    left, owner = spec.rsplit("=", 1)
    if ":" in left:
        action, item = left.split(":", 1)
    else:
        action, item = left, None
    return (action.strip().lower(), (item.strip().lower() if item else None),
            owner.strip().lower())


def _state_for_world(record):
    # type: (Dict) -> Optional[Dict]
    """把 trace 记录还原成 World 接受的帧（state_dump.snapshot → 原始帧形状）。"""
    state = record.get("state")
    if not isinstance(state, dict) or not state:
        return None
    out = dict(state)
    map_ = out.pop("map", None)
    if isinstance(map_, dict):
        for key in ("grid", "grids", "terrain", "occupancy", "hazards",
                    "belts", "wind_zones", "portals", "teleports",
                    "level_bounds"):
            if key in map_ and map_.get(key) is not None:
                out[key] = map_[key]
    return out


def _iter_records(path):
    # type: (str) -> Any
    import gzip
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield line_no, record


def _find_frame(traces, level):
    # type: (List[str], str) -> Optional[Tuple[str, int, Dict]]
    """在 trace 里找第一个 `phase=level` 且关卡匹配、有站点的帧。"""
    for path in traces:
        for line_no, record in _iter_records(path):
            state = _state_for_world(record)
            if not state:
                continue
            if state.get("phase") != "level" or state.get("paused") is True:
                continue
            if state.get("level") != level:
                continue
            if not state.get("stations"):
                continue
            return path, line_no, state
    return None


def _latest_traces():
    # type: () -> List[str]
    # 新 trace 落 runs/，旧 trace 还在仓库根：两个都扫，按 mtime 取最新。
    patterns = [os.path.join(_ROOT, "runs", "trace-*.jsonl"),
                os.path.join(_ROOT, "trace-*.jsonl")]
    files = sorted((p for pat in patterns for p in glob.glob(pat)),
                   key=os.path.getmtime, reverse=True)
    return files


def _load_llm(args):
    # type: (Any) -> LLMClient
    """MIMO（游戏同款 provider）；不打印 key。"""
    key = os.environ.get("MIMO_API_KEY")
    model = args.model or os.environ.get("MIMO_MODEL")
    if not key or not model:
        print("[test] 缺 MIMO_API_KEY / MIMO_MODEL（.env 或环境变量）",
              file=sys.stderr)
        sys.exit(2)
    base = args.base_url or os.environ.get(
        "MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
    timeout = args.timeout or float(os.environ.get("OC2_LLM_TIMEOUT", "30"))
    return LLMClient(key, base, model, timeout=timeout)


def _build_agent(world):
    # type: (World) -> Any
    with patch("ai.agent.StateClient", lambda *a, **k: MagicMock()):
        from ai.agent import Agent
        agent = Agent(voice=None, exec_client=NullExec())
    agent.book = RecipeBook(world.recipes, world.stations)
    agent.bb.tick = 1000
    agent.tick = 1000
    agent._load_guide(getattr(world, "level", None),
                      getattr(world, "level_number", None))
    # 开局分工事件会带站位事实（district + 出生点）；复刻 _update_division 的
    # 首触发路径，尽量和线上一致。zones 算不出来就跳过 district 那句。
    try:
        agent.bb.zones = agent._zones.observe(world, agent.bb.division)
    except Exception:                     # noqa: BLE001
        pass
    return agent


def _build_messages(agent, world, show_prompt=False):
    # type: (Any, World, bool) -> Tuple[List[dict], List[dict], str]
    """按 worker._run_llm 的 division 路径拼消息与工具。"""
    context = agent._build_dialog_context(world)
    tools = _tools_for_event(context.get("tools") or None, "division")
    head = [system_message(
        context.get("dag") or "", context.get("guide") or "",
        division=True, cookbook=(agent._guide is not None), actions=False)]
    messages = head + [state_message("division", context,
                                     event_detail=agent._division_map(world)),
                       event_note("division")]
    if show_prompt:
        for msg in messages:
            print("---- [%s] ----" % msg["role"])
            print(msg.get("content") or "")
        print("---- tools ----")
        for t in tools:
            fn = (t or {}).get("function") or {}
            print("  - %s" % fn.get("name"))
        print("---- /prompt ----")
    return messages, tools, facts


def _effective_owner(assignments):
    # type: (List[Dict]) -> Any
    """物料级覆盖动作级（与 DivisionState 同语义），返回 owner_of(action, item)。"""
    action_owner = {}   # type: Dict[str, str]
    item_owner = {}     # type: Dict[Tuple[str, str], str]
    for a in assignments or ():
        action = str(a.get("action") or "").strip().lower()
        owner = str(a.get("owner") or "").strip().lower()
        item = str(a.get("item") or "").strip().lower() or None
        if item is None:
            action_owner[action] = owner
        else:
            item_owner[(action, item)] = owner

    def owner_of(action, item=None):
        # type: (str, Optional[str]) -> Optional[str]
        if item is not None:
            got = item_owner.get((action, item))
            if got is not None:
                return got
        return action_owner.get(action)
    return owner_of


def _run_once(agent, world, client, args):
    # type: (Any, World, LLMClient, Any) -> Dict
    messages, tools, _facts = _build_messages(
        agent, world, show_prompt=bool(args.show_prompt))
    t0 = time.monotonic()
    reply = client.chat(messages, tools, tool_choice="auto")
    total_ms = (time.monotonic() - t0) * 1000.0

    assignments = []      # type: List[Dict]
    speaks = []           # type: List[str]
    for call in reply.tool_calls or ():
        if call.name == "set_division":
            args_obj = call.args() or {}
            assignments.extend(args_obj.get("assignments") or [])
        elif call.name == "speak":
            s = (call.args() or {}).get("text")
            if s:
                speaks.append(s)

    owner_of = _effective_owner(assignments)
    hard_fails = []
    for action, item, want in _HARD_EXPECT:
        got = owner_of(action, item)
        if got != want:
            hard_fails.append("%s%s=%s（期望 %s）" % (
                action, ":" + item if item else "", got or "未分配", want))
    loose_notes = []
    for action, item, want in _LOOSE_EXPECT:
        got = owner_of(action, item)
        if got not in (want, "both"):
            loose_notes.append("%s%s=%s（建议 %s/both）" % (
                action, ":" + item if item else "", got or "未分配", want))

    if not assignments:
        hard_fails.append("没有调用 set_division")
    return {
        "total_ms": total_ms,
        "first_token_ms": reply.first_token_ms,
        "assignments": assignments,
        "speak": "｜".join(speaks),
        "ok": not hard_fails,
        "hard_fails": hard_fails,
        "loose_notes": loose_notes,
    }


def _fmt_ms(v):
    # type: (Optional[float]) -> str
    return "%.0fms" % v if v is not None else "n/a"


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--level", default=DEFAULT_LEVEL)
    parser.add_argument("--trace", action="append",
                        help="trace JSONL（可多次；默认最新 trace-*.jsonl）")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--model", help="覆盖 MIMO_MODEL")
    parser.add_argument("--base-url", help="覆盖 MIMO_BASE_URL")
    parser.add_argument("--timeout", type=float, default=0.0)
    parser.add_argument("--dry-run", action="store_true", help="只打 prompt 不请求")
    parser.add_argument("--show-prompt", action="store_true")
    parser.add_argument("--env-file")
    parser.add_argument("--no-env-file", action="store_true")
    args = parser.parse_args(argv)

    if not args.no_env_file:
        env_path = args.env_file or os.path.join(_ROOT, ".env")
        if os.path.exists(env_path):
            load_env_file(env_path)

    traces = args.trace or _latest_traces()
    if not traces:
        print("[test] 没有 trace-*.jsonl", file=sys.stderr)
        return 2
    found = _find_frame(traces, args.level)
    if found is None:
        print("[test] 这些 trace 里没有 %s 的 level 帧：%s"
              % (args.level, ", ".join(os.path.basename(t) for t in traces)),
              file=sys.stderr)
        return 2
    path, line_no, state = found
    world = World(json.loads(json.dumps(state)))
    agent = _build_agent(world)

    print("[test] 关卡=%s  模型=%s  thinking=disabled  runs=%d"
          % (args.level, args.model or os.environ.get("MIMO_MODEL", "?"),
             args.runs))
    print("[test] 帧 %s:%d" % (os.path.basename(path), line_no))
    if args.dry_run:
        _build_messages(agent, world, show_prompt=True)
        return 0

    client = _load_llm(args)
    first = []      # type: List[float]
    total = []      # type: List[float]
    ok_count = 0
    for run in range(1, args.runs + 1):
        result = _run_once(agent, world, client, args)
        first.append(result["first_token_ms"])
        total.append(result["total_ms"])
        ok_count += 1 if result["ok"] else 0
        print("run %d: 总耗时 %s  首token %s  %s"
              % (run, _fmt_ms(result["total_ms"]),
                 _fmt_ms(result["first_token_ms"]),
                 "PASS" if result["ok"] else "FAIL"))
        for a in result["assignments"]:
            item = a.get("item")
            print("    set_division %s%s -> %s" % (
                a.get("action"), (":" + item) if item else "", a.get("owner")))
        if result["speak"]:
            print("    speak: %s" % result["speak"])
        for f in result["hard_fails"]:
            print("    ✗ %s" % f)
        for n in result["loose_notes"]:
            print("    · %s" % n)

    def _agg(vals):
        if not vals:
            return "-"
        return "%s / %s / %s" % (
            _fmt_ms(min(vals)), _fmt_ms(sum(vals) / len(vals)), _fmt_ms(max(vals)))

    print("汇总: 首token(min/avg/max)=%s  总耗时(min/avg/max)=%s  通过 %d/%d"
          % (_agg(first), _agg(total), ok_count, args.runs))
    return 0 if ok_count == args.runs else 1


if __name__ == "__main__":
    sys.exit(main())

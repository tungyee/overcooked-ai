#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用历史 trace 的真实状态回放 LLM 对话回合（v2：账本 + function calling）。

历史 trace 每拍落一条 JSONL；较新的 trace 带 `state` 快照。本脚本把快照
还原成 `World`，按局面选帧，然后走**线上同一条路径**：

  DialogWorker._run_llm（真实方舟 LLM）→ DutyBatch → Agent._service_dialog
  落账本 → ack 写回历史 → planner 重新选

每个场景是一串回合（玩家原话或 new_order / rescue 事件），逐轮检查：

  - 有没有回复、回复长度；
  - 工具调用是否全部落地（被拒的打印原因）；
  - 账本是否符合预期（mode + target）；
  - 落地后 P2 的 planner 选择是否符合预期（避开 / 转向某类节点）。

用法（仓库根执行）：
  python3 tools/replay_dialog_llm.py --trace ai/trace-20261019-145954.jsonl
  python3 tools/replay_dialog_llm.py --trace ... --scenario swap --show-prompt
  python3 tools/replay_dialog_llm.py --trace ... --dry-run   # 只选帧、打印上下文

只读 trace 与 `.env`，不连游戏、不注入按键、不播放音频。密钥只从环境
变量读取，不会打印。
"""
import argparse
import gzip
import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ai.core import planner  # noqa: E402
from ai.dialog.env import load_env_file  # noqa: E402
from ai.dialog.llm import (LLMClient, speech_message,  # noqa: E402
                           state_message, system_message)
from ai.dialog.schema import VoiceConfig  # noqa: E402
from ai.dialog.worker import DialogClients, DialogWorker  # noqa: E402
from ai.query.recipes import RecipeBook  # noqa: E402
from ai.state.world import World  # noqa: E402

REPLY_HARD_MAX = 40


# ----------------------------------------------------------------- 帧还原


def _state_for_world(record):
    # type: (Dict) -> Optional[Dict]
    """把 trace 记录还原成 World 接受的帧。

    新 trace 的 `state` 是 `state_dump.snapshot()` 的精简形状：地图相关
    字段收在 `map` 下，且没有 `nodeMap`。这里展开回顶层；旧 trace 没有
    `state` 时，如果记录本身长得像原始帧就直接用。
    """
    state = record.get("state")
    if not isinstance(state, dict) or not state:
        if all(key in record for key in ("chefs", "orders", "stations")):
            state = record
        else:
            return None
    out = dict(state)
    map_ = out.pop("map", None)
    if isinstance(map_, dict):
        for key in ("grid", "grids", "terrain", "occupancy", "hazards"):
            if out.get(key) is None and map_.get(key) is not None:
                out[key] = map_[key]
        if not out.get("belts") and map_.get("belt_cells") is not None:
            out["belts"] = _belts_from_map(map_)
    return out


def _belts_from_map(map_):
    # type: (Dict) -> List[Dict]
    """snapshot 只存 belt_cells/belt_dir；还原成 World 读的 belts[]。"""
    dirs = map_.get("belt_dir") or {}
    out = []
    for cell in map_.get("belt_cells") or ():
        if not isinstance(cell, (list, tuple)) or len(cell) != 2:
            continue
        gx, gz = cell
        direction = dirs.get("%d,%d" % (gx, gz)) or (0, 0)
        out.append({"gx": gx, "gz": gz,
                    "vx": direction[0], "vz": direction[1]})
    return out


def _tick_of(record):
    # type: (Dict) -> int
    tick = record.get("t")
    if tick is None:
        tick = (record.get("state") or {}).get("tick")
    try:
        return int(tick or 0)
    except (TypeError, ValueError):
        return 0


def _iter_records(path):
    # type: (str) -> Iterable[Tuple[int, Dict]]
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


@dataclass
class Frame:
    path: str
    line: int
    tick: int
    state: Dict

    def world(self):
        # type: () -> World
        # 每次新建：planner/agent 会改 World 上的派生字段
        return World(json.loads(json.dumps(self.state)))


# ----------------------------------------------------------------- 选帧


def _types_of(world, book):
    # type: (World, RecipeBook) -> set
    from ai.agent import Agent
    with patch("ai.agent.StateClient", lambda *a, **k: MagicMock()):
        agent = Agent(voice=None)
    agent.book = book
    _live, pm, cm = agent._dialog_matches(world, book)
    cands = planner.generate_candidates(world, agent.bb, None, book,
                                        plate_match=pm, cooker_match=cm)
    return {(c.task.TYPE, c.task.item) for c in cands}


def _has(types, task_type):
    return any(t == task_type for t, _item in types)


# 选帧打分：>0 才入选，分高者胜。参数 (world, 候选身份集合)。
SELECTORS = {
    "any": lambda w, ty: 1.0 + len(w.orders),
    "chop": lambda w, ty: (2.0 if _has(ty, "FetchToBoard") or _has(ty, "Chop")
                           else 0) + len(w.orders),
    "cook": lambda w, ty: (2.0 if _has(ty, "FetchToCooker") or
                           _has(ty, "FillCooker") else 0) + len(w.orders),
    "deliver": lambda w, ty: 5.0 if _has(ty, "DeliverPlate") else 0,
    "busy": lambda w, ty: (1.0 if any(c.get("isCooking") for c in w.cookers)
                           else 0) + (1.0 if len(w.orders) >= 2 else 0),
}


def scan_frames(paths, sample_every, max_frames):
    # type: (List[str], int, int) -> Dict[str, Frame]
    best = {}  # type: Dict[str, Tuple[float, Frame]]
    built = 0
    for path in paths:
        for line_no, record in _iter_records(path):
            if max_frames and built >= max_frames:
                break
            state = _state_for_world(record)
            if state is None or state.get("phase") != "level" \
                    or state.get("paused") is True:
                continue
            tick = _tick_of(record) or line_no
            if sample_every > 1 and tick % sample_every:
                continue
            try:
                world = World(state)
                book = RecipeBook(world.recipes, world.stations)
                types = _types_of(world, book)
            except Exception:                     # noqa: BLE001
                continue
            if not world.orders:
                continue
            built += 1
            frame = Frame(path, line_no, tick, state)
            for key, fn in SELECTORS.items():
                score = fn(world, types)
                if score > 0 and (key not in best or score > best[key][0]):
                    best[key] = (score, frame)
    print("[replay] 构造 %d 帧" % (built,), file=sys.stderr)
    return {key: frame for key, (_s, frame) in best.items()}


# ----------------------------------------------------------------- 场景


@dataclass
class Expect:
    """一轮的期望；全部可选。

    duties：账本里必须有的 (mode, target 或链上的 task 类型)；no_duties：不能有的。
    avoid：落地后 P2 选择不能是这些类型；prefer：选择必须是这些类型之一。
    tools：True 必须调用工具 / False 不能调用 / None 不管。
    """
    duties: List[Tuple[str, str]] = field(default_factory=list)
    no_duties: List[Tuple[str, str]] = field(default_factory=list)
    avoid: Tuple[str, ...] = ()
    prefer: Tuple[str, ...] = ()
    tools: Optional[bool] = None
    speak: bool = True
    # 账本里每条 (mode) 约定都必须满足的加工方式（如 "切"）：查多认/错认
    only: Optional[Tuple[str, str]] = None


@dataclass
class Step:
    say: str = ""                 # 玩家原话；空 = 事件
    event: str = "speech"         # speech / new_order / rescue
    expect: Expect = field(default_factory=Expect)
    # 回合前改黑板（例如把某个 task 设成截止闸抢回），参数 (agent, world)
    setup: Optional[Callable[[Any, World], None]] = None
    # 回复里必须出现其一 / 不能出现的词（检查话术和事实是否一致）
    say_any: Tuple[str, ...] = ()
    say_none: Tuple[str, ...] = ()


@dataclass
class Scenario:
    name: str
    frame: str                    # SELECTORS 的 key
    steps: List[Step]
    note: str = ""


CHOP = ("FetchToBoard", "Chop")
COOK = ("FetchToCooker", "FillCooker", "TakeCookedToPlate")
PLATE = ("FetchToPlate",)

def _drop_sink(agent, world):
    """模拟没有洗碗池的关卡：从帧里拿掉 washing 站点。"""
    world.stations[:] = [st for st in world.stations
                         if st.get("t") != "washing"]
    _KITCHEN["current"] = agent._kitchen(world)


# run8（voice-real-20261019-run8.jsonl）里真机说过的话放在前面。
SCENARIOS = [
    Scenario("greet", "any", [
        Step("你好。", expect=Expect(tools=False)),
    ], "闲聊：要回话，不改账本"),
    Scenario("ask_doing", "busy", [
        Step("你在干啥？", expect=Expect(tools=False)),
    ], "状态提问：照世界状态如实回答"),
    Scenario("wash_and_i_chop", "chop", [
        Step("你先洗盘子，我来切菜。", expect=Expect(
            duties=[("leave_to_p1", "Chop")], tools=True, avoid=CHOP)),
    ], "run8：一句两处分工都要落"),
    Scenario("you_chop", "chop", [
        Step("你去切菜，我去。", expect=Expect(
            duties=[("do", "Chop")], tools=True)),
    ], "run8：半截话也要落「你去切菜」"),
    Scenario("nudge", "busy", [
        Step("你接着动，别在这里闷。", expect=Expect()),
    ], "run8：催促，要回话"),
    Scenario("insult", "busy", [
        Step("你这个垃圾还干啥了？", expect=Expect(tools=False)),
    ], "run8：被吐槽，认个错再说正事"),
    Scenario("swap", "chop", [
        Step("切菜我来。", expect=Expect(
            duties=[("leave_to_p1", "Chop")], avoid=CHOP,
            only=("leave_to_p1", "切"))),
        Step("算了，你来切，我去煮饭。", expect=Expect(
            duties=[("do", "Chop")],
            no_duties=[("leave_to_p1", "Chop")])),
    ], "改口：应 edit 原条目而不是叠加"),
    Scenario("stop_cook", "cook", [
        Step("先别煮了。", expect=Expect(
            duties=[("stop", "")], avoid=COOK)),
        Step("可以煮了。", expect=Expect(no_duties=[("stop", "")])),
    ], "叫停与恢复"),
    Scenario("i_deliver", "deliver", [
        Step("送餐我来。", expect=Expect(tools=False),
             say_any=("送餐",)),
    ], "送餐不开放认领：谁在旁边谁送，P2 不避开"),
    Scenario("i_cook", "cook", [
        Step("煮的东西我来负责。", expect=Expect(
            duties=[("leave_to_p1", "FetchToCooker")], tools=True,
            only=("leave_to_p1", "煮"), avoid=COOK)),
    ], "按加工方式认领：只认「要煮」的物料"),
    Scenario("absent_item", "any", [
        Step("帮我切个番茄。", expect=Expect(tools=False),
             say_any=("没有", "没番茄", "不在")),
    ], "场上没有的食材：不调工具，说明本关没有"),
    Scenario("no_sink", "any", [
        Step("你去洗碗。", setup=_drop_sink, expect=Expect(tools=False),
             say_any=("没有", "洗不了", "没洗碗池", "做不了")),
    ], "本关没有洗碗池：不调工具，如实说做不了"),
    Scenario("offtopic", "any", [
        Step("给我捶捶背。", expect=Expect(tools=False)),
    ], "跟游戏无关：轻松接一句，不调工具"),
    Scenario("new_order_then_yes", "any", [
        Step(event="new_order", expect=Expect(tools=False)),
        Step("好，就这么办。", expect=Expect(tools=True)),
    ], "新菜商量 → 玩家同意后落账本"),
]


# ----------------------------------------------------------------- 运行


class _Bridge(object):
    """把 DialogWorker 的队列接到 Agent；不启动线程、不碰音频。"""

    def __init__(self, cmd_q, ack_q):
        self.cmd_q, self.ack_q = cmd_q, ack_q
        self.ctx_q = queue.Queue(maxsize=1)
        self.event_q = queue.Queue(maxsize=8)
        self.events = []

    def start(self):
        pass

    def stop(self):
        pass

    def reset_state(self):
        pass

    def put_event(self, event):
        self.events.append(event)

    def publish_context(self, context):
        pass


class _Mute(object):
    """TTS/播放器/采集替身：只记下要说的话。"""

    def __init__(self):
        self.spoken = []
        self.paused = False
        self.is_playing = False

    def synthesize(self, text):
        self.spoken.append(text)
        return iter(())

    def play(self, chunks):
        list(chunks)

    def flush(self):
        pass


def _make_pair(frame, llm):
    # type: (Frame, Any, str) -> Tuple[Any, DialogWorker, _Mute]
    cmd_q, ack_q = queue.Queue(), queue.Queue()
    bridge = _Bridge(cmd_q, ack_q)
    with patch("ai.agent.StateClient", lambda *a, **k: MagicMock()):
        from ai.agent import Agent
        agent = Agent(dialog_worker=bridge)
    mute = _Mute()
    config = VoiceConfig(asr_key="", asr_resource_id="", tts_key="",
                         tts_voice="", tts_resource_id="", ark_key="",
                         ark_base_url="", ark_model="")
    clients = DialogClients(capture=mute, vad=mute, asr=None, tts=mute,
                            player=mute, llm=llm)
    worker = DialogWorker(config, cmd_q, ack_q, bridge.ctx_q,
                          bridge.event_q, clients=clients)
    return agent, worker, mute


def _prime(agent, world):
    """跑一拍对话服务 + planner：生成 new_order 记账、p2_options、承诺。"""
    agent.bb.tick = agent.tick = 1000
    agent.book = RecipeBook(world.recipes, world.stations)
    agent._service_dialog(world)
    return _replan(agent, world)


def _replan(agent, world):
    """与线上 ⑨–⑪ 同口径：规划、记下排名、登记承诺。"""
    from ai.agent import _ranked_tasks
    from ai.state.blackboard import Commitment
    chosen, record = planner.plan(world, agent.bb, None, agent.book)
    agent._last_ranked = _ranked_tasks(record)
    # 线上 ⑪ 会把选中的 task 登记成承诺；上下文的「P2 正在做」读它。
    if chosen is not None:
        agent.bb.commitment = Commitment(chosen, agent.tick, 0, 0, 0)
    return chosen


def _run_turn(agent, worker, world, step):
    # type: (Any, DialogWorker, World, Step) -> Dict
    """一轮：worker 线程里跑 LLM；主线程 20 Hz 驱动 _service_dialog 落地批次。"""
    context = agent._build_dialog_context(world)
    done = threading.Event()
    started = time.monotonic()

    def target():
        try:
            if step.event == "speech":
                worker.state.last_transcript = ""
                worker._run_llm(context, "speech", step.say)
            else:
                worker._run_llm(context, step.event, "",
                                new_orders=[o.get("id") for o in world.orders])
        finally:
            done.set()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    while not done.wait(0.05):
        agent.bb.tick += 1
        agent.tick += 1
        agent._service_dialog(world)
    thread.join(1)
    chosen = _replan(agent, world)
    return {"elapsed": time.monotonic() - started, "chosen": chosen,
            "context": context}


_KITCHEN = {}  # type: Dict[str, Any]


def _covers(duty, want):
    """期望里的 `want`：空串 = 任意；物料/共享动作名 = target 相同；
    task 类型名（如 Chop）= 这条物料链里有这一步。"""
    if not want or duty.target == want:
        return True
    kitchen = _KITCHEN.get("current")
    mat = getattr(kitchen, "materials", {}).get(duty.target) if kitchen else None
    return bool(mat is not None and want in mat.types)


def _check(step, mute_before, history_before, mute, agent, worker, chosen):
    # type: (Step, int, int, _Mute, Any, DialogWorker, Any) -> List[str]
    fails = []
    exp = step.expect
    said = mute.spoken[mute_before:]
    if exp.speak and not said:
        fails.append("没有回复")
    for text in said:
        if len(text) > REPLY_HARD_MAX:
            fails.append("回复太长（%d 字）" % (len(text),))
    turn_msgs = worker.state.history[history_before:]
    called = any(m.get("tool_calls") for m in turn_msgs)
    if exp.tools is True and not called:
        fails.append("应调用工具但没有")
    if exp.tools is False and called:
        fails.append("不该调用工具")
    for msg in turn_msgs:
        if msg.get("role") == "tool" and '"ok": false' in msg.get("content", ""):
            fails.append("工具被拒：%s" % (msg["content"],))
    duties = agent.ledger.duties
    for mode, prefix in exp.duties:
        if not any(d.mode == mode and _covers(d, prefix) for d in duties):
            fails.append("账本缺 %s %s*" % (mode, prefix))
    for mode, prefix in exp.no_duties:
        if any(d.mode == mode and _covers(d, prefix) for d in duties):
            fails.append("账本不该有 %s %s*" % (mode, prefix))
    text = "".join(said)
    if exp.only is not None:
        mode, proc = exp.only
        kitchen = _KITCHEN.get("current")
        for d in agent.ledger.duties:
            if d.mode != mode:
                continue
            procs = kitchen.processes(d.target) if d.target in kitchen.materials else []
            if proc not in procs:
                fails.append("%s %s 不是「要%s」的物料" % (mode, d.target, proc))
    if step.say_any and not any(w in text for w in step.say_any):
        fails.append("回复里没提到 %s" % ("/".join(step.say_any),))
    for word in step.say_none:
        if word in text:
            fails.append("回复里不该出现「%s」" % (word,))
    ctype = getattr(chosen, "TYPE", None)
    if exp.avoid and ctype in exp.avoid:
        fails.append("P2 仍选了 %s" % (ctype,))
    if exp.prefer and ctype not in exp.prefer:
        fails.append("P2 选了 %s，期望 %s" % (ctype, "/".join(exp.prefer)))
    return fails


def run_scenario(scenario, frame, llm, args):
    # type: (Scenario, Frame, Any, Any) -> bool
    world = frame.world()
    agent, worker, mute = _make_pair(frame, llm)
    before = _prime(agent, world)
    print("\n=== %s（%s）帧 %s:%d tick=%d" % (
        scenario.name, scenario.note, os.path.basename(frame.path),
        frame.line, frame.tick))
    print("    基线 P2 选择：%s" % (_label(before),))
    ok = True
    for index, step in enumerate(scenario.steps, 1):
        what = "P1「%s」" % (step.say,) if step.event == "speech" \
            else "[%s]" % (step.event,)
        _KITCHEN["current"] = agent._kitchen(world)
        if step.setup is not None:
            step.setup(agent, world)
        if args.show_prompt or args.dry_run:
            ctx = agent._build_dialog_context(world)
            msgs = [system_message(ctx.get("dag") or "")]
            msgs += worker.state.history
            msgs.append(state_message(step.event, ctx))
            if step.event == "speech":
                msgs.append(speech_message(step.say))
            print("---- prompt ----")
            for msg in msgs:
                print("[%s] %s" % (msg["role"], msg.get("content")))
            print("---- tools: %d ----" % (len(ctx.get("tools") or []),))
        if args.dry_run:
            continue
        spoken_before = len(mute.spoken)
        history_before = len(worker.state.history)
        result = _run_turn(agent, worker, world, step)
        fails = _check(step, spoken_before, history_before, mute, agent,
                       worker, result["chosen"])
        calls = []
        for msg in worker.state.history[history_before:]:
            for call in msg.get("tool_calls") or ():
                calls.append(call["function"]["arguments"])
        print("  %d. %s  (%.1fs)" % (index, what, result["elapsed"]))
        print("     回复：%s" % ("｜".join(mute.spoken[spoken_before:]) or "（无）",))
        if calls:
            print("     工具：%s" % ("  ".join(calls),))
        print("     账本：%s" % ("；".join(
            "%s %s %s" % (d.id, d.mode, d.target)
            for d in agent.ledger.duties) or "（空）",))
        print("     P2 选择：%s" % (_label(result["chosen"]),))
        for fail in fails:
            print("     ✗ %s" % (fail,))
        ok = ok and not fails
    return ok


def _label(task):
    # type: (Any) -> str
    if task is None:
        return "（无）"
    from ai.agent import _task_text
    return _task_text(task) or str(task)


def _load_llm(args):
    # type: (Any) -> Any
    if args.dry_run:
        return None
    if not args.no_env_file:
        env_path = args.env_file or os.path.join(_ROOT, ".env")
        if os.path.exists(env_path):
            load_env_file(env_path)
    key = os.environ.get("ARK_API_KEY")
    model = args.model or os.environ.get("ARK_MODEL")
    if not key or not model:
        print("[replay] 缺 ARK_API_KEY / ARK_MODEL", file=sys.stderr)
        sys.exit(2)
    base = os.environ.get("ARK_BASE_URL",
                          "https://ark.cn-beijing.volces.com/api/v3")
    return LLMClient(key, base, model,
                     timeout=float(os.environ.get("OC2_LLM_TIMEOUT", "20")))


def main(argv=None):
    # type: (Optional[List[str]]) -> int
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trace", action="append", required=True,
                        help="trace JSONL（可多次）")
    parser.add_argument("--scenario", action="append",
                        help="只跑指定场景（可多次）：%s"
                        % ", ".join(s.name for s in SCENARIOS))
    parser.add_argument("--sample-every", type=int, default=20)
    parser.add_argument("--max-frames", type=int, default=400)
    parser.add_argument("--model", help="覆盖 ARK_MODEL")
    parser.add_argument("--dry-run", action="store_true",
                        help="只选帧、打印 prompt，不请求 LLM")
    parser.add_argument("--show-prompt", action="store_true")
    parser.add_argument("--env-file")
    parser.add_argument("--no-env-file", action="store_true")
    args = parser.parse_args(argv)

    scenarios = [s for s in SCENARIOS
                 if not args.scenario or s.name in args.scenario]
    if not scenarios:
        print("[replay] 没有匹配的场景", file=sys.stderr)
        return 2
    frames = scan_frames(args.trace, args.sample_every, args.max_frames)
    llm = _load_llm(args)
    passed = failed = skipped = 0
    for scenario in scenarios:
        frame = frames.get(scenario.frame)
        if frame is None:
            print("\n=== %s：trace 里没有 %s 局面，跳过"
                  % (scenario.name, scenario.frame))
            skipped += 1
            continue
        if run_scenario(scenario, frame, llm, args):
            passed += 1
        else:
            failed += 1
    print("\n[replay] 通过 %d / 失败 %d / 跳过 %d" % (passed, failed, skipped))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

# 语音对话交互改进 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 P2 AI 陪玩「听懂并照做」：新增 `now`/`stop` 指令、收紧 `leave_to_p1` 抢回、修换关串味、补上下文事实、加随机送单 check-in 与几处体验打磨。

**Architecture:** 改动横跨对话层（`ai/dialog/`）与决策层（`ai/core/planner.py`、`ai/tasks/`、`ai/agent.py`）。核心是给账本加 `now`/`stop` 两档，并在 `planner.plan()` 可行性过滤之后、分层之前插一道 `_select_imperative` 硬指令闸（跳过层/打分/承诺惯性、只让位 L0/L1/L2）；其余是独立的小修（收闸、换关判据、上下文字段、check-in 定时器、UX）。

**Tech Stack:** Python 3.9（纯标准库决策逻辑）、pytest 7.4.4、`ai/` 纯库（无构建）。

**Spec:** `docs/superpowers/specs/2026-10-21-dialog-interaction-improvement-design.md`

## Global Constraints

- 全程**不动**任何 `W_*` / 分层阈值 / `COMMITMENT_MARGIN` / `INTERLEAVE_*`（见 `docs/agent/status-backlog.md` §4）。
- `now` 是**新增控制流**（闸），不是调权重；`stop` 是接通已有逻辑；收闸只改一个 margin 常数。
- 注释/文档中文、标识符英文；类型注解用 `# type: (...) -> ...` 注释形式；谓词无副作用、只读入参。
- 对外入口只认 `from ai import tasks as T` / `from ai import primitives as PR`，**不要 import 子模块**。
- 每个工作流先写失败测试再改实现，最后跑 `python3 -m pytest tests/` 全量。
- 不新增第三方依赖。

## Review Focus

按最可能咬人的顺序：

1. **`now` 与火情/糊锅/刚熟同时发生** → 必须让位 L0/L1/L2，不能为听指令而烧糊。测试钉在 Task 2。
2. **`now` 打到不可行的 target**（本关没这物料/动作、或物理做不了）→ 闸静默返回、回退正常规划，不崩溃不空转。测试钉在 Task 2。
3. **`stop`/`now`/`do` 对同一 target 先后出现** → 同 target 只有一条约定，后说覆盖先说。测试钉在 Task 1。
4. **check-in 与用户说话/播报撞车** → 不在录音/播放时开口；任何回合结束都重置随机排期。测试钉在 Task 7。
5. **换关同图重开**（`scene_epoch` 变、`level` 名不变）→ LLM 历史必须清空。测试钉在 Task 5。

---

### Task 1: 账本 + prompt 四档（`now` / `stop`）

**Files:**
- Modify: `ai/dialog/duties.py`（`MODES`、`WEIGHT_NOW`、`_MODE_TEXT`、`preferences()`、`tool_schemas()`）
- Modify: `ai/dialog/schema.py:29-33`（`Preference.kind` docstring）
- Modify: `ai/dialog/llm.py:176-209`（`_SYSTEM` 分工段 + 承诺段）
- Test: `tests/test_duties.py`、`tests/test_preferences.py`

**Interfaces:**
- Consumes: `Preference`（`ai/dialog/schema.py`，字段 `kind/scope/exclude/owner/weight/expires_tick/duty_id`）。
- Produces:
  - `MODES = ("do", "leave_to_p1", "now", "stop")`
  - `WEIGHT_NOW: float`（仅 trace 展示，实际选择走 Task 2 的闸）
  - `Preference(kind="now", owner="P2", weight=WEIGHT_NOW, ...)`（`Ledger.preferences()` 编译产物，Task 2 消费）
  - `tool_schemas()` 的 `mode` enum 含 4 值（LLM 侧消费）

- [ ] **Step 1: 写失败测试**

`tests/test_duties.py` 更新 `:313-317` 的断言：`mode enum == ["do", "leave_to_p1", "now", "stop"]`。
新增：`ledger.apply({op:"add", mode:"now", target:"Cucumber"})` → `preferences()` 产出 `Preference(kind="now", owner="P2", weight=WEIGHT_NOW)`；`mode="stop"` → `Preference(kind="stop", weight=0.0)`。
`tests/test_preferences.py` 新增：同 target 先 `do` 后 `now`，编译结果只留 `now` 一条（后说覆盖先说）。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_duties.py tests/test_preferences.py -q`
Expected: FAIL（mode enum 仍是 `["do","leave_to_p1"]`；`now` 被 `Ledger.apply` 的 `mode not in MODES` 拒绝）。

- [ ] **Step 3: 实现四档**

在 `ai/dialog/duties.py`：
- `MODES = ("do", "leave_to_p1", "now", "stop")`；新增 `WEIGHT_NOW = 20.0`（注释：仅 trace 展示）。
- `_MODE_TEXT` 加 `"now": "P2 马上去"`。
- `preferences()` 加 `now` 分支 → `Preference(kind="now", owner="P2", weight=WEIGHT_NOW, **common)`（`stop` 分支已存在，保持）。
- `tool_schemas()` 的 `mode.description` 改为描述 4 档（do=有空优先 / now=马上去 / leave_to_p1=交给P1 / stop=先别做）。
- `ai/dialog/schema.py:29` `kind` docstring 补 `now`。

在 `ai/dialog/llm.py` 的 `_SYSTEM` 分工段（`:188-203`）与承诺段（`:184`）：
- 分工段改为 4 档映射 + 正反例：`去/马上/先/现在/帮我X`→now；`你负责/归你/你来管/你包了`→do；`我来X/X交给我`→leave_to_p1；`先别X/别做X`→stop。加一句「拿不准一律 `now`」。
- 承诺段把「不要承诺『马上』『这就去』」改为「只有 `now` 命令可以说『这就去』，`do` 说『有空就做』，其余不承诺」（即 spec G.3）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_duties.py tests/test_preferences.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/duties.py ai/dialog/schema.py ai/dialog/llm.py tests/test_duties.py tests/test_preferences.py
git commit -m "feat(dialog): add now/stop duty modes and prompt mapping"
```

---

### Task 2: planner `now` 硬指令闸

**Files:**
- Modify: `ai/core/planner.py`（`plan()` 内插入点 + 新函数 `_select_imperative`）
- Test: `tests/test_now_command.py`（新建）

**Interfaces:**
- Consumes: `Preference(kind="now", ...)`（Task 1）；`preference_matches_task(pref, task)`（`ai/dialog/schema.py`）；`layer_of_task(task, world)`（`ai/core/planner_layers.py`）。
- Produces: `_select_imperative(feasible, world, bb) -> Optional[Candidate]`。

- [ ] **Step 1: 写失败测试**

`tests/test_now_command.py`：
- `test_now_selects_match_over_higher_layer`：feasible 里有一个 L3 DeliverPlate（不匹配）和一个 L6 Chop（匹配 `now` 的黄瓜 scope）→ `_select_imperative` 返回 Chop 那个 Candidate。
- `test_now_yields_to_fire`：feasible 里有 Extinguish → 返回 None。
- `test_now_yields_to_rescue`：feasible 里有 `_time_pressure >= 1.0` 的 TakeCookedToPlate → 返回 None。
- `test_now_no_match_returns_none`：feasible 无候选匹配 `now` scope → 返回 None。
- `test_plan_applies_imperative`：构造 bb + world 走 `plan()`，断言 `chosen_task` 是匹配候选且 record 带 `imperative` 标记。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_now_command.py -q`
Expected: FAIL（`_select_imperative` 未定义）。

- [ ] **Step 3: 实现 `_select_imperative` + 接入 `plan()`**

在 `ai/core/planner.py` 加：

```python
def _select_imperative(feasible, world, bb):
    # type: (List[Candidate], World, Blackboard) -> Optional[Candidate]
    # 找未过期的 now 偏好；没有则返回 None。
    # 任一 feasible 候选落在 L0/L1/L2（火/快糊/刚熟救锅）→ 让位返回 None。
    # 否则返回第一个命中 now scope 的 feasible 候选（feasible 顺序即链的下一步）。
```

接入 `plan()`：在可行性过滤循环（`planner.py:1162-1166` 之后、`:1174` `if feasible:` 之前）算 `imperative = _select_imperative(feasible, world, bb)`。若非 None：`chosen_task = imperative.task`，仍用 `layer_filter` 算 `layer_map` 供 trace，`record["imperative"] = chosen_task.key`，直接 `return chosen_task, record`（跳过 `_select_layer`/`_score_tasks`/`_apply_commitment_inertia`）。若 None：走原路径。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_now_command.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add ai/core/planner.py tests/test_now_command.py
git commit -m "feat(planner): add now imperative gate (yields to L0-L2)"
```

---

### Task 3: 洗盘软闸拆分（feasible → layer）

**Files:**
- Modify: `ai/tasks/plate.py:222-248`（`WashPlate.feasible()` 删软闸）
- Modify: `ai/core/planner_layers.py:63-83`（`build_rules` 的 L4 谓词）
- Modify: `ai/core/planner.py`（新增 world 级 `_placed_plates(world)` 或复用）
- Test: `tests/test_plate_supply_guard.py`（或新建 `tests/test_wash_plate_now.py`）

**Interfaces:**
- Consumes: `_plate_pressure(world) -> float`（`planner.py:1836`，0块=2.0 / 1块=1.0 / ≥2=0.0）；`dirty_plate_exists`、`clean_plate_count`（`ai/tasks/plate.py` / `ai/query/predicates.py`）。
- Produces: `_placed_plates(world) -> bool`（world 级：是否有盘落在 counter 拼装格）；L4 谓词 `plate_blocked(world) = (_plate_pressure(world) >= 2.0) and not _placed_plates(world)`。

- [ ] **Step 1: 写失败测试**

- `test_wash_feasible_when_clean_plates_remain`：干净盘 >0 + 有脏盘 → `WashPlate.feasible` 返回 True（物理可行，不再是 False）。
- `test_wash_infeasible_no_dirty`：无脏盘 → 仍 False（物理约束保留）。
- `test_wash_layer_only_when_plate_blocked`：`layer_of_task(WashPlate)` 只在 `clean==0 and not placed` 时为 L4，否则 L7。
- `test_wash_not_l4_when_placed_plate`：`clean==0` 但台上已有装配盘 → 不 L4（回归 guard，防召回「装配中却去洗盘」）。
- `test_now_wash_selects_when_clean_remain`：`_select_imperative`（或 `plan()`）在 `now 洗盘` 且干净盘有剩时能选中 WashPlate。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_plate_supply_guard.py -q`（含新用例）
Expected: FAIL（`WashPlate.feasible` 现在对干净盘>0 返回 False）。

- [ ] **Step 3: 实现拆分**

- `ai/tasks/plate.py` `WashPlate.feasible()`：删除 `if P.clean_plate_count(...) > 0 or placed_plates(ctx): return False` 这段（`:246-247`），保留 `done` / 续洗态 / `return dirty_plate_exists(...)`。
- `ai/core/planner.py`：加 `_placed_plates(world) -> bool`（world 级，把 `ai/tasks/base.py:246-264` 的 `placed_plates` 逻辑按 world 重写，或复用其判据）。
- `ai/core/planner_layers.py`：`build_rules` 的 L4 谓词从 `plate_pressure_fn(w) > 0` 改为 `plate_pressure_fn(w) >= 2.0 and not placed_plates_fn(w)`；`layer_of_task` 生产路径同步注入新谓词（`planner_layers.py:111-113`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_plate_supply_guard.py tests/test_layered_selection.py -q`
Expected: PASS（分层相关既有断言若因 L4 谓词收紧而变，按新语义更新，并在 commit 里注明）。

- [ ] **Step 5: Commit**

```bash
git add ai/tasks/plate.py ai/core/planner_layers.py ai/core/planner.py tests/test_plate_supply_guard.py
git commit -m "refactor(plate): split wash 'should' gate out of feasible into layer"
```

---

### Task 4: `leave_to_p1` 收闸

**Files:**
- Modify: `ai/core/planner.py:1400-1410`（`_rescue_margin_ticks`）
- Test: `tests/test_rescue_gate.py`

**Interfaces:**
- Consumes: `ETA.task_cost_ticks`、`ETA.COST_DELIVER`（`ai/revenue/eta.py`）。
- Produces: `_rescue_margin_ticks(task_type) -> int`（新余量窗口 2.5~5s）。

- [ ] **Step 1: 写失败测试**

`tests/test_rescue_gate.py`：新增断言 `_rescue_margin_ticks("Chop")` 返回 `max(50, min(0.15*d, 100))` 区间内的值，且 < 旧值（旧 `max(100, min(0.3*d, 200))`）。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_rescue_gate.py -q`
Expected: FAIL（旧 margin 更大）。

- [ ] **Step 3: 实现收窄**

`ai/core/planner.py:1400-1410` 的 `return int(max(100, min(int(0.3 * d), 200)))` 改为 `return int(max(50, min(int(0.15 * d), 100)))`。注释更新：余量窗口 5~10s → 2.5~5s，说明是「P1 有把握时不抢、真放鸽子还抢得回来」的折中。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_rescue_gate.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add ai/core/planner.py tests/test_rescue_gate.py
git commit -m "tune(planner): narrow leave_to_p1 rescue-gate margin to 2.5-5s"
```

---

### Task 5: 换关重置改用场景身份

**Files:**
- Modify: `ai/agent.py:404-432`（`_maybe_reset_dialog_level`）
- Test: `tests/test_agent_dialog.py`

**Interfaces:**
- Consumes: `world.level`、`world.raw` 的 `session_id`/`scene_epoch`（与 `_observe_scene` `agent.py:860` 同口径）。
- Produces: `self._dialog_scene_key`（场景身份三元组，替换 `self._dialog_level` 的判变依据）。

- [ ] **Step 1: 写失败测试**

`tests/test_agent_dialog.py`：新增 `test_level_replay_same_name_clears_history`——`level` 名不变、`scene_epoch` 变时，`_maybe_reset_dialog_level` 走完整重置（`ledger.clear()`、`bb.preferences` 清空、`bb.dialog = DialogState()`、`_seen_orders` 清空、`dialog_worker.reset_state()` 被调用）。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_agent_dialog.py::test_level_replay_same_name_clears_history -q`
Expected: FAIL（`level == self._dialog_level` 提前 return，不重置）。

- [ ] **Step 3: 实现**

`_maybe_reset_dialog_level` 的判变从 `world.level` 字符串改为 `key = (raw.get("session_id"), raw.get("scene_epoch"), world.level)`，存 `self._dialog_scene_key`。`key != self._dialog_scene_key` 时走完整重置；首次观察只记账不重置；`world.level is None`（菜单/加载帧）仍提前返回。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_agent_dialog.py -q`
Expected: PASS（既有的「首次观察不清」「换名清」「暂停不清」用例保持通过）。

- [ ] **Step 5: Commit**

```bash
git add ai/agent.py tests/test_agent_dialog.py
git commit -m "fix(dialog): reset on scene_epoch change, not just level name"
```

---

### Task 6: 上下文系统性规整

**Files:**
- Modify: `ai/agent.py:180-197`（`_plates_text`）、`ai/agent.py:559-612`（`_build_dialog_context`）
- Test: `tests/test_agent_dialog.py`

**Interfaces:**
- Consumes: `world.stations`（washing/chopping）、`world.cookers`、`world.items`（DirtyPlateStack）、`world.raw.score`、trace revenue 状态（`trace.py`）。
- Produces: `_dirty_plate_count(world) -> int`；`_build_dialog_context` 新增字段 `dirty_plates`、`boards`、`idle_cookers`、`combo`、`expiring_orders`。

- [ ] **Step 1: 写失败测试**

`tests/test_agent_dialog.py`：`test_build_dialog_context_has_grounding_fields` 断言 `_build_dialog_context(world)` 返回 dict 含 `dirty_plates`（整数）、`boards`（列表）、`idle_cookers`（整数）、`combo`（可为 None/int）、`expiring_orders`（列表）；`_plates_text` 输出含「脏盘 N 个」。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_agent_dialog.py::test_build_dialog_context_has_grounding_fields -q`
Expected: FAIL（字段缺失）。

- [ ] **Step 3: 实现**

- 新增 `_dirty_plate_count(world)`：`sum(st.plate_count for st in world.stations if st.t=="washing")` + `world.items` 里名字含 `dirtyplate` 的条数。
- `_plates_text` 末尾追加「脏盘 N 个」。
- `_build_dialog_context` 加字段：`dirty_plates`（上）、`boards`（chopping 板内容列表）、`idle_cookers`（空灶计数）、`combo`（`world.raw.score` 连击，取不到就 None）、`expiring_orders`（`orders` 里 remaining 低于阈值的单，阈值用常量）。
- `llm.py:render_state` 补渲染这些新字段（给 LLM 看）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_agent_dialog.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add ai/agent.py ai/dialog/llm.py tests/test_agent_dialog.py
git commit -m "feat(dialog): add grounding fields (dirty plates, boards, idle cookers, combo)"
```

---

### Task 7: 定时送单 check-in（随机排期）

**Files:**
- Modify: `ai/dialog/policy.py`（`should_checkin`）
- Modify: `ai/dialog/worker.py`（`_next_checkin_at`、`_run_llm` 末尾、`_run` 循环、`reset_state`）
- Modify: `ai/dialog/llm.py:211-215`（`_EVENT_TEXT["checkin"]`）
- Test: `tests/test_dialog_policy.py`、`tests/test_dialog_worker.py`

**Interfaces:**
- Consumes: `_run_llm(context, event, transcript, new_orders, timing)`（`worker.py:981`）；`reset_state`（`worker.py:515`）。
- Produces: `policy.should_checkin(next_checkin_at, now, has_orders, in_level) -> bool`；worker 字段 `_next_checkin_at`。

- [ ] **Step 1: 写失败测试**

`tests/test_dialog_policy.py`：`should_checkin(None, now, True, True) -> False`；`should_checkin(now+10, now, True, True) -> False`；`should_checkin(now-1, now, True, True) -> True`；`should_checkin(now-1, now, False, True) -> False`（无订单不聊）。
`tests/test_dialog_worker.py`：`reset_state` 把 `_next_checkin_at` 归 None；`_run_llm` 正常结束后 `_next_checkin_at` 非 None。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_dialog_policy.py tests/test_dialog_worker.py -q`
Expected: FAIL（`should_checkin` 未定义）。

- [ ] **Step 3: 实现**

- `policy.py` 加 `should_checkin(next_checkin_at, now, has_orders, in_level)`（纯函数，逻辑如上）。
- `worker.py`：`__init__`/字段加 `self._next_checkin_at = None`；`reset_state` 归 None；`_run_llm` 末尾（`stream.wait()` 之后）`self._next_checkin_at = time.monotonic() + random.uniform(CHECKIN_MIN_S, CHECKIN_MAX_S)`（模块常量 `CHECKIN_MIN_S=35`、`CHECKIN_MAX_S=75`）；`_run` 循环在 `vad.paused` 那跳之后、读音频帧之前加 `_maybe_checkin()`：条件 `not utterance and not vad_active and not ptt_recording and should_checkin(...)`，满足则 `_run_llm(self._latest_context(), "checkin", "", timing=...)`。
- `llm.py` 加 `_EVENT_TEXT["checkin"]`：「按【世界状态】用一两句大白话报当前送单局面，可轻松闲聊」。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_dialog_policy.py tests/test_dialog_worker.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/policy.py ai/dialog/worker.py ai/dialog/llm.py tests/test_dialog_policy.py tests/test_dialog_worker.py
git commit -m "feat(dialog): add randomized periodic check-in"
```

---

### Task 8: UX 打磨（stuck 上报 / 漏听 / 语速）

**Files:**
- Modify: `ai/agent.py:603-605`（`p2["stuck"]` 填充）
- Modify: `ai/output/trace.py`（结构化 `stuck` 事件）
- Modify: `ai/dialog/worker.py`（漏听兜底）
- Modify: `ai/dialog/tts.py`（`rate` 接线）
- Test: `tests/test_agent_dialog.py`、`tests/test_dialog_worker.py`

**Interfaces:**
- Consumes: arbiter 的 `_is_stuck`（`ai/core/arbiter.py:83`）、motion 的 `stall_seconds`/`stalled`（`ai/primitives/motion.py:102-105`）；`VoiceConfig.rate`（`ai/dialog/schema.py`）。
- Produces: `p2["stuck"]` 字符串；trace 的 `stuck` 事件字段；TTS `rate` 参数。

- [ ] **Step 1: 写失败测试**

- `tests/test_agent_dialog.py`：卡住场景下 `_build_dialog_context(world)` 的 `p2["stuck"]` 非空（含 task key 或原因）；trace 记录含 `stuck` 事件（`task`/`reason`/`pos`/`duration_ticks`/`retries`）。
- `tests/test_dialog_worker.py`：ASR 返回空/失败时，worker 走漏听兜底（记录或调用 `_speak("没听清，再说一遍")`），不再静默。
- `tests/test_dialog_worker.py`（或 tts 相关）：`VoiceConfig.rate` 传入 TTS 客户端的 rate 参数。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_agent_dialog.py tests/test_dialog_worker.py -q`
Expected: FAIL（字段/事件/兜底缺失）。

- [ ] **Step 3: 实现**

- `agent.py` `_build_dialog_context`：把 arbiter 同格卡住（或 motion `stalled`）拼成 `p2["stuck"]`（如「卡在 (gx,gz) N 拍」）。
- `ai/output/trace.py`：当 stuck 判据成立时，写一条结构化 `stuck` 事件（`task`/`reason`/`pos`/`duration_ticks`/`retries`），grep 友好。
- `worker.py`：ASR 空/失败路径（`_handle_transcript` 抑制点、`_run_llm` first 为 None 的兜底）改为说「没听清，再说一遍」。
- `ai/dialog/tts.py`：把 `VoiceConfig.rate` 接到 TTS 的 rate 参数（现未接线）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_agent_dialog.py tests/test_dialog_worker.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add ai/agent.py ai/output/trace.py ai/dialog/worker.py ai/dialog/tts.py tests/test_agent_dialog.py tests/test_dialog_worker.py
git commit -m "feat(dialog): surface stuck to trace+llm, missed-speech fallback, wire TTS rate"
```

---

## 执行顺序与依赖

Task 1（账本+prompt）→ Task 2（闸，依赖 Task 1 的 `now` 偏好）→ Task 3（洗盘，依赖 Task 2 闸语义）。Task 4/5/6 相互独立、可与 1-3 并行。Task 7 依赖 Task 6 的上下文字段（话料）。Task 8 独立。每个 Task 内 TDD，末尾 commit；全部完成后跑 `python3 -m pytest tests/` 全量。

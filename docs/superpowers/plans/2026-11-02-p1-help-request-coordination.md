# P1 求助、双侧分工与灵活指挥 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 P2 能按「这一步谁做更划算」主动开口求 P1 扔/做并把该步挪走，支持步骤级语音指挥，并播报任务切换。

**Architecture:** 纯 Python 侧改动，不碰插件协议。核心是一个新模块 `ai/dialog/help.py`（主循环侧，由 `agent._service_dialog` 调用）做「订单 DAG 前沿扫描 + P1/P2 成本比较 + 阈值」，产出 `help_request` 事件经既有 worker 管道让 LLM 措辞；「挪走」编译成 `Ledger` 里的瞬态 claim 偏好；步骤级指挥扩展 `update_duties` 的 target 枚举；任务切换播报走 `task_switch` 事件。

**Tech Stack:** Python 3.9、pytest 7.4.4；无新依赖；不 push（本地提交）。

**Spec:** `docs/superpowers/specs/2026-11-02-p1-help-request-coordination-design.md`

## Global Constraints

- 对话层线程边界：主循环 20Hz 是唯一碰 `World`/`Blackboard` 的地方；`ai/dialog/worker.py` 独立线程不读 World、不写 Blackboard。求助判定、挪走偏好、步骤级 duty 都由主循环产出，事件经队列交 worker。
- 「挪走」是软偏好（`Preference`），不硬改 planner 分层。
- 成本用 `reachability.field_for(world, bb, name)` + `reachability._interact_goals(ctx)`，P1/P2 只换 `name`/`ctx["chef"]`。
- 触发扫订单 DAG 前沿（复用 `recipe_graph.emit`，不做 P2 feasible 过滤），不扫 P2 候选。
- 阈值：`P2=∞` 必喊；`P1=∞` 不喊；否则 `P2−P1 ≥ 8 步` 才喊。救援类永不喊（`RESCUE_TYPES`+`Extinguish` 用 dialog 口径；`TakeCookedToPlate` 锅快糊时除外）。
- 每次冷却窗口最多喊 1 条，同 (料/站) 未解决不重复。
- 步骤级 target 用英文 `TaskType:item` 枚举，中文标签只进 prompt。
- 不查 P1 忙不忙。

## Review Focus

1. **P1 够不着、P2 够得着** → 不喊（P1 帮不上）。→ Task 1 的 `test_p1_saves_enough_p1_unreachable`。
2. **救援态盛盘（锅快糊）** → 不喊，即使 P1 更近。→ Task 2 的 `test_frontier_skips_rescue_take_cooked`。
3. **连通图 P1 只近 1–2 步** → 不喊（`P2−P1 < 8`）。→ Task 1 的 `test_p1_saves_enough_below_threshold`。
4. **同一 (料/站) 反复请求** → 已有 active 求助 duty 时不重复。→ Task 4 的 `test_ledger_help_request_no_duplicate`。
5. **步骤不是上游就绪（料还没到那一步）** → 不进前沿，不提前喊。→ Task 2 的 `test_frontier_only_upstream_ready`。
6. **步骤级 target 与整链 target 重叠** → 编译正确（新条目优先）。→ Task 6 的 `test_step_and_material_overlap`。

---

## Task 1: P1/P2 成本比较与阈值

**Files:**
- Create: `ai/dialog/help.py`
- Test: `tests/test_help.py`

**Interfaces:**
- Produces: `ai.dialog.help.cost_to(world, bb, name, source) -> Optional[int]`、`ai.dialog.help.p1_saves_enough(p1_cost, p2_cost, threshold=8) -> bool`、常量 `ai.dialog.help.HELP_THRESHOLD_STEPS = 8`。

- [ ] **Step 1: Write the failing test**

```python
from ai.dialog.help import p1_saves_enough, cost_to, HELP_THRESHOLD_STEPS

def test_p1_saves_enough_p1_unreachable():
    assert p1_saves_enough(None, 5) is False

def test_p1_saves_enough_p2_unreachable():
    assert p1_saves_enough(3, None) is True

def test_p1_saves_enough_at_threshold():
    assert p1_saves_enough(2, 10) is True      # 10-2 == 8
    assert p1_saves_enough(2, 9) is False      # 7 < 8

def test_cost_to_rapids_p2_cannot_reach_potato():
    # 用 tests/fixtures/rapids/rapids21-20261028.ndjson.gz 帧：厨房土豆箱 P2=∞、P1 可达
    w = W.World(frame)
    crate = next(s for s in w.stations if s.get("ing") == "Potato")
    assert cost_to(w, None, P.P2_NAME, crate) is None
    assert isinstance(cost_to(w, None, P.P1_NAME, crate), int)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_help.py -v`
Expected: FAIL（`ModuleNotFoundError: ai.dialog.help`）

- [ ] **Step 3: Implement `cost_to` and `p1_saves_enough` in `ai/dialog/help.py`**

`cost_to` 用 `reachability.context(world, bb, name)` 取 ctx、`reachability._interact_goals(ctx, source)` 取交互位、`reachability.field_for(world, bb, name)` 取场，返回 `min(field[g] for g in goals if g in field)`；场为 None 或全够不着返回 None。`p1_saves_enough` 按 Global Constraints 的阈值规则。

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_help.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/help.py tests/test_help.py
git commit -m "feat(dialog): add P1/P2 reachability cost compare and help threshold"
```

---

## Task 2: 订单 DAG 前沿步骤枚举

**Files:**
- Modify: `ai/dialog/help.py`
- Test: `tests/test_help.py`

**Interfaces:**
- Consumes: Task 1 的 `cost_to`。
- Produces: `ai.dialog.help.FrontierStep`（dataclass：`task_type: str`、`item: Optional[str]`、`source: Dict`、`p1_cost: Optional[int]`、`p2_cost: Optional[int]`）、`ai.dialog.help.frontier_steps(world, bb, book, orders, plate_match, cooker_match) -> List[FrontierStep]`。

- [ ] **Step 1: Write the failing test**

```python
from ai.dialog.help import frontier_steps

def test_frontier_rapids_includes_unreachable_potato_fetch():
    # rapids21 帧：订单要薯条，土豆箱在厨房、P2 在筏上
    steps = frontier_steps(w, None, book, w.orders, {}, {})
    by = {(s.task_type, s.item): s for s in steps}
    assert ("FetchToBoard", "Potato") in by
    assert by[("FetchToBoard", "Potato")].p2_cost is None

def test_frontier_only_upstream_ready():
    # 黄瓜还没上板时，Chop(Cucumber) 不在前沿；上板后进入前沿
    steps = frontier_steps(w, None, book, w.orders, {}, {})
    assert not any(s.task_type == "Chop" and s.item == "Cucumber" for s in steps)

def test_frontier_skips_rescue_take_cooked():
    # 锅快糊（burning/cooked 且 progress 超阈）时 TakeCookedToPlate 不进前沿
    steps = frontier_steps(w, None, book, w.orders, {}, {})
    assert not any(s.task_type == "TakeCookedToPlate" for s in steps)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_help.py::test_frontier_rapids_includes_unreachable_potato_fetch -v`
Expected: FAIL（`frontier_steps` 未定义）

- [ ] **Step 3: Implement `frontier_steps` in `ai/dialog/help.py`**

对每个 order 调 `recipe_graph.emit(world, bb, book, order, plate_match, cooker_match)` 得候选 task；保留 `task.feasible(ctx)` 为真（上游就绪、不看 veto）且 `not task.done(ctx)` 的；来源用 `reachability._task_target(task, rid, ctx)`（遍历 `task.claims()` 的资源 id）解析；救援类（`RESCUE_TYPES`+`Extinguish`）跳过，`TakeCookedToPlate` 在锅快糊时跳过（用 `planner._burn_eta_ticks` 或等价的紧迫信号）；`p1_cost`/`p2_cost` 用 Task 1 的 `cost_to`。

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_help.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/help.py tests/test_help.py
git commit -m "feat(dialog): enumerate order-DAG frontier steps with sources"
```

---

## Task 3: Ledger 瞬态「挪走」duty

**Files:**
- Modify: `ai/dialog/duties.py`
- Test: `tests/test_duties.py`

**Interfaces:**
- Produces: `ai.dialog.duties.Ledger.add_help_request(task_type, item, tick, ttl_ticks=600) -> str`（返回 duty id；scope=`{(task_type, item)}`、`kind="claim"`、`owner="P1"`、负分、`source="help_request"`）、`ai.dialog.duties.Ledger.help_request_ids() -> List[str]`。

- [ ] **Step 1: Write the failing test**

```python
def test_ledger_help_request_compiles_to_claim():
    ledger = Ledger()
    ledger.add_help_request("Chop", "Cucumber", tick=0)
    prefs = ledger.preferences()
    assert any(p.kind == "claim" and p.owner == "P1" and
               p.scope == frozenset({("Chop", "cucumber")}) for p in prefs)

def test_ledger_help_request_expires_via_prune():
    ledger = Ledger()
    ledger.add_help_request("Chop", "Cucumber", tick=0, ttl_ticks=10)
    ledger.prune(10)
    assert ledger.help_request_ids() == []

def test_ledger_help_request_no_duplicate():
    ledger = Ledger()
    a = ledger.add_help_request("Chop", "Cucumber", tick=0)
    b = ledger.add_help_request("Chop", "Cucumber", tick=0)
    assert a == b and len(ledger.help_request_ids()) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_duties.py::test_ledger_help_request_compiles_to_claim -v`
Expected: FAIL（`add_help_request` 未定义）

- [ ] **Step 3: Implement `Ledger.add_help_request` / `help_request_ids` in `ai/dialog/duties.py`**

复用 `Ledger.apply` 的落账逻辑：同 `(task_type,item)` 已有 help_request 则返回原 id；否则新建 Duty（`mode="leave_to_p1"`、`scope=frozenset({(task_type, item.lower())})`、`source="help_request"`、`expires_tick=tick+ttl_ticks`）。`preferences()` 里该 Duty 走现有 claim 负分分支即可（`_MODE_TEXT` 渲染 help_request 时给「P2 求助 P1」文案）。

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_duties.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/duties.py tests/test_duties.py
git commit -m "feat(dialog): add transient help-request claim duty to Ledger"
```

---

## Task 4: 求助候选排名与去重（top_help_request）

**Files:**
- Modify: `ai/dialog/help.py`
- Test: `tests/test_help.py`

**Interfaces:**
- Consumes: Task 2 的 `frontier_steps`、Task 3 的 `ledger.help_request_ids()`（通过参数传入 ledger）。
- Produces: `ai.dialog.help.top_help_request(world, bb, book, orders, plate_match, cooker_match, ledger) -> Optional[FrontierStep]`。

- [ ] **Step 1: Write the failing test**

```python
from ai.dialog.help import top_help_request

def test_top_help_request_picks_blocking_first():
    # 两个 P1 更划算步骤：一个 P2=∞、一个 P2=5/P1=2，取 P2=∞ 那个
    got = top_help_request(w, None, book, w.orders, {}, {}, ledger)
    assert got is not None and got.p2_cost is None

def test_top_help_request_skips_already_requested():
    ledger.add_help_request("FetchToBoard", "Potato", tick=0)
    got = top_help_request(w, None, book, w.orders, {}, {}, ledger)
    assert got is None or (got.task_type, got.item) != ("FetchToBoard", "Potato")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_help.py::test_top_help_request_picks_blocking_first -v`
Expected: FAIL（`top_help_request` 未定义）

- [ ] **Step 3: Implement `top_help_request` in `ai/dialog/help.py`**

取 `frontier_steps` 里 `p1_saves_enough(s.p1_cost, s.p2_cost)` 为真的，排除 `(task_type, item)` 已在 `ledger.help_request_ids()` 对应 scope 中的；排序：`p2_cost is None` 优先，其次 `(p2_cost - p1_cost)` 降序，再按所属 order 的 `remaining` 升序；返回第一个或 None。

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_help.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/help.py tests/test_help.py
git commit -m "feat(dialog): rank help-request candidates and dedup via ledger"
```

---

## Task 5: help_request 事件管道（agent → worker → LLM 措辞）

**Files:**
- Modify: `ai/agent.py`（`_service_dialog` 内生成事件 + 落挪走 duty）
- Modify: `ai/dialog/worker.py`（`_handle_event` 加 `help_request` 分支）
- Modify: `ai/dialog/llm.py`（`_EVENT_TEXT["help_request"]`）
- Test: `tests/test_agent_dialog.py`、`tests/test_dialog_worker.py`

**Interfaces:**
- Consumes: Task 4 的 `top_help_request`、Task 3 的 `ledger.add_help_request`。
- Produces: 事件 `("help_request", {"task_type": str, "item": Optional[str], "text": str})`。

- [ ] **Step 1: Write the failing test**

```python
def test_help_request_event_emitted_when_p1_blocked():   # test_agent_dialog.py
    # 用 rapids 帧驱动 _service_dialog：应 push ("help_request", ...) 且 ledger 有 help_request duty
    agent._service_dialog(world)
    assert any(e[0] == "help_request" for e in captured_events)
    assert agent.ledger.help_request_ids()

def test_worker_handles_help_request_without_tool():     # test_dialog_worker.py
    # 喂 ("help_request", {...})：_handle_event 走 _run_llm(context, "help_request", "")
    # 断言 _run_llm 的 tool_choice == "none"（spy 或 fake llm client 记录）
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_agent_dialog.py::test_help_request_event_emitted_when_p1_blocked -v`
Expected: FAIL

- [ ] **Step 3: Implement the pipeline**

`agent.py` `_service_dialog`：在 `new_order`/`last_seconds` 之后，若 `top_help_request(...)` 非 None → `self.ledger.add_help_request(...)` → `self._put_event(("help_request", payload))` → `events.append(...)`。`worker.py` `_handle_event`：加 `if name == "help_request": self.last_proactive_tick = tick; self._run_llm(context, "help_request", "", timing=...)`（`_run_llm` 的 `choice` 逻辑已自动给非 speech/new_order 事件 `tool_choice="none"`）。`llm.py` `_EVENT_TEXT["help_request"]`：告诉 LLM 用一句话请 P1 做 payload 里说的那步，不调工具。

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_agent_dialog.py tests/test_dialog_worker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai/agent.py ai/dialog/worker.py ai/dialog/llm.py tests/test_agent_dialog.py tests/test_dialog_worker.py
git commit -m "feat(dialog): wire help_request event through agent to worker phrasing"
```

---

## Task 6: B 步骤级指挥（step-level target）

**Files:**
- Modify: `ai/dialog/duties.py`（`Kitchen.targets()`/`expand`/`tool_schemas`）
- Modify: `ai/dialog/llm.py`（`_SYSTEM` 分工说明 + 工具描述）
- Test: `tests/test_duties.py`

**Interfaces:**
- Produces: `ai.dialog.duties.Kitchen.step_targets() -> List[str]`（返回 `TaskType:item` 枚举）、`Kitchen.expand` 解析 `TaskType:item` 成 `frozenset({(task_type, item.lower())})`。

- [ ] **Step 1: Write the failing test**

```python
def test_step_target_expand():
    kitchen = Kitchen.from_orders(book, orders, compile_node, meta)
    scope, err = kitchen.expand("Chop:Cucumber")
    assert err == "" and scope == frozenset({("Chop", "cucumber")})

def test_step_target_rejects_unknown_task():
    _, err = kitchen.expand("NotATask:Cucumber")
    assert err != ""

def test_step_and_material_overlap():
    # 先「切黄瓜」再「黄瓜你包了」：编译后新条目优先（整链 exclude 掉旧步骤条目）
    ledger = Ledger()
    ledger.apply({"op": "add", "mode": "do", "target": "Chop:Cucumber"}, kitchen, 0)
    ledger.apply({"op": "add", "mode": "do", "target": "Cucumber"}, kitchen, 0)
    prefs = ledger.preferences()
    # 旧的 Chop:Cucumber 条目 exclude 含 (None, cucumber)
    chop = [p for p in prefs if p.duty_id == "a1"][0]
    assert (None, "cucumber") in chop.exclude
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_duties.py::test_step_target_expand -v`
Expected: FAIL

- [ ] **Step 3: Implement `Kitchen.step_targets` + `expand` 步骤解析**

`step_targets()`：遍历 `self.materials` 的 `Material.steps`，对每个 `(task_type, item)`（非共享、非救援、item 非空）生成 `"{task_type}:{material_name}"`（用物料名，不用内部生熟形态）。`expand`：遇到含 `:` 的 target 按 `TaskType:item` 解析，校验 task_type 在 `self.meta` 且非共享、item 是该物料（大小写不敏感），命中返回步骤 scope；否则返回原因。`targets()` = 物料 + 步骤 + 共享动作。`llm.py` 的 `_SYSTEM`「分工约定」段加：步骤级 target 只认领那一步，例「切黄瓜→Chop:Cucumber」「下锅我来→leave_to_p1 FetchToCooker:Rice」。

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_duties.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/duties.py ai/dialog/llm.py tests/test_duties.py
git commit -m "feat(dialog): add step-level TaskType:item duty targets"
```

---

## Task 7: C 任务切换播报

**Files:**
- Modify: `ai/agent.py`（`_tick_once` 承诺切换处生成 `task_switch` 事件）
- Modify: `ai/dialog/worker.py`（`_handle_event` 加 `task_switch` 分支）
- Modify: `ai/dialog/llm.py`（`_EVENT_TEXT["task_switch"]`）
- Test: `tests/test_agent_dialog.py`

**Interfaces:**
- Consumes: `self.bb.commitment.active_task`（`(TYPE, item)`）。
- Produces: 事件 `("task_switch", {"task_type": str, "item": Optional[str]})`。

- [ ] **Step 1: Write the failing test**

```python
def test_task_switch_event_on_commitment_change():
    # 承诺从 (FetchToBoard, Cucumber) 换成 (Chop, Cucumber) → 发 task_switch
    agent._maybe_emit_task_switch(old_key=("FetchToBoard", "Cucumber"),
                                  new_key=("Chop", "Cucumber"), tick=0)
    assert captured_event[0] == "task_switch"

def test_task_switch_same_item_no_event():
    agent._maybe_emit_task_switch(old_key=("FetchToBoard", "Cucumber"),
                                  new_key=("FetchToBoard", "Cucumber"), tick=0)
    assert captured_event is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_agent_dialog.py::test_task_switch_event_on_commitment_change -v`
Expected: FAIL（`_maybe_emit_task_switch` 未定义）

- [ ] **Step 3: Implement the switch detection + event**

`agent.py`：新增 `_maybe_emit_task_switch(old_key, new_key, tick)`——`(TYPE, item)` 跨 task 类型或物料才算切换；4s 内（`tick - last < 80`）不重复；通过 `self._put_event(("task_switch", {...}))` 发。在 `_tick_once` ⑪ 登记 commitment 前后调它。`worker.py` `_handle_event` 加 `if name == "task_switch": self._run_llm(context, "task_switch", "", timing=...)`。`llm.py` `_EVENT_TEXT["task_switch"]`：用一句话报 P2 现在去做什么，不调工具。

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_agent_dialog.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai/agent.py ai/dialog/worker.py ai/dialog/llm.py tests/test_agent_dialog.py
git commit -m "feat(dialog): announce significant task switches"
```

---

## Self-Review 结果

- **Spec coverage**：A（触发 §5.1→Task1/2/4、求助动作 §5.2→Task3/5、投掷两侧 §5.3 的 P1 扔几何标注为后续细化、覆盖关卡 §5.4 由 Task2 的真实帧测试间接覆盖）、B（§5.7→Task6）、C（§5.8→Task7）。「P1 扔得过来」的投掷几何复用在 v1 简化成「P1 够得着料源即视为能扔」，已在 Global Constraints 末尾留白，属 builder 可合理裁量。
- **Step scan**：每步要么给测试名+断言、要么给签名+文件+关键值；阈值 8、TTL 600 拍、4s 去重均来自 spec。
- **Type consistency**：`FrontierStep` 字段、`cost_to`/`p1_saves_enough`/`frontier_steps`/`top_help_request` 签名跨任务一致；`ledger.add_help_request(task_type, item, tick, ttl_ticks)` 在 Task 3 定义、Task 4/5 使用一致。
- **Review Focus**：6 条都已落到对应任务的测试步骤。
- **Proportion**：计划以签名+测试名+断言为主，无整段函数体。

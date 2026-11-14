# 移除 Firefighting 模式仲裁，火情下沉到 planner 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删掉 arbiter 的「Firefighting 模式」仲裁，把「是否去灭火 / 何时去灭火」改由 planner 的候选生成 + L_FIRE 分层 + 打分统一处理；同时删除死代码 `_handle_stuck`。

**Architecture:** 现在的「模式切换」其实是两档：arbiter 每拍判定 `Normal / Firefighting`，Firefighting 走独立的 `planner.plan_firefighting`（只发一个全局 `Extinguish` 候选、不打分、不分层）。分层系统（`planner_layers.py`）**已经**为火情预留了 `L_FIRE = 0` 层、`Extinguish → L_FIRE` 的映射、`Extinguish.NO_ABANDON` 的拉黑豁免、以及 `_crossing_hold` / `_select_reposition` / `_select_imperative` / `_scope_with_preemption` 对火情的让位。唯一缺口是：`plan()` 的候选生成里**从不发 `Extinguish` 候选**（只有 `plan_firefighting` 发）。本计划补上这个缺口，然后删掉模式仲裁这套外壳。

**Tech Stack:** Python 3 + pytest 7.4.4（无新依赖，无 C# 改动）。

**Spec:** `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md`（§5 Arbiter、§6 分层/打分）、`docs/superpowers/specs/2026-10-15-layered-scoring-design.md`（L_FIRE 层来源）。

## Global Constraints

- 代码注释/文档中文、标识符英文；类型注解用 `# type: (...) -> ...` 注释形式。
- 跨目录 import 用 `from ai.<子目录> import X`；对外只认 `from ai import tasks as T`、`from ai import primitives as PR`。
- 「拍」= Python 循环拍 = 50 ms（20 Hz）；按拍计的阈值都按 20 Hz 理解。
- 改行为先加/改测试；真实帧 fixture 以 `.ndjson.gz` 入库（本计划不新增 fixture）。
- 变更后 `python3 -m pytest tests/` 全绿才算完成。

## Review Focus

这些是 refactor 最容易悄悄改掉行为、又没有任何现有单测盯住的地方，逐条配测试钉死：

1. **P1 已在灭火时 P2 不该抢**：P1 站在着火灶旁或手持灭火器时，planner 不得发 `Extinguish`（避免双人抢灭火器）。→ Task 1 测试 `test_no_extinguish_when_p1_handling`。
2. **火情要压过当前承诺惯性**：P2 正做备菜时锅着火，`_apply_commitment_inertia` 的进度加成/不可行迟滞不得把 `Extinguish` 挡掉。→ Task 1 测试 `test_extinguish_preempts_commitment`。
3. **火情要压过玩家命令 scope**：命令 scope 激活时 `Extinguish` 仍须抢占（`PREEMPT_COMMAND_TYPES` 已含它，但要验证走新路径后仍生效）。→ Task 1 测试 `test_extinguish_preempts_command_scope`。
4. **burning（糊锅）≠ onFire（着火）的边界**：只有 `onFire` 才发 `Extinguish`；仅 `burning` 的锅仍走 `RescueCooked`（L1），不能因为下沉被误判成「无火」。→ Task 1 测试 `test_no_extinguish_for_burning_only`。
5. **删除后无残留引用**：`bb.mode` / `FireScratch` / `blacklisted_resources` / `interrupt_failure_count` / `plan_firefighting` / `arbiter.select_mode` 在全仓库（含 tests/）零引用。→ Task 2 用 `grep` 验证，不是靠测试。

---

### Task 1: 把火情候选加进 planner 正常路径（纯增量，可独立验证）

**Files:**
- Modify: `ai/core/planner.py`（新增 `_gen_fire` / `_fire_needs_ai`；在 `generate_candidates` 接入；在 `_apply_commitment_inertia` 加火情抢占）
- Test: `tests/test_planner_fire.py`（新建）

**Interfaces:**
- Consumes: 现有 `Candidate` / `T.Extinguish` / `P.chef_by_name` / `P.held_name` / `P.cooker_burning` / `bb.p1_model` / `world.extinguishers` / `world.cookers`。
- Produces:
  - `_fire_needs_ai(world, bb) -> bool`：从 arbiter 平移来的三条件合取（见 Step 3 伪代码）。
  - `_gen_fire(world, bb) -> List[Candidate]`：`_fire_needs_ai` 为真时返回 `[Candidate(T.Extinguish(), [], EST_COOK_OPS)]`，否则 `[]`。

- [ ] **Step 1: 写失败测试 `tests/test_planner_fire.py`**

沿用 `tests/test_cook_pressure.py` 的 `_world` / `_cooker` 构造模式（`World` + `RecipeBook([], stations)` + `Blackboard()`），补上 `extinguishers` 与 `Player 1` 厨师。断言：

```python
def test_plan_chooses_extinguish_when_fire():
    stations = [{"t": "cooking", "n": "cooker_01", "gx": 1, "gz": 1}]
    w = _world(cookers=[_cooker(burning=True)], stations=stations,
               extinguishers=[{"n": "ext", "x": 2.0, "z": 2.0}],
               chefs=[{"name": "Player 2", "x": 3.0, "z": 3.0},
                      {"name": "Player 1", "x": 9.0, "z": 9.0}])
    bb = Blackboard()
    chosen, record = planner.plan(w, bb, None, RecipeBook([], stations))
    assert chosen is not None and chosen.TYPE == "Extinguish"
    assert record["top_layer"] == 0  # L_FIRE

def test_no_extinguish_when_p1_handling():
    # P1 站在灶旁（距离 <= 2.0）→ 不发 Extinguish
    ...  # 同上，但 Player 1 放在 cooker 旁 (1.0, 1.0)
    chosen, record = planner.plan(w, bb, None, book)
    assert not any(c.task.TYPE == "Extinguish"
                   for c in record["candidates"])

def test_no_extinguish_for_burning_only():
    # cooker burning=False, onFire=False → 无 Extinguish 候选
    ...

def test_extinguish_preempts_commitment():
    # bb.commitment.active_task = 一个备菜 task（如 T.Chop(...)），
    # 世界里有火 → plan() 仍选 Extinguish（绕过承诺惯性）
    ...
```

`test_extinguish_preempts_command_scope` 放到本任务末尾与 `command_scope` 一起验证（见 Step 3 后半）。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_planner_fire.py -v`
Expected: FAIL —— `plan()` 当前不发 `Extinguish`，`chosen` 不是 `Extinguish`。

- [ ] **Step 3: 实现 `_fire_needs_ai` + `_gen_fire` 并接入 `generate_candidates`**

`_fire_needs_ai(world, bb)` 从 `ai/core/arbiter.py` **逐字平移**以下三条件（保持 `onFire` 判定口径不变）：

```python
def _fire_needs_ai(world, bb):
    # ① 有任一 onFire 的灶（注意：只用 onFire，不用 burning——糊锅走 RescueCooked/L1）
    on_fire = [c for c in world.cookers if c.get("onFire")]
    if not on_fire:
        return False
    # ② 有 AI 可获得的灭火器（平移 arbiter._has_available_extinguisher，
    #    含「P1 持唯一灭火器时不进」的 p1_model 特判）
    if not _extinguisher_available(world, bb):
        return False
    # ③ 至少一口着火灶「未被 P1 处理」（平移 arbiter._is_fire_unhandled）
    p1 = P.chef_by_name(world.chefs, "Player 1")
    return any(_fire_unhandled(c, p1, world) for c in on_fire)
```

`_extinguisher_available` / `_fire_unhandled` 也逐字平移（`HANDLING_DISTANCE = 2.0` 一并搬来）。`_gen_fire` 里 `_fire_needs_ai` 为真时 append `Candidate(T.Extinguish(), [], EST_COOK_OPS)`（与旧 `plan_firefighting` 一致，不带 `branch`）。

在 `generate_candidates` 里 `candidates.extend(_gen_misc(...))` 之后加一行 `candidates.extend(_gen_fire(world, bb))`。

在 `_apply_commitment_inertia` 里 `current_task = bb.commitment.active_task`（`planner.py:3021`）之后、压力紧急切换之前加：

```python
# 火情无条件抢占当前承诺：L0 火会蔓延，不能等承诺惯性/进度加成。
if getattr(top_task, "TYPE", None) == "Extinguish" \
        and not _tasks_equal(top_task, current_task):
    return top_task
```

**为什么这一步必须、为什么「最高层」不够：** 分层（`_select_layer` 选出 L0）只决定打分候选池；`top_task = Extinguish` 之后还要过 `_apply_commitment_inertia`。此时当前任务（如 L6 的 `Chop`）不在 L0 的 `scored` 里 → 命中 `planner.py:3050-3054` 的 `current_entry is None` 分支 → `infeasible_streak += 1` 且 `< INFEASIBLE_THRESHOLD(8)` 时 `return current_task`，即 AI 继续做旧活 8 拍才换灭火。L1/L2（锅熟/快糊）靠 `planner.py:3032` 的 `_time_pressure(top_task) >= 1.0` 紧急切换绕过这 8 拍，但 `Extinguish` 的 `_time_pressure` 恒为 0（`planner.py:2939` 白名单只含 `TakeCookedToPlate/RescueCooked/Discard/FillCarrier`），走不了那条路——所以必须显式给 L0 补上同等的无条件抢占。

命令 scope 抢占无需改代码（`PREEMPT_COMMAND_TYPES` 已含 `"Extinguish"`，`_scope_with_preemption` 会自动放行），用 `test_extinguish_preempts_command_scope` 钉死即可：`bb.command_scope = <某个与 Extinguish 无关的 scope>`，仍选 `Extinguish`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_planner_fire.py -v`
Expected: PASS

- [ ] **Step 5: 全量回归（确保增量不破坏现有 Firefighting 路径）**

Run: `python3 -m pytest tests/ -q`
Expected: 全绿（本任务后 `plan_firefighting` 仍在，旧测试不受影响）。

- [ ] **Step 6: Commit**

```bash
git add ai/core/planner.py tests/test_planner_fire.py
git commit -m "feat: emit Extinguish candidate in normal planner path (L_FIRE)"
```

---

### Task 2: 删除 Firefighting 模式仲裁与死代码

**Files:**
- Modify: `ai/core/arbiter.py`（删火情/降级/`_handle_stuck`/`select_mode`/`on_mode_transition`，只留 `_is_stuck` + `STUCK_SAME_CELL_THRESHOLD` + `_STATIONARY_TASK_TYPES`）
- Modify: `ai/core/planner.py`（删 `plan_firefighting`，`planner.py:1670-1724`；更新 `plan()` docstring 里 `plan_firefighting 不分层` 那句）
- Modify: `ai/agent.py`（`1764-1771` 模式仲裁 → 改为维护卡住计数；`1815-1829` 删 Firefighting/else 分支；`1944-1945` 删 `interrupt_failure_count`；trace.emit 传 `"Normal"`，record 不再写 `mode_reason`）
- Modify: `ai/state/blackboard.py`（删 `mode`、`FireScratch`、`Commitment.interrupt_failure_count`、`blacklisted_resources`）
- Modify: `ai/primitives/stuck.py`（`note_motion_failure` 删 `or bb.mode != "Normal"` 守卫）
- Modify: `ai/output/trace.py`（更新 `:254` 注释里的 `plan_firefighting` 表述；`emit` 的 `mode` 参数保留）
- Test: 更新下列文件（逐个列在 Step 1）

**Interfaces:**
- Consumes: Task 1 的 `_gen_fire` / `_fire_needs_ai`（本任务后 `planner.plan()` 是唯一决策入口）。
- Produces: 无新符号；删除 `arbiter.select_mode` / `arbiter.on_mode_transition` / `planner.plan_firefighting` / `bb.mode` / `bb.blacklisted_resources` / `bb.commitment.interrupt_failure_count` / `FireScratch`。

- [ ] **Step 1: 删 arbiter.py 里的火情/降级/模式外壳**

删除：`select_mode`、`on_mode_transition`、`_should_degrade_from_failure`、`_handle_failure_degradation`、`_handle_stuck`、`_should_enter_firefighting`、`_has_available_extinguisher`、`_is_fire_unhandled`；常量 `INTERRUPT_FAILURE_THRESHOLD`、`BLACKLIST_TICKS`、`HANDLING_DISTANCE`。

保留：`STUCK_SAME_CELL_THRESHOLD`、`_STATIONARY_TASK_TYPES`、`_is_stuck`。模块 docstring 改为「卡住检测（原 Arbiter 的模式仲裁已下沉 planner，`_handle_stuck` 死代码已删）」之类，去掉「每拍第一个跑、决定控制权交给哪个模式」与「Spec §5 Arbiter」的过时描述。

- [ ] **Step 2: agent.py 去掉模式分支**

把 `ai/agent.py:1764-1771` 的 `new_mode, mode_reason = arbiter.select_mode(...)` + `if new_mode != self.bb.mode: halt...` 整块替换为一行卡住计数维护：`arbiter._is_stuck(world, self.bb)`（只取副作用，推进 `bb.scratch.same_count`，供 `_stuck_info` 读）。

`1777-1829`：删掉 `elif new_mode == "Firefighting"` 与 `else` 防御分支，只保留 Normal 的 `planner.plan(...)` 主路径；`claims_new` 统一按 Normal 路径绑定 `chosen_task.order_id`。

`1944-1945`：删 `if self.bb.commitment: self.bb.commitment.interrupt_failure_count += 1` 两行。

`1959`：`trace.emit(record, "Normal", ...)`；`record` 里删 `"mode_reason"` 键（`1835` 那行删掉；`1909` 的 `record['mode_reason'] = record['waiting']['reason']` 一并删，等待原因仍在 `record['waiting']` 里）。

- [ ] **Step 3: blackboard / stuck.py / trace.py 清残留**

`blackboard.py`：删 `Blackboard.mode`（`:106`）、`FireScratch` 类（`:91-95`）、`Commitment.interrupt_failure_count`（`:48`）、`Blackboard.blacklisted_resources`（`:110`）。`stuck.py:49-54`：`note_motion_failure` 的守卫改成只按 `failure.get("reason")` 判断，删 `or bb.mode != "Normal"`（火情中 motion 失败现在也写 `bad_cells`，`NO_ABANDON` 仍挡资源拉黑，属预期变化）。`trace.py:254`：注释里的 `plan_firefighting` 改为「测试仍可能传短元组，兼容保留」。

- [ ] **Step 4: 删 planner.plan_firefighting**

删除 `ai/core/planner.py:1670-1724` 整个 `plan_firefighting` 函数；`plan()` docstring 里「`plan_firefighting 不分层，仍发 6 元组`」一句删掉。

- [ ] **Step 5: 更新受影响的测试**

  - `tests/test_arbiter.py`：删火情/模式/降级/`on_mode_transition` 相关测试（`test_stationary_guard_does_not_block_firefighting`、`test_firefighting_*`、`test_mode_transition_*`、`test_interrupt_task_failure_*`）；保留 `_make_world/_make_bb/_make_chef/_make_cooker/_make_extinguisher` 与卡住测试 `test_not_stuck_when_moving`。文件 docstring 改为「卡住检测单测」。
  - `tests/test_stall_recovery.py`：删 `test_stationary_work_does_not_suppress_new_fire`、`test_stationary_firefighter_exits_when_fire_is_gone`（其余 `_is_abandoned` 测试不受影响）。
  - `tests/test_agent.py`：删/改 patch `ai.core.arbiter.select_mode` 且断言 `agent.bb.mode == "Firefighting"` 的用例（`:82-99` 等），改为验证「火情下 `planner.plan` 被调用且选 `Extinguish`」或直接删除。
  - `tests/test_agent_dialog.py`：去掉 `patch("ai.agent.arbiter.select_mode", ...)`（`:368/386/494/540/560`）；保留 `arbiter.STUCK_SAME_CELL_THRESHOLD` 引用（`:511/536`）。
  - `tests/test_e2e_smoke.py`：删 `:222-259` 的「Normal→Firefighting→Normal」用例及 `:112` 注释断言。
  - `tests/test_soak.py`：删 `:266-307` 的 Firefighting 检测段（原已 `pytest.skip`）与 `agent.bb.mode` 引用。
  - `tests/test_blackboard.py`：删 `FireScratch` 相关用例（`:129/144-145`）与 import。
  - `tests/test_seam_clock.py:273`：删 `assert not bb.blacklisted_resources`。
  - `tests/test_trace_layers.py:35`：删/改「plan_firefighting 仍发 6 元组」用例。
  - `tests/test_planner_layers.py:547`：更新「L0 不打分（plan_firefighting…）」注释/用例，改断言 `plan()` 火情路径 `top_layer == 0`。
  - `tests/test_cook_pressure.py:36-54`：`test_plan_firefighting_ignores_abandon_blacklist` 改为调 `planner.plan()`，断言火情下 `chosen.TYPE == "Extinguish"`（`scratch.abandoned` 里有该灶也不挡，靠 `NO_ABANDON`）。
  - `tests/test_endgame.py:124-142`：删/改 `plan_firefighting` 相关用例。
  - `tests/test_rapids42_live_fixes.py`：不动（`arbiter._is_stuck` / `STUCK_SAME_CELL_THRESHOLD` 均保留）。

- [ ] **Step 6: 全量回归**

Run: `python3 -m pytest tests/ -q`
Expected: 全绿。

- [ ] **Step 7: 零残留 grep**

Run: `grep -rn "select_mode\|on_mode_transition\|plan_firefighting\|bb\.mode\|FireScratch\|blacklisted_resources\|interrupt_failure_count" ai/ tests/`
Expected: 无输出（`docs/` 与历史 spec/plan 允许残留，不动）。

- [ ] **Step 8: Commit**

```bash
git add ai/ tests/
git commit -m "refactor: remove Firefighting mode; fire flows through planner L_FIRE"
```

---

### Task 3: 同步文档

**Files:**
- Modify: `AGENTS.md`、`docs/agent/architecture.md`

- [ ] **Step 1: 改 architecture.md 的 Arbiter 描述**

`docs/agent/architecture.md:40` 把「Arbiter…优先级：卡住守卫 → 中断 task 连续失败降级 → Firefighting → Normal」改为「火情由 planner 的 `L_FIRE`（Extinguish 候选）统一处理，不再有独立 Firefighting 模式；arbiter.py 只保留卡住检测（`_is_stuck`，供 trace）」。同步 `:22` 表里 `arbiter.py` 的职责描述。

- [ ] **Step 2: 改 AGENTS.md**

`AGENTS.md` §2 里 `core/（arbiter/planner/planner_layers/executor）` 改述 arbiter 的新职责；若 §2/§3 有提到「模式切换 / Firefighting」的句子一并更新。

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md docs/agent/architecture.md
git commit -m "docs: fire handled by planner L_FIRE; arbiter now stuck-detection only"
```

---

## Self-Review 备注（已自查）

- **Spec 覆盖**：§5 Arbiter 的「模式仲裁」被本计划整体移除，替换为 §6 分层里已有的 `L_FIRE`；§5.3 的 P1 协作（不抢 P1 正在处理的火）被 `_fire_needs_ai` 保留；§7.2 的火情拉黑豁免由 `NO_ABANDON` 继续承担。
- **类型/命名一致**：Task 1 定义 `_fire_needs_ai` / `_gen_fire`，Task 2 只消费、不再改名。
- **Review Focus 五条**：分别落在 Task 1 的 4 个新测试 + Task 2 Step 7 的 grep。
- **比例**：本计划聚焦决策点（判定口径、抢占点、删除清单），未转抄实现代码。

# 分工协调模型重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「谁做什么」的协调从「LLM 按物料写账本」改成「planner 单一权威 + 动作级（=站点）分工 + 判/做/分工/禁」，删除 `update_duties`，换动作级新 tool。

**Architecture:** 分工单位从「物料整链」换成「动作」（切/下锅/拌/装盘/送餐/洗盘，动作↔站点↔边同构）；取料/投掷不暴露给 LLM，由 planner 按可达性自动归位；LLM 只提意图、planner 校验落地。做/禁=硬闸，分工=软偏好，补位=显式截止闸。

**Tech Stack:** Python 3（本项目纯 Python 侧改动，无 C# 插件改动），pytest 7.4.4，复用 `ai/space/reachability.py`、`ai/space/regions.py`、`ai/core/planner.py` 现成判据。

**Spec:** `docs/superpowers/specs/2026-11-04-duty-coordination-redesign-design.md`

## Global Constraints

- 对话层线程边界：主循环 20Hz 唯一碰 `World`/`Blackboard`；`ai/dialog/worker.py` 独立线程不读 World、不写 Blackboard。
- planner 是「谁做」唯一权威；LLM 只做两层翻译（P1 话→意图、planner 结果→人话）。
- 硬/软分离：做/禁=硬闸，分工=软偏好，补位=显式截止闸；不再揉进一个软权重。
- 复用 `reachability.reachable_by` / `field_for` / `_interact_goals`，不另起一套测量标准。
- 开发在当前分支，可 commit，不 push。

## Review Focus

1. **跨边 assign 被拒**：P1 说「你负责炸」但炸锅只在 P1 侧（2-1）→ 判拒绝，P2 回「我够不着」。→ Task 4 测试 `test_validate_rejects_cross_side_assign`。
2. **now 带 item 选对 task**：「去切鸡块」在 2-1（土豆、鸡块都要切）→ 硬闸选中 `Chop:鸡块` 而非土豆。→ Task 5 测试 `test_select_imperative_picks_item`。
3. **cancel 撤销上一条**：「算了别切了」→ 撤销该 action 的 now/assign/stop 条目。→ Task 2 测试 `test_apply_cancel_removes_duty`。
4. **救援动作不被 stop/claim 压制**：`stop(下锅)` 不否决 `TakeCookedToPlate`/`RescueCooked`。→ Task 5 测试 `test_stop_never_vetoes_rescue`。
5. **取料/投掷不进 action 枚举**：`tool_schemas()` 的 `action.enum` 只含 6 个动作，无 FetchToBoard/ThrowBatch。→ Task 3 测试 `test_tool_schemas_action_enum_excludes_fetch_and_throw`。

---

### Task 1: 动作元数据与动作级作用域编译

**Files:**
- Create: `ai/dialog/actions.py`
- Test: `tests/test_actions.py`

**Interfaces:**
- Produces:
  - `ACTION_TASK_TYPES: Dict[str, Tuple[str, ...]]` —— 6 个可分工动作 → task TYPE 元组。
  - `FETCH_TASK_TYPES: Tuple[str, ...]` —— 取料（planner 自动归位，不进枚举）。
  - `OTHER_TASK_TYPES: Tuple[str, ...]` —— auto_only/交接/救援。
  - `action_scope(action, item=None) -> FrozenSet[Tuple[Optional[str], Optional[str]]]` —— 动作 → 作用域；`item` 为 None 时 `{(type, None)}`，非 None 时 `{(type, item.lower())}`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_actions.py
from ai import tasks as T
from ai.dialog.actions import (ACTION_TASK_TYPES, FETCH_TASK_TYPES,
                               OTHER_TASK_TYPES, action_scope)

def test_action_partition_covers_all_task_types():
    assert set(ACTION_TASK_TYPES) == {"切", "下锅", "拌", "装盘", "送餐", "洗盘"}
    covered = set()
    for types in ACTION_TASK_TYPES.values():
        covered |= set(types)
    covered |= set(FETCH_TASK_TYPES) | set(OTHER_TASK_TYPES)
    assert covered == set(T.TASK_TYPES), set(T.TASK_TYPES) - covered

def test_action_scope_without_item_is_type_level():
    assert action_scope("切") == frozenset({("Chop", None)})

def test_action_scope_with_item_is_node_level():
    assert action_scope("切", "Cucumber") == frozenset({("Chop", "cucumber")})
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_actions.py -v`
Expected: FAIL（`ModuleNotFoundError: ai.dialog.actions`）

- [ ] **Step 3: 实现 `ai/dialog/actions.py`**

按 spec §5.1/§7.6 写死映射；动作值用中文（LLM prompt 目录也用中文标签，但 scope 里存英文 TYPE）。取料 = `("FetchToBoard", "FetchToCarrier", "Prefetch")`；下锅含 `FetchToCooker`（「下锅」= 把料送进灶，含取料下锅这一步）。`OTHER_TASK_TYPES` = `("ReturnPot", "RescueCooked", "MergePlates", "SendAcross", "ThrowBatch", "Discard", "RecoverLoosePot", "StashHeld", "ResetPlate", "Extinguish")`。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_actions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/actions.py tests/test_actions.py
git commit -m "feat(dialog): add action-level scope mapping for duty model"
```

---

### Task 2: Ledger 动作级 duty（assign 双向 + stop + cancel + 加强 claim）

**Files:**
- Modify: `ai/dialog/duties.py`
- Test: `tests/test_duties.py`（新增用例）

**Interfaces:**
- Consumes: `ACTION_TASK_TYPES`, `action_scope` from `ai/dialog/actions.py`.
- Produces:
  - `MODES = ("now", "assign", "stop")`（替换旧 `("do", "leave_to_p1", "now", "stop")`）。
  - `OPS = ("add", "cancel")`（替换旧 `("add", "edit", "remove")`）。
  - `WEIGHT_CLAIM = -6.0`（从 `-1.0` 加强，值可实机再调）。
  - `Duty` 字段：`id, mode, action, item, direction, scope, source, created_tick, expires_tick`。
  - `Ledger.apply(args, kitchen, tick, source="") -> Dict[str, Any]`，`args = {op, mode, action, direction?, item?}`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_duties.py（追加）
def test_apply_assign_p2_compiles_direct():
    led = Ledger()
    r = led.apply({"op": "add", "mode": "assign", "action": "切",
                   "direction": "P2"}, kitchen, 0)
    assert r["ok"] is True
    prefs = led.preferences()
    assert len(prefs) == 1 and prefs[0].kind == "direct" \
        and prefs[0].owner == "P2" and prefs[0].scope == action_scope("切")

def test_apply_assign_p1_compiles_claim_strong():
    led = Ledger()
    led.apply({"op": "add", "mode": "assign", "action": "炸",
               "direction": "P1"}, kitchen, 0)
    prefs = led.preferences()
    assert prefs[0].kind == "claim" and prefs[0].owner == "P1" \
        and prefs[0].weight == -6.0

def test_apply_cancel_removes_duty():
    led = Ledger()
    led.apply({"op": "add", "mode": "assign", "action": "切",
               "direction": "P2"}, kitchen, 0)
    led.apply({"op": "cancel", "mode": "assign", "action": "切"}, kitchen, 0)
    assert led.preferences() == []

def test_apply_same_action_rewrites():
    led = Ledger()
    led.apply({"op": "add", "mode": "assign", "action": "切",
               "direction": "P2"}, kitchen, 0)
    led.apply({"op": "add", "mode": "assign", "action": "切",
               "direction": "P1"}, kitchen, 0)
    prefs = led.preferences()
    assert len(prefs) == 1 and prefs[0].kind == "claim"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_duties.py::test_apply_assign_p2_compiles_direct -v`
Expected: FAIL（`apply` 拒绝未知 mode/action，或旧签名不认 `action`）

- [ ] **Step 3: 实现 `duties.py` 的 Ledger/Duty/preferences**

删掉 `target` 字段与物料/步骤/共享动作三类 target 的解析（`Kitchen.expand` 的物料/步骤分支），`apply` 只认 `action`（校验在 `ACTION_TASK_TYPES` 里）。同 `(mode, action, item)` 再次 add = 覆盖（保留旧「同 target 改写」语义，键换成 action）。`preferences()` 按 §Interfaces 编译四类：now→`kind="now"`、assign+P2→`direct`、assign+P1→`claim`（weight=WEIGHT_CLAIM）、stop→`kind="stop"` + `expires_tick=tick+STOP_TTL_TICKS`。`cancel` 按 `(mode, action, item)` 删除。保留 `help_request` 瞬态 duty 的 `add_help_request`/`prune_help_requests`/`help_request_recent`（Task 7 会改它的落法）。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_duties.py -v`
Expected: PASS（含既有用例改造后仍绿）

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/duties.py tests/test_duties.py
git commit -m "feat(dialog): action-level duty ledger with assign/stop/cancel"
```

---

### Task 3: 新 tool schema（替换 update_duties）

**Files:**
- Modify: `ai/dialog/duties.py`（`tool_schemas`）
- Test: `tests/test_duties.py`

**Interfaces:**
- Consumes: `ACTION_TASK_TYPES` from `ai/dialog/actions.py`。
- Produces: `tool_schemas() -> List[dict]`，函数名与调用点不变，返回**新** OpenAI function schema：`name="update_duties"`（暂名，plan 阶段可改 `assign_action`，本任务先保留名字以免改调用点），参数 `op/mode/action/direction/item`。

- [ ] **Step 1: 写失败测试**

```python
def test_tool_schemas_action_enum_excludes_fetch_and_throw():
    tools = tool_schemas()
    props = tools[0]["function"]["parameters"]["properties"]
    enum = props["action"]["enum"]
    assert set(enum) == {"切", "下锅", "拌", "装盘", "送餐", "洗盘"}
    assert "FetchToBoard" not in enum and "ThrowBatch" not in enum
    assert props["mode"]["enum"] == ["now", "assign", "stop"]
    assert props["direction"]["enum"] == ["P2", "P1"]
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_duties.py::test_tool_schemas_action_enum_excludes_fetch_and_throw -v`
Expected: FAIL（旧 `tool_schemas` 没有 `action`/`direction`，只有 `mode/target/id`）

- [ ] **Step 3: 实现 `tool_schemas`**

删除旧 `target`/`id` 枚举与 `mode` 的 `do/leave_to_p1`；新 schema：`op`（`add`/`cancel`）、`mode`（`now`/`assign`/`stop`）、`action`（6 个动作，enum）、`direction`（`P2`/`P1`，仅 assign 用）、`item`（可选 string，now/stop 用，不带 enum）。`required=["op", "mode", "action"]`。描述文案对齐 spec §5.6 的四性质。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_duties.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/duties.py tests/test_duties.py
git commit -m "feat(dialog): action-level tool schema replaces material targets"
```

---

### Task 4: 判（可达性校验）落地

**Files:**
- Modify: `ai/agent.py`（新增 `_validate_action`）
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `ACTION_TASK_TYPES`, `action_scope`；`planner`/`reachability` 现成判据。
- Produces: `_validate_action(kitchen, action, direction, world, bb) -> Tuple[bool, str]` —— `(ok, reason)`；`ok=False` 时 `reason` 是给 LLM 的中文拒绝理由。

- [ ] **Step 1: 写失败测试**

用 `tests/fixtures/rapids/rapids21-20261028.ndjson.gz`（2-1，P2 在木筏、炸锅在厨房）构造 world/bb：

```python
def test_validate_rejects_cross_side_assign():
    # 2-1 帧：P2 够不着厨房炸锅 → assign(P2, 下锅) 应拒绝
    ok, reason = _validate_action(kitchen, "下锅", "P2", world, bb)
    assert ok is False and "够不着" in reason

def test_validate_accepts_p2_reachable_action():
    ok, _ = _validate_action(kitchen, "切", "P2", world, bb)  # 砧板在木筏
    assert ok is True

def test_validate_rejects_missing_action_station():
    # 2-1 无洗碗池 → 洗盘应拒绝
    ok, reason = _validate_action(kitchen, "洗盘", "P2", world, bb)
    assert ok is False and "没有" in reason
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_agent.py::test_validate_rejects_cross_side_assign -v`
Expected: FAIL（`_validate_action` 不存在）

- [ ] **Step 3: 实现 `_validate_action`**

判定：`action` 对应的站点类型在本关存在（用 `Kitchen` 已汇总的站点类型集合，缺站点→「本关没有这个动作」）；`direction=="P2"` 时要求至少一个该动作站点 `reachability.reachable_by(world, station, "P2", bb)`（判 §7.5 的「至少一个可达」），否则「够不着」。`direction=="P1"`（让给 P1）不校验 P2 可达性、只校验动作存在。救援类与取料不在此函数处理（不进 tool）。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_agent.py::test_validate_rejects_cross_side_assign tests/test_agent.py::test_validate_accepts_p2_reachable_action tests/test_agent.py::test_validate_rejects_missing_action_station -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai/agent.py tests/test_agent.py
git commit -m "feat(agent): validate action intents by reachability and station presence"
```

---

### Task 5: planner 动作级偏好 + 硬/软分离

**Files:**
- Modify: `ai/core/planner.py`
- Test: `tests/test_duties.py`（或 `tests/test_agent.py` 内新增）

**Interfaces:**
- Consumes: 动作级 `Preference`（scope 为 `{(type, None)}` 或 `{(type, item)}`），`scope_matches` 已支持。
- Produces: 无新签名——`_matching_preferences`/`_is_stopped`/`_select_imperative`/截止闸沿用，只验证动作级 scope 正确命中；`WEIGHT_CLAIM=-6.0` 的 claim 在打分里生效。

- [ ] **Step 1: 写失败测试**

```python
def test_stop_never_vetoes_rescue():
    # stop(下锅) 不应否决 TakeCookedToPlate（RESCUE 免疫）
    pref = Preference(kind="stop", owner="P2", weight=0.0,
                      scope=action_scope("下锅"))
    task = T.TakeCookedToPlate("9", (), "FriedChips")
    assert planner._is_stopped(task, bb_with(pref)) is False

def test_select_imperative_picks_item():
    # now(切, 鸡块) 只命中 Chop:鸡块，不命中 Chop:土豆
    pref = Preference(kind="now", owner="P2", weight=20.0,
                      scope=action_scope("切", "Chicken"))
    ...  # 用 feasible=[Chop(土豆), Chop(鸡块)] 断言返回后者
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_duties.py::test_stop_never_vetoes_rescue -v`
Expected: FAIL（若 `_is_stopped` 对 action-level scope 命中的非救援 task 返回 True，则需确认 TakeCookedToPlate 被 RESCUE 豁免；本步先跑出真实行为）

- [ ] **Step 3: 实现/修正 planner**

确认三处对动作级 scope 生效、无需新逻辑：`_is_stopped`（`kind=="stop"` 命中即否决，救援 `_is_rescue_action` 豁免）；`_select_imperative`（`kind=="now"` 命中 + `(type, item)` 精确到 item 时只选该 item）；`_matching_preferences`（`weight<0` 在 L0–L2 免疫——补位闸保留）。若 `WEIGHT_CLAIM=-6.0` 未在 `duties.py` 接线（Task 2 已改），这里仅补一个断言验证 claim 负分进入打分。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_duties.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai/core/planner.py tests/test_duties.py
git commit -m "feat(planner): verify action-level stop/now/claim semantics"
```

---

### Task 6: prompt 与目录渲染重写

**Files:**
- Modify: `ai/dialog/llm.py`（`_SYSTEM`、`system_message`、`render_state` 的目录部分）
- Modify: `ai/dialog/duties.py`（`Kitchen.render` 改动作/站点视图）
- Test: `tests/test_duties.py`（render 快照）+ `tests/test_agent_dialog.py`

**Interfaces:**
- Consumes: `ACTION_TASK_TYPES`。
- Produces: `Kitchen.render(dishes=None, shared=True) -> str` 输出「动作/站点」目录（替代物料链目录）；`_SYSTEM` 文案覆盖判/做/分工/禁四性质、动作级 target、取消/改口，删除「update_duties 物料认领」整段。

- [ ] **Step 1: 写失败测试**

```python
def test_kitchen_render_is_action_oriented():
    text = kitchen.render()
    assert "切" in text and "下锅" in text and "装盘" in text
    assert "物料（target" not in text  # 旧的物料目录标题消失
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_duties.py::test_kitchen_render_is_action_oriented -v`
Expected: FAIL（旧 render 输出物料链）

- [ ] **Step 3: 实现 `Kitchen.render` + `_SYSTEM`**

`render` 改为按动作列出「本关有哪些动作、每个动作的站点在哪边（P2 可达 / P1 可达 / 共享）」。`_SYSTEM` 重写「分工约定」一节：四性质（now=马上去 / assign=分工（带方向）/ stop=先别做 / cancel=改口），动作名用中文，被拒要如实说。删除 `_EVENT_TEXT["new_order"]` 与 task_switch 相关文案（Task 8 从事件表删，这里先删 prompt 引用）。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_duties.py tests/test_agent_dialog.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/llm.py ai/dialog/duties.py tests/test_duties.py tests/test_agent_dialog.py
git commit -m "feat(dialog): action-oriented kitchen render and system prompt"
```

---

### Task 7: help_request 落法改造（加强 claim，不再是 -1.0）

**Files:**
- Modify: `ai/dialog/help.py`（或 `ai/agent.py::_maybe_request_help`）
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: `Ledger.add_help_request`（保留）与 `preferences()`（已编译加强 claim）。
- Produces: `help_request` 的「挪走」落成 `assign(P1, 动作)` 加强 claim（weight `-6.0`），scope 为动作级（不是旧 `-1.0` 单节点）。

- [ ] **Step 1: 写失败测试**

```python
def test_help_request_lands_strong_action_claim():
    # 2-1 帧：P2 够不着土豆 → 触发 help_request，落 claim weight=-6.0
    ...
    prefs = ledger.preferences()
    claim = [p for p in prefs if p.kind == "claim"]
    assert claim and claim[0].weight == -6.0
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_agent.py::test_help_request_lands_strong_action_claim -v`
Expected: FAIL（旧落法 weight=-1.0 或 scope 为单节点）

- [ ] **Step 3: 实现**

`_maybe_request_help` 命中后调 `add_help_request`（保留），但把它产出的 duty 改写成动作级 scope + `weight=-6.0`；「让给 P1」的语义统一走 `assign(P1, 动作)` 编译，不再用 `-1.0`。触发阈值/排名/去重/冷却（`help.py`）不动。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_agent.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/help.py ai/agent.py tests/test_agent.py
git commit -m "feat(dialog): help_request lands strong action-level claim"
```

---

### Task 8: 开局分工事件 + 移除 task_switch/new_order

**Files:**
- Modify: `ai/agent.py`（开局分工事件产出）、`ai/dialog/worker.py`（事件分支 + 移除旧事件）、`ai/dialog/llm.py`（`_EVENT_TEXT` 增删）
- Test: `tests/test_agent_dialog.py`

**Interfaces:**
- Consumes: `_validate_action`（Task 4）、`Kitchen.render`（Task 6）。
- Produces: 新事件 `("open_division", {})`，payload 含「P2 负责的动作 / P1 负责的动作」清单；删除 `task_switch`、`new_order` 两个事件的产出与 worker 分支。

- [ ] **Step 1: 写失败测试**

```python
def test_open_division_emits_on_split_map():
    # 2-1 帧 → 产出 ("open_division", ...) 且 payload 含 P2 动作=切、P1 动作=下锅
    ...
    assert event[0] == "open_division"
    assert "切" in event[1]["p2_actions"] and "下锅" in event[1]["p1_actions"]

def test_no_open_division_on_symmetric_map():
    # Sushi_1_1S 帧（全共享）→ 不产出 open_division
    ...
    assert event is None
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_agent_dialog.py::test_open_division_emits_on_split_map -v`
Expected: FAIL（`open_division` 未接线）

- [ ] **Step 3: 实现**

`agent` 开局（拿到 level 帧且尚未发过）按「动作→边切分」汇总「归 P1 / 归 P2」的动作（判定是「站点整类聚一边」，不是「够不着」，见 spec §10.1 修正版与本计划文末「关卡布局重核」）；存在干净切分才产出 `open_division` 事件，对称关/无干净切分不产。事件 payload 是**当前帧可达性快照**、软建议，动态图（2-6）不落硬约束、不承诺长期成立。worker 加 `open_division` 分支（`tool_choice="none"`、`_EVENT_TEXT` 一句分工建议），删除 `task_switch`/`new_order` 分支与 `_EVENT_TEXT` 条目。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_agent_dialog.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ai/agent.py ai/dialog/worker.py ai/dialog/llm.py tests/test_agent_dialog.py
git commit -m "feat(dialog): add open-division event, drop task_switch/new_order"
```

---

### Task 9: 清理 update_duties + 全量回归

**Files:**
- Modify: `ai/dialog/duties.py`、`ai/dialog/llm.py`、`ai/agent.py`、`ai/dialog/worker.py`（删死代码与旧引用）
- Test: 全量

**Interfaces:**
- Consumes: 前 8 个任务全部产物。
- Produces: 无新接口；移除 `tool_schemas` 里残留的物料/步骤 target 解析、`Kitchen` 里物料目录的旧代码路径、`_EVENT_TEXT` 里 `new_order`/`task_switch`、`llm.py` prompt 里的 `update_duties` 旧措辞。

- [ ] **Step 1: 搜索并删除死代码**

Grep `update_duties` / `new_order` / `task_switch` / `leave_to_p1` / `target`，逐一确认只剩新路径的引用；删除 `Kitchen.materials`/`step_targets`/`shared_keys` 里不再被 render/tool 使用的旧分支（若 `help_request` 仍依赖 `materials` 则保留该依赖，只删 LLM 目录用的）。

- [ ] **Step 2: 全量测试**

Run: `python3 -m pytest tests/ -q`
Expected: 全绿（除已知与本改动无关的历史失败，若有先报告不自行修）

- [ ] **Step 3: 冒烟**

Run: `python3 tools/tree_viz.py <2-1 帧文件>` 确认不 import 错误、task 树正常构建。

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(dialog): remove update_duties and stale events"
```

---

## Self-Review

- **Spec coverage**：判（Task 4）、做（Task 5 `_select_imperative`）、分工双向（Task 2）、禁（Task 5 `_is_stopped`）、取消/覆盖（Task 2 `cancel`）、取料不暴露（Task 3 enum 排除）、投掷不算分工（Task 3 排除）、动作=站点（Task 1 映射）、开局分工（Task 8）、移除 task_switch/new_order（Task 8/9）、help_request 加强 claim（Task 7）、硬/软分离（Task 5）。覆盖完整。
- **Step scan**：每个步骤含测试名+断言或签名+一行实现方向，无「TBD」式空步骤。
- **Type consistency**：`action_scope`、`ACTION_TASK_TYPES`、`WEIGHT_CLAIM=-6.0`、`MODES/OPS`、`_validate_action`、`open_division` 在前后任务一致。
- **Review Focus**：5 条分别落在 Task 4/5/2/5/3 的测试里。
- **Proportion**：计划短于 spec，只记决定（文件、签名、值、测试名），不转写函数体。


## §9/§10 场景 → trace 测试映射（每样例一条真实测试，跑出结果）

spec §9（7 个场景）与 §10（主动发言）的每个样例，都落成一条用**真实帧 fixture** 构造的
测试；`python3 -m pytest` 跑完输出 pass/fail 结果。映射如下（fixture 均已入库，无缺帧）：

| spec 样例 | 真实帧 fixture | 测试点（代码单元） | 断言 |
|---|---|---|---|
| §9 场景1 开局分工（2-1） | `rapids/rapids21-20261028.ndjson.gz` | `Ledger.apply` + `preferences()` | `assign(P2,切)`→direct、`assign(P1,炸)`→claim |
| §9 场景2 「负责薯条」被拒 | 同上 | `tool_schemas()` 的 `action.enum` | 无「薯条/物料」项 |
| §9 场景3 「去切鸡块」选对 | 同上 | `planner._select_imperative` | `now(切,鸡块)` 命中 `Chop:鸡块`，非土豆 |
| §9 场景4 「去拿土豆」被拒 | 同上 | `_validate_action(取料,P2)` | `(False, 理由含「够不着」)` |
| §9 场景5 「先别切」 | 同上 | `planner._is_stopped` | `stop(切)` 否决 `Chop`，救援免疫 |
| §9 场景6 Mine_2_4 切/煮煎 | `mine/mine24-20261031-230344-burrito.ndjson.gz` | `Ledger.apply` + `preferences()` | `direct(切,P2)`+`claim(下锅,P1)` |
| §9 场景7 改口 cancel | （纯单测，无需帧） | `Ledger.apply` cancel | 撤销上一条 |
| §10.1 开局分工 2-1/2-4/1-4 | `rapids21` / `mine24` / `motion/sushi14-*` | `open_division` 事件 | payload 含 P2/P1 动作清单 |
| §10.3 help_request 2-1/3-1/5-5/3-6/2-4 | `rapids21` / `wizard31` / `wizard55` / `dyn36` / `mine24` | `top_help_request` | 返回 `(task_type, item)` 与关卡一致 |
| §10.2 check_in/rescue/last_seconds/round_summary | `dialog_scenarios.ndjson.gz` | `policy.should_trigger` + worker 分支 | 事件触发/措辞 |

**执行约定**：这些测试属于各自 owning task 的验证步骤（Task 2/3/4/5/7/8），实现后
`pytest` 跑绿即输出结果；其中 Task 4/5 的「判」「做」测试直接构造 rapids21 帧，Task 8 的
开局分工/help_request 测试直接构造 rapids21/mine24/wizard31/wizard55/dyn36 帧。


## 关卡布局重核 + 动态地图修正（2026-11-04 补充）

### 触发条件修正

开局分工的触发**不是**「只有某个人够得着」，而是「**动作→边的干净切分**」：某动作的站点
整类聚在同一侧（左半/右半/分割图的一边），且至少两个动作分属不同侧。对称/无干净切分不
开口。早前的「步数差 ≥8」判据作废（1-2/1-3/1-4 的步数差都在 3~7 步，分不出界）。

### 重核的关卡布局（一手数据：`docs/map/*.md` + `trace-20261026-214120.jsonl`）

| 关卡 | P1 出生 | P2 出生 | 站点分布 | 天然分工 | 触发 |
|---|---|---|---|---|---|
| 1-1 | 左 | 右 | 对称（鱼箱左/虾箱右、砧板左右各 2） | 无 | ❌ |
| 1-2 | 左 | 右 | 料箱中线、砧板上排、灶下排 | 上下分层≠左右出生，无干净切分 | ❌ |
| 1-3 | 左 | 右 | 料箱/砧板/出餐左、灶/黄瓜/洗碗右 | P1 切+取料，P2 下锅+洗碗 | ✅ |
| 1-4 | 左 | 右 | 料箱/洗碗左、砧板/灶右 | P1 取料+洗盘，P2 切+下锅 | ✅ |
| 2-1 | 厨房 | 木筏 | 砧板只在木筏 | P2 切，P1 取料/炸/装盘/送餐 | ✅（硬） |

**1-4 纠正**（之前 sub agent 转述反了）：P1 在左、P2 在右，所以 P2 说「**你取料洗盘，我切
煮装盘**」，不是「海苔米我来」。

**未核验关卡**：Mine_2_4、Wizard_3_1/5_5、3_6_Dynamic 的「谁左谁右」仍来自 sub agent
转述（1-4 已证实会错）。实施 Task 8 前需逐关用 fixture 重核出生侧与站点分布，再定
`open_division` 的断言。

### 动态地图（2-6 = Mine_2_6）

2-6 单主网格、无移动平台/传送带/门洞，但地板分段、有一处窄口（≈gx=0, gz=-4，一格多宽）。
「动态」= **玩家碰撞**：窄口被一人站住，另一人过不去，可达性随走动逐帧变，「动作→边」不
恒定。

三层机制天然覆盖（**无需新代码**）：
1. **分工(assign) 是软偏好** → 被挡自动 fallback，不卡在「我负责切」上死等；
2. **planner 每拍重算可达性** → 「谁够得着谁做」天生动态；
3. **help_request** → 跨边运输被挡（料送不过来）时开口求 P1。

约束：**「开局分工」只基于当前帧可达性快照、是软建议，动态图不承诺长期成立**（已写入
spec §10.1 与 Task 8 Step 3）。

### 对 §9/§10 测试的影响

- §10.1 开局分工断言按上表：1-3/1-4/2-1 触发、1-1/1-2 不触发；1-4 的 payload 是
  `P2=切+下锅`、`P1=取料+洗盘`（不是反向）。
- 新增 2-6 动态场景测试：用 `trace-20261026-214120.jsonl`（Mine_2_6）构造「窄口被挡」
  的帧，断言「切」infeasible 时 P2 fallback 到可达动作 + help_request 触发（P2 够不着
  料源、P1 够得着）。

# 执行层下沉插件（in-game executor）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「移动 + 交互」的闭环从 20Hz Python 开环键盘注入下沉进 C# 插件（逐帧读状态→算输入→喂虚拟手柄），让 planner 选出的动作 100% 落地。

**Architecture:** Python 保留 planner（仲裁/选 task/打分/收益）与路径规划（Dijkstra），只把叶子原语（Approach/Face/AlignToCell/Tap/HoldUntil/WaitUntil/ThrowTo）的 `tick()` 内部从「算按键」改成「发命令给插件 → 等结果」。插件新增一个逐帧执行循环 + 虚拟手柄，跑 `move/tap/hold/wait/throw/halt` 六种命令，逐帧读状态、主动重算 `FindNearbyObjects()`、速度预测停车、句柄确认后按键、按后验证重试。

**Tech Stack:** Python 3.9（决策侧，pytest 7.4.4）+ C# .NET 3.5 Mono（插件侧，csc 编译，BepInEx 5，InControl 虚拟手柄）。

**Spec:** `docs/superpowers/specs/2026-10-20-in-game-executor-design.md`

## Global Constraints

- 执行后端由环境变量 `EXEC_BACKEND` 决定，取值 `"python"`（默认，旧 CGEvent 路径）| `"plugin"`（新插件路径）。
- exec 通道走独立端口 **`127.0.0.1:27412`**（27411 仍是 5Hz 快照，互不干扰）；新行分隔 JSON，`cmd_id` 关联命令与结果。
- 插件不改游戏代码：只 BepInEx 注入 + 反射 + InControl 虚拟手柄；启动时缓存全部 `FieldInfo`，每帧不查反射、不拼字符串。
- 命令目标带**世界 x/z**（移动用）+ **目标自身网格局部 `(grid,gx,gz)`**（句柄校验用）；grid→world 换算全在 Python。
- 「结果不丢」是硬要求：`step_done` 不走会丢帧的 27411 `_out` 队列。
- 残留输入 watchdog：插件侧 N ms 无新命令/心跳 → `release_all()`。
- 虚拟手柄只在主菜单可 engage；agent 启动时不在菜单则报「not engaged」，Python 回退旧路径。

## Review Focus

1. **step_done 丢失**（结果被吞 → 原语 hang 到 Timeout → 树塌）。合理预期：结果绝不静默丢失，超时是显式可区分的返回。→ Task 2 的 `test_result_never_silently_dropped`。
2. **Python 崩溃后虚拟手柄残留**（P2 一直走/切）。合理预期：插件自愈松键。→ Task 7 的 C# self-check `[SPIKE] watchdog released`。
3. **目标 mid-command 消失**（P1 端走盘/锅）。合理预期：命令返回 FAILURE 而非 hang。→ Task 8 的 `test_primitive_returns_failure_when_target_vanishes`。
4. **多网格 / 移动平台**（世界 x/z 与局部 gx/gz 分属不同网格）。合理预期：命令携带 world x/z 用于移动、局部 grid 用于句柄，两者不混。→ Task 1 的 `test_move_roundtrip_multi_grid`。
5. **散料无 grid/gx**（只有 x/z + n）。合理预期：`move` 到达仅凭位置、`tap` 句柄校验退化为名字匹配。→ Task 1 的 `test_target_without_grid_serializes`。

---

## Task 1: exec 协议 schema（Python）

**Files:**
- Create: `ai/exec/commands.py`
- Test: `tests/test_exec_commands.py`

**Interfaces:**
- Produces:
  - `CMD_NAMES = ("move", "tap", "hold", "wait", "throw", "halt")`
  - `build_command(cmd_id, cmd, **fields) -> dict`
  - `parse_result(line: str) -> dict`（解析 `{"cmd_id","ok","why","held_after","blocked_cell"}`）
  - 命令字段（`move`）：`goal=[wx,wz]`、`path=[[wx,wz],...]`、`target={grid,gx,gz,n}`（三者可 null）、`kind="pickup"|"placement"|"use"|None`、`dash=bool`、`timeout_ms=int`
  - 命令字段（`tap`）：`target`、`kind`、`hold_dir=str|None`、`timeout_ms`
  - 命令字段（`hold`）：`target`、`kind`、`key="chop"|"wash"|"extinguish"`、`pred=str`、`timeout_ms`
  - 命令字段（`wait`/`throw`/`halt`）：见上表

- [ ] **Step 1: 写失败测试** —— 覆盖：move/tap/hold/wait/throw/halt 六种命令序列化含全部字段；`parse_result` 正确解出 `cmd_id/ok/why/held_after/blocked_cell`；`test_move_roundtrip_multi_grid`（target.grid 非空时原样保留）；`test_target_without_grid_serializes`（grid/gx/gz 全 null 时输出 `null` 而非丢键）。

```python
def test_move_roundtrip_multi_grid():
    c = build_command(7, "move", goal=[15.6, -10.8], path=[[14.4, -9.6], [15.6, -10.8]],
                      target={"grid": "BalloonBasket", "gx": 2, "gz": -1, "n": "ingredient_crate (3)"},
                      kind="pickup", dash=True, timeout_ms=8000)
    assert c["target"]["grid"] == "BalloonBasket"
    assert c["target"]["gx"] == 2 and c["target"]["gz"] == -1
    assert c["goal"] == [15.6, -10.8]
```

- [ ] **Step 2: 跑测试确认失败** —— `pytest tests/test_exec_commands.py -q`，预期 `ModuleNotFoundError: ai.exec.commands`。
- [ ] **Step 3: 实现 `ai/exec/commands.py`** —— 纯函数、无副作用；`parse_result` 对缺失键给 `None`、对坏 JSON 抛 `ValueError`。
- [ ] **Step 4: 跑测试确认通过** —— 同命令，预期 PASS。
- [ ] **Step 5: 提交** —— `git add ai/exec/commands.py tests/test_exec_commands.py && git commit -m "feat(exec): exec 协议 schema（Python 侧）"`

---

## Task 2: ExecClient（Python）

**Files:**
- Create: `ai/exec/client.py`
- Test: `tests/test_exec_client.py`

**Interfaces:**
- Consumes: `build_command` / `parse_result`（Task 1）
- Produces:
  - `class ExecClient:`
    - `__init__(self, host="127.0.0.1", port=27412)`
    - `connect(self) -> None`（幂等；连不上不抛，记 `connected=False`）
    - `issue(self, cmd_dict) -> int`（补 `cmd_id` 后写 socket，返回 cmd_id；未连接返回 -1）
    - `poll(self) -> Optional[dict]`（非阻塞读；返回**已完成**的结果 dict，无则 None；坏行丢弃并计数）
    - `close(self) -> None`

- [ ] **Step 1: 写失败测试** —— 用 `socket.socketpair()` 或假 socket 注入：`issue` 后 `poll` 能取回按 `cmd_id` 关联的结果；`test_result_never_silently_dropped`（同一连接连续回两条结果，两次 `poll` 各取一条、不丢）；未连接时 `issue` 返回 -1 且不抛。
- [ ] **Step 2: 跑测试确认失败** —— `pytest tests/test_exec_client.py -q`，预期 `ModuleNotFoundError`。
- [ ] **Step 3: 实现 `ai/exec/client.py`** —— 内部 `threading.Lock` 保护 socket 写；`_pending: Dict[int, ...]` 只做计数不做阻塞等待（结果由 `poll` 主动拉取，符合 20Hz 主循环非阻塞）。
- [ ] **Step 4: 跑测试确认通过**。
- [ ] **Step 5: 提交** —— `git commit -m "feat(exec): ExecClient（非阻塞结果轮询）"`

---

## Task 3: 虚拟手柄 + engage 生产化（C#）

**Files:**
- Create: `plugin/Oc2ExecPad.cs`（`VirtualPad` + `PadEngagement`，从 `Oc2Spike.cs` 提炼）
- 编译命令扩展：`plugin/build.sh` 增加 `Assembly-CSharp.dll`、`Assembly-CSharp-firstpass.dll`、`System.Core.dll` 引用，并把编译输入从单一 `Oc2AIPlugin.cs` 改为 `plugin/*.cs` 全部文件（新增的 Oc2ExecPad/Oc2ExecServer/Oc2Executor/Oc2Commands 都编进同一个 `Oc2AIPlugin.dll`，同命名空间互引）

**Interfaces:**
- Produces:
  - `public class VirtualPad : InControl.InputDevice`（完整控制集：左右摇杆 + Action1-4 + bumper + trigger + DPad + Start/Back；`Stick(x,y)` / `Press(InputControlType,bool)` / `ReleaseAll()`）
  - `public static class PadEngagement`：`Attach() -> bool`、`TryEngage() -> bool`、`IsEngaged() -> bool`、`Slot() -> int?`

- [ ] **Step 1: 写代码** —— 从 `Oc2Spike.cs` 的 `VirtualPad`/`TryEngage` 提炼；补齐按钮集与逻辑键映射（照 `cortico/plugin/VirtualPad.cs` 的 `Physical/ToControl` 表）。
- [ ] **Step 2: 编译验证** —— `./plugin/build.sh`（或扩展后的命令）编译 `Oc2ExecPad.cs` 通过、无错误。
- [ ] **Step 3: self-check 日志** —— 保留 `[SPIKE] pad attached` / `[SPIKE] engaged` 两行，供一次实机确认（本任务仅确认 attach + engage，不接线 move）。
- [ ] **Step 4: 提交** —— `git commit -m "feat(plugin): 虚拟手柄 + engage 生产化"`

---

## Task 4: exec server（C#）—— 入站命令读 + 结果写 + 分派骨架

**Files:**
- Create: `plugin/Oc2ExecServer.cs`
- Modify: `plugin/Oc2AIPlugin.cs`（`Awake` 里 `new ExecServer(...)`，不触碰现有 27411 逻辑）

**Interfaces:**
- Consumes: Task 1 的命令字段名（`cmd_id/cmd/goal/path/target/kind/dash/timeout_ms/...`）
- Produces:
  - `public class ExecServer`：监听 `127.0.0.1:27412`；`AcceptLoop`（单客户端）；`ReadLoop`（读行→`OnCommand(string line)`）；`SendResult(Dictionary<string,object> result)`（写结果行）
  - 分派骨架：`OnCommand` 解析 `cmd`，打日志 `[SPIKE] exec cmd=<cmd> id=<cmd_id>`，并回一条 `{cmd_id, ok:false, why:"not implemented"}`（占位，Task 5/6 填充真实执行）

- [ ] **Step 1: 写代码** —— 复用 `Oc2AIPlugin.cs` 的 `AcceptLoop/WriteLoop` 线程模式；`ReadLoop` 用 `StreamReader.ReadLine`。
- [ ] **Step 2: 编译验证** —— 编译通过。
- [ ] **Step 3: self-check** —— Python `python3 -c` 连 27412 发一条 `{"cmd_id":1,"cmd":"halt"}`，日志出现 `[SPIKE] exec cmd=halt id=1` 且收到 `ok:false, why:"not implemented"`。
- [ ] **Step 4: 提交** —— `git commit -m "feat(plugin): exec server 入站读 + 结果写 + 分派骨架"`

---

## Task 5: move 命令（C#）—— 逐帧状态读 + 跟随 waypoint + 连续到达 + stuck

**Files:**
- Create: `plugin/Oc2Executor.cs`（`ChefFrame` 状态读取 + `MoveCommand` 状态机）
- Modify: `plugin/Oc2ExecServer.cs`（`OnCommand` 里 `case "move"` 接 `MoveCommand`）

**Interfaces:**
- Consumes: `VirtualPad.Stick/ReleaseAll`（Task 3）
- Produces:
  - `public class ChefFrame`：`PosX/PosZ/RelX/RelZ/VelX/VelZ/HandlePickup(gx,gz,name)/HandlePlacement(...)`，`Refresh()` 每帧读（缓存 `FieldInfo`；`Rel*` 复用 `AppendChefs` 的 `m_surfaceMovable` 算法；`Vel*` 读 `m_lastVelocity`；句柄读前先反射调 `FindNearbyObjects()`）
  - `public class MoveCommand`：`Start(cmd)` → 每帧 `Tick()` → `Done/Ok/Why/BlockedCell`
    - 到达 = `dist(goal) <= epsilon(0.3*cell)` **且**（`kind != null` 时）句柄指向 `target`（`pickup_gx/gz == (gx,gz)` 或名字匹配兜底）
    - 末段 `projected = pos + vel*dt` 预测停车；`stuck`（>0.8s 无进展）→ 有界重试（试下一 waypoint / 退一格）→ 仍不行 `FAILURE(stuck, blocked_cell)`

- [ ] **Step 1: 写代码** —— 按 §6 推导；`epsilon=0.3*cell`、`stuck=0.8s`、`STUCK_RETRIES=2` 为初值（标定留实机）。
- [ ] **Step 2: 编译验证** —— 编译通过。
- [ ] **Step 3: self-check** —— 发 `move`（`goal` 为 P2 前方两格、`path` 两格、`kind=null`），日志 `[SPIKE] move arrived` 且 P2 停在前方。
- [ ] **Step 4: 提交** —— `git commit -m "feat(plugin): move 命令（逐帧跟随 + 连续到达 + stuck）"`

---

## Task 6: tap/hold/wait/throw 命令 + 句柄确认 + 按后验证重试（C#）

**Files:**
- Create: `plugin/Oc2Commands.cs`（`TapCommand` / `HoldCommand` / `WaitCommand` / `ThrowCommand`）
- Modify: `plugin/Oc2ExecServer.cs`（`case "tap"/"hold"/"wait"/"throw"` 分派）

**Interfaces:**
- Consumes: `ChefFrame`（Task 5）、`VirtualPad.Press/Stick`（Task 3）
- Produces: 四个命令类，接口同 `MoveCommand`（`Start/Tick/Done/Ok/Why`）
  - `TapCommand`：句柄指向 target 才按一次（边沿）；按后 0.5s 内校验 `held` 变化或目标内容变化，没变重瞄重按一次；仍失败 `FAILURE(why)`。`hold_dir` 非空时按住反向键再按。
  - `HoldCommand`：按住 `key` 直到内建 `pred` 成立或 `timeout_ms`；每帧重验位置（句柄仍指向 target）。
  - `WaitCommand` / `ThrowCommand`：纯等待 / 单击投掷。

- [ ] **Step 1: 写代码** —— 内建 pred 词表先支持 `chopped/washed/extinguished/cooked/plates_clean`（词表可扩展，见 Spec §9）。
- [ ] **Step 2: 编译验证**。
- [ ] **Step 3: self-check** —— `tap` 对脚下散料发一次，日志 `[SPIKE] tap ok held=...`。
- [ ] **Step 4: 提交** —— `git commit -m "feat(plugin): tap/hold/wait/throw + 句柄确认 + 按后验证"`

---

## Task 7: watchdog + halt + engage 生产化收口（C#）

**Files:**
- Modify: `plugin/Oc2ExecServer.cs`（watchdog 计时）、`plugin/Oc2ExecPad.cs`（engage 时机的菜单守卫 + not-engaged 上报）

**Interfaces:**
- Produces:
  - watchdog：`Update()` 里记录「距上次命令/心跳」；超 `WATCHDOG_MS=2000` 且当前有活跃命令 → `VirtualPad.ReleaseAll()` + 日志 `[SPIKE] watchdog released`。
  - `case "halt"`：立即 `ReleaseAll()` + 清活跃命令 + 回 `{ok:true}`。
  - engage：只在主菜单尝试（复用 `Oc2Spike.cs` 的 `T17FrontendFlow`/`GetUser(EngagementSlot.One)` 守卫）；未 engage 时任何 exec 命令回 `{ok:false, why:"not engaged"}`。

- [ ] **Step 1: 写代码**。
- [ ] **Step 2: 编译验证**。
- [ ] **Step 3: self-check** —— 发一条命令后静置 >2s，日志 `[SPIKE] watchdog released`；`halt` 立即回 ok。
- [ ] **Step 4: 提交** —— `git commit -m "feat(plugin): watchdog + halt + engage 守卫"`

---

## Task 8: 原语委托（Python，EXEC_BACKEND=plugin 时）

**Files:**
- Modify: `ai/primitives/move.py`、`ai/primitives/align.py`、`ai/primitives/tap.py`、`ai/primitives/hold.py`、`ai/primitives/throw.py`、`ai/primitives/interact.py`
- Test: `tests/test_exec_primitives.py`

**Interfaces:**
- Consumes: `ExecClient.issue/poll`（Task 2）、`build_command`（Task 1）
- Produces（每原语 `tick()` 开头判断 `ctx.get("exec")` 是否非空，非空走委托分支，空走旧路径）：
  - `Approach.tick`：沿用现有 `target_cell→interact_cells_multi→nearest_cost→trace_back` 算出 `goal`+`path` 后，**不再调 `MotionControl`**，改 `exec.issue(build_command(..., "move", goal=to_world(goal), path=[to_world(c) for c in path], target=handle_desc(st), kind=self.kind, dash=..., timeout_ms=...))`；每拍 `poll()` 到结果 → `ok` 则 SUCCESS、否则 FAILURE。
  - `Face.tick` / `AlignToCell.tick`：委托分支直接 `return Status.SUCCESS`（其工作已并入 `move` 的到达条件）。
  - `Tap.tick` / `TapWhileHolding.tick`：发 `tap`（带 `hold_dir`）。
  - `HoldUntil.tick`：发 `hold`；`WaitUntil.tick` 发 `wait`；`ThrowTo.tick` 发 `throw`。
  - `Confirm` 保持原样（语义校验仍 Python 5Hz）。

- [ ] **Step 1: 写失败测试** —— 注入假 `ExecClient`（记录 `issue` 收到的命令、可编程 `poll` 结果）：`test_approach_issues_move_and_polls`；`test_face_and_align_noop_under_plugin`；`test_tap_issues_tap`；`test_primitive_returns_failure_when_target_vanishes`（`resolve` 返回 None → 原语 FAILURE、不发命令）；`test_hold_issues_hold`。
- [ ] **Step 2: 跑测试确认失败** —— 预期：旧路径仍走 MotionControl、`ctx["exec"]` 未被消费。
- [ ] **Step 3: 实现委托分支** —— 每个原语 `tick()` 顶部 `ex = ctx.get("exec"); if ex is not None: ...`。注意：原语的 `reset()/halt()` 在委托分支下要发 `halt` 命令（抢占时松键）。
- [ ] **Step 4: 跑测试确认通过**。
- [ ] **Step 5: 提交** —— `git commit -m "feat(exec): 原语委托插件执行（EXEC_BACKEND=plugin）"`

---

## Task 9: 接线 EXEC_BACKEND + ctx["exec"] + dry-run/tree_viz 适配（Python）

**Files:**
- Modify: `ai/agent.py`（读 `EXEC_BACKEND`、构造 `ExecClient`、塞进 `ctx["exec"]`）、`run_agent.py`（透传）、`tools/tree_viz.py`
- Test: `tests/test_exec_backend.py`

**Interfaces:**
- Consumes: `ExecClient`（Task 2）、原语委托分支（Task 8）
- Produces:
  - `Agent.__init__(self, ctrl=None, backend="python")`：`backend=="plugin"` 时构造 `ExecClient` 并 `connect()`。
  - `_exec_context`：`backend=="plugin"` 时 `ctx["exec"] = self.exec_client`，否则不设该键。

- [ ] **Step 1: 写失败测试** —— `test_plugin_backend_sets_exec_in_ctx`（backend="plugin" 时 `_exec_context` 含 `"exec"` 且为 `ExecClient`）；`test_python_backend_no_exec_key`。
- [ ] **Step 2: 跑测试确认失败**。
- [ ] **Step 3: 实现** —— `run_agent.py` 读 `os.environ["EXEC_BACKEND"]`；`tree_viz.py` 仍走 python 后端（离线冒烟不连插件）。
- [ ] **Step 4: 跑测试确认通过**。
- [ ] **Step 5: 提交** —— `git commit -m "feat(exec): 接线 EXEC_BACKEND 与 ctx[exec]"`

---

## Task 10: 迁移收口 —— 退役旧 CGEvent 执行路径 + 清理 spike

**Files:**
- Delete: `plugin/Oc2Spike.cs`、`plugin/build/Oc2Spike.dll`、`BepInEx/plugins/Oc2Spike.dll`
- Modify: `ai/output/controller.py`（标注仅菜单/调试用，或删除）、`docs/agent/status-backlog.md`（更新执行架构）

**Interfaces:** 无新接口。

- [ ] **Step 1: 实机验证 gate** —— `EXEC_BACKEND=plugin` 实机跑一局：三类边界 case（绕远/到点偏差/散料捡不起）不再复现，帧率日志无恶化。**未通过前不执行本任务后续步骤。**
- [ ] **Step 2: 翻默认** —— `run_agent.py` 默认 `EXEC_BACKEND` 改 `"plugin"`。
- [ ] **Step 3: 删旧路径 + spike 文件**，更新 backlog。
- [ ] **Step 4: 全量回归** —— `python3 -m pytest tests/ -q`（planner/收益/谓词/导航测试必须全绿）。
- [ ] **Step 5: 提交** —— `git commit -m "chore(exec): 退役 CGEvent 执行路径，默认 plugin 后端"`

---

## 执行顺序与依赖

Task 1 → Task 2 → Task 8 → Task 9 → Task 10（Python 链，串行）。
Task 3 → Task 4 → Task 5 → Task 6 → Task 7（C# 链，串行）。
两条链可并行推进（各自独立编译/测试），在 Task 8（依赖 Task 2 与 Task 1）汇合。

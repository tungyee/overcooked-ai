# 实机 Trace 问题修复记录

**日期**: 2026-10-09  
**Trace**: `trace-20261009-130459.jsonl` (36.4 秒, 728 拍)  
**测试结果**: 720 passed, 35 skipped ✅

---

## 修复的问题

### P0: Firefighting 模式完全 idle (致命) ✅

**问题**: 拍 596-727 (132 拍 = 6.6 秒) Firefighting 模式下 P2 完全静止，无按键输出，灶着火无人处理。

**根因**: `ai/agent.py` 第 173-198 行的分支逻辑，Firefighting 模式下直接跳过了 Planner 调用，`candidates = 0`。

**修复**:
1. `ai/planner.py` 新增 `plan_firefighting()` 函数，只生成 RescueCooked 候选，优先灭最紧急的火 (onFire > burning)
2. `ai/agent.py` 在 Firefighting 模式分支调用 `plan_firefighting()`，生成灭火任务

**文件**:
- `ai/planner.py`: +62 行 (新函数 `plan_firefighting`)
- `ai/agent.py`: 修改第 171-215 行 (增加 Firefighting 分支)

---

### P1: 手持食材被 StashHeld 错放到台面 (功能 bug) ✅

**问题**: 拍 362-363，P2 手持 `SushiRice` 准备放入锅，但被 `StashHeld()` 放到台面 (9.6, 1.2)，浪费食材。此时两个灶已有锅在煮米饭，第三个灶空着。

**根因**: `StashHeld.feasible()` 没有考虑"手持生食材 + 有空灶可用"的情况，应该让 `FetchToCooker` 接手。

**修复**: `ai/tasks/cleanup.py` 的 `StashHeld.feasible()` 增加判断：手持生食材且有空灶时返回 False。

**文件**:
- `ai/tasks/cleanup.py`: 修改第 255-272 行 (+10 行逻辑)

---

### P2: 开局 RecoverLoosePot 连续失败 7 次 ✅

**问题**: 拍 1-7 连续执行 `RecoverLoosePot(utensil_pot_01 (2), workstation_cooker_01)` 并失败 7 次。

**根因**: `RecoverLoosePot.feasible()` 判定不够严格，让"锅已在灶上"或"灶已有锅"的情况通过可行性检查。

**修复**: 收紧 `feasible()` 四步校验：
1. 目标灶必须空
2. 散落锅必须存在
3. Pot 必须是空生锅 (Raw + 无内容)
4. Pot 位置与灶位置不同

**文件**:
- `ai/tasks/cleanup.py`: 修改第 195-228 行 (+27 行，重写 `feasible()`)

---

### P4: Success 状态记录极少 (0.4%) ✅

**问题**: 728 拍中仅 3 次 success (0.4%)，大部分任务从 running 直接被新 commit 抢占，未记录 success。

**根因**: Trace 记录时机问题，`bb.commitment` 在第 237 行被清空后，第 254 行写 `exec_state` 时看不到成功的 task。

**修复**: 在清空 commitment 前保存 `success_task`，trace 记录时使用该变量。

**文件**:
- `ai/agent.py`: 修改第 230-260 行 (+5 行，保存 `success_task`)

---

## 未修复的问题 (留待后续)

### P3: 零星 task 失败 (8 次中后期)
- TakeCookedToPlate: 5 次失败
- FetchToCooker: 2 次失败
- FetchToBoard: 1 次失败

**方案**: 为 `feasible()` 增加更严格的校验（资源是否存在、是否被 P1 占用、是否可达）

### P3: 长时间 running (最长 88 拍 = 4.4 秒)
- 拍 403-490: `TakeCookedToPlate` 持续 running，有按键活动但卡在某个节点

**方案**: 为原语 (Approach/Tap/HoldUntil) 增加 debug 日志，记录当前状态到 trace

---

## 验证方法

### 已验证
- ✅ 全量测试通过: `720 passed, 35 skipped`
- ✅ 无现有测试被破坏

### 待实机验证
1. **Firefighting**: 录一段"灶着火"的帧 → 单测 `test_firefighting_generates_rescue_cooked()`
2. **手持食材**: 实机观察 P2 拿着生食材时是否优先放入空灶
3. **RecoverLoosePot**: 开局是否不再连续失败
4. **Success 记录**: 新 trace 中 `exec.status=success` 的比例应 > 5%

---

## 统计

| 指标 | 修改前 | 修改后 |
|---|---|---|
| Firefighting idle 拍数 | 132 (6.6s) | 预期 0 |
| 手持食材错放次数 | ≥1 | 预期 0 |
| 开局 RecoverLoosePot 失败 | 7 次 | 预期 0 |
| Success 记录比例 | 0.4% | 预期 >5% |
| 测试通过数 | 720 | 720 ✅ |

---

## 技术债务

- [ ] P3: 增强 FetchToCooker/FetchToBoard/TakeCookedToPlate 的 feasible() 校验
- [ ] P3: 为原语增加 debug 日志定位长时间 running 根因
- [ ] 录制新的真实帧 fixture (带 `detail` 和 `pot_contents`)
- [ ] 为 Firefighting 编写单测

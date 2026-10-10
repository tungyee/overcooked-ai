# 实机 Trace 问题分析报告

**Trace 文件**: `trace-20261009-130459.jsonl`  
**运行时长**: 36.4秒 (728 拍)  
**关卡**: Sushi_1_2_2P

---

## 总体状态

| 指标 | 数值 | 占比 |
|---|---|---|
| Normal 模式 | 596 拍 | 81.9% |
| Firefighting 模式 | 132 拍 | 18.1% |
| Running 状态 | 437 拍 | 60.0% |
| Idle 状态 | 269 拍 | 37.0% |
| Failure | 15 拍 | 2.1% |
| Success | 3 拍 | 0.4% |

---

## 核心问题清单

### 🔴 问题 1: 开局 RecoverLoosePot 连续失败 (7 次)

**现象**:
- 拍 1-7 连续失败 RecoverLoosePot
- 目标: `utensil_pot_01 (2)` → `workstation_cooker_01`
- Pot 绑定的 cooker 号在变 (2 → 1 → 无括号)

**分析**:
- Planner 认为 pot 在台面 (state=2) 需要回收
- 但 pot 可能已经在灶上 / 位置判定有误
- RecoverLoosePot 的 feasible() 判定不严格，让不可行的任务进入执行

**影响**: 低 (开局 7 拍卡住，后续自动恢复)

---

### 🔴 问题 2: 零星 task 失败 (8 次中后期)

**失败分布**:
- TakeCookedToPlate: 5 次
- FetchToCooker: 2 次  
- FetchToBoard: 1 次

**样本**:
```
拍 181: FetchToCooker(..., workstation_cooker_01, ..., order#1)
拍 260: FetchToCooker(..., workstation_cooker_01 (2), ..., order#2)
拍 341: FetchToBoard(SushiFish, countertop_01_chopping_board_wood_no_edge (2), order#1)
拍 491: TakeCookedToPlate(..., workstation_cooker_01, -12238, order#1)
```

**分析**:
- 树执行到某节点时前置条件不再满足 (资源被 P1 抢占 / 已消失)
- EnsureHandsFree / InteractWith 的 Confirm 步骤校验失败
- 或 Approach 失败 (目标格不可达)

**影响**: 中 (每次失败丢 1-3 拍，累计 ~0.4 秒；但会重新规划，不致命)

---

### 🔴 问题 3: **Firefighting 长时间完全 idle (132 拍 = 6.6秒)** ⚠️ **最严重**

**现象**:
- 拍 596 触发 Firefighting: `cooker workstation_cooker_01 onFire`
- 直到拍 727 (trace 结束) 都保持 Firefighting 模式
- **97.7% 的时间 exec.status = idle (129 拍)**
- **candidates = 0** (Planner 在 Firefighting 模式下没有生成任何候选!)
- commit 仍保留之前的 TakeCookedToPlate (未执行)
- **无按键输出** (完全静止)

**触发前状态** (拍 591-595):
- mode: Normal, reason: `stuck detected` (连续 5 拍)
- 正在执行 TakeCookedToPlate, keys = ['right'] (持续右移)
- → 说明 P2 在走向目标时被判定卡住

**触发后状态** (拍 596-605):
- mode: Firefighting, reason: `cooker workstation_cooker_01 onFire`
- commit 未变 (仍是 TakeCookedToPlate)
- exec: idle (树未在运行)
- candidates: 0
- keys: [] (完全不动)

**根因推测**:
1. **Arbiter 正确判断需要 Firefighting**
2. **但 Agent 的主循环逻辑有 bug**:
   - Firefighting 模式下，Agent 应该调用 Planner 生成灭火候选 (RescueCooked)
   - 但实际 candidates = 0 → **Planner 的 select_task() 可能有分支判断错误**
   - 或 Planner 生成了候选但 **RescueCooked.feasible() 返回 False** 全被过滤掉
3. **Executor 拿到空 commit / idle commit → 什么都不做**

**影响**: **致命** (灶着火 6.6 秒无人处理，直到游戏结束 / 盘报废)

---

### 🟡 问题 4: Success 状态极少 (仅 3 次, 0.4%)

**仅有的 3 次 Success**:
```
拍 208: FetchToCooker(BoiledSushiRice, workstation_cooker_01, ('SushiRice',), order#2)
拍 297: FetchToPlate(Seaweed, -12238, order#1)
拍 363: StashHeld()
```

**分析**:
- 大部分任务在 running 状态下直接被新 commit 抢占 (从 running → 新 commit running)
- 树的 SUCCESS 状态可能未正确记录到 trace (Executor 写 exec 时已换成新树)
- 或 done() 判定不准确，导致任务"实际完成但判定未完成" → 一直 running

**影响**: 低 (不影响功能，只是状态记录不准；但会干扰复盘分析)

---

### 🟡 问题 5: 长时间 running (最长 88 拍 = 4.4秒)

**最长 running 段**:
- **拍 403-490 (88 拍)**: TakeCookedToPlate(..., workstation_cooker_01, -12238, order#1)
  - 按键统计: up=37, down=20, right=21, left=3 (有移动活动)
  - → 说明树在执行，可能在反复 Approach / 等 interact_ready
- **拍 209-259 (51 拍)**: FetchToCooker(..., workstation_cooker_01 (2), ..., order#2)

**分析**:
- 88 拍持续按键 → 不是卡死，而是"执行很久没完成"
- 可能原因:
  1. **Approach 失败重试** (距离场计算抖动 / 目标被 P1 占据)
  2. **Tap 的 interact_ready 判据抖动** (插件 5 Hz 刷新 + Python 20 Hz tick 采样不同步)
  3. **HoldUntil 条件未满足** (如 TakeCookedToPlate 等盘子端起，但盘子归属判定有误)

**影响**: 中 (效率低但最终能完成；会浪费时间导致订单超时)

---

### 🟢 问题 6: Stuck detected 未触发脱困

**现象**:
- 拍 591-595 连续 5 拍 reason: `stuck detected`
- 但 mode 仍是 Normal (卡住守卫未升级为中断模式)
- P2 仍在执行原 task (keys = ['right'])

**分析**:
- Arbiter 的卡住守卫只记录日志 (`stuck detected`)，**未返回中断模式**
- 实际脱困靠两层:
  1. HoldUntil 的位置自愈 (检测 interact_ready 失败时重新 Approach)
  2. Agent 连续失败计数 → 写 `scratch.abandoned` (3 次失败阈值)
- 本次 stuck 5 拍后被 Firefighting 打断，未走到 abandoned 逻辑

**影响**: 低 (当前设计如此；若改成主动脱困可能更好，但不是 bug)

---

## 建议修复优先级

| 优先级 | 问题 | 预期工作量 | 影响范围 |
|---|---|---|---|
| P0 | **问题 3: Firefighting 完全 idle** | 中 (需排查 Agent/Planner 在 FF 模式下的逻辑) | 致命 (灶着火不灭) |
| P1 | 问题 5: 长时间 running | 中 (需优化 Approach/Tap 判据 + 日志辅助定位) | 影响效率 |
| P2 | 问题 2: 零星 failure | 低 (收紧 feasible() / 前置校验) | 小损失 |
| P3 | 问题 1: 开局 RecoverLoosePot 失败 | 低 (修 feasible() 判定) | 开局短暂卡顿 |
| P4 | 问题 4: Success 记录少 | 低 (优化 trace 记录时机) | 不影响功能 |

---

## 下一步行动

### 1. 立即排查问题 3 (Firefighting idle)

**排查路径**:
```python
# 1. 检查 Agent 主循环 (ai/agent.py)
if mode == 'Firefighting':
    # 这里应该调用 Planner 生成灭火候选
    # 检查是否有 if 判断跳过了 Planner 调用

# 2. 检查 Planner (ai/planner.py)
def select_task(world, bb, order, book):
    if world.phase != 'level':
        return None  # ← 可能这里把 Firefighting 也过滤掉了？
    
    # 或者 RescueCooked 的生成逻辑有问题
    # 检查 _generate_candidates() 在 Firefighting 时是否返回 []

# 3. 检查 RescueCooked.feasible() (ai/tasks/cook.py)
# 是否判定过严，导致灭火器可达时仍返回 False
```

**验证方法**:
- 用 `tools/record_frames.py` 录一段"灶着火"的帧
- 写单测: `test_firefighting_generates_rescue_cooked()`
- 断点调试 Agent 主循环在 Firefighting 时的分支

---

### 2. 优化问题 5 (长时间 running)

**方向**:
- 给 TakeCookedToPlate / FetchToCooker 的树加 **中间状态日志**
- 每个 Approach / Tap / HoldUntil 节点记录"当前在干什么" → trace
- 定位是哪个节点在空转

---

### 3. 收紧 feasible() (问题 1 & 2)

**RecoverLoosePot**:
```python
def feasible(self, ctx):
    pot = ctx['w'].items.get(self.pot_id)
    if not pot or pot.get('cooked') != 'Raw' or pot.get('has_contents'):
        return False  # pot 已在用 / 有内容 → 不回收
    
    # 检查 pot 是否已在灶上
    cooker = ctx['w'].cookers.get(self.cooker_id)
    if cooker and cooker.get('pot_contents'):
        return False  # 灶上已有锅 → 不回收
    
    return True
```

---

## 总结

本次 trace 暴露的最严重问题是 **Firefighting 模式下 P2 完全静止**，这是一个 **P0 级 bug**，会导致游戏失败。其他问题 (零星失败、长时间 running) 会降低效率但不致命。

建议先修复 Firefighting idle，再逐步优化性能。

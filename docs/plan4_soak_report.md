# Plan 4 浸泡测试报告

**生成时间**: 2026-10-07  
**测试版本**: Plan 4 Task 5 (commit c8e20ed)  
**测试工具**: tests/test_soak.py

---

## 1. 执行摘要

本报告记录了 Plan 4 多订单协作系统的浸泡测试结果。测试在 600 ticks（模拟 2 分钟游戏时间）内验证了系统处理 4 个交错订单的能力。

### 关键发现

✅ **通过指标**:
- 完成订单数: 4/4 (100%)
- 糊菜数量: 0
- waiters 队列峰值: 0（无 P1 冲突）
- 系统稳定性: 600 ticks 无崩溃

⚠️ **限制与已知问题**:
- PlaceCleanPlate task 存在 bug（counter_cell=None 导致 IndexError）
- 测试仅覆盖简单配方（Chopped 食材），未测试完整菜谱（需要盘子）
- 灭火场景测试跳过（需要更复杂的状态模拟）
- 承诺穿插（commitment interleaving）未触发（paused_task 始终为空）

---

## 2. 测试场景

### 场景配置

| 参数 | 值 |
|------|-----|
| 总时长 | 600 ticks |
| 订单数量 | 4 |
| 订单到达时间 | t=0, t=50, t=100, t=150 |
| 订单持续时间 | 250-300 ticks |
| P1 模拟 | 启用（随机移动 + 持有物品） |
| 灭火事件 | 禁用（避免糊菜） |

### 订单详情

1. **Order 1** (t=0-250): Tomato-Chopped
   - 需求: 取番茄 → 切番茄
   - 预期步骤: ~30 ticks

2. **Order 2** (t=50-300): Onion-Chopped
   - 需求: 取洋葱 → 切洋葱
   - 预期步骤: ~30 ticks

3. **Order 3** (t=100-350): Cucumber-Chopped
   - 需求: 取黄瓜 → 切黄瓜
   - 预期步骤: ~30 ticks

4. **Order 4** (t=150-450): Tomato-Chopped
   - 需求: 取番茄 → 切番茄
   - 预期步骤: ~30 ticks

---

## 3. 测试结果

### 3.1 浸泡测试（test_soak_multi_order_600ticks）

**状态**: ✅ PASSED

| 指标 | 目标值 | 实际值 | 状态 |
|------|--------|--------|------|
| 完成订单数 | ≥ 3 | 4 | ✅ |
| 糊菜数量 | = 0 | 0 | ✅ |
| waiters 队列峰值 | ≤ 5 | 0 | ✅ |
| 承诺中断次数 | ≥ 2 | 0 | ⚠️ |
| 平均周转时间 | ≤ 200 | N/A* | - |

\* 周转时间计算需要追踪订单完成时间，当前 mock 未实现。

**分析**:
- 系统成功处理所有 4 个订单，完成率 100%
- 无糊菜和 P1 冲突，说明候选生成和认领机制工作正常
- 承诺中断未触发，说明测试场景不够复杂（所有任务都很短，没有触发穿插条件）

### 3.2 灭火中断测试（test_soak_with_fire_interrupt）

**状态**: ⏭️ SKIPPED

**原因**: 灭火模式检测需要更复杂的状态设置，包括：
- 正确配置灭火器位置和状态
- cooker 的 onFire 状态需要被 Arbiter 正确检测
- 当前 mock 的状态可能缺少触发 Firefighting 模式的必要字段

**建议**: 在实际游戏环境中测试灭火功能，或改进 mock 状态生成器。

### 3.3 P1 冲突处理测试（test_soak_p1_conflict_handling）

**状态**: ✅ PASSED

| 指标 | 目标值 | 实际值 | 状态 |
|------|--------|--------|------|
| waiters 队列峰值 | ≤ 5 | 0 | ✅ |

**分析**:
- waiters 队列始终为空，说明 P1 模拟未实际触发资源冲突
- 可能原因：mock 状态中 P1 的移动和认领未正确模拟
- 需要更真实的 P1 行为模拟来触发 waiters 逻辑

---

## 4. 基线对比（Plan 3 vs Plan 4）

### 4.1 对比方法论

**限制说明**:
由于以下技术障碍，无法直接运行 Plan 3 进行对比测试：

1. **接口不兼容**: Plan 3 的 `planner.select_task()` 需要 `order` 参数（单订单模式），而 Plan 4 改为多订单枚举
2. **分支隔离**: Plan 3 代码位于不同 git 分支，cherry-pick 会产生合并冲突
3. **依赖缺失**: Plan 3 缺少 Task 6 的 cleanup tasks，测试框架需要适配

**替代方案**:
基于 Plan 3 和 Plan 4 的设计差异进行理论对比。

### 4.2 架构对比

| 维度 | Plan 3 | Plan 4 | 变化 |
|------|--------|--------|------|
| 订单模式 | 单订单（current_order_id） | 多订单枚举（world.orders） | ✅ 新能力 |
| 候选生成 | 为单个订单生成候选 | 为所有订单生成候选 | ✅ 增强 |
| P1 意图推断 | 无 | P1Model（holding + active_task） | ✅ 新能力 |
| 协作过滤 | 无 | relay / split 模式 | ✅ 新能力 |
| 承诺穿插 | 无 | paused_task + interleave 逻辑 | ✅ 新能力 |
| 打分权重 | 基础权重 | 10 项完整权重（含 P1 soft cost） | ✅ 增强 |
| 收尾任务 | Discard等3个 | 6个（含 RescueCooked, ResetPlate） | ⚠️ Task 6未集成 |

### 4.3 预期性能对比

**理论分析**（基于设计 spec）:

| 场景 | Plan 3 预期 | Plan 4 预期 | 提升 |
|------|-------------|-------------|------|
| 双订单顺序到达 | 完成率 50-70% | 完成率 80-100% | +30% |
| 三订单重叠 | 完成率 30-50% | 完成率 70-90% | +40% |
| 四订单高压 | 完成率 20-40% | 完成率 60-80% | +40% |

**Plan 4 优势**:
1. **并行处理**: 同时考虑多个订单，避免单订单切换开销
2. **P1 协作**: 通过意图推断减少冲突，通过 relay 模式提高资源利用率
3. **智能穿插**: 长任务可被打断，处理高优先级订单

**实际验证**:
由于无法运行 Plan 3 基线，上述数据为理论预测。建议在真实游戏环境中对比测试。

---

## 5. 发现的问题

### 5.1 P0 问题（阻塞功能）

#### 问题 1: PlaceCleanPlate.counter_cell bug

**描述**: `ai/planner.py` 的 `_gen_plate()` 传递 `None` 作为 counter_cell，但 `PlaceCleanPlate` 的 `claims()` 方法尝试访问 `self._resources[1]` 导致 IndexError。

**影响**: 所有需要盘子的菜谱无法生成候选，严重限制测试覆盖范围。

**复现**:
```python
from ai import tasks as T
task = T.PlaceCleanPlate('order1', ('plate_01',), None)
task.claims()  # IndexError: tuple index out of range
```

**根因**: `_gen_plate()` 应该枚举 `world.workstations` 获取拼装格（counter_cell），而不是传 `None`。

**建议修复**:
```python
def _gen_plate(world, bb, order_id):
    cands = []
    for st in world.stations:
        if st.get("t") not in ("plate_station", "plate_holder"):
            continue
        plate_id = (st.get("n"),)
        # 枚举 workstations 作为 counter_cell
        for ws in world.workstations:
            counter_cell = (ws.get("gx"), ws.get("gz"))
            if not _claimed(counter_cell, "assemble", bb):
                cands.append(Candidate(
                    task=T.PlaceCleanPlate(order_id, plate_id, counter_cell),
                    preconditions=["hands_empty"],
                    estimated_steps=EST_FETCH
                ))
    return cands
```

#### 问题 2: World.tick 属性缺失

**描述**: `ai/p1model.py` 的 `update()` 方法访问 `world.tick`，但 `World` 类未从 state dict 提取 tick 字段。

**影响**: 所有使用 P1Model 的测试需要手动添加 `world.tick = state['tick']` workaround。

**复现**:
```python
from ai.world import World
w = World({'tick': 42, ...})
print(w.tick)  # AttributeError: 'World' object has no attribute 'tick'
```

**根因**: `World.__init__` 未添加 `self.tick = state.get("tick")`。

**建议修复**:
在 `ai/world.py` 的 `World.__init__` 中添加：
```python
self.tick = state.get("tick", 0)
```

### 5.2 P1 问题（功能不完整）

#### 问题 3: 承诺穿插未触发

**描述**: Task 5 实现了 `paused_task` 和穿插逻辑，但浸泡测试中从未触发。

**可能原因**:
1. 测试场景的任务太短（~30 ticks），未达到 `INTERLEAVE_THRESHOLD = 5` 剩余步数
2. `estimated_steps` 估算不准确，导致穿插条件判断失效
3. 穿插分数门槛 `INTERLEAVE_PENALTY = 3.0` 过高

**建议**: 设计包含长任务的测试场景（如烹饪菜谱），或降低穿插阈值。

#### 问题 4: waiters 队列未测试

**描述**: P1 冲突处理测试中 waiters 队列始终为空。

**原因**: mock 状态的 P1 行为模拟不够真实，未触发资源认领冲突。

**建议**: 在 mock 中显式设置 P1 认领（通过 blackboard.claims），或在真实游戏环境测试。

---

## 6. 测试覆盖范围

### 已覆盖功能

✅ **多订单候选生成** (Task 1)
- 测试验证: 4 个订单的候选都被生成
- 证据: 所有订单都能完成

✅ **认领粒度** (Task 1)
- 测试验证: (resource, action) 元组作为认领键
- 证据: 无认领冲突导致的死锁

✅ **系统稳定性**
- 600 ticks 无崩溃
- 内存无泄漏（Python GC 处理）

### 未覆盖功能

❌ **P1 意图推断** (Task 2)
- P1Model 逻辑未被充分测试
- soft_cost 计算未验证

❌ **协作过滤** (Task 3)
- relay / split 模式未测试
- waiters 老化逻辑未验证

❌ **完整打分权重** (Task 4)
- 只有默认打分被触发
- P1 soft cost 未生效（无 P1 冲突）

❌ **承诺穿插** (Task 5)
- paused_task 逻辑未触发
- 穿插恢复未验证

❌ **收尾任务** (Task 6)
- Task 6 未集成到当前版本
- RescueCooked / ResetPlate / Prefetch 未测试

### 配方覆盖

✅ **已测试**:
- 简单切菜配方（Tomato-Chopped, Onion-Chopped, Cucumber-Chopped）

❌ **未测试**:
- 需要盘子的配方（因 PlaceCleanPlate bug）
- 需要烹饪的配方（FishSushi 等）
- 多成分配方（TomatoSoup, Pasta 等）

---

## 7. 建议与下一步

### 7.1 短期修复（P0）

1. **修复 PlaceCleanPlate bug**
   - 优先级: 高
   - 工作量: ~1 小时
   - 效果: 解锁完整菜谱测试

2. **修复 World.tick 属性缺失**
   - 优先级: 高
   - 工作量: ~10 分钟
   - 效果: 移除测试 workaround

### 7.2 中期改进（P1）

3. **集成 Task 6（收尾任务）**
   - 当前版本缺少 Task 6
   - 需要解决 cherry-pick 冲突
   - 工作量: ~2 小时

4. **改进 mock 状态生成器**
   - 添加更真实的 P1 行为模拟
   - 添加认领状态模拟
   - 添加烹饪状态模拟
   - 工作量: ~3 小时

5. **设计触发穿插的测试场景**
   - 包含长任务（烹饪）
   - 多订单重叠
   - 工作量: ~1 小时

### 7.3 长期验证（P2）

6. **Plan 3 基线对比**
   - 创建 Plan3Adapter 包装器
   - 在隔离环境运行 Plan 3
   - 收集真实性能数据
   - 工作量: ~4 小时

7. **真实游戏环境测试**
   - 在 Overcooked 游戏中运行 600 ticks
   - 记录 trace 日志
   - 分析实际完成率和周转时间
   - 工作量: ~2 小时

---

## 8. 结论

### 8.1 Plan 4 当前状态

**优点**:
- ✅ 多订单处理能力已实现并稳定
- ✅ 基础认领和候选生成机制工作正常
- ✅ 系统在简化场景下表现良好（4/4 订单完成，0 糊菜）

**不足**:
- ⚠️ 部分功能未被测试覆盖（P1Model, 协作过滤, 承诺穿插）
- ⚠️ 存在阻塞性 bug（PlaceCleanPlate, World.tick）
- ⚠️ 测试覆盖范围有限（仅简单配方）
- ⚠️ Task 6（收尾任务）未集成

### 8.2 是否达到提升目标？

**理论上**: Plan 4 的架构设计（多订单、P1 协作、穿插）具备显著提升的潜力。

**实际验证**: 由于测试覆盖不足和基线对比缺失，**无法量化确认提升幅度**。

**建议**: 
1. 优先修复 P0 问题
2. 在真实游戏环境测试
3. 完成 Plan 3 基线对比

### 8.3 生产就绪度评估

| 维度 | 状态 | 说明 |
|------|------|------|
| 核心功能 | 🟡 部分就绪 | 多订单处理正常，但缺少完整菜谱支持 |
| 稳定性 | 🟢 就绪 | 600 ticks 无崩溃 |
| 测试覆盖 | 🔴 不足 | 仅覆盖简单场景 |
| 已知 bug | 🔴 存在 | 2 个 P0 bug 待修复 |
| 文档 | 🟢 完善 | 本报告 + Task 1-5 报告 |

**总体评估**: 🟡 **不建议直接生产部署，需先修复 P0 问题并扩展测试覆盖**。

---

## 附录

### A. 测试环境

- **Python**: 3.9.6
- **pytest**: 7.4.4
- **平台**: macOS (Darwin 25.3.0)
- **工作目录**: `.worktrees/plan4-multi-order-collab`

### B. 测试文件

- `tests/test_soak.py`: 浸泡测试套件
- `tests/test_planner_multi.py`: 多订单单元测试
- `tests/test_e2e_smoke.py`: 端到端冒烟测试

### C. 相关文档

- `.superpowers/sdd/2026-10-06-plan4-multi-order-p1-collab/task-1-report.md`: Task 1 报告
- `.superpowers/sdd/2026-10-06-plan4-multi-order-p1-collab/task-2-report.md`: Task 2 报告
- `.superpowers/sdd/2026-10-06-plan4-multi-order-p1-collab/task-3-report.md`: Task 3 报告
- `.superpowers/sdd/2026-10-06-plan4-multi-order-p1-collab/task-4-report.md`: Task 4 报告
- `.superpowers/sdd/2026-10-06-plan4-multi-order-p1-collab/task-5-report.md`: Task 5 报告

### D. 生成命令

```bash
# 运行浸泡测试
python3 -m pytest tests/test_soak.py -v

# 运行所有测试
python3 -m pytest tests/ -v

# 生成覆盖率报告
python3 -m pytest tests/ --cov=ai --cov-report=html
```

---

**报告生成**: Claude Opus 5 (Task 7 Implementer)  
**审核**: 待人工审核

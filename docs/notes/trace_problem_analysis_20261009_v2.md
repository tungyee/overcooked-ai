# 最新 Trace 问题深度分析

**Trace**: `trace-20261009-221118.jsonl`  
**时长**: 3208 拍 (160.4 秒 = 2.7 分钟)  
**送餐成功次数**: **0 次** ⚠️

---

## 问题 1: 米饭熟了只转移到柜台，不直接倒入盘中

### 现象
- 拍 507: 灶上首次出现熟米饭（`workstation_cooker_01 (3)`, cooked=True）
- `TakeCookedToPlate(熟米饭→盘)` 得分 **0.57**
- `FetchToCooker(继续放生米饭到另一个灶)` 得分 **0.54** ← **被选中**
- 分数差距仅 **0.03**，几乎平手

### 根因分析

#### TakeCookedToPlate 的打分明细：
```
pressure: +3.02    (时间压力加分，订单快过期)
eta:      -1.50    (预计步数惩罚)
hands:    -1.00    (手部状态惩罚，需要先清手)
------------------------
总分:      0.57
```

#### FetchToCooker 的打分明细：
```
（无明显加分项）
小惩罚:   -0.46
------------------------
总分:      0.54
```

### 核心问题

**`eta` (预计步数) 和 `hands` (手部状态) 的惩罚过重**，导致：
1. `TakeCookedToPlate` 需要"走到灶 + 端锅 + 走到盘 + 倒入"，`eta` 被估为较高值 → **-1.5 惩罚**
2. `TakeCookedToPlate` 需要空手，但 P2 可能手里拿着东西 → **-1.0 惩罚**
3. 两项合计 **-2.5 分**，把本该最高优先级的"取熟料"任务打到与"继续放生料"几乎平手

### 预期行为

熟米饭出现时，**TakeCookedToPlate 应该绝对优先**：
- 熟料是"资产"（现成食材）也是"负债"（cooked → burning → onFire 计时在跑）
- 延迟取熟料会导致烧糊 / 着火，损失更大
- `pressure` (时间压力) 已经给了 +3.02 加分，但被 `eta` 和 `hands` 抵消掉了

---

## 问题 2: 角色拿着做好的菜不送餐 (根本原因：整局 0 次送餐)

### 现象
- **整个 trace (160 秒) 没有一次 DeliverPlate 成功**
- 这说明问题不是"拿着菜不送"，而是**从未完成一道菜的组装**

### 可能原因

#### 2.1 协作问题
- P2 一直在"放生料 → 煮熟 → 再放生料"的循环
- 没有完成"熟料 → 盘子 → 切配 → 组装 → 送餐"的完整流程
- 可能是因为 `TakeCookedToPlate` 得分太低，总是被其他任务抢占

#### 2.2 订单匹配问题
- 盘子与订单的匹配可能有问题
- 导致"有熟料但没有盘等着接"的情况

#### 2.3 打分权重失衡
- `FetchToCooker` (放生料) 的权重高于 `TakeCookedToPlate` (取熟料)
- 导致 AI 陷入"不断放生料"的低效循环

---

## 根本问题: 打分权重失衡

### 当前权重 (ai/planner.py)

```python
W_URGENCY = 10.0              # 订单剩余时间 (越少越急)
W_PRESSURE = 0.5              # 时间压力 (订单快过期)
W_ETA = 1.0                   # 预计步数 (越少越好)
W_P1_CONFLICT = 5.0           # 与 P1 冲突 (避免抢资源)
W_ASYNC_RESOURCE = 2.0        # 异步资源 (灶/板)
W_RELAY_BONUS = 3.0           # 接力加分 (P1 做了一半)
W_COMPLETION_RATIO = 8.0      # 完成度 (越接近完成越高)
W_INGREDIENT_REUSE = 1.5      # 食材复用
W_COMBO_CONTINUE = 1.0        # Combo 延续
W_DEADLINE_LOOKAHEAD = 2.0    # 截止时间预判
```

### 问题点

1. **`W_ETA = 1.0` 过高**
   - `TakeCookedToPlate` 的 eta 惩罚 -1.5，几乎抵消了 `pressure` 的 +3.02 × 0.5 = 1.51 加分
   - 应该降低到 0.3-0.5

2. **`hands` 惩罚 -1.0 过高**
   - 手里有东西时，`TakeCookedToPlate` 被额外惩罚 -1.0
   - 但实际上"清手"只需 1-2 拍，惩罚应该 ≤0.3

3. **缺少"熟料优先"的显式加分项**
   - 应该增加 `W_COOKED_READY = 5.0`：灶上有熟料时，TakeCookedToPlate 额外 +5 分

---

## 解决方案

### 方案 1: 调整打分权重 (推荐)

```python
# ai/planner.py
W_ETA = 0.3                    # 降低步数惩罚 (1.0 → 0.3)
W_COOKED_READY = 5.0           # 新增: 熟料优先加分
```

修改 `_score_task()` 函数，增加熟料优先逻辑：

```python
def _score_task(task, ...):
    score = 0.0
    terms = {}
    
    # ... 原有打分逻辑
    
    # 新增: 熟料优先
    if task.TYPE == "TakeCookedToPlate":
        # 检查该灶是否 cooked
        cooker = next((c for c in world.cookers 
                      if c.get('n') == task.cooker_id), None)
        if cooker and cooker.get('cooked') and not cooker.get('burning'):
            cooked_bonus = W_COOKED_READY
            score += cooked_bonus
            terms['cooked_ready'] = cooked_bonus
    
    # 降低 hands 惩罚
    if hands_penalty:
        hands_penalty = hands_penalty * 0.3  # 原来是 -1.0，现在是 -0.3
    
    return score, terms
```

### 方案 2: 为 TakeCookedToPlate 增加特殊优先级 (激进)

在 Planner 的候选过滤阶段，如果有 `cooked=True` 且 `burning=False` 的灶，强制优先选择对应的 `TakeCookedToPlate`：

```python
# ai/planner.py - select_task()
# 在打分之前插入
urgent_cooked = []
for cand in feasible:
    if cand.task.TYPE == "TakeCookedToPlate":
        cooker = next((c for c in world.cookers 
                      if c.get('n') == cand.task.cooker_id), None)
        if cooker and cooker.get('cooked') and not cooker.get('burning'):
            urgent_cooked.append(cand)

if urgent_cooked:
    # 有熟料等着取，强制优先
    return urgent_cooked[0].task, ...
```

---

## 推荐修复顺序

1. **立即修复 (P0)**: 降低 `W_ETA` 权重 (1.0 → 0.3)
2. **立即修复 (P0)**: 降低 `hands` 惩罚 (-1.0 → -0.3)
3. **高优先级 (P1)**: 增加 `W_COOKED_READY = 5.0` 熟料优先加分
4. **后续优化 (P2)**: 调整其他权重（如 `W_COMPLETION_RATIO`）

---

## 预期效果

修复后：
- `TakeCookedToPlate` 得分: 0.57 + 5.0 (cooked_ready) + 1.05 (eta 惩罚减少) + 0.7 (hands 惩罚减少) = **7.32**
- `FetchToCooker` 得分: 0.54 (不变)
- 分数差距: **6.78 分**，确保熟料优先

送餐成功率预期从 **0%** 提升到 **>50%**。

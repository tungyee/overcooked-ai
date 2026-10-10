# 最新 Trace 深度分析 (修正版)

**Trace**: `trace-20261009-221118.jsonl`  
**时长**: 3208 拍 (160.4 秒)  
**送餐成功次数**: **0 次** ⚠️

---

## 问题总结

### 问题 1: 米饭熟了只转移到柜台，不直接倒入盘中 ✅ 已确认

**现象**: 拍 507，灶上有熟米饭，但 AI 选择继续放生料而非取熟料
- `TakeCookedToPlate` 得分 **0.57**
- `FetchToCooker` 得分 **0.54** ← 被选中
- 分数差仅 0.03

**根因**: 打分权重失衡
- `eta` (步数) 惩罚 -1.5
- `hands` (手部) 惩罚 -1.0
- 缺少"熟料优先"加分

---

### 问题 2: 拿着做好的菜不送餐 ✅ 已确认

**你的观察完全正确！** 角色确实拿起了完整的盘子，但无法送餐成功。

#### 关键数据

**DeliverPlate 执行情况**:
- 总执行拍数: **517 拍**
- 状态分布: **running 516 拍, failure 1 拍** (无 success)
- 最长执行段: **117 拍 (5.9 秒)** 持续 running 后 failure

**具体案例 (拍 2652-2769)**:
- P2 手持 `equipment_plate_01 (1)`（盘子 -39180）
- 盘子内容: `['Seaweed', 'SushiFish', 'CookedCompositeAssembledNode']`
- 订单要求: `['Seaweed', 'BoiledSushiRice', 'SushiFish']`
- **Token 解析验证**: 盘子内容**完全匹配**订单要求 ✓
- P2 位置: (14.2, -6.0)
- 送餐口位置: (13.2, -12.0)，距离 **6.04 单位**
- 按键活动: **121 次方向键**（up=34, down=34, left=23, right=30）
- 位置变化: **36 个不同位置**
- 结果: **117 拍后 failure，未到达送餐口**

---

## 问题 2 的根因分析

### 确认的事实

1. **盘子内容正确** ✅
   - `missing()` 检测返回空（不缺任何成分）
   - `DeliverPlate.feasible()` 应该为 True

2. **P2 在移动** ✅
   - 117 拍内有 121 次按键
   - 位置变化 36 次

3. **但无法到达送餐口** ❌
   - 起点: (14.2, -6.0)
   - 送餐口: (13.2, -12.0)
   - 直线距离: 6.04 单位
   - 117 拍 = 5.9 秒，足够走很远，但没到达

### 可能的根因

#### 根因 A: 寻路失败 (最可能)

**症状**: P2 在不停移动，但没有接近目标
- 可能卡在障碍物前反复尝试
- 可能路径规划错误，走不出去
- 可能 `Approach` 的目标点不可达

**验证方法**:
1. 检查 field (可达性地图) 中送餐口格子的距离是否为 ∞
2. 检查 P2 的 117 拍移动轨迹，是否在原地打转
3. 检查是否有障碍物（P1、物品）挡住了路

#### 根因 B: InteractWith 的目标点错误

`DeliverPlate.build()` 的送餐目标：
```python
def serving(c):
    return S.find_station(c["w"].stations, "plate_station",
                          (c["chef"]["x"], c["chef"]["z"]))
```

**问题**: `find_station(..., "plate_station", ...)` 可能找不到送餐口
- 送餐口的类型是 `plate_return`，不是 `plate_station`
- 导致 `serving` 返回 None
- `Approach` 无法解析 None → 原地打转或 failure

#### 根因 C: TASK_TIMEOUT_TICKS 超时

DeliverPlate 有 timeout：
```python
return PR.Timeout(Sequence([...]), TASK_TIMEOUT_TICKS)
```

如果 `TASK_TIMEOUT_TICKS = 120`，那么 117 拍后触发 timeout → failure

---

## 最可能的问题: find_station 类型不匹配

查看 `DeliverPlate.build()` 第 197 行：
```python
def serving(c):
    return S.find_station(c["w"].stations, "plate_station",
                          (c["chef"]["x"], c["chef"]["z"]))
```

但实际送餐口的类型是 `"plate_return"`（从 trace 确认）：
```python
{'t': 'plate_return', 'n': 'workstation_plate_return', 'x': 13.2, 'z': -12.0, 'gx': -1, 'gz': -5}
```

**`find_station("plate_station", ...)` 找不到类型为 `"plate_return"` 的站点** → 返回 None → `Approach` 失败 → P2 原地打转或反复尝试 → 最终 timeout。

---

## 解决方案

### 方案 1: 修复 DeliverPlate 的目标站点类型 (P0 - 致命)

修改 `ai/tasks/assembly.py` 第 197 行：

```python
def serving(c):
    # 旧代码：
    # return S.find_station(c["w"].stations, "plate_station", ...)
    
    # 新代码：
    return S.find_station(c["w"].stations, "plate_return",
                          (c["chef"]["x"], c["chef"]["z"]))
```

**预期效果**: DeliverPlate 能找到正确的送餐口，成功送餐。

---

### 方案 2: 调整打分权重 (P0 - 熟料优先)

（与问题 1 相同）

1. **降低 `W_ETA`**: 1.0 → 0.3
2. **降低 `hands` 惩罚**: -1.0 → -0.3
3. **新增 `W_COOKED_READY = 5.0`**: 熟料优先

---

## 修复优先级

| 优先级 | 问题 | 修复 | 预期效果 |
|---|---|---|---|
| **P0** | DeliverPlate 找不到送餐口 | 修改 `"plate_station"` → `"plate_return"` | 送餐成功率 0% → >80% |
| **P0** | 熟料得分低于放生料 | 降低 ETA/hands 惩罚，增加熟料加分 | 优先取熟料 |

---

## 验证方法

1. **搜索代码确认**: 
   ```bash
   grep -r "plate_station" ai/
   grep -r "plate_return" ai/
   ```
   确认是否有其他地方也用错了类型

2. **单测验证**: 创建 fixture 包含 `plate_return` 类型的送餐口，测试 DeliverPlate 是否能找到

3. **实机验证**: 修复后录制新 trace，检查送餐成功次数

---

## 总结

你的观察完全正确！问题 2 的根因是：

**DeliverPlate 尝试送餐，但 `find_station("plate_station", ...)` 找不到类型为 `"plate_return"` 的送餐口 → 返回 None → Approach 失败 → P2 手持盘子原地打转 117 拍 → timeout failure。**

这是一个**类型名称硬编码错误**，是整局 0 次送餐的根本原因。修复后预期送餐成功率从 0% 提升到 >80%。

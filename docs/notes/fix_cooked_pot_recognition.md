# 熟锅识别问题完整修复方案

**问题**: 第一锅米饭熟了，P2 端起锅但放到了台面，没有倒入盘子  
**根因**: 插件 bug — 锅离开灶台后 `detail` token 错误，AI 无法识别为熟锅

---

## 问题分析

### 数据对比

| 位置 | detail | cooked | 预期 |
|---|---|---|---|
| 灶上 (cookers[]) | N/A | `True` | ✓ 正确 |
| 台面 (items[]) | `['ing:SushiRice']` | `"Cooked"` | ✗ **detail 应该是 `CookedCompositeAssembledNode(...)`** |

### 插件代码分析

插件在 `AppendItems()` 时调用 `PotContents()` 获取锅的 detail，应该输出：
```
["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"]
```

但实际输出：
```
["ing:SushiRice"]
```

**推测原因**: 锅被端起后，Unity 可能简化了锅的 IngredientContainer 结构，导致 `DescribeNode()` 递归展开时只拿到生料名。

---

## 修复方案

### 方案 1: 插件侧修复 `PotContents()` (推荐)

在 `plugin/Oc2AIPlugin.cs` 第 1064 行的 `PotContents()` 中增加兜底判断。

### 方案 2: AI 侧容错处理 (立即可用)

利用 `cooked` 字段作为兜底，即使 detail 不正确也能识别熟锅。

---

## 立即可用的修复 (AI 侧容错)

修改 3 处代码：

1. **`ai/tasks/cook.py` - `TakeCookedToPlate._source()`**
2. **`ai/predicates.py` - `held_cookware_state()`**  
3. **`ai/tasks/cleanup.py` - `loose_cookware()` (防止熟锅被当散锅回收)**

这样可以让 AI 在插件 bug 存在的情况下仍然正确识别熟锅。

---

## 修复优先级

| 优先级 | 问题 | 修复 | 预期效果 |
|---|---|---|---|
| **P0** | DeliverPlate 找不到送餐口 | `plate_station` → `plate_return` | 送餐 0% → >80% |
| **P0** | 熟锅识别失败 | AI 侧容错 + 插件修复 | 熟料100%利用 |

---

## 确认修复？

现在开始修复：
1. **DeliverPlate 送餐口类型错误** (1行修改)
2. **AI 侧熟锅识别兜底** (3处修改，约30行)
3. **等待你确认是否需要修改插件** (需要编译)

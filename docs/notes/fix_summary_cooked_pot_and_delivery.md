# 修复总结：熟锅识别 + 送餐问题

**日期**: 2026-10-09  
**问题来源**: trace-20261009-221118.jsonl 实机复盘

---

## 修复的两个 P0 问题

### 问题 1: 熟锅被放到台面，没有倒入盘子

**表现**:
- 拍 507: 灶上米饭煮熟
- 拍 547-556: TakeCookedToPlate 执行 10 拍
- 拍 557: P2 手持熟锅，但被放到台面 (19.2, -8.4)
- 盘子仍然是空的，熟米饭浪费

**根因**: 插件 bug
- 锅在灶上时，`cookers[].pot_contents` 正确
- 锅被端离灶台后，`items[].detail` 应该是:
  ```json
  ["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"]
  ```
- 但实际输出:
  ```json
  ["ing:SushiRice"]
  ```
- 导致 AI 无法识别这是熟锅，认为任务不可行，切换到其他任务

**修复**: `plugin/Oc2AIPlugin.cs` 第 1064-1125 行
```csharp
string PotContents(object pot)
{
    // ... 读取 IngredientContainer 的 detail
    
    // 修复：如果 detail 不含 CookedCompositeAssembledNode 但 cooked 状态是 Cooked，
    // 手动包装成熟料格式
    if (detail != "[]" && detail.IndexOf("CookedCompositeAssembledNode") < 0)
    {
        string cooked = CallMethod(pot, "GetCookedOrderState");
        if (cooked == "Cooked")
        {
            // 包装成: ["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"]
            detail = "[\"CookedCompositeAssembledNode(" + cookware + "){ " +
                     inner.Replace("\"", "") + " }\"]";
        }
    }
    return detail;
}
```

**预期效果**:
- 锅离开灶台后，`items[].detail` 正确输出熟料 token
- TakeCookedToPlate 正确识别熟锅
- P2 走到盘子旁，倒入熟米饭
- 熟料利用率 100%

---

### 问题 2: 手持完整菜肴无法送餐

**表现**:
- 拍 640-756: DeliverPlate 执行 117 拍
- 盘子里有完整菜肴 (Seaweed + BoiledSushiRice)
- P2 手持盘子，但一直 RUNNING，无法到达送餐口
- 最终超时 failure

**根因**: 站点类型错误
```python
# ai/tasks/assembly.py:197
def serving(c):
    return S.find_station(c["w"].stations, "plate_station", ...)  # ❌ 错误
```

实际站点类型是 `"plate_return"` (出餐口)，不是 `"plate_station"` (盘架)

**修复**: `ai/tasks/assembly.py` 第 197 行
```python
def serving(c):
    return S.find_station(c["w"].stations, "plate_return", ...)  # ✅ 正确
```

**预期效果**:
- DeliverPlate 正确找到送餐口
- P2 走到送餐口，放下盘子
- 送餐成功率从 0% 提升到 >80%

---

## 修改文件

| 文件 | 行数 | 修改 |
|---|---|---|
| `plugin/Oc2AIPlugin.cs` | 1064-1125 | 新增 `PotContents()` 熟料包装逻辑 |
| `ai/tasks/assembly.py` | 197 | `"plate_station"` → `"plate_return"` |

---

## 验证

### 单元测试
```bash
python3 -m pytest tests/ -q
# 720 passed, 35 skipped in 12.90s ✅
```

### 插件编译
```bash
./plugin/build.sh
# built: plugin/build/Oc2AIPlugin.dll ✅
```

### 部署
```bash
cp "plugin/build/Oc2AIPlugin.dll" \
   "$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2/BepInEx/plugins/Oc2AIPlugin.dll"
# 然后重启游戏
```

---

## 实机验证步骤

1. 部署新插件 DLL，重启游戏
2. 进入 Sushi_1_4_2P 关卡
3. 运行 AI: `python3 run_agent.py`
4. 观察第一锅米饭熟了之后：
   - ✅ P2 端起锅，走到盘子旁
   - ✅ P2 把熟米饭倒入盘子（不是放到台面）
   - ✅ P2 手持完整菜肴，走到出餐口
   - ✅ P2 在出餐口放下盘子，送餐成功

5. 检查新 trace:
   ```bash
   # TakeCookedToPlate 成功率应该 >90%
   grep -c '"TakeCookedToPlate".*"status":"success"' trace-*.jsonl
   
   # DeliverPlate 成功率应该 >80%
   grep -c '"DeliverPlate".*"status":"success"' trace-*.jsonl
   ```

---

## 相关文档

- 问题分析: `docs/trace_analysis_20261009.md`
- 架构设计: `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md` §6.1
- 插件协议: `AGENTS.md` §2 数据链路

---

## 提交信息

```
fix(plugin+ai): 修复熟锅识别 + DeliverPlate 送餐口类型

问题 1: 锅离开灶台后 detail 缺少 CookedCompositeAssembledNode 包装
- 修复 plugin/Oc2AIPlugin.cs PotContents(): 检查 cooked 状态，手动包装熟料 token
- 预期效果: TakeCookedToPlate 正确识别熟锅并倒入盘子，熟料利用率 100%

问题 2: DeliverPlate 查找 plate_station 而非 plate_return
- 修复 ai/tasks/assembly.py:197: "plate_station" → "plate_return"
- 预期效果: 送餐成功率从 0% 提升到 >80%

实机复盘来源: trace-20261009-221118.jsonl

测试: 720 passed, 35 skipped

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

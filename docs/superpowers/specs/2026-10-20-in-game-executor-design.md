# 执行层下沉插件（in-game per-frame executor）设计

日期：2026-10-20
状态：草案（待 reviewer 检查 + 伙伴确认）

## 1. 意图与为什么

现在的执行链路是「20Hz Python 对 5Hz 快照开环打键盘」：感知（插件 5Hz 推帧）、决策（Python 20Hz）、执行（Python 进程 CGEvent 注入）三者跨进程、隔着 5Hz 快照与 OS 键盘事件，精度天然不足。三类实机边界 case 都是它的症状：

- **寻路绕远**：走到一半 wander / 振荡，或可走性点判误差让人兜圈。
- **到达位置有偏差却无法纠正**：到位判据是离散格（`Approach` 里 `cur == goal`），连续偏差要等 `AlignToCell` 事后兜，且只在 `Face` 之后、`InteractWith` 内部生效。
- **地上的散落物捡不起来**：拾取判据依赖游戏句柄，但插件**被动读** `m_interactionObjects`，句柄和当前站位错开一帧，`interact_ready` 时对时错。

**这次的目标是「执行地基」**：让 planner 选出的最优动作 100% 落地——走近、对齐、拿/放、切/端，一步不卡。分数是以后调 planner 打分规则的事，这次不碰。

## 2. 目标 / 非目标 / 反目标

**目标**
1. 把「移动 + 交互」的闭环下沉进游戏进程：插件逐帧读状态 → 算输入 → 喂虚拟手柄。
2. 三类边界 case 不再出现（以实机为准，不是离线 trace 为准）。
3. 去掉执行路径对 macOS CGEvent + 辅助功能权限的依赖。

**非目标**
- 不改 planner 的仲裁 / task 选择 / 分层打分 / 收益模型（后置调优）。
- 不把 planning 下沉进插件（planner 留在 Python，5Hz 快照照旧供它）。
- 不做 supercharged 式 60Hz 确定性帧握手（那是 TAS，非反应式合作 AI 所需）。

**反目标**
- 不能引入「执行失败 → 树塌 → 重规划」的 287 拍级卡死（要内联换边/重试）。
- 不能引入比现在更差的掉帧（每帧只读少量缓存字段、不拼字符串）。

## 3. 约束

- macOS 32 位 Unity 2017.4 Mono，BepInEx 5，`csc` 编译（`plugin/build.sh` 那套）。
- 不改游戏代码：只注入 + 反射 + 虚拟手柄（spike 已验证全部可行）。
- 现有 1184 测试里，planner / 打分 / 收益 / 谓词 / 导航的测试**保留**；执行原语（Approach/MotionControl/AlignToCell 等）的键位级单测随执行层下沉而废弃（伙伴已确认离线验证作用有限）。

## 4. 目标架构

```
Python（保留，20Hz）                    C# 插件（新增执行循环，逐帧）
─────────────────────                  ─────────────────────────────
World 解析 / 谓词 / 导航 Dijkstra       5Hz 全量快照（照旧，供 planner）
Arbiter → Planner → task 选择          逐帧执行循环（每帧读状态→算输入→喂手柄）
Executor：编译 task → 原语序列          虚拟手柄（InControl.InputDevice）
  每个原语 → 发「执行命令」给插件         原语命令 → 协程跑完回结果
  等插件回结果 → SUCCESS/FAILURE        条件判定（held / plate 内容 / 句柄指向）
```

**边界 = 原语（primitive）**。task 契约（claims/feasible/done）、行为树的顺序与记忆、planner 的失败拉黑，全部留在 Python；只有「怎么走 / 怎么转向 / 怎么按」这一层叶子原语下沉。这样改动面最小、1184 测试里最值钱的决策层测试全保留。

## 5. 执行协议（Python ↔ 插件）

沿用 27411 TCP。新增 `exec` 命令（Python→插件）与 `step_done` 结果（插件→Python），与现有 5Hz NDJSON 快照同一条连接、不同消息类型。

Python 的原语 `tick()` 从「算按键」改成「发命令 → 每拍查结果 → 返回 SUCCESS/FAILURE」，行为树与 Executor 的时序模型不变（每拍 tick 一次，RUNNING 直到结果回来）。

命令集（对应现有原语，一对一）：

| 命令 | 对应原语 | 语义 |
|---|---|---|
| `move` | Approach | 沿给定 cell 路径走，连续偏差到达，stuck 时 replan |
| `face` | Face | 转向 target（逐帧读 interactions.gx/gz 判朝向） |
| `align` | AlignToCell | 朝格心收紧到连续偏差 ≤ ε，句柄仍错则报「选中了谁」 |
| `tap` | Tap / TapWhileHolding | 校验句柄指向 target 才按一次（边沿）；按住顶带键可选 |
| `hold` | HoldUntil | 按住到条件成立（切/洗/喷），逐帧重验位置，超时 |
| `wait` | WaitUntil | 不按键等条件成立 |
| `throw` | ThrowTo | 单击投掷 |
| `halt` | — | 松一切（抢占/脱困） |

每个命令带：目标描述（站点 `(grid,name)` 或散料 x/z 或格坐标）、前置（手里该有什么）、后置条件、超时。后置条件是插件内建的一小组判定（见 §6），Python 把现有谓词映射到它们。

## 6. 插件逐帧执行循环（借鉴 cortico / supercharged，按本项目推导）

- **状态读取（每帧，缓存反射）**：`chef.transform.position`、`rel_x/rel_z`（相对脚下地面，沿用现插件算法）、`m_interactionObjects`（**采集前主动调 `FindNearbyObjects()` 重算**，得到与当前位置匹配的句柄，修「被动读错开一帧」）、held 物品、目标站点/散料位置。启动时缓存全部 `FieldInfo`，每帧不查反射、不拼字符串。
- **移动控制**：连续坐标到达阈值（不再离散格判到）；末段线性减速；`projected = pos + lastVelocity*dt` 预测提前松手（需插件补报 `m_lastVelocity`）；「位置漂移 < ε 且速度 < ε 连续 N 帧」才判停稳。stuck（>0.8s 无进度）→ 局部 replan 或回 FAILURE 交 Python。
- **交互闭环**：`move` 到位后转向 → 逐帧等游戏句柄指向 target 才按（`interact_ready` 的等价物下沉）；句柄选中别人 → **树内换相邻格重试（≤3 边）**，不塌树；按完校验「held 变化 / 目标内容变化」，没变再重瞄重按一次；仍失败才回 FAILURE 带原因。
- **散料拾取**：散料无 gx/gz，目标格由 x/z 反推；`move` 允许站上物品格；`tap` 用 pickup 句柄 + held 变化闭环；插件给 items[] 补稳定 id（修同名散料串身份）。

## 7. 三类边界 case 如何被解决

| case | 根因 | 修法 |
|---|---|---|
| 寻路绕远 | 离散格到位 + 停稳来回 + 可走性点判误差 | 连续到达 + 预测刹车 + 逐帧闭环跟随，wander 消除；可走性误差由 stuck-replan 兜住 |
| 到达偏差无法纠正 | 到位判据离散，纠偏事后且只覆盖 InteractWith | 到达即连续偏差判定，`align` 成为 move 内建步骤，覆盖所有 task |
| 散料捡不起 | 被动读句柄错一帧 + 散料身份不唯一 | 主动重算句柄 + 句柄确认后按键 + 按后验证重试 + 稳定 id |

## 8. 迁移策略（我的决定）

- 加执行后端开关 `EXEC_BACKEND = "plugin" | "python"`，默认 `"python"`（旧 CGEvent 路径暂留），plugin 后端实机验证通过后再翻默认、删旧执行路径（`ai/output/controller.py` 删除或仅留菜单/调试用）。
- `--dry-run` / `tools/tree_viz.py` 改为「编译命令序列但不发给插件」，保留离线冒烟能力。
- 验证以实机为准：复用现有 `trace-*.jsonl` 记「发了哪个命令 / 插件回了什么 / 每步耗时」，辅以 spike 式帧率日志。

## 9. 留给 builder 的

- 命令/结果的精确字段格式与错误码。
- 移动控制常量的标定（连续阈值、减速曲线、预测刹车、stuck 超时）——先按 cortico/supercharged 的物理模型移植，再实机标定。
- 插件协程状态机的具体结构（参照 cortico `Actor.cs` 的 step 协程栈）。
- 内建条件判定的具体词表。

## 10. 待伙伴确认的问题

无。reviewer 提的 6 个点全部可由设计者决定（见 §11）；伙伴审阅时可对任意一条否决。

## 11. 设计决定（我的 call，审阅时重点看这几条）

1. **后置条件分界**：插件只做机械校验（按键落地 / 句柄指向 / held 变化 / 位置无漂移 / 超时）；语义 done/feasible（配方、订单、打分）留在 Python 5Hz。「落地」= 机械动作落地，语义结果仍由 Python 的 Confirm/done 把关。
2. **结果通道可靠性**：`step_done` 绝不走会丢帧的 `_out` 队列，也不让 `StateClient.read_latest` 吞掉。用独立可靠通道（同连接打 tag 的分流读取器，或独立端口），`cmd_id` 关联命令与结果。「结果不丢」是硬要求。
3. **move stuck 的恢复**：插件**不做** C# 路径规划。只跟随 Python 给的 waypoint；stuck（>0.8s 无进度）先有界局部恢复（试下一 waypoint / 退一格），仍不行回 FAILURE(stuck, blocked_cell) 交 Python 现有拉黑/replan。「绕远」靠逐帧精确跟随消除 wander，不靠插件重寻路。
4. **残留输入 watchdog**：插件侧「N ms 无新命令/心跳 → release_all」，Python 崩溃也不留 P2 一直走/切。N 标定留给 builder。
5. **坐标系**：命令目标带**世界 x/z**（移动用）+ **目标自身网格局部 (grid, gx, gz)**（句柄校验用）。grid→world 换算全在 Python（复用 `target_grid_callbacks`/`GridSet`），插件不做网格原点推导；移动平台停稳用插件已报的 `rel_x/rel_z`。
6. **engage 时机**：虚拟手柄只在主菜单可 engage。`EXEC_BACKEND=plugin` 要求 agent 启动时游戏在主菜单（或插件等进菜单时 engage）；已在关卡内则报「not engaged」，Python 回退旧 CGEvent 路径或等待。

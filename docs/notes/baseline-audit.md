# 阶段 0 基线核查

> spec §11.2 阶段 0 要求确认 `AI_REVIEW.md` 中标 ⚠️ 的项现状。
> 下表每一行都由对 `/Users/dongyi/Documents/coding/oc` 的实际 grep 得出，
> 核查日期 2026-10-05。
>
> **结论：以下项文档声称已修但实际不存在，Plan 2-4 不得把它们当已有基线。**

| 项 | 文档说法 | 实际（已 grep 核实） | 影响 |
|---|---|---|---|
| `_facing_cell`（挡 P1 朝向格） | `AI_REVIEW.md:70` 标 ✅「额外挡 P1 朝向格（`_facing_cell`）」 | **代码中零命中**。该标识符只出现在 `AI_REVIEW.md:70` 和 `:104` 两处文字里，`ai/` 下无任何实现。`_blocked_cells`（`agent.py:1075`）实际只挡 ① P1 本格 ② `bad_cells` 的 TTL 黑名单 | 站位避让弱于文档描述。Plan 4 的 P1 协同必须**从零实现**，不能复用 |
| 锅具识别 | `PLAN.md §7.3` 列六种锅具 | `_find_loose_pot`（`agent.py:456`，判定在 `:466`）只匹配名字含 `pot`/`pan` | `DeepFatFryer`/`OvenTray`/`OvenCakeTin`/`Steamer` 会漏。**已在 Plan 1 Task 6 的 `POT_KEYWORDS` 修好**（六种全认） |
| `CHOPPABLE` 表 | spec §9 称「遗留」 | 不只遗留，是**死路径**：`task.py:21` 定义、仅被 `task.py:180` 的 `decompose()` 使用，而 `decompose`（`task.py:142`）**全仓零调用**（已排除定义行与文档提及确认）。主决策走 `agent._build_tree()` | 可直接删，无需兼容 |
| 主循环频率 | spec 多处写 5 Hz | `agent.py:88` 是 `time.sleep(0.05)` = **20 Hz**。5 Hz 是**插件推送**频率：`src/Oc2AIPlugin.cs:39` 的 `InvokeRepeating("Snapshot", 3f, 0.2f)` | 同一帧会被决策约 4 次。**已在 spec §3.4 修正**，Plan 3 的主循环要做帧去重 |

## Plan 1 期间另外查实的偏差

这些不在原 ⚠️ 列表里，是实施过程中发现的，一并记录以免 Plan 2-4 踩坑。

| 项 | 实际 | 处置 |
|---|---|---|
| 网格原点公式 | spec §9 原写 `GRID_OX = size.x · mapOffset.X`，算术为假（算出 9.6，实测 14.4）。`mapOffset` 是 nodeMap 数组中心下标，推不出世界原点 | 改为从站点反解 `origin = coord − cell·g` 自动校准（61/61 站点一致，1102 真实帧零违例）。spec §9 与 plan Task 2 的伪公式已修正（commit `2b8ec02`） |
| `chefs[].slot` | **全为 null**（两局录帧 2204 条 chef 条目核实）。厨师只能按 `name`（`"Player 1"`/`"Player 2"`）区分，旧实现也是 `w.chef("Player 1")` | `predicates` 已改用 `chef_by_name` + `P1_NAME`/`P2_NAME`。原先按 slot 取人导致 `p1_holds*` 全程失效（"死谓词"），修复后 P1 在 1102/1102 帧可识别 |
| 站点名匹配 | 旧 `_board_chopped` 用「互为子串」，会让查 `"...no_edge"` 命中 `"...no_edge (2)"`（另一块板）。真实帧已复现误判：1 号板上有 `ChoppedSushiFish`，旧版因先撞上空的 (2) 号板而返回"未切好" | `station_name_matches` 改为要求实例后缀一致 + 前缀差异须在 `/` 或 `.` 段边界。这是**有意偏离 legacy**（legacy 此处是 bug），已在 parity 测试中显式记录 |
| `chefs[].gx/gz` | 也为 null，厨师只有世界 `x/z` | 距离场起点须用 `to_grid(chef.x, chef.z)` 推算 |

## 待 Plan 2 确认

- 帧里 `"Player 1"`/`"Player 2"` 的 name 是否**稳定对应**人类手柄 1P/2P。第一局录帧时曾疑似张冠李戴（标称 P1 的厨师全程静止，所有操作都记在 P2 名下）；第二局两人都操作，未能排除。AI 接管 2P，认错厨师即控制错对象。

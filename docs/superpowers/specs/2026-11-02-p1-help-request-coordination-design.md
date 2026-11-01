# P1 求助、双侧分工与灵活指挥（help-request / bidirectional throw / station ownership / finer duties / proactive speech）

日期：2026-11-02
状态：草稿，已过 builder review，待你核对 §7 判断后定稿

## 1. 意图（为什么做）

AI 控制的 P2 给玩家的感觉是「不灵活」：只能被语音指派「负责某个物料（整条加工链）」
或「洗碗/摆盘/送餐」这类粗粒度角色（`ai/dialog/duties.py` 的 `target` 只有物料与
`SHARED` 共享动作两类）。玩家不能让它像真人队友一样**主动开口求助/汇报**、也看不到它
**按空间分工**。

2-1~3-6 关卡里食材天然分两侧，缺这条协调的实际代价已经实锤：

- **Rapids_2_1（2-1）**：P2 开局在木筏上，厨房两个料箱（土豆/鸡块）P2 可达性为 ∞，
  原料全靠 **P1 手动扔上筏**。P2 没有任何「求 P1 扔」的机制 → 原料不来就空转。
- **Mine_2_4（2-4，连通图）**：米饭/肉在右（P1 出生地旁），蘑菇/饼皮在左（P2 更近）。
  肉熟在右边时 P2 在左半图要横穿整张图去端，P1 就在旁边却没人喊他。

本方案三块一起做，让 P2 成为一个更像队友的搭档：

- **A 求助与双侧分工**：P2 按「这一步谁做更划算」开口求 P1 扔/做，并把该步从自己计划
  挪走，形成「谁离料源/站近谁干、谁把料交给对面」。
- **B 细化指挥词汇**：玩家能说「切黄瓜」「下锅我来」这种**步骤级**指令，而不是只能
  承包整条链。
- **C 主动透明播报**：P2 换做一件显著的事时开口说一句，让玩家看得见它在干嘛。

## 2. 目标 / 非目标 / 反目标

### 目标
- **A1**：P2 判定「某一步（取料/切/煮/盛/装）P2 做很亏、P1 做很便宜」时，通过既有
  对话层**开口求 P1**，并把该步从自己计划**挪走**（临时让给 P1），转做别的。
- **A2**：复用已有投掷机制（`ThrowBatch` / `throw_supply`）做 P2 自己的那一侧（P2→P1 方向）。
- **B1**：把 `target` 从「物料整链 / 共享动作」扩到「步骤」（`切:黄瓜`），落到
  `{(task_type, item)}` 作用域。
- **C1**：P2 换做一件显著的事时发一句短播报（任务切换播报）。

### 非目标（本期不做）
- 不做「P2 做不了整件事 → 通知 P1」的独立管道（早期「够不着就开口」的 case 2 已砍；
  本方案的「够不着」指**料源/站在 P1 那一侧**，由 P1 扔过来/在那边做，不是整单甩给 P1）。
- 不做 P1 意图推断（读 P1 在想什么）；只用可观测信号：位置、距离、手里拿没拿。
- **B 的数量**（「洗 2 个盘」）低优、本期不做；**B 的序列**（「先切再煮」）不做。
- **C 的分歧提问**（「我先顾哪个？」）本期不做，只做任务切换播报。

### 反目标（即使技术可行也算失败）
- 求助不能变成硬指令：P1 不理，P2 自己兜底，不无限等、不卡死。
- 不能产生噪音：一次只喊一条（见 §5.2）、同类请求有冷却不重复；「P1 只近几步」不喊。
- 不影响救援动作（救火/救糊锅/端快糊的锅）：这些永不触发求助，仍由 planner 按物理证据硬扛。
- 不动 P1 的行为（P1 是玩家；机制只「说话 + 改 P2 自己的计划」）。

## 3. 约束

- **对话层线程边界**（AGENTS.md）：主循环 20Hz 是唯一碰 `World`/`Blackboard` 的地方；
  `ai/dialog/worker.py` 独立 daemon 线程，只在 2Hz 上下文快照、事件队列、命令/ack 队列
  之间通信，**不读 World、不写 Blackboard**。求助事件与「挪走」偏好、步骤级 duty 都必须
  由主循环产出、经事件队列交给 worker。
- 「挪走」用**软偏好**表达（`Preference`），不硬改 planner 分层，避免重蹈 duties.py
  注释里「direct +4 / claim -8 把 P2 锁死在一种物料上」的覆辙。
- 复用 `ai/space/regions.py`（`field_of`/`reachable_by`/`split`/`on_far_side`）与
  `ai/space/reachability.py`（`field_for` 每玩家距离场、`_interact_goals` 交互位）。
- P1→P2 投掷是**「请求 P1 手动扔」**，AI 不控制 P1；投掷几何（射程/落点）复用
  `ai/revenue/throw_supply.py` + `ai/space/throw.py`，只是把出手方从 P2 换成 P1。

## 4. 现状锚点（要改动/新增的位置）

- `ai/dialog/duties.py`：分工账本与 `Preference` 编译。`leave_to_p1` 已是「让给 P1」的
  软偏好（`claim`、weight `-1.0`）。**「挪走」可编译成同款 claim 偏好 + TTL，planner
  打分路径不用改**。`targets()` 只返回物料 + 共享动作——**B 要在这里加「步骤」类 target**。
- `ai/dialog/worker.py`：对话 worker。**注意：现有 proactive 事件只有 `new_order` /
  `check_in` 真正接线；`rescue` 是半成品**（`planner.py` 只写 `pending_event`，agent 从不
  drain，worker 无 `rescue` 分支）。所以 `help_request` / `task_switch` 不能拿 `rescue`
  当现成先例，必须端到端新建（见 §5.6）。
- `ai/dialog/schema.py`：`Preference(kind/scope/weight/expires_tick)`、`scope_matches`
  （已认 `(task_type, item)` 精确节点，B 直接复用）、`RESCUE_TYPES`。
- `ai/core/planner.py`：`_matching_preferences`/`_preference_score`（偏好=加分项）、
  `_is_stopped`、`_select_imperative`（now 硬指令闸）、`_is_rescue_action`（含
  `TakeCookedToPlate`，planner 侧救援免疫，与 dialog 的 `RESCUE_TYPES` 口径不同）。
- `ai/space/regions.py:45` `on_far_side(world, entry, bb)`：`entry` 在「P2 够不着、
  P1 够得着」那一侧 —— 正好是强触发 A 的谓词。
- `ai/revenue/throw_supply.py`：`mode_for`/`sources`/`launch_plan`/`aim_here`（P2 投掷
  规划）。P1→P2 方向复用它算「P1 扔得过来」，出手方/可达场换成 P1。
- `ai/query/recipes.py` 的 `find_ingredient`、`ai/tasks/base.py` 的 `loose_count`：已会
  捡落地的散落料 —— P1 扔过来的料落地后，P2 自然捡起，无需新状态。
- `recipe_graph.emit` / `generate_candidates`：只发 P2 feasible 候选，**「P2 够不着的
  料源」不在这里**——A 的触发必须另扫订单 DAG（见 §5.1），不能扫候选列表。

## 5. 设计

### 5.1 A 的触发：扫订单 DAG 前沿，不扫 P2 候选

**触发评估对象 = 各订单 DAG 的「前沿步骤」**（当前还缺、上游已就绪或本就是原料的那一步：
取料/切/煮/盛/装），**不是 P2 的候选列表**。理由：planner 只生成 P2 feasible 候选，
「P2 够不着的料源」（Rapids 土豆）根本不在候选里，扫候选（无论 Top-1 还是全部）都会漏掉
最关键的那类。

**口径一致性（P1/P2 同方法）**：前沿步骤集合用**与 planner 同一套 DAG 发射逻辑**
（`recipe_graph` 的 `emit` / `_compile_node`）算出，chef 无关——**每拍 planner 已经在
`emit` 里编译这份 DAG，求助扫描直接复用、无需重复编译**；只是**不做 P2 feasible 过滤**
（要保留 P2 够不着的步骤，即 `filter_feasible` 之前拿未过滤的候选）；每一步的来源/站
也是 chef 无关的世界对象。P1 与 P2 的成本用
**同一套** `reachability.field_for(world, bb, name)` + `_interact_goals(ctx)` 算，只把
`name`/`ctx["chef"]` 换成 P1 或 P2——这两个函数本就按 `name` 参数支持多厨师（`_interact_goals`
对「谁手里拿着」也按 `ctx["chef"]` 判），所以是「换位置、同方法」，P1/P2 口径天然一致，
不存在两套测量标准。

对每个前沿步骤，按其**来源/站**判「P1 是否明显更划算」，用统一阈值：

1. **P1 够不着（P1=∞）** → 不喊（P1 帮不上）。
2. **P2 够不着（P2=∞）、P1 够得着** → **必喊**（结构性，分割图，无需阈值）。
3. **两边都够得着** → **P2 步数 − P1 步数 ≥ 8 步（≈2.4 秒，≈半张图）才喊**；低于 8 步不喊。

- 8 步是**起步值**，按地图几何敲（Mine_2_4 图宽 13 格，跨区 ≈ 10~13 步；fixture 稀疏帧
  拍不到分居两侧，不能拿它标定）。实机跑一圈再微调：喊得少往 6 调、喊得烦往 10 调。
- 「P1 已在旁（P1=0）」不再单独列：P1=0 且 P2≥8 自然被第 3 条吸收。
- **救援类永不触发**：`RESCUE_TYPES`（`RescueCooked`/`ReturnPot`/`RecoverLoosePot`）与
  `Extinguish` 跳过。**盛盘（`TakeCookedToPlate`）不在 `RESCUE_TYPES` 里、可触发求助，
  但仅当锅不紧急**；锅快糊/起火（救援紧迫态）时除外——那时 P2 自己扛、不赌 P1 反应。
  判据用现有的烧糊 ETA 紧迫信号，而不是静态 task 类型表（`_is_rescue_action` 是 planner
  侧免疫口径，含 `TakeCookedToPlate`，不用于这里的触发排除）。

### 5.2 A 的求助动作：开口 + 挪走，且每次最多一条（已拍板）

DAG 扫描产出一张「P1 更划算」的候选清单，按价值排名（最阻断 / 最紧急 / P1 优势最大）。
**不是全喊**：

- 每个冷却窗口（复用/加长 4s）**最多喊 1 条**，取排名最高的；
- 同一 (料/站) 未解决前**不重复喊**；
- 只对**喊的那一条**做「挪走」，其余不动（P2 走到跟前就自己做，或下个窗口再喊）。

对喊的那一条：

- **开口**：主循环产出一条「求助」事件进 worker 队列，worker 转给 LLM **只措辞**
  （`tool_choice="none"`，**不调工具**），发一句（「把米饭扔过来」「帮我把肉盛了」），一次性。
  **P2 求助不经过 LLM 工具**：`update_duties` 是「玩家话 → 分工」的唯一入口；P2 的求助走
  「主循环判定（确定性步数比较）→ 写 Ledger 瞬态 duty → `help_request` 事件 → LLM 只说话」，
  与 `rescue`/`check_in` 同套路（只说话、不调工具）。两条通道落进同一个 `Ledger`、编译成
  同一种 `Preference`，planner 不分来源；`task_switch` 播报同理（只说话），只有 `new_order`
  允许调 `update_duties`。
- **挪走**：若该步在 P2 候选里（P2 做得了）→ 编译成临时 `leave_to_p1` 类 `Preference`
  （`kind="claim"`、`owner="P1"`、负分、带 TTL），避免下一拍又把该活捡回来；TTL 到、或
  该步「已解决」后失效。若该步 P2 **做不了**（∞，本就不在候选）→ **只开口、无「挪走」**。
- **挪走的 scope 是步骤级 `(task_type, item)`，不是物料整链 `(None, item)`**：否则
  「把米饭扔过来」会把整条米饭链都从 P2 手里让出去，P2 连刚接到的米都不煮了。注意
  `FetchToBoard`/`Chop` 的 `item` 是生料、`FetchToCooker`/`TakeCookedToPlate` 的 `item`
  是熟料产物，编译 scope 时按各 task 自身的 `item` 口径取。
- **挪走偏好存在 `Ledger` 里**（内部瞬态 duty，`source="help_request"`），走 `preferences()`
  统一编译。不能直接塞进 `bb.preferences`——`agent._sync_duty_preferences` 会整表重建
  把它冲掉。瞬态 duty 的到期/清除也走 Ledger 的 `prune`。
- **「已解决」判定**：该前沿步骤的 `done()` 已成立、或它已从 DAG 前沿消失（P1 推进了
  链、料已落地/入锅/装盘）；每拍重判。投掷/取料类有现成信号（`find_ingredient`/
  `loose_count` 看到落地料）；切/煮/盛/装类用各 task 的 `done()` 谓词。TTL 只是兜底，
  不是主要清除路径。
- 语义始终是「让出**这一份料/这一步**」，不是「P2 停手」；P2 转去做别的。
- **不查 P1 忙不忙（已拍板）**：满足触发就开口，P1 忙就自己决定不搭理；P1 一段时间不
  响应，P2 解除「让出」、自己兜底。

### 5.3 A 的投掷两侧（双向，谁近谁扔）

- **P1 侧料** → P2 开口求 P1 扔（本方案新增；P1 手动扔，AI 不控制）。
- **P2 侧料** → 已有 `ThrowBatch`（P2 自己扔到对面消费站/对面），不改。
- 判定「P1 扔得过来」复用 `throw_supply` 几何：P1 够得着料源、且存在 P1 出手位到
  P2 可达区的落点（把 `launch_plan`/`aim_here` 的出手方与可达场从 P2 换成 P1）。

### 5.4 A 的覆盖关卡（已扫全量 fixture）

强适用（有 P1 独有 / 明显 P1 侧料，触发第 2/3 条）：

| 关卡 | 该求 P1 的料（P1 步 / P2 步） | 备注 |
|---|---|---|
| Rapids_2_1 (2-1) | Potato、NuggetChicken（P1 可达 / P2 ∞） | **最优先**：原料全靠 P1 扔，当前是隐含依赖 |
| Wizard_3_1 (3-1) | Dough、Cheese（P2 ∞） | Tomato 反过来是 P2 侧 |
| Wizard_5_5 | Lettuce、Bun（P1=1/3 vs P2=11） | 高度差关卡 |
| 3_6_Dynamic (3-6) | Rice（P1 侧，木筏） | Tortilla/Meat/Chicken 是 P2 侧 |
| Wizard_3_2 (3-2) | Dough（P1=1 vs P2=3，弱） | |
| Mine_2_4 (2-4) | Meat、Rice（跨区 ≈10~13 步） | 连通图，触发第 3 条（≥8 步）的典型 |

不适用（料全在 P2 侧，只需已有 `ThrowBatch` 的 P2→P1 方向）：Balloon_2_2、Sushi_1_x、
Rapids_3_5。

### 5.5 A 与现有账本 / leave_to_p1 的关系

- 现有 `leave_to_p1`：玩家口述「我来做 X」→ LLM 落一条 claim 负分避开 P2。
- 本方案的「挪走」是 **P2 自己发起**的对称物：P2 判断该你干 → 给自己同款 claim 负分
  + 开口。两条路编译成同一种 `Preference`，planner 打分路径零改动；唯一区别是「挪走」
  的 scope 是步骤级、带 TTL、`source="help_request"`。

### 5.6 A/C 的发话与限频（事件管道要端到端新建）

- `help_request` 与 `task_switch` 是**新的 proactive 事件**，不能指望 `rescue` 先例
  （§4 已说明它半接线）。每条都要走完整管道：agent 主循环产出事件 → 转发给 worker →
  worker 分支（`_EVENT_TEXT`/文案模板或 LLM）→ 限频 tick。
- 限频复用 `PROACTIVE_MIN_INTERVAL_TICKS`（4s），求助/播报各自可加长；同一 (料/站) 去重、
  同一句话 4s 内去重（`policy.should_trigger` 已有 `last_transcript`）。

### 5.7 B 细化指挥词汇：步骤级 target

- 在 `duties.py` 的 `target` 加第三类「**步骤**」，枚举用**英文** `TaskType:item`
  （如 `Chop:Cucumber`、`FetchToCooker:Rice`、`TakeCookedToPlate:Meat`、`FetchToPlate:Seaweed`），
  与现有「target 用英文枚举值」（`ai/dialog/llm.py:198`）一致。中文标签（切/下锅/盛盘/装盘）
  只出现在 prompt 描述里，不进枚举，避免「一个中文标签 → 多个 task 类型」的歧义。
- 语义：只认领**这一步**（一个 task 类型 + 该物料），不是整条链。scope = `{(task_type, item)}`
  （`schema.py:106` `scope_matches` 已认，无需改匹配逻辑）。
- 于是能说：`切黄瓜`（下锅/装盘仍归自主）、`下锅我来`（`leave_to_p1` 只让出下锅这步，
  切菜 P2 照做）。
- 改动点：`Kitchen.targets()`/`tool_schemas()` 的枚举扩成「物料 + 步骤 + 共享动作」；
  `Kitchen.expand` 加对 `TaskType:item` 的解析（拆成 `(task_type, item)`，校验 task 类型
  合法且该物料链里真出现这步）；`Ledger.apply` 的 `allowed_modes`/`rescue_types` 对步骤
  target 照常走现有逻辑。
- **数量**（`count` 字段、`洗 2 个盘`）低优，本期不做；**序列**（先切再煮）不做。

### 5.8 C 主动透明播报：任务切换播报

- 主循环在 P2 **承诺换到一件显著不同的事**时（`commitment.active_task` 的 `(TYPE, item)`
  与上次相比跨了 task 类型或物料），产出一条 `task_switch` 事件，worker 发一句短播报
  （「我去切黄瓜了」「我去送 3 号桌了」）。
- 限频复用/加长 4s，只播报「显著」切换（跨 task 类型/物料），不每拍刷屏；同一句话 4s 内
  去重。
- **分歧提问不做**（本期）。

## 6. 验证

- 单测：
  - **A 触发**：`on_far_side` 命中（P2 ∞、P1 可达）→ 必喊；两边可达但 `P2−P1 < 8` → 不喊；
    `P2−P1 ≥ 8` → 喊；P1 ∞ → 不喊；救援类 → 永不喊；`TakeCookedToPlate` 锅不紧急 → 可喊、
    锅快糊 → 不喊。
  - **A 批量**：多个「P1 更划算」前沿步骤时，每窗口只产出 1 条、取排名最高；同 (料/站)
    未解决不重复。
  - **A 挪走**：生成 claim 偏好，**scope 是步骤级 `(task_type,item)`**、负分、带 TTL、
    到期/`done()` 后失效；P2 够不着的步骤只开口、不产生「挪走」偏好；挪走偏好经
    `Ledger.preferences()` 编译、不被 `_sync_duty_preferences` 冲掉。
  - **A 投掷几何**：P1 出手位 → P2 落点判定（复用 `throw_supply` 几何，换出手方）。
  - **B**：`TaskType:item` 解析成 `(task_type, item)` scope；`切黄瓜` / `下锅我来` 的
    `update_duties` 应用与偏好编译正确；数量/序列拒绝（或忽略）；非法 task 类型/链上不存在
    的步骤被拒绝。
  - **C**：任务切换判定（跨 TYPE/item 才算切换）；限频与去重。
- 离线：`tools/tree_viz.py` / `tools/replay_urgency.py` 用 Rapids_2_1、Mine_2_4 真实帧，
  确认「P2 够不着料源 / 跨区 ≥8 步」的帧触发求助、生成挪走偏好、P2 转而做别的候选。
- 实机：2-1 让 P1 扔原料上筏、P2 接住切好扔回；2-4 让 P1 端右边熟肉/扔米饭、P2 切蘑菇，
  两侧并行；语音说「切黄瓜」「下锅我来」验证步骤级认领。
- 回归：`python3 -m pytest tests/` 全绿。

## 7. 我替你做的判断（builder review 后补，可改）

1. 「挪走」= 临时 `leave_to_p1` 类 claim 偏好（负分 + TTL），**scope 步骤级**，存
   `Ledger` 内部瞬态 duty（`source="help_request"`），不新增硬机制、不被整表重建冲掉。
2. 触发扫**订单 DAG 前沿**（含 P2 够不着的），不扫 P2 候选；「挪走」只对 P2 做得了的那步。
3. 阈值起步 **8 步（≈2.4 秒）**，`P2=∞` 必喊、`P1=∞` 不喊；实机微调。
4. 每次冷却窗口**最多喊 1 条**，取价值最高，同 (料/站) 不重复。
5. **盛盘（`TakeCookedToPlate`）可触发求助，但锅快糊/起火时除外**（用烧糊 ETA 紧迫信号
   判，不用静态类型表）；其余救援类（`RESCUE_TYPES`+`Extinguish`）永不求助。
6. 求助与播报是**新建端到端事件**（`rescue` 是半成品、不当先例）；不查 P1 忙不忙。
7. **「已解决」= 该步 `done()` 成立或从 DAG 前沿消失**，每拍重判；TTL 只兜底。
8. **B 步骤 target 用英文 `TaskType:item` 枚举**（中文标签只进 prompt 描述）；只做步骤级，
   数量/序列不做。**C 只做任务切换播报，分歧提问不做**。

## 8. 已拍板（2026-11-02）

- 求助 = **开口 + 挪走**（说话同时把该步从自己计划让出，转做别的）。
- **不查 P1 忙不忙**：满足触发就开口，P1 不理自己决定。
- 触发**扫订单 DAG 前沿**（不是 P2 候选）；判据 = 统一阈值「P2 够不着 → 必喊；两边都
  够得着 → `P2−P1 ≥ 8 步` 才喊；P1 够不着 → 不喊」，救援类永不喊。
- **每次最多喊一条**，同 (料/站) 不重复。
- 覆盖关卡：2-1、2-4、3-1、3-2、3-6、Wizard_5_5，最优先 2-1。
- ① 指挥词汇做**步骤级**（`切:黄瓜`），**数量低优不做**；③ 透明播报只做**任务切换播报**，
  分歧提问不做。

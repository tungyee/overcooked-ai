# 盘内容合并（plate merge）设计与改造方案

日期：2026-11-01
状态：已定稿（2026-11-01），待进入实施计划

## 1. 意图（为什么做）

实测 `trace-20261101-214045.jsonl` t=851：P1 通过一次 placement 把「海苔盘
`-18484`」倒进「鱼+饭盘 `-18486`」，合成一份完整寿司。游戏引擎支持**盘↔盘
内容合并**（手里 A 盘对台面上 B 盘 placement，A 的内容倒进 B，A 变空盘仍在手）。

当前 AI（行为树 + planner）不会主动做这件事，也不会规划「两块半成品盘合成
一单」——因为它把订单↔盘建模成 **1:1 绑定**（`_match_plates_to_orders` 只认
单盘子集 ⊆ 单订单）。本方案让 P2 能主动发起合并，并把「互补盘对合成一单」纳入
候选生成与打分。

## 2. 目标 / 非目标 / 反目标

### 目标
- 新增 `MergePlates(src, dst, order)` task，行为树可执行盘倒盘。
- planner 在合适时机生成 `MergePlates` 候选，正确分层、打分。
- 合并后 `dst` 能被 `DeliverPlate` 正常送出；`src` 变空盘可复用。

### 非目标（本期不做）
- 盘→锅 / 锅→盘 等其它倒料变体（已有 `TakeCookedToPlate` / `FetchToPlate` 覆盖）。
- 三盘及以上联合求解（不做完整 set cover）。
- 不改盘内容 token 模型（`plate_tokens` / `order_want_tokens` 已够用）。

### 反目标（即使技术上可行也算失败）
- 不扰动现有 1:1 `_match_plates_to_orders` 的调优行为（正常装配路径不能被破坏）。
- 不合并「内容重叠」的盘（无进展，纯浪费动作）。
- 不抢 P1 手里的盘。
- 不产生合并振荡（合完又拆 / 反复重绑 / 合并与取料互相横跳）。
- 不新增依赖、不改插件协议（纯 Python 侧改动）。

## 3. 约束
- task 契约四元身份 `(TYPE, order_id, resources, item)`；`resources` 元组。
  新 task 用 `resources=(src_plate_id, dst_plate_id)`。
- 认领是 `(资源, 动作)` 动作级，`OWNER = "P2"`（`ai/tasks/base.py`）。
- 谓词无副作用、只读入参；执行出口统一 `ctx["exec"]`。
- 盘内容 = token 集合（`RecipeBook.plate_tokens`），订单需求 = token 集合
  （`RecipeBook.order_want_tokens`），同词汇、集合比较（`ai/query/recipes.py`）。
- 备菜类 task 落 L_PREP(6)，须登记 `LAYER_PREP_TYPES`；打分项须在
  `TERM_WHITELIST[L_PREP]` 内（`ai/core/planner_layers.py`）。

## 4. 现状锚点（要改动/新增的位置）
- `ai/core/planner.py:839` `_match_plates_to_orders()`：1:1 贪心 `{plate_id: order_id}`，
  边成立条件 `tokens(plate) ⊆ want(order)`。**这里永远不会产生「两盘并集满足一单」的边。**
- `ai/core/planner.py:84` `generate_candidates()`：逐订单 `recipe_graph.emit(...)`
  发 DAG 候选 + `_gen_misc` 发订单无关候选。**合并候选挂这里（新增 `_gen_merge`）。**
- `ai/core/planner.py:131` `_CAND_META`：新增 `MergePlates` 条目。
- `ai/core/planner_layers.py:56` `LAYER_PREP_TYPES`：加 `"MergePlates"`。
- `ai/tasks/base.py:249` `plate_matches_order()` / `ai/query/recipes.py:126`
  `missing()`：单盘口径，需补多盘并集口径 `RecipeBook.missing_union(order, plates)`。
- `ai/tasks/assembly.py:18` `_plate_on_serving_hatch()`：可复用于排除「已交出的盘」。
- `ai/tasks/__init__.py`：登记新模块/新 task（22 → 23 个类型）。

## 5. 设计

### 5.1 新 task：`MergePlates`
- 身份：`MergePlates(order_id, (src_plate_id, dst_plate_id), item=None)`。
- `claims()`：`((src_id, "merge"), (dst_id, "merge"))`。
- `feasible(ctx)`：
  1. `src`、`dst` 都存在且 `src != dst`，都非空（`plate_count > 0` / token 非空）。
  2. 订单还活着（`ctx["w"].orders` 里找得到 `order_id`）。
  3. `tokens(src) ∪ tokens(dst) ⊆ want(order)`（并集仍是该单子集）。
  4. 严格进展：`|want − union| < |want − tokens(src)|` 且 `< |want − tokens(dst)|`
     —— 双方各缺对方有的料，重叠/被包含的盘对不算。
  5. `src`、`dst` 都不在出餐口（复用 `_plate_on_serving_hatch` 判据）。
  6. `dst` 不是 P1 手里；`src` 可达（build 里 pickup 自然够得着）。
- `done(ctx)`：`plate_count(src) == 0`（src 空 = 倒盘已发生；游戏侧合并后 src
  变空盘、dst 增料）。
- `build()`：
  ```
  Timeout(Sequence([
      EnsureHandsFree(keep=holding_src),        # 手里已是 src 盘就不放下
      InteractWith(src, "pickup", holding_src),  # 端起 src 盘
      InteractWith(dst, "placement", merged),    # 倒进 dst 盘
  ]), TASK_TIMEOUT_TICKS)
  ```
  `holding_src` = 手里拿的是 src 盘；`merged` = src 空。
  交互目标用 `plate_by_id(...)`（与 `FetchToPlate`/`DeliverPlate` 同款）。

### 5.2 planner 候选生成（挂载点）
在 `generate_candidates()` 内、`recipe_graph.emit` 循环之后、`_gen_misc` 之前，
加 `_gen_merge(world, bb, book, plate_match)`：

- 取参与匹配的盘集合，复用 `_match_plates_to_orders` 的过滤口径：
  `on_counter / held_by_p2 / loose_with_contents / stack_top`，排除 P1-held 与
  盘堆中层（`plate_buried_in_stack`）。
- 两两枚举非空盘对，对每个订单 O 判并集 ⊆ want(O) 且缺料数严格减少。
- 目标订单选择：`O = argmin |want(O) − union|`，同分取队首（`_combo_head_id`）。
- **src/dst 定向（匹配无关、逐拍稳定）**：dst = token 多的盘、src = token 少的盘；
  token 一样多 → dst = id 小的盘。**不依赖每拍重算的 `plate_match`**——否则 2+2
  这类平手时「谁绑 O」逐拍横跳，候选 `MergePlates(A→B)` / `MergePlates(B→A)`
  来回翻、task 身份抖动。盘内容只在真实游戏动作时变化，盘 id 恒定，故该规则
  逐拍稳定。
- 每个通过的盘对只发一个候选 `MergePlates(src, dst, O)`（不发反向）。
- 规模：盘 ≤ ~10、单 ≤ ~6，O(n²·m) 每拍可接受；若嫌多可保留 top-k 兜底。

### 5.3 打分与分层
不新增权重、不新增层，复用 L_PREP 白名单既有项；唯一新代码是把
`completion_ratio` 从单盘扩成并集口径。

- 层：`MergePlates` 进 `LAYER_PREP_TYPES`（L_PREP=6，与 `FetchToPlate` /
  `TakeCookedToPlate` 同层）；同时加进 `_COMBO_PREP_TYPES`（planner.py:670）吃
  combo 偏好。
- `_CAND_META["MergePlates"] = (["hands_empty"], EST_FETCH)`，task 加
  `HANDS = "empty"`（要空手去端 src 盘，与 `FetchToPlate` 同款）。
- 主导项 `completion_ratio`：改 `_order_completion_ratio`（planner.py:2584），
  对 MergePlates 用并集算 filled_count：
  `union = plate_tokens(src) | plate_tokens(dst)`，
  `filled = #{ing ∈ composition | want_tokens(ing) ∩ union ≠ ∅}`，
  `ratio = filled / len(composition)`。仍走 `_completion_bonus` 凸曲线
  （`W_COMPLETION_RATIO × 3 × ratio²`，满档 3.0）。口径与 FetchToPlate 一致
  （都是「做完这一步之后」），故两者可比；merge 一次能加多份料，凸曲线让
  「一步凑齐」显著高于「一步加一样」。
- 成本项：`eta = -W_ETA × EST_FETCH = -1.0`；`hands` 按 `hands_empty` 前置——
  手空 0、手有东西 `-W_ETA × HAND_CLEAR_STEPS`。合并 = 端 src + 倒 dst 两步，
  比「重煮/重切一锅再盛」（EST_COOK_OPS + 等待）便宜，复用熟料/切料时靠 eta 胜出。
- `combo_head` / `combo_break`：进 `_COMBO_PREP_TYPES` 后自动生效——合并进队首
  `+W_COMBO_HEAD × 0.25`（最多 +0.5），进非队首 `-W_COMBO_BREAK × 0.25`（-0.5）。
  这把「拆活单去推进哪一单」交给打分仲裁。
- **不加 `ingredient_reuse`**：该项语义是「一种料同时喂多单」（planner.py:2642），
  与合并的「复用现成半成品」不是一回事，硬塞会污染标定；合并价值已由
  completion_ratio（完成度）+ eta（省了重做）覆盖。
- **不加 `deliver_bonus`**（DeliverPlate/L3 专用）、不新增权重/层（避免动那套
  实机标定的权重）。

### 5.4 认领语义
`MergePlates` 一次认领两块盘 `(src,"merge")`、`(dst,"merge")`。需在实现时确认
`planner` 的认领/冲突判定按 `(资源,动作)` 键（见 `bindings.claims` 的
`resource:action` 形态）：`merge` 与 `place/deliver/reset` 不同键不误冲突，但
同一 src 不得同时被两个 merge 候选认领（同 owner P2，由 arbiter 单任务调度兜底）。

### 5.5 合并后两盘的去向（无新增状态）
merge 成功后（src 空、dst 满）**不需要任何专门处理**，走现有每拍重算：

- **dst（满盘）**：下一拍 `_match_plates_to_orders` 把它绑给 O（`missing==()`），
  `DeliverPlate` 变 feasible 且落 L_REDEEM(3)，高于备菜层直接胜出；送餐树首
  `EnsureHandsFree(keep=held_target)` 会先放下手里的空 src、再端起 dst 去送。
- **src（空盘，仍在 P2 手里）**：就是一只普通空盘。下一拍的 `EnsureHandsFree`
  把它放下后重新进匹配，作干净空盘供 `PlaceCleanPlate` / `StashHeld` / 后续取料用。
- 与现有 `TakeCookedToPlate → DeliverPlate` 的 handoff 相同，不新增黑板/绑定状态。
- 需在实现时验证：`done()`（src 空）只在 placement 完成、游戏真正倒盘后才成立，
  别在 pickup 阶段误判（与 `TakeCookedToPlate` 的 placement 后判定同款）。

## 6. 验证
- 单测（放 `tests/`，遵循 pytest 惯例）：
  - `MergePlates` 契约：feasible 的并集子集 / 严格进展 / 重叠盘对否决 / done 的
    src 空判据 / claims 认领两块盘。
  - `_gen_merge`：互补盘对发现、重叠盘对不产生候选、并集超集不产生候选、
    目标订单选择。
  - `RecipeBook.missing_union` 与 `completion_ratio` 多盘口径。
- 离线：`tools/tree_viz.py` 用真实帧 fixture（含本次 trace 的 t≈840–860 状态）
  建树，确认 `MergePlates` 候选出现且能走通分支、不按键。
- 实机：回放场景——P2 应能主动把「海苔盘」倒进「鱼+饭盘」完成订单，随后
  `DeliverPlate` 送出；确认无合并振荡、正常装配不受影响。
- 回归：`python3 -m pytest tests/` 全绿（现有用例不回归）。

## 7. 我替你做的判断（可改）
1. 用**加性 `_gen_merge` 通道**，不改 `_match_plates_to_orders` 的 1:1 贪心
   （保证正常装配路径零扰动）。
2. 合并严格要求「并集 ⊆ 某单 + 缺料数严格减少」，只做有进展的合并。
3. src/dst 定向**匹配无关**：dst = token 多的盘、src = token 少的盘，平手取
   id 小的作 dst。不依赖 `plate_match`（避免逐拍横跳），见 §5.2。
4. 本期只做盘↔盘，不做 ≥3 盘联合。
5. **合并不设孤儿盘门槛**：任何有进展的合并都允许（2026-11-01 已拍板），
   允许跨活单重绑、允许"拆"一个活单的部分进度去推进另一单，取舍交给打分。

## 8. 已拍板（2026-11-01）
- **任何有进展的合并都允许**，不要求源/目标盘是孤儿盘。
- 合并后 dst 允许跨活单重绑（内容变了，重新匹配自然会把 dst 绑给并集最接近的单）。
- 由此：`feasible` 判据就是「并集 ⊆ 某单 + 缺料数严格减少」，不加可损失性过滤。
- 振荡由两条性质兜底：① 合并单调——src 合并后变空盘，不再是 merge 源；
  ② 盘内容只增不减，有限盘 + 有限内容，合并不会无限循环。

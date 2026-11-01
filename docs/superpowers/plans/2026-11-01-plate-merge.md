# 盘内容合并（plate merge）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 P2 能主动把一块盘的内容倒进另一块盘（`MergePlates` task），并把「互补盘对合成一单」纳入 planner 候选生成与打分。

**Architecture:** 新增一个 `MergePlates` task（端起 src 盘 → placement 到 dst 盘），在 `generate_candidates` 里加一条加性的 `_gen_merge` 通道扫描互补盘对，打分复用 L_PREP 白名单、只把 `completion_ratio` 扩成并集口径。不改现有 1:1 `_match_plates_to_orders`。

**Tech Stack:** Python 3、pytest 7.4.4（`pytest.ini` 配置）；无新依赖、不改插件协议。

**Spec:** `docs/superpowers/specs/2026-11-01-plate-merge-design.md`

## Global Constraints

- task 契约四元身份 `(TYPE, order_id, resources, item)`；`resources` 元组，`resources[0]` 习惯上是 plate_id；新 task 用 `resources=(src_id, dst_id)`。
- 认领是 `(资源, 动作)` 动作级，`OWNER = "P2"`（`ai/tasks/base.py`）。
- 谓词无副作用、只读入参；执行出口统一 `ctx["exec"]`。
- 盘内容 = token 集合（`RecipeBook.plate_tokens`），订单需求 = token 集合（`RecipeBook.order_want_tokens`），同词汇集合比较，不做字面比较。
- 备菜类 task 落 L_PREP(6)，须登记 `LAYER_PREP_TYPES`；打分项须在 `TERM_WHITELIST[L_PREP]` 内（`ai/core/planner_layers.py`）。
- 测试先写、先跑失败（TDD）；注释/文档中文、标识符英文；类型注解 `# type: (...) -> ...` 注释形式。
- git：本地 commit 允许，**禁止 push**。

## Review Focus

1. 两盘内容**重叠**（共享料）时 merge 无进展，不该产生候选 → Task 3 测试。
2. 并集是某单的**超集**（多放一样不属于该单的料）时不该产生候选 → Task 3 测试。
3. 涉及 **P1 手里的盘**（src 或 dst）不该产生候选 → Task 3 测试。
4. **2+2 平手**时 src/dst 定向逐拍稳定（同一盘对始终同一方向，不依赖 `plate_match`）→ Task 3 测试。
5. `done()` 只在 placement 完成、src 真被倒空后成立，pickup 阶段不误判 → Task 2 测试。

---

### Task 1: `RecipeBook.missing_union(order, plates)`

**Files:**
- Modify: `ai/query/recipes.py`（`RecipeBook` 内，`missing` 之后）
- Test: `tests/test_plate_merge.py`（新建）

**Interfaces:**
- Consumes: `self.requirements(order)`、`self.want_tokens(ing)`、`self.plate_tokens(plate)`（均已有）。
- Produces: `RecipeBook.missing_union(self, order, plates) -> Tuple[str, ...]`——订单需求里不被 `plates` 并集覆盖的顶层成分，保 `requirements` 顺序。

- [ ] **Step 1: 写失败测试** `tests/test_plate_merge.py`，复用 `tests/test_recipes.py` 的 `RECIPES/STATIONS/ORDER_FISH/book()` 模式：

```python
from ai.query import recipes as R

def test_missing_union_empty_when_union_covers_order():
    b = R.RecipeBook(RECIPES, STATIONS)
    a = {"n": "-1", "detail": ["ing:Seaweed"]}
    b_ = {"n": "-2", "detail": ["ing:SushiFish",
                                "CookedCompositeAssembledNode(Pot){ ing:SushiRice }"]}
    assert b.missing_union(ORDER_FISH, [a, b_]) == ()

def test_missing_union_only_counts_union():
    b = R.RecipeBook(RECIPES, STATIONS)
    a = {"n": "-1", "detail": ["ing:Seaweed"]}
    assert b.missing_union(ORDER_FISH, [a]) == ("BoiledSushiRice", "SushiFish")
```

- [ ] **Step 2: 跑测试确认失败** `pytest tests/test_plate_merge.py -v` → FAIL（`RecipeBook` 没有 `missing_union`）。
- [ ] **Step 3: 实现 `missing_union`**：并集 `have = set()`，`for p in plates: have |= self.plate_tokens(p)`，再套 `missing` 的同一行判定。
- [ ] **Step 4: 跑测试确认通过** → PASS。
- [ ] **Step 5: commit** `git add ai/query/recipes.py tests/test_plate_merge.py && git commit -m "feat(recipes): add RecipeBook.missing_union for multi-plate orders"`。

---

### Task 2: `MergePlates` task

**Files:**
- Modify: `ai/tasks/assembly.py`（新增 `MergePlates`，复用 `_plate_on_serving_hatch`、`plate_by_id`、`plate_matches_order`；模块 docstring 的「两 task」改「三 task」）
- Test: `tests/test_plate_merge.py`

**Interfaces:**
- Consumes: `Task`/`_register`/`TASK_TIMEOUT_TICKS`（base.py）、`plate_by_id`、`P.plate_count`/`P.holds_plate`/`P.hands_free`、`_plate_on_serving_hatch`、`book.plate_tokens`/`book.order_want_tokens`。
- Produces: `T.MergePlates(order_id, (src_id, dst_id))`，属性 `src_id`/`dst_id`，`claims()`、`feasible(ctx)`、`done(ctx)`、`build()`。

- [ ] **Step 1: 写失败测试**（`FakeWorld`/`make_ctx` 模式见 `tests/test_tasks.py`），断言：
  - `claims() == ((src, "merge"), (dst, "merge"))`。
  - feasible：src/dst 存在且非空、并集 ⊆ 单需求、缺料严格减少 → True；src 与 dst 重叠导致无进展 → False；src 或 dst 是 P1 手里 → False。
  - `done()`：src `plate_count==0` → True；src 仍有料 → False。

- [ ] **Step 2: 跑测试确认失败** → FAIL（`T.MergePlates` 不存在）。
- [ ] **Step 3: 实现 `MergePlates`**：
  - `TYPE="MergePlates"`、`HANDS="empty"`；`src_id = resources[0]`、`dst_id = resources[1]`。
  - `claims()` 返回两块盘 `merge`。
  - `feasible`：src/dst 存在且非空、`src != dst`、订单存活、`tokens(src)|tokens(dst) ⊆ want(order)`、`|want−union|` 严格小于两盘各自 `|want−tokens|`、两盘都不在出餐口（`_plate_on_serving_hatch`）、dst 非 P1 手里。
  - `done`：`P.plate_count(src) == 0`。
  - `build`：`Timeout(Sequence([EnsureHandsFree(keep=holding_src), InteractWith(src,"pickup",holding_src), InteractWith(dst,"placement",merged)]), TASK_TIMEOUT_TICKS)`；`holding_src`=手持 src 盘、`merged`=src 空（`plate_by_id` 定位）。
- [ ] **Step 4: 跑测试确认通过** → PASS。
- [ ] **Step 5: commit** `git commit -m "feat(tasks): add MergePlates plate-to-plate merge task"`。

---

### Task 3: `_gen_merge` 候选生成 + 注册

**Files:**
- Modify: `ai/core/planner.py`（`generate_candidates` 调用点、新增 `_gen_merge`、`_CAND_META`、`_COMBO_PREP_TYPES`）
- Modify: `ai/core/planner_layers.py`（`LAYER_PREP_TYPES`）
- Test: `tests/test_plate_merge.py`

**Interfaces:**
- Consumes: `Candidate(task, preconditions, est, branch=...)`、`_dialog_live_orders`、`_combo_head_id`、`book.plate_tokens`/`book.order_want_tokens`/`book.missing_union`、`P.all_plates`/`P.chef_by_name`/`P.plate_buried_in_stack`/`P.plate_stack_top`/`P.held_name`、`_plate_cell`、`from ai.query.recipe_graph import _counter_cells`。
- Produces: `_gen_merge(world, bb, book) -> List[Candidate]`；`T.MergePlates` 注册进 `_CAND_META`、`_COMBO_PREP_TYPES`、`LAYER_PREP_TYPES`。

- [ ] **Step 1: 写失败测试**（`_gen_merge` 直接喂 `FakeWorld`），断言：
  - 互补盘对（各缺对方有的料）→ 产生一个 `MergePlates` 候选，`dst` 是 token 多的盘。
  - 重叠盘对（共享料、无进展）→ 0 候选。
  - 并集超集（多放不属于该单的料）→ 0 候选。
  - 涉及 P1 手里的盘 → 0 候选。
  - 2+2 平手（两盘 token 一样多）→ 定向稳定：`dst` 是 id 小者，连跑两拍结果一致。

- [ ] **Step 2: 跑测试确认失败** → FAIL（`_gen_merge` 不存在）。
- [ ] **Step 3: 实现 `_gen_merge`**：
  - 过滤盘集合复用 `_match_plates_to_orders` 的口径（on_counter / held_by_p2 / loose_with_contents / stack_top，排除 P1-held、buried）。
  - 两两枚举非空盘对；对每个 `_dialog_live_orders` 里的 O 判并集 ⊆ want(O) 且 `missing_union(O, [src,dst])` 严格小于两盘各自的 missing。
  - 目标订单 `O = argmin missing_union(O, union)`，同分取队首。
  - 定向：`dst = token 多的盘`，平手 `id` 小者；每个盘对只发一个候选。
  - 在 `generate_candidates` 的 `recipe_graph.emit` 循环后、`_gen_misc` 前调用 `_gen_merge`，`extend` 进 candidates。
  - `_CAND_META["MergePlates"] = (["hands_empty"], EST_FETCH)`；`_COMBO_PREP_TYPES` 与 `LAYER_PREP_TYPES` 各加 `"MergePlates"`。
- [ ] **Step 4: 跑测试确认通过** → PASS。
- [ ] **Step 5: commit** `git commit -m "feat(planner): add _gen_merge candidates for complementary plate pairs"`。

---

### Task 4: `completion_ratio` 并集扩展

**Files:**
- Modify: `ai/core/planner.py`（`_order_completion_ratio`）
- Test: `tests/test_plate_merge.py`

**Interfaces:**
- Consumes: `_find_plate_for_order`、`book.plate_tokens`、`book.want_tokens`、`task.TYPE`/`task.order_id`。
- Produces: 对 `MergePlates` 用并集算 filled_count（`_score_candidate` 无需改动，走原调用）。

- [ ] **Step 1: 写失败测试**：`MergePlates` 的 `_order_completion_ratio` = 并集覆盖数 / `len(composition)`；两盘并集凑齐 → 1.0，只并集覆盖 2/3 → 2/3。
- [ ] **Step 2: 跑测试确认失败** → FAIL（当前实现按单盘 `_find_plate_for_order` 算，拿不到 src 的料）。
- [ ] **Step 3: 实现**：在 `_order_completion_ratio` 里对 `task.TYPE == "MergePlates"` 分支——`union = book.plate_tokens(src) | book.plate_tokens(dst)`，`filled = #{ing ∈ composition | want_tokens(ing) ∩ union}`。src/dst 由 `task.src_id`/`task.dst_id` 经 `_plate_by_n` 反查。
- [ ] **Step 4: 跑测试确认通过** → PASS。
- [ ] **Step 5: commit** `git commit -m "feat(planner): union-based completion ratio for MergePlates"`。

---

### Task 5: 端到端验证

**Files:** 无新增（仅验证）。

- [ ] **Step 1: 全量回归** `python3 -m pytest tests/ -q` → 全绿（现有用例不回归）。
- [ ] **Step 2: 离线建树** `python3 tools/tree_viz.py <含 t≈840–860 的真实帧 fixture>`（NullExec）→ `MergePlates` 候选出现、能走通分支、不按键。
- [ ] **Step 3: 实机回放**（如环境允许）：P2 主动把「海苔盘」倒进「鱼+饭盘」完成订单，随后 `DeliverPlate` 送出；确认无合并振荡、正常装配不受影响。
- [ ] **Step 4: 确认无 push**（`git log` 本地提交、`git status` 干净；不执行任何 push）。

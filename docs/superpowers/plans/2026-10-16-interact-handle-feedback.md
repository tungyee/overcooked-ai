# 交互句柄反馈闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `Tap` 的 placement/pickup 句柄校验失败时，先往格心收紧站位重试，
而不是直接 FAILURE 让整棵树塌掉。

**Architecture:** 新增 `AlignToCell` 原语，插在 `Face` 与 `Tap` 之间。它读
游戏上报的真实句柄（不预测、不读高亮），不符就朝格心短按一步；已在格心
容差内仍不符才 FAILURE，并按 (资源, 落脚格) 二元粒度拉黑，让 `Approach`
下次换格。

**Tech Stack:** Python 3.9.6 标准库 + pytest 7.4.4。无新依赖。

**Spec:** `docs/superpowers/specs/2026-10-16-interact-handle-feedback-design.md`

## Global Constraints

- 注释/文档用中文，标识符用英文；注释讲「为什么」，带实机依据。
- 类型注解用 `# type: (...) -> ...` 注释形式，非 PEP 526 内联。
- 谓词无副作用；`predicates.py` 不依赖 `World` 类。
- 单文件 ≤ 400 行（spec 硬指标）。`ai/primitives/move.py` 现 299 行。
- 「拍」= Python 循环拍 = 50 ms（20 Hz）。TTL 一律按拍，不用秒。
- 执行层对 `bb.claims` / `bb.commitment` 无写权，只能写 `scratch` 与
  `commitment.steps_done/retries`（spec §4.5）。
- 格宽从 `world.mapping.cell` 读，不硬编码 1.2。
- 全量测试基线：`python3 -m pytest tests/` 当前 1137 passed, 77 skipped。

## Review Focus

spec 蕴含但无 task 测试覆盖、最可能咬人的五类输入：

1. **`AlignToCell` 的目标在收紧途中消失**（P1 端走台面上的物、盘被拿走）。
   `resolve` 返回 None 时应 FAILURE 交还 Planner，不是 NPE 也不是死等。
   → Task 2 Step 1 的 `test_align_target_gone_is_failure`。
2. **格宽非 1.2 的关卡**。容差写死 0.3 会在 cell=0.8 的关卡变成 3/8 格、
   在 cell=2.0 变成 3/20 格。→ Task 1 Step 1 的
   `test_align_epsilon_scales_with_cell_size`。
3. **目标就在脚下格**（散落物落在自己站的格）。格心方向为零向量，短按
   方向无从计算。→ Task 2 Step 1 的 `test_align_target_underfoot_no_direction`。
4. **传送带上收紧**。传送带持续施加外力，永远到不了格心容差内，会无限
   RUNNING 直到 task 超时。→ Task 2 Step 1 的
   `test_align_on_belt_does_not_loop_forever`。
5. **pair 拉黑的 TTL 过期后要能重试**。TTL 不生效会让一次偶发失败永久
   废掉一个 (资源,格) 组合。→ Task 3 Step 1 的
   `test_blocked_pair_expires_after_ttl`。

---

### Task 1: 格心对齐判据（纯函数）

**Files:**
- Modify: `ai/predicates.py`（在 `_target_cell` 之后、`interact_ready` 之前插入）
- Test: `tests/test_predicates.py`

**Interfaces:**
- Consumes: 无（纯函数，只读 dict）
- Produces:
  - `cell_center_offset(chef, cell, to_world) -> Optional[float]`
    —— 角色到格 `cell` 中心的平面距离；缺坐标返回 None。
  - `align_epsilon(cell_size) -> float` —— 容差 = `cell_size / 4.0`。

- [ ] **Step 1: 写失败测试**

```python
def test_cell_center_offset_measures_plane_distance():
    chef = {"x": 15.3, "z": -10.97}
    # 格 (1,-4) 的中心按 to_world 给的 (15.6, -10.8)
    to_world = lambda gx, gz: (15.6, -10.8)
    got = P.cell_center_offset(chef, (1, -4), to_world)
    assert abs(got - 0.3447) < 1e-3


def test_cell_center_offset_none_without_coords():
    assert P.cell_center_offset({"x": None, "z": 1.0}, (0, 0),
                                lambda gx, gz: (0.0, 0.0)) is None
    assert P.cell_center_offset(None, (0, 0), lambda gx, gz: (0.0, 0.0)) is None


def test_align_epsilon_scales_with_cell_size():
    """容差是格宽的函数，不是写死的 0.3（spec Review Focus 2）。"""
    assert P.align_epsilon(1.2) == 0.3
    assert P.align_epsilon(0.8) == 0.2
    assert P.align_epsilon(2.0) == 0.5
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_predicates.py -q -k "cell_center_offset or align_epsilon"`
Expected: FAIL，`AttributeError: module 'ai.predicates' has no attribute 'cell_center_offset'`

- [ ] **Step 3: 实现两个函数**

在 `ai/predicates.py` 实现：

- `cell_center_offset(chef, cell, to_world)`
  `# type: (Optional[Dict], Optional[Tuple[int,int]], Any) -> Optional[float]`
  —— `to_world(*cell)` 得格心世界坐标，返回与 `chef["x"]/["z"]` 的
  `math.hypot`。`chef` 为 None、`cell` 为 None、或 x/z 任一为 None 时返回 None。
- `align_epsilon(cell_size)` `# type: (float) -> float` —— 返回 `cell_size / 4.0`。

注释要记：0.3（cell=1.2）这个值的实机依据是 trace-20261016-220409 t=9523
偏离格心 (-0.30,-0.17) 时 placement 句柄被出餐口抢走，而格心处全朝向可用；
`cell/4` 是「偏离超过 1/4 格就先收紧」，比实测失效点保守一档。标定状态见
spec §6。

`math` 已在模块顶部导入则复用，否则加 import。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_predicates.py -q -k "cell_center_offset or align_epsilon"`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
git add ai/predicates.py tests/test_predicates.py
git commit -m "feat(predicates): 格心偏离度与对齐容差（句柄反馈闭环 Task 1）"
```

---

### Task 2: `AlignToCell` 原语

**Files:**
- Create: `ai/primitives/align.py`
- Modify: `ai/primitives/__init__.py`（导出 `AlignToCell`，加入 `__all__`）
- Test: `tests/test_primitives.py`

新建文件而非塞进 `move.py`：后者 299 行，逼近 400 行硬指标，且 `AlignToCell`
的语义（句柄反馈）与 `Approach`/`Face`（寻路与转向）是不同职责。

**Interfaces:**
- Consumes: `P.interact_ready`、`P.cell_center_offset`、`P.align_epsilon`（Task 1）、
  `P.chef_cell`、`primitives.base.Primitive`/`resolve`/`target_cell`、
  `primitives.motion.MotionControl`
- Produces: `AlignToCell(target, kind, max_ticks=ALIGN_MAX_TICKS)` —— `Primitive`
  子类。`target` 可调用或 dict；`kind` 为 `"placement"`/`"pickup"`/None。
  tick 返回 `Status.SUCCESS`（句柄已指向目标）/ `FAILURE`（收紧到位仍不符、
  目标消失、超时）/ `RUNNING`（正在收紧）。
  模块常量 `ALIGN_MAX_TICKS = 40`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_primitives.py`（复用该文件已有的 `make_ctx` / `_inter` /
`FakeWorld`）：

```python
# ---------- AlignToCell ----------

def test_align_success_when_handle_already_matches():
    """句柄已指向目标 → 直接 SUCCESS，不按任何键。"""
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "pot",
            "interactions": _inter(placement_gx=1, placement_gz=0)}
    ctx, ctrl = make_ctx(chef=chef)
    node = PR.AlignToCell({"n": "c", "gx": 1, "gz": 0}, "placement")
    assert node.tick(ctx) == Status.SUCCESS
    assert ctrl.calls == []


def test_align_presses_toward_cell_center_when_handle_mismatches():
    """句柄被别的物体抢走、且偏离格心 → 朝格心短按，RUNNING。

    实机 trace-20261016-220409 t=9523：P2 偏离格心 0.3 格，placement 句柄
    被出餐口抢走，而格心处全朝向可用（spec §2.3）。
    """
    # 格 (1,0) 的中心是 (1.2, 0.0)；放在 (0.9, 0.0) 即 -0.3 偏离
    chef = {"name": "Player 2", "x": 0.9, "z": 0.0, "held": "pot",
            "interactions": _inter(gx=2, gz=0, placement_gx=0, placement_gz=-1)}
    ctx, _ = make_ctx(chef=chef, start=(1, 0))
    node = PR.AlignToCell({"n": "c", "gx": 2, "gz": 0}, "placement")
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "right", "格心在 +x 方向"


def test_align_failure_when_centered_but_handle_still_mismatches():
    """已在格心容差内仍不符 → FAILURE 交还 Planner（不无限收紧）。"""
    chef = {"name": "Player 2", "x": 1.2, "z": 0.0, "held": "pot",
            "interactions": _inter(gx=2, gz=0, placement_gx=0, placement_gz=-1)}
    ctx, _ = make_ctx(chef=chef, start=(1, 0))
    node = PR.AlignToCell({"n": "c", "gx": 2, "gz": 0}, "placement")
    assert node.tick(ctx) == Status.FAILURE


def test_align_target_gone_is_failure():
    """收紧途中目标消失（P1 端走）→ FAILURE，不 NPE 不死等（Review Focus 1）。"""
    ctx, _ = make_ctx()
    node = PR.AlignToCell(lambda c: None, "placement")
    assert node.tick(ctx) == Status.FAILURE


def test_align_target_underfoot_no_direction():
    """目标就在脚下格 → 无方向可按，FAILURE 交还 Planner（Review Focus 3）。"""
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "pot",
            "interactions": _inter(placement_gx=9, placement_gz=9)}
    ctx, _ = make_ctx(chef=chef, start=(0, 0))
    node = PR.AlignToCell({"n": "loose", "gx": 0, "gz": 0}, "placement")
    assert node.tick(ctx) == Status.FAILURE


def test_align_on_belt_does_not_loop_forever():
    """传送带上外力持续，永远到不了格心 → max_ticks 兜底 FAILURE（Review Focus 4）。"""
    w = FakeWorld(walkable=lambda gx, gz: True, belt_cells={(1, 0)})
    chef = {"name": "Player 2", "x": 0.9, "z": 0.0, "held": "pot",
            "interactions": _inter(gx=2, gz=0, placement_gx=0, placement_gz=-1)}
    ctx, _ = make_ctx(w=w, chef=chef, start=(1, 0))
    node = PR.AlignToCell({"n": "c", "gx": 2, "gz": 0}, "placement", max_ticks=3)
    seen = [node.tick(ctx) for _ in range(5)]
    assert Status.FAILURE in seen, "max_ticks 没兜住，会无限 RUNNING"


def test_align_halt_releases_movement():
    chef = {"name": "Player 2", "x": 0.9, "z": 0.0, "held": "pot",
            "interactions": _inter(gx=2, gz=0, placement_gx=0, placement_gz=-1)}
    ctx, _ = make_ctx(chef=chef, start=(1, 0))
    node = PR.AlignToCell({"n": "c", "gx": 2, "gz": 0}, "placement")
    node.tick(ctx)
    node.halt()
    assert ctx["hands"].move_dir is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_primitives.py -q -k align`
Expected: FAIL，`AttributeError: module 'ai.primitives' has no attribute 'AlignToCell'`

- [ ] **Step 3: 实现 `AlignToCell` 于 `ai/primitives/align.py`**

tick 的判定顺序（顺序有意义，照此实现）：

1. `resolve(self.target, ctx)` 为 None → `set_move(None)`，FAILURE。
2. `P.interact_ready(chef, st, self.kind, w.to_grid)` 成立 → `reset()`，SUCCESS。
   （**先查成功再查其它**：句柄已对就什么都不做，这是常态路径。）
3. `self._ticks > self.max_ticks` → `set_move(None)`，`reset()`，FAILURE。
4. `cur = P.chef_cell(chef, w.to_grid)`，为 None → FAILURE。
5. `off = P.cell_center_offset(chef, cur, w.to_world)`；为 None → FAILURE。
6. `off <= P.align_epsilon(w.mapping.cell)` → FAILURE
   （已在格心仍不符，当前位置对这个目标真不可用）。
   取格宽用 `getattr(getattr(w, "mapping", None), "cell", 1.2)`——`FakeWorld`
   没有 `mapping`，默认回落 1.2。
7. 否则按 `chef` 到格心的 x/z 偏差主轴求方向（|dx| >= |dz| 则 left/right，
   否则 up/down；方向指向格心），交给 `MotionControl.tick(ctx, d, None,
   True, False, belt=cur in w.belt_cells)` 走短按+停稳，返回它的状态。
   主轴偏差都在浮点零附近（目标就在脚下格、已居中）时方向求不出来 → FAILURE。

`reset()` 要重建 `MotionControl()`（与 `Face` 一致）；`halt()` 要
`set_move(None)` 并 halt 内部 motion。

`ctx["motion"]` 可能未初始化（离线调用），用 `ctx.setdefault("motion", {})`
再 update，不要直接下标赋值。

- [ ] **Step 4: 导出原语**

在 `ai/primitives/__init__.py` 加 `from ai.primitives.align import AlignToCell`
并把 `"AlignToCell"` 加入 `__all__`。

- [ ] **Step 5: 跑测试确认通过**

Run: `python3 -m pytest tests/test_primitives.py -q -k align`
Expected: PASS（7 passed）

- [ ] **Step 6: 跑全量确认无回归**

Run: `python3 -m pytest tests/ -q`
Expected: 1144 passed, 77 skipped（1137 + 7 新增）

- [ ] **Step 7: 提交**

```bash
git add ai/primitives/align.py ai/primitives/__init__.py tests/test_primitives.py
git commit -m "feat(primitives): AlignToCell 原语——句柄不符时往格心收紧（Task 2）"
```

---

### Task 3: (资源, 格) 二元粒度拉黑

**Files:**
- Modify: `ai/blackboard.py`（`StuckScratch` 加字段）
- Modify: `ai/primitives/base.py:46-58`（`_blocked_cells` 支持 pair 过滤）
- Test: `tests/test_blackboard.py`、`tests/test_primitives.py`

**Interfaces:**
- Consumes: `StuckScratch`、`primitives.base._blocked_cells`
- Produces:
  - `StuckScratch.bad_pairs: Dict[Tuple[Any, Tuple[int,int]], int]`
    —— 键是 `(resource_id, (gx,gz))`，值是失效拍号。
  - `_blocked_cells(ctx, resource_id=None)` —— 新增可选形参。给
    `resource_id` 时，返回值在原 `bad_cells` 基础上并入该资源的
    `bad_pairs` 格；不给时行为与今天逐字相同（向后兼容既有 11 处调用）。

- [ ] **Step 1: 写失败测试**

`tests/test_blackboard.py`：

```python
def test_stuck_scratch_has_bad_pairs():
    """(资源,格) 二元拉黑：格对别的目标可能是好的，资源对别的格也是。"""
    from ai.blackboard import StuckScratch
    s = StuckScratch()
    assert s.bad_pairs == {}
    s.bad_pairs[(("GridManager", "counter_01"), (1, -4))] = 300
    assert len(s.bad_pairs) == 1
```

`tests/test_primitives.py`：

```python
def test_blocked_cells_ignores_pairs_without_resource_id():
    """不传 resource_id 时行为不变（向后兼容既有调用点）。"""
    from ai.blackboard import StuckScratch
    bb = Blackboard()
    bb.scratch = StuckScratch(bad_cells={(1, 0): 999},
                              bad_pairs={("res_a", (2, 0)): 999})
    ctx, _ = make_ctx(bb=bb)
    assert PR._blocked_cells(ctx) == {(1, 0)}


def test_blocked_cells_includes_pairs_for_matching_resource():
    from ai.blackboard import StuckScratch
    bb = Blackboard()
    bb.scratch = StuckScratch(bad_cells={(1, 0): 999},
                              bad_pairs={("res_a", (2, 0)): 999,
                                         ("res_b", (3, 0)): 999})
    ctx, _ = make_ctx(bb=bb)
    assert PR._blocked_cells(ctx, "res_a") == {(1, 0), (2, 0)}
    assert PR._blocked_cells(ctx, "res_b") == {(1, 0), (3, 0)}


def test_blocked_pair_expires_after_ttl():
    """TTL 过期后要能重试，否则一次偶发失败永久废掉一个组合（Review Focus 5）。"""
    from ai.blackboard import StuckScratch
    bb = Blackboard()
    bb.scratch = StuckScratch(bad_pairs={("res_a", (2, 0)): 100})
    ctx, _ = make_ctx(bb=bb, tick=100)
    assert PR._blocked_cells(ctx, "res_a") == set(), "expire <= tick 即失效"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_blackboard.py tests/test_primitives.py -q -k "bad_pairs or blocked_cells or blocked_pair"`
Expected: FAIL，`TypeError: StuckScratch.__init__() got an unexpected keyword argument 'bad_pairs'`

- [ ] **Step 3: 加 `StuckScratch.bad_pairs` 字段**

在 `ai/blackboard.py` 的 `StuckScratch` 加
`bad_pairs: Dict[Tuple[Any, Tuple[int, int]], int] = field(default_factory=dict)`。
注释记粒度理由：只拉黑格会废掉该格对所有目标的用途（出餐口旁那格对隔壁台面
以外的目标是好的），只拉黑资源会废掉整个台面（换个格子能放）。

- [ ] **Step 4: 给 `_blocked_cells` 加 `resource_id` 形参**

`ai/primitives/base.py:46`。保持默认 None 时逐字等价于今天的实现（既有
11 处调用点不改）。过期判据沿用 `expire > tick`。

- [ ] **Step 5: 跑测试确认通过**

Run: `python3 -m pytest tests/test_blackboard.py tests/test_primitives.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add ai/blackboard.py ai/primitives/base.py tests/test_blackboard.py tests/test_primitives.py
git commit -m "feat(blackboard,primitives): (资源,格) 二元粒度拉黑（Task 3）"
```

---

### Task 4: 接入复合原语与 `EnsureHandsFree`

**Files:**
- Modify: `ai/primitives/interact.py`（`InteractWith._children`、`EnsureHandsFree` 阶段机）
- Test: `tests/test_primitives.py`

**Interfaces:**
- Consumes: `AlignToCell`（Task 2）、`_blocked_cells(ctx, resource_id)`（Task 3）
- Produces: 无新公开接口。`InteractWith` 的子节点序列变为
  `Approach → [Face] → AlignToCell → TapWhileHolding → Confirm`；
  `EnsureHandsFree` 的台面路径在 `_P_FACE` 与 `_P_TAP` 之间插入对齐阶段。

- [ ] **Step 1: 写失败测试**

```python
def test_interact_with_aligns_before_tapping():
    """句柄被别的物体抢走时，InteractWith 先收紧站位而不是整体 FAILURE。

    实机 trace-20261016-220409：EnsureHandsFree 在这种局面直接 FAILURE，
    树塌 → 重建 → 同样位置再塌，P2 钉住 287 拍到收盘（spec §1）。
    """
    chef = {"name": "Player 2", "x": 0.9, "z": 0.0, "held": "pot",
            "interactions": _inter(gx=2, gz=0, placement_gx=0, placement_gz=-1)}
    ctx, ctrl = make_ctx(chef=chef, start=(1, 0))
    node = PR.InteractWith({"n": "c", "gx": 2, "gz": 0}, "placement",
                           lambda c: False)
    got = [node.tick(ctx) for _ in range(4)]
    assert Status.FAILURE not in got, "不该直接失败，应先收紧站位"
    assert ("tap", "pickup") not in ctrl.calls, "句柄不对时不许瞎按"


def test_ensure_hands_free_aligns_before_tapping():
    """EnsureHandsFree 同理——它是每棵树的树首，塌了整个 task 都没了。"""
    station = {"t": "counter", "n": "c", "gx": 2, "gz": 0, "x": 2.4, "z": 0.0}
    w = FakeWorld(stations=[station],
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 4)
    chef = {"name": "Player 2", "x": 0.9, "z": 0.0, "held": "pot",
            "interactions": _inter(gx=2, gz=0, placement_gx=0, placement_gz=-1)}
    ctx, ctrl = make_ctx(w=w, chef=chef, start=(1, 0))
    node = PR.EnsureHandsFree()
    got = [node.tick(ctx) for _ in range(4)]
    assert Status.FAILURE not in got
    assert ("tap", "pickup") not in ctrl.calls
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_primitives.py -q -k "aligns_before_tapping"`
Expected: FAIL —— 两个用例都在第一拍拿到 `Status.FAILURE`

- [ ] **Step 3: 接入 `InteractWith`**

`__init__` 里建 `self._align = AlignToCell(target, kind)`，`_children()`
在 `self._face` 之后、`self._tap` 之前插入它。`reset`/`halt` 走 `_children()`
故自动覆盖。

`face=False` 的路径（拾取散落物）也要带 `AlignToCell`：句柄抢占与朝向无关
（`ScanForObject` 纯按距离，spec §2.2）。

- [ ] **Step 4: 接入 `EnsureHandsFree`**

加模块常量 `_P_ALIGN`（在 `_P_FACE` 与 `_P_TAP` 之间），`__init__` 建
`self._align_station = AlignToCell(lambda c: self._tgt, "placement")`，
加入 `_nodes()`。

`_step` 的台面路径：`_P_FACE` 成功后进 `_P_ALIGN`，对齐成功才进 `_P_TAP`。
**地板格路径不走对齐**——裸地板格插件不给 placement/facing 信号，
`interact_ready` 恒 False，对齐会无条件收紧到 max_ticks。所以
`self._on_station` 为 False 时 `_P_APPROACH` 直接跳到 `_P_TAP`（与今天相同）。

- [ ] **Step 5: 跑测试确认通过**

Run: `python3 -m pytest tests/test_primitives.py -q -k "aligns_before_tapping"`
Expected: PASS（2 passed）

- [ ] **Step 6: 跑全量确认无回归**

Run: `python3 -m pytest tests/ -q`
Expected: 全部 passed，无 failure。新增 2 个用例。

**若有既有用例失败**：优先怀疑它们在用「句柄不符时期望立即 FAILURE」做
断言。逐个核对该用例描述的是否真实机场景；是则说明本设计与它冲突，停下来
报告而不是改断言迁就。

- [ ] **Step 7: 提交**

```bash
git add ai/primitives/interact.py tests/test_primitives.py
git commit -m "feat(primitives): InteractWith/EnsureHandsFree 接入对齐阶段（Task 4）"
```

---

### Task 5: 对齐失败落到 pair 拉黑

**Files:**
- Modify: `ai/primitives/align.py`（FAILURE 时写 `scratch.bad_pairs`）
- Modify: `ai/primitives/move.py:102`（`Approach` 读 pair）
- Test: `tests/test_primitives.py`

**Interfaces:**
- Consumes: `StuckScratch.bad_pairs`、`_blocked_cells(ctx, resource_id)`（Task 3）
- Produces: `align.ALIGN_PAIR_TTL_TICKS = 200`（与 `ABANDON_TTL_TICKS` 同量级：
  一次换位重试的时间尺度）

- [ ] **Step 1: 写失败测试**

```python
def test_align_failure_blacklists_resource_cell_pair():
    """收紧到位仍不符 → 记 (资源,格)，让 Approach 下次换格。"""
    from ai.blackboard import StuckScratch
    bb = Blackboard()
    bb.scratch = StuckScratch()
    chef = {"name": "Player 2", "x": 1.2, "z": 0.0, "held": "pot",
            "interactions": _inter(gx=2, gz=0, placement_gx=0, placement_gz=-1)}
    ctx, _ = make_ctx(chef=chef, start=(1, 0), bb=bb, tick=50)
    station = {"t": "counter", "n": "c", "grid": "GridManager", "gx": 2, "gz": 0}
    node = PR.AlignToCell(station, "placement")
    assert node.tick(ctx) == Status.FAILURE
    key = (P.resource_id(station), (1, 0))
    assert bb.scratch.bad_pairs.get(key) == 50 + PR.ALIGN_PAIR_TTL_TICKS


def test_approach_avoids_pair_blacklisted_interact_cell():
    """(资源,格) 被拉黑 → Approach 换别的交互位，不再撞同一格。"""
    from ai.blackboard import StuckScratch
    station = {"t": "counter", "n": "c", "grid": "GridManager", "gx": 2, "gz": 0}
    w = FakeWorld(stations=[station],
                  walkable=lambda gx, gz: -1 <= gz <= 1 and 0 <= gx <= 4)
    bb = Blackboard()
    bb.scratch = StuckScratch(
        bad_pairs={(P.resource_id(station), (1, 0)): 999})
    ctx, _ = make_ctx(w=w, start=(0, 0), bb=bb)
    node = PR.Approach(station, "placement")
    for _ in range(3):
        node.tick(ctx)
    assert node._goal != (1, 0), "被拉黑的交互位不该再被选为目标"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_primitives.py -q -k "pair_blacklist or avoids_pair"`
Expected: FAIL，`AttributeError: ... has no attribute 'ALIGN_PAIR_TTL_TICKS'`

- [ ] **Step 3: `AlignToCell` 在「已居中仍不符」时写 pair**

只在 Task 2 Step 3 判定顺序第 6 条（已在格心容差内仍不符）那一支写，
**不在**目标消失 / 超时 / 坐标缺失那几支写——那些不是「这格对这个目标
不可用」的证据。

键用 `(P.resource_id(st), cur)`。资源 id 走 `P.resource_id`（spec §5.2
的 `(grid, name)`），与 task 的 `claims()` 同一套，便于日后对账。
导出 `ALIGN_PAIR_TTL_TICKS` 到 `ai/primitives/__init__.py` 的 `__all__`。

scratch 可能不是 `StuckScratch`（卡住守卫是跨模式前置，可能先于任何模式
进入）。复用 `ai/primitives/stuck.py` 的 `_ensure_stuck_scratch(ctx)`，
不要自己再写一遍类型检查。

- [ ] **Step 4: `Approach` 读 pair**

`ai/primitives/move.py:102` 的 `blocked = _blocked_cells(ctx)` 改为传入
当前目标的资源 id：`_blocked_cells(ctx, P.resource_id(st))`。`st` 在该行
上方已解析。

- [ ] **Step 5: 跑测试确认通过**

Run: `python3 -m pytest tests/test_primitives.py -q -k "pair_blacklist or avoids_pair"`
Expected: PASS（2 passed）

- [ ] **Step 6: 跑全量确认无回归**

Run: `python3 -m pytest tests/ -q`
Expected: 全部 passed。

- [ ] **Step 7: 提交**

```bash
git add ai/primitives/align.py ai/primitives/move.py ai/primitives/__init__.py tests/test_primitives.py
git commit -m "feat(primitives): 对齐失败按 (资源,格) 拉黑，Approach 据此换位（Task 5）"
```

---

### Task 6: 真实帧回归 + 可观测性

**Files:**
- Create: `tests/test_align_real_frame.py`
- Create: `tests/fixtures/align_stuck_20261016.json`
- Modify: `ai/trace.py`（`motion` 记录加 `align` 阶段）

**Interfaces:**
- Consumes: Task 1~5 全部
- Produces: 无新接口。trace 的 `exec.observation` 旁新增
  `motion.phase == "align"`，用于实机复盘介入频次（spec §6 待标定项）。

- [ ] **Step 1: 落 fixture 并写失败测试**

fixture 取 trace-20261016-220409 的 t=9523 那一帧 state（即本次分析用的
`/tmp/f9523.json`）。它是**未压缩 json 单帧**而非 `.ndjson.gz`：
`.gitignore` 排除的是 `*.ndjson`，单帧 json fixture 不受影响；且这一帧是
本设计唯一的实机依据，必须入库。

```python
# -*- coding: utf-8 -*-
"""句柄抢占的真实帧回归（trace-20261016-220409 t=9523）。

这一帧是本设计的唯一实机依据：P2 在格 (1,-4) 偏离格心 0.30/0.17，
placement 句柄被 1.4% 之差的出餐口抢走，于是 EnsureHandsFree 直接
FAILURE、树塌、重建、再塌，287 拍钉死到收盘。
"""
import json
import os

from ai import predicates as P
from ai import primitives as PR
from ai.blackboard import Blackboard
from ai.hands import Hands
from ai.navigation import distance_field
from ai.recipes import RecipeBook
from ai.world import World

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "align_stuck_20261016.json")


def _ctx():
    state = json.load(open(FIXTURE))
    w = World(state)
    chef = w.chef("Player 2")

    class Ctrl(object):
        def __init__(self):
            self.calls = []

        def tap(self, name, hold=0.12):
            self.calls.append(("tap", name))

        def press(self, name):
            self.calls.append(("press", name))

        def release(self, name):
            self.calls.append(("release", name))

        def release_all(self):
            pass

    ctrl = Ctrl()
    return {
        "w": w, "chef": chef, "hands": Hands(ctrl), "bb": Blackboard(),
        "field": distance_field(P.chef_cell(chef, w.to_grid), w.walkable,
                                w.belt_dir),
        "book": RecipeBook(w.recipes, w.stations), "tick": 0,
    }, ctrl


def test_real_frame_handle_is_stolen_by_delivery_hatch():
    """钉住根因：两个字段指向不同目标，这是设计的出发点。"""
    ctx, _ = _ctx()
    w, chef = ctx["w"], ctx["chef"]
    counter = [s for s in w.stations
               if s.get("n") == "countertop_01_standard_wood (36)"][0]
    assert P.interact_ready(chef, counter, None, w.to_grid) is True, \
        "通用 gx/gz 指着台面（朝向对）"
    assert P.interact_ready(chef, counter, "placement", w.to_grid) is False, \
        "placement 句柄被出餐口抢走"


def test_real_frame_ensure_hands_free_no_longer_fails_immediately():
    """修复后：第一拍不再 FAILURE，而是收紧站位。"""
    ctx, ctrl = _ctx()
    node = PR.EnsureHandsFree()
    got = [node.tick(ctx) for _ in range(4)]
    assert got[0] != "failure", "树首不该在第一拍就塌（这是 287 拍卡死的入口）"
    assert ("tap", "pickup") not in ctrl.calls, "句柄不对时不许瞎按"
```

- [ ] **Step 2: 跑测试确认第二条失败**

Run: `python3 -m pytest tests/test_align_real_frame.py -q`
Expected: 第一条 PASS（根因钉子，本来就成立），第二条 FAIL

**若第二条已 PASS**：说明 Task 4 已覆盖这条路径，把本测试留作回归、
在报告里说明，不要删。

- [ ] **Step 3: trace 记 align 阶段**

`ai/trace.py` 的 `motion` 记录透传 `phase`，确认 `"align"` 能出现在
`exec.observation` 旁。`AlignToCell` 在收紧那一支设
`ctx.setdefault("motion", {})["phase"] = "align"`。

- [ ] **Step 4: 跑全量**

Run: `python3 -m pytest tests/ -q`
Expected: 全部 passed。

- [ ] **Step 5: 提交**

```bash
git add tests/test_align_real_frame.py tests/fixtures/align_stuck_20261016.json ai/trace.py ai/primitives/align.py
git commit -m "test(align): t=9523 真实帧回归 + trace 记 align 阶段（Task 6）"
```

---

### Task 7: 文档

**Files:**
- Modify: `AGENTS.md`（§6.2 原语表加一行、§10 现状、§10.1 待办）

**Interfaces:**
- Consumes: Task 1~6
- Produces: 无代码接口。

- [ ] **Step 1: 更新 §6.2 原语表**

原语数从 15 改 16，加 `AlignToCell` 一行：
「句柄校验不符时朝格心收紧一步（`ScanForObject` 纯按距离选最近物，偏离
格心时相邻目标会抢走句柄）。（**只在 `Face` 之后用**——`Approach` 的到位
判据是位置不是朝向，混进去会左右摇摆；地板格不走对齐，裸地板无 placement
信号会白收紧到超时）」

同一节 `MarkBlocked / Nudge / AbandonTarget` 那行的「三级脱困全部预留未接线」
要加注：pair 粒度拉黑现由 `AlignToCell` 写 `scratch.bad_pairs`、`Approach`
读它，是第一个接线的脱困路径。

- [ ] **Step 2: 更新 §10 现状与 §10.1 待办**

§10 记本轮修的根因（两个交互字段语义不同：`gx/gz` 来自
`GetFacingGridOccupant` 按朝向打分、`placement_gx/gz` 来自 `ScanForObject`
按距离取最近；反编译依据在 spec §2.2）。

§10.1 加三条待办，均为 spec §6 的待标定项：
- `ALIGN_EPSILON = cell/4` 只有一个实机样本（t=9523 偏离 0.30/0.17）。
  下一局用 trace 的 `motion.phase == "align"` 看介入频次与成功率。
- 收紧动作一次 ≈ 8 拍（observe 至少 4 拍，插件 5 Hz），若介入频繁是新的
  时间成本，要测。
- pickup 侧理论同构（同一次 `ScanForObject` 扫描）但无实机样本。

- [ ] **Step 3: 提交**

```bash
git add AGENTS.md
git commit -m "docs: AGENTS.md 记录 AlignToCell 与两个交互字段的语义差异（Task 7）"
```

# Rapids 3-5 分工 / 跨缝 dash / 洗盘触发 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 3-5 的 AI 不再被固定分工束缚、能安全跨木筏缝隙，并在 2 盘关出现脏盘时及时洗盘。

**Architecture:** 三个相互独立的 Phase，可分别实施和提交：Phase 1 改攻略/提示词并让移动网格的站点不再触发分工复核；Phase 2 在寻路里识别「厨房↔木筏」的水面缝隙，只允许 dash 跳边通过，并由插件在缝隙前按 Dash；Phase 3 让「总共 2 块盘 + 有 1 个脏盘」的 WashPlate 直接进 L4 解卡层，并让 WashPlate 拒绝够不着的脏盘摞。

**Tech Stack:** Python 3.9、pytest 7.4.4、C# BepInEx 插件、InControl 虚拟手柄。

**Spec:**
- `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md`（分层、分工、寻路）
- `docs/superpowers/specs/2026-10-20-in-game-executor-design.md`（move 命令与插件执行）
- `docs/guides/story/3-5.md`（本关攻略）

## Global Constraints

- Python 只跑 3.9；不新增第三方依赖。
- 注释/文档中文，标识符英文；类型注解沿用 `# type: (...) -> ...` 注释形式。
- 纯谓词/查询不写副作用；执行出口统一是 `ctx["exec"]`。
- 插件协议字段只增不改：旧 Python 不发新字段时插件行为必须不变。
- 所有 Python 测试用 `python3 -m pytest ...`；插件改动必须 `./plugin/build.sh` 通过。
- 每个 Task 结束 commit 一次；**不要 `git push`**。
- 不读、不写 `.env` 或密钥文件。

## Review Focus

- 木筏每帧移动导致缝隙宽度变化：超过 2.0m 的缝隙不能被当成可 dash 跳边，否则会继续落水。
- 没有 `dash` 授权但前方地面消失（沿边走路踩空）时必须 `Finish(false, "ledge")`，不能继续走。
- 静态关卡原有的 4 邻接、门洞、传送门、半格走廊不能被缝隙逻辑误伤。
- Dash 按键必须在 finish / timeout / stuck / teleport 所有退出口释放，避免按键卡住。
- 洗盘在 2 盘关升到 L4 后不能压过 L0/L1/L2（火、快糊、刚熟）和 L3 送餐；这些由更小的层号结构性保证。
- `_plate_total` 必须把「手持/台面/池里/脏盘摞」都算上，否则 2 盘关会在换手时误判成 1 盘或 3 盘。
- 分工签名仍要能识别静态网格上的真实站点搬家，不能因为忽略世界坐标而漏报。

---

## Phase 1：3-5 去固定分工 + 分工复核降噪

### Task 1: 攻略和提示词明确「3-5 不设固定分工」

**Files:**
- Modify: `docs/guides/story/3-5.md:30-35`
- Modify: `ai/dialog/prompts/system.j2:24-32`
- Test: `tests/test_cookbook.py`
- Test: `tests/test_llm_client.py`

**Interfaces:**
- Consumes: `default_cookbook().resolve("Rapids_3_5_2P")`（已有），`system_message(...)`（已有）。
- Produces: 无新接口；只改文案和提示词。

- [ ] **Step 1: 写失败测试（攻略内容）**

在 `tests/test_cookbook.py` 末尾加：

```python
def test_rapids35_guide_has_no_fixed_division():
    """3-5 木筏在巡航，固定分工会让 P2 等玩家切菜；攻略要明确不设固定分工。"""
    guide = default_cookbook().resolve("Rapids_3_5_2P")
    assert guide is not None
    text = guide.section("division")
    assert "不设固定分工" in text
    assert "一人切菜" not in text
```

- [ ] **Step 2: 写失败测试（提示词）**

在 `tests/test_llm_client.py` 里 `test_system_prompt_lists_division_tools_when_available` 之后加：

```python
def test_system_prompt_respects_no_fixed_division_guide():
    """攻略明确说不设固定分工时，模型不应再调用 set_division。"""
    text = system_message("", guide="本关不设固定分工", division=True,
                          cookbook=True)["content"]
    assert "不要调用 set_division" in text
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python3 -m pytest tests/test_cookbook.py::test_rapids35_guide_has_no_fixed_division tests/test_llm_client.py::test_system_prompt_respects_no_fixed_division_guide -q`

Expected: 两个用例 FAIL（攻略/提示词还没有对应文案）。

- [ ] **Step 4: 改攻略**

把 `docs/guides/story/3-5.md` 的「## 打法与分工（2P）」整节替换成：

```markdown
## 打法与分工（2P）

- **本关不设固定分工**：木筏在巡航，站点会跟着移动，谁离得近、谁在那一侧就谁做。
- 切菜 / 下锅 / 装盘 / 送餐都按当前距离临时决定；不要因为上次是谁做的就等对方。
- 盘子只有 2 个：送完一单就顺手把脏盘带回洗碗池；谁在洗碗池那侧谁洗。
- 两端木筏贴近时，优先送餐或上筏；拿不准就等木筏靠近，不要硬走过去。
```

- [ ] **Step 5: 改提示词**

在 `ai/dialog/prompts/system.j2` 的「## 分工」第一段后加一条：

```
- 如果【本关攻略】明确写「本关不设固定分工」，不要调用 set_division；按攻略的动作优先级和「谁近谁做」自主选活。
```

- [ ] **Step 6: 跑测试确认通过**

Run: `python3 -m pytest tests/test_cookbook.py tests/test_llm_client.py -q`

Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add docs/guides/story/3-5.md ai/dialog/prompts/system.j2 tests/test_cookbook.py tests/test_llm_client.py
git commit -m "fix(guides,prompt): 3-5 改为不设固定分工"
```

---

### Task 2: 移动网格上的站点不再按世界坐标触发 division_review

**Files:**
- Modify: `ai/dialog/division.py:188-209`
- Test: `tests/test_division.py`

**Interfaces:**
- Consumes: `world.raw.get("grids")`（插件帧原样保留，含 `moving` 字段）。
- Produces: `station_signature(world)` 对 `grids[].moving == true` 的网格只比较 `(action, grid, gx, gz)`，不比较 `x/z`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_division.py` 的 `test_station_signature_changes_only_when_stations_move` 后加：

```python
def test_station_signature_ignores_world_coords_on_moving_grid():
    """木筏巡航会让 cooking 站点的 x/z 每帧变化；局部 gx/gz 没变就不该复核。"""
    raw = {"grids": [{"id": "MovingRaft", "moving": True}]}
    a = SimpleNamespace(raw=raw, stations=[
        {"t": "cooking", "grid": "MovingRaft", "gx": 1, "gz": 2,
         "x": -8.0, "z": 2.4},
    ])
    b = SimpleNamespace(raw=raw, stations=[
        {"t": "cooking", "grid": "MovingRaft", "gx": 1, "gz": 2,
         "x": -6.0, "z": 2.4},
    ])
    assert D.station_signature(a) == D.station_signature(b)


def test_station_signature_detects_local_move_on_moving_grid():
    """木筏上的站点真的换了局部格，仍要触发复核。"""
    raw = {"grids": [{"id": "MovingRaft", "moving": True}]}
    a = SimpleNamespace(raw=raw, stations=[
        {"t": "cooking", "grid": "MovingRaft", "gx": 1, "gz": 2,
         "x": -8.0, "z": 2.4},
    ])
    b = SimpleNamespace(raw=raw, stations=[
        {"t": "cooking", "grid": "MovingRaft", "gx": 2, "gz": 2,
         "x": -6.8, "z": 2.4},
    ])
    assert D.station_signature(a) != D.station_signature(b)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_division.py::test_station_signature_ignores_world_coords_on_moving_grid tests/test_division.py::test_station_signature_detects_local_move_on_moving_grid -q`

Expected: 第一个 FAIL（x/z 不同导致签名不同），第二个 PASS。

- [ ] **Step 3: 实现**

在 `ai/dialog/division.py` 的 `station_signature` 上方加：

```python
def _moving_grids(world):
    # type: (Any) -> set
    """插件帧里 moving=true 的网格 id；旧帧没有 grids 时返回空集。"""
    raw = getattr(world, "raw", None) or {}
    return {str(g.get("id")) for g in raw.get("grids") or ()
            if isinstance(g, dict) and g.get("moving") is True}
```

`station_signature` 里对每个站点：

```python
grid = str(station.get("grid") or "")
row = (action, grid,
       _q(station.get("gx"), 1.2),
       _q(station.get("gz"), 1.2))
if grid not in moving:
    row += (_q(station.get("x"), 1.2),
            _q(station.get("z"), 1.2))
parts.append(row)
```

其中 `moving = _moving_grids(world)` 在函数开头取一次。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_division.py tests/test_agent_division.py -q`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add ai/dialog/division.py tests/test_division.py
git commit -m "fix(division): 移动网格站点不再按世界坐标触发复核"
```

---

## Phase 2：跨木筏缝隙的 dash

### Task 3: 新增 `ai/space/gaps.py`，识别缝隙并生成 dash 跳边

**Files:**
- Create: `ai/space/gaps.py`
- Test: `tests/test_gaps.py`

**Interfaces:**
- `GAP_MAX_M = 2.0`：可 dash 跨过的最大缝隙。
- `DASH_COST = 2`：dash 跳边的寻路代价。
- `gap_pairs(world) -> Dict[Pair, float]`：所有检测到的缝隙对（双向），值为缝隙宽度（米）；按 `world` 缓存到 `world._gap_pairs`。
  - 枚举 4 邻接对，以及**轴向隔一格的 2 格对**（中间格在本帧不可走时才算跨缝）。木筏离厨房 1.5m 时，中间那一格是水面，需要 2 格跳边才能 dash 过去。
  - 枚举范围由 Ground 盒并集外扩一格决定，不要遍历无限网格。
- `wrap(nav, world) -> Dict`：给 `portals.nav_args` 的返回值叠一层：所有缝隙对在普通邻接上 `edge_blocked=True`；宽度 `<= GAP_MAX_M` 且中间无阻挡层的缝隙对加进 `jumps`，代价 `DASH_COST`。
- `path_has_gap(world, path) -> bool`：`path` 里是否有相邻两格属于缝隙对。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_gaps.py`：

```python
# -*- coding: utf-8 -*-
"""跨地板缝隙识别：缝隙不能走，只能 dash 跳。"""
from types import SimpleNamespace

from ai.space import gaps
from ai.space.navigation import distance_field

CELL = 1.2


class FakeWorld(object):
    def __init__(self, ground, blocking=(), walk=True):
        self.terrain = {9: list(ground)}
        if blocking:
            self.terrain[10] = list(blocking)
        self._walk = walk
        self.raw = {}
        self.edge_blocked = None

    def walkable(self, gx, gz):
        return self._walk(gx, gz) if callable(self._walk) else self._walk

    def to_world(self, gx, gz):
        return (gx * CELL, gz * CELL)


def _nav(w):
    return gaps.wrap(
        {"is_walkable": w.walkable, "edge_blocked": None,
         "jumps": {}, "jump_cost": {}}, w)


def test_gap_pair_found_between_adjacent_floors():
    # 厨房 x∈[0,2.4]，木筏 x∈[3.0,5.4]，格心 (2,1) 在厨房边缘、(3,1) 在木筏上
    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (3.0, 5.4, 0.0, 2.4)])
    pairs = gaps.gap_pairs(w)
    assert ((2, 1), (3, 1)) in pairs
    assert ((3, 1), (2, 1)) in pairs
    assert 0.5 < pairs[((2, 1), (3, 1))] < 0.7


def test_two_cell_gap_has_dash_jump():
    # 厨房格心 (2,1)=2.4，木筏格心 (4,1)=4.8；中间 (3,1)=3.6 是水面
    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (3.6, 6.0, 0.0, 2.4)],
                  walk=lambda gx, gz: (gx, gz) != (3, 1))
    pairs = gaps.gap_pairs(w)
    assert ((2, 1), (4, 1)) in pairs
    assert 0.9 < pairs[((2, 1), (4, 1))] < 1.5
    nav = _nav(w)
    field = distance_field((2, 1), belt_dir={}, **nav)
    assert (4, 1) in field


def test_wrap_blocks_normal_edge_and_adds_dash_jump():
    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (3.0, 5.4, 0.0, 2.4)])
    nav = _nav(w)
    assert nav["edge_blocked"]((2, 1), (3, 1)) is True
    assert (3, 1) in nav["jumps"].get((2, 1), ())
    field = distance_field((2, 1), belt_dir={}, **nav)
    assert (3, 1) in field


def test_gap_too_wide_is_blocked_without_jump():
    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (5.4, 7.8, 0.0, 2.4)])
    nav = _nav(w)
    assert nav["edge_blocked"]((2, 1), (3, 1)) is True
    assert (3, 1) not in nav["jumps"].get((2, 1), ())
    field = distance_field((2, 1), belt_dir={}, **nav)
    assert (3, 1) not in field


def test_gap_through_wall_has_no_jump():
    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (3.0, 5.4, 0.0, 2.4)],
                  blocking=[(2.7, 3.3, 0.0, 2.4)])
    nav = _nav(w)
    assert nav["edge_blocked"]((2, 1), (3, 1)) is True
    assert (3, 1) not in nav["jumps"].get((2, 1), ())


def test_path_has_gap():
    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (3.0, 5.4, 0.0, 2.4)])
    assert gaps.path_has_gap(w, [(2, 1), (3, 1)]) is True
    assert gaps.path_has_gap(w, [(1, 1), (2, 1)]) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_gaps.py -q`

Expected: FAIL，`ModuleNotFoundError: No module named 'ai.space.gaps'`。

- [ ] **Step 3: 实现 `ai/space/gaps.py`**

至少包含以下结构（实现细节按测试调整）：

```python
"""地板缝隙识别：两块地板之间的水面只能 dash 跨过，不能当普通邻接。"""
from typing import Dict, List, Tuple

from ai.space import occupancy

GAP_MAX_M = 2.0
DASH_COST = 2

Cell = Tuple[int, int]
Pair = Tuple[Cell, Cell]
_SAMPLES = 21


def _ground(world):
    return getattr(world, "terrain", {}).get(occupancy.LAYER_GROUND) or []


def _blocking(world):
    out = []
    for layer in occupancy.BLOCKING_LAYERS:
        out.extend(getattr(world, "terrain", {}).get(layer) or ())
    return out


def _measure(world, a, b):
    # 沿 a→b 格心连线采样，返回最长一段不在地板上的长度（米）。
    # 没有地板层、或整段都在地板上返回 0.0。


def gap_pairs(world):
    # 对相邻且 walkable 的格对调用 _measure；宽度 > 0.0 的记进双向 dict。
    # 结果缓存到 world._gap_pairs。


def _edge_blocked(base, pairs):
    def edge_blocked(cur, nxt):
        if (tuple(cur), tuple(nxt)) in pairs:
            return True
        return bool(base and base(cur, nxt))
    return edge_blocked


def wrap(nav, world):
    # 1) edge_blocked 包一层：所有缝隙对普通邻接不可走
    # 2) jumps：宽度 <= GAP_MAX_M 且连线中点不在阻塞层的缝隙对
    # 3) jump_cost：DASH_COST
    # 用 _MergedJumps / _MergedJumpCost 包住 nav 里已有的 jumps/jump_cost


def path_has_gap(world, path):
    pairs = gap_pairs(world)
    return any((tuple(path[i]), tuple(path[i + 1])) in pairs
               for i in range(len(path) - 1))
```

`_measure` 必须用 `occupancy._point_in_any_box(x, z, boxes)` 判是否在地板上；不要重新写 AABB 相交。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_gaps.py -q`

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add ai/space/gaps.py tests/test_gaps.py
git commit -m "feat(space): 识别地板缝隙并生成 dash 跳边"
```

---

### Task 4: 把缝隙接入寻路和 move 命令的 `dash` 字段

**Files:**
- Modify: `ai/space/portals.py:193-219`
- Modify: `ai/primitives/move.py:1-45`（`_move_cmd`）
- Modify: `ai/primitives/move.py:380-390`（`GoToCell` 的纠偏 move）
- Test: `tests/test_exec_primitives.py`
- Test: `tests/test_gaps.py`（补一个 `portals.nav_args` 集成用例）

**Interfaces:**
- Consumes: `gaps.wrap(nav, world)`、`gaps.path_has_gap(world, path)`（Task 3）。
- Produces: `build_command("move", ..., dash=True/False)`；插件据此决定是否在缝隙前按 Dash。

- [ ] **Step 1: 写失败测试**

在 `tests/test_exec_primitives.py` 末尾加：

```python
def test_move_cmd_sets_dash_when_path_has_gap(monkeypatch):
    from ai.primitives import move as M
    monkeypatch.setattr(M.gaps, "path_has_gap", lambda w, path: True)
    cmd = M._move_cmd(W, [(0, 0), (1, 0)], None, Blackboard(),
                      [1.2, 0.0], None, None, 8000)
    assert cmd["dash"] is True


def test_move_cmd_leaves_dash_false_without_gap(monkeypatch):
    from ai.primitives import move as M
    monkeypatch.setattr(M.gaps, "path_has_gap", lambda w, path: False)
    cmd = M._move_cmd(W, [(0, 0), (1, 0)], None, Blackboard(),
                      [1.2, 0.0], None, None, 8000)
    assert cmd["dash"] is False
```

在 `tests/test_gaps.py` 末尾加：

```python
def test_portals_nav_args_include_gap_jump():
    from ai.space import portals

    w = FakeWorld([(0.0, 2.4, 0.0, 2.4), (3.0, 5.4, 0.0, 2.4)])
    w.seam_gates = None
    w.portal_map = None
    w.lane_map = None
    w.belt_dir = {}
    nav = portals.nav_args(w)
    field = distance_field((2, 1), belt_dir={}, **nav)
    assert (3, 1) in field
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_exec_primitives.py::test_move_cmd_sets_dash_when_path_has_gap tests/test_exec_primitives.py::test_move_cmd_leaves_dash_false_without_gap tests/test_gaps.py::test_portals_nav_args_include_gap_jump -q`

Expected: 前两个 FAIL（`_move_cmd` 里还没有 `gaps`），第三个 FAIL（`nav_args` 还没接缝隙）。

- [ ] **Step 3: 接入 `portals.nav_args`**

在 `ai/space/portals.py` 顶部加 `from ai.space import gaps`；把 `nav_args` 的最后一个 `return` 改成：

```python
nav = lanes.wrap(nav, getattr(world, "lane_map", None), lanes.excluded_cells(world))
return gaps.wrap(nav, world)
```

注意 `gaps.wrap` 必须能处理 `nav["jumps"]`/`nav["jump_cost"]` 已被 `lanes._Jumps/_JumpCost` 包装过的情况。

- [ ] **Step 4: 接入 move 命令**

在 `ai/primitives/move.py` 顶部 import 里加 `gaps`：

```python
from ai.space import gaps, navigation, portals, seams, shuttle
```

`_move_cmd` 里把 `dash=False` 改成：

```python
dash=gaps.path_has_gap(w, path),
```

`GoToCell` 的纠偏 move 里同样把 `dash=False` 改成：

```python
dash=gaps.path_has_gap(w, path),
```

（`GoToCell` 的 `path` 是 `[[chef["x"], chef["z"]], point]`，世界点不是格心；如果该函数拿不到格路径，就保持 `dash=False`，只让 `Approach` 的格子路径负责跨缝。以测试和实际调用为准。）

- [ ] **Step 5: 跑测试确认通过**

Run: `python3 -m pytest tests/test_gaps.py tests/test_exec_primitives.py tests/test_navigation.py tests/test_portals.py -q`

Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add ai/space/portals.py ai/primitives/move.py tests/test_gaps.py tests/test_exec_primitives.py
git commit -m "feat(exec): 跨缝路径给 move 命令打 dash 标记"
```

---

### Task 5: 插件 `MoveCommand` 在缝隙前按 Dash

**Files:**
- Modify: `plugin/Oc2Executor.cs`（`MoveCommand`）

**Interfaces:**
- Consumes: `cmd["dash"]`（Task 4），`PlayerInputLookup.LogicalButtonID.Dash`（游戏程序集已确认存在）。
- Produces: 无 Python 接口变化；插件在 `dash=true` 且前方地面消失时按一次 Dash，并在所有退出口释放。

- [ ] **Step 1: 加字段和常量**

在 `MoveCommand` 常量区加：

```csharp
// 跨地板缝隙：前方探针距离、Dash 按住时长、冷却
private const float DASH_PROBE = 0.55f;
private const float DASH_HOLD = 0.08f;
private const float DASH_COOLDOWN = 1.0f;
```

在字段区加：

```csharp
private readonly bool _allowDash;
private bool _dashDown;
private float _dashUntil = -1f;
private float _lastDashAt = -10f;
```

构造函数里读：

```csharp
_allowDash = Req.Bool(cmd, "dash");
```

- [ ] **Step 2: 加地面探针和释放方法**

```csharp
private bool GroundGapAhead(ChefFrame f, Vector2 dir)
{
    if (dir.sqrMagnitude < 1e-6f) return false;
    Vector3 p = new Vector3(
        f.PosX + dir.x * DASH_PROBE,
        f.Chef.transform.position.y + ANCHOR_RAY_UP,
        f.PosZ + dir.y * DASH_PROBE);
    return !Physics.Raycast(p, Vector3.down, ANCHOR_RAY_LEN,
                            GROUND_LAYER_MASK, QueryTriggerInteraction.Ignore);
}

private void ReleaseDash()
{
    if (!_dashDown) return;
    _dashDown = false;
    _dashUntil = -1f;
    try { PadEngagement.Pad.Press(PlayerInputLookup.LogicalButtonID.Dash, false); }
    catch (System.Exception) { }
}
```

- [ ] **Step 3: 在 Tick 里触发和释放**

在 `Tick` 的每帧开头（`if (!f.Valid)` 之后）加：

```csharp
if (_dashDown && now >= _dashUntil) ReleaseDash();
```

把 `PadEngagement.Pad.Move(...)` 那段改成「先判前方地面，再决定走 / dash / 停」：

```csharp
bool gapAhead = GroundGapAhead(f, dir);
if (gapAhead && !_allowDash)
{
    // 没有 dash 授权却要踩空：宁可不走，交回 Python 重算，也不掉水。
    PadEngagement.Pad.Stop();
    UnityEngine.Debug.Log("[EXEC] move ledge pos=(" + f.PosX + "," + f.PosZ
        + ") idx=" + _idx + "/" + _path.Count);
    Finish(false, "ledge", new double[] { pos.x, pos.y });
    return;
}

PadEngagement.Pad.Move(f.Chef, dir.x * mag, dir.y * mag);

if (gapAhead && _allowDash && PadEngagement.Pad != null
    && now - _lastDashAt > DASH_COOLDOWN)
{
    try
    {
        PadEngagement.Pad.Press(PlayerInputLookup.LogicalButtonID.Dash, true);
        _dashDown = true;
        _dashUntil = now + DASH_HOLD;
        _lastDashAt = now;
        UnityEngine.Debug.Log("[EXEC] move dash pos=(" + f.PosX + "," + f.PosZ
            + ") idx=" + _idx + "/" + _path.Count);
    }
    catch (System.Exception e)
    {
        UnityEngine.Debug.Log("[EXEC] move dash failed: " + e.Message);
    }
}
```

在 `TickTeleport` 的 `PadEngagement.Pad.Stop()` 旁、以及 `Finish(...)` 里调用 `ReleaseDash()`。

- [ ] **Step 4: 检查所有退出口**

Run: `grep -n "Finish(\|Pad.Stop()\|ReleaseDash" plugin/Oc2Executor.cs`

Expected: 每条 `Finish(...)` 之前或 `Finish` 内部都有 `ReleaseDash()`；`TickTeleport` 的停止分支也有。不能出现「按了 Dash 但提前 return 没释放」的路径。

- [ ] **Step 5: 编译**

Run: `./plugin/build.sh`

Expected: 编译成功，生成 `plugin/build/Oc2AIPlugin.dll`。

- [ ] **Step 6: 跑插件相关 Python 测试**

Run: `python3 -m pytest tests/test_exec_commands.py tests/test_exec_primitives.py tests/test_gaps.py -q`

Expected: PASS（Python 侧仍会发 `dash` 字段）。

- [ ] **Step 7: Commit**

```bash
git add plugin/Oc2Executor.cs
git commit -m "feat(plugin): move 跨缝段按 Dash"
```

---

## Phase 3：洗盘及时触发

### Task 6: 2 盘关有 1 个脏盘时 WashPlate 升 L4

**Files:**
- Modify: `ai/core/planner.py`（新增 `_plate_total`）
- Modify: `ai/core/planner_layers.py:62-115`（`build_rules` 新增 `plate_total_fn`，改 L4 的 WashPlate 条件）
- Test: `tests/test_planner_layers.py`
- Test: `tests/test_wash_plate_now.py`

**Interfaces:**
- `_plate_total(world) -> int`：场上盘子总数 = `len(world.all_plates)` + 脏盘摞 item 数（`world.items` / `world.held_items` 中名字含 `dirtyplate`）+ 各洗碗池 `plate_count` 之和。
- `build_rules(pressure_fn, plate_pressure_fn, placed_plates_fn, usable_plate_fn=None, plate_total_fn=None)`：新增可选参数；缺省 `lambda w: 99`，旧调用/旧测试行为不变。
- L4 的 `WashPlate` 条件改成「**总共 2 块盘且至少 1 块不干净**」或旧的「0 干净盘且无装配盘」：

```python
(t.TYPE == "WashPlate"
 and ((plate_total_fn(w) == 2 and plate_pressure_fn(w) > 0)
      or (plate_pressure_fn(w) >= 2.0 and not placed_plates_fn(w))))
```

`plate_pressure > 0` 在 2 盘关卡里等价于「至少 1 块盘不干净」；`WashPlate` 候选本身只会在有脏盘时生成，所以不需要再注入一个 dirty 谓词。

- [ ] **Step 1: 写失败测试**

在 `tests/test_planner_layers.py` 里把 `_rules` 扩成：

```python
def _rules(pressure=0.0, plate_pressure=0.0, placed=False, plate_total=99):
    return PL.build_rules(lambda t, w: pressure, lambda w: plate_pressure,
                          lambda w: placed,
                          plate_total_fn=lambda w: plate_total)
```

加：

```python
def test_wash_two_plate_level_with_one_dirty_is_unblock():
    """2 盘关：只要不是两块都干净，wash 就进 L4。"""
    t = _T("WashPlate")
    assert PL.layer_of(t, None, rules=_rules(plate_pressure=1.0, plate_total=2)) == PL.L_UNBLOCK
    assert PL.layer_of(t, None, rules=_rules(plate_pressure=2.0, plate_total=2, placed=True)) == PL.L_UNBLOCK
    assert PL.layer_of(t, None, rules=_rules(plate_pressure=1.0, plate_total=3)) == PL.L_CLEANUP
    assert PL.layer_of(t, None, rules=_rules(plate_pressure=0.0, plate_total=2)) == PL.L_CLEANUP
```

在 `tests/test_wash_plate_now.py` 加：

```python
def test_plate_total_counts_clean_dirty_and_sink():
    w = _wash_world(plates=[("p1", 2, 3, ())], items=[_dirty()])
    assert planner._plate_total(w) == 2
    w.stations[0]["plate_count"] = 1
    w.items.clear()
    assert planner._plate_total(w) == 2   # 1 干净盘 + 1 池里的盘
```

把 `tests/test_wash_plate_now.py::test_wash_layer_only_when_plate_blocked` 改名为 `test_wash_layer_two_plate_level_or_blocked`，并把 w1 断言改成：

```python
# 2 盘关：1 块干净 + 1 块脏 → 也在 L4
assert PL.layer_of_task(wash, w1) == PL.L_UNBLOCK
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_planner_layers.py::test_wash_two_plate_level_with_one_dirty_is_unblock tests/test_wash_plate_now.py::test_plate_total_counts_clean_dirty_and_sink tests/test_wash_plate_now.py::test_wash_layer_only_when_plate_blocked -q`

Expected: FAIL（`plate_total_fn` 不存在，2 盘 1 脏仍是 L7）。

- [ ] **Step 3: 在 `planner.py` 加总数谓词**

在 `_plate_pressure` 旁边加：

```python
def _plate_total(world):
    # type: (Any) -> int
    """场上盘子总数：干净/装料/手持 + 洗碗池里的 + 脏盘摞。

    3-5 只有 2 块盘；这个数用来把「2 盘关有 1 个脏盘」直接抬进 L4。
    脏盘摞在 items 里、池里的盘只体现在 washing.plate_count，两者都要算。
    """
    total = len(world.all_plates)
    for it in list(world.items) + list(getattr(world, "held_items", ()) or ()):
        if "dirtyplate" in str(it.get("n") or "").lower():
            total += 1
    for st in getattr(world, "stations", ()) or ():
        if not isinstance(st, dict) or st.get("t") != "washing":
            continue
        try:
            total += int(st.get("plate_count") or 0)
        except (TypeError, ValueError):
            pass
    return total
```

- [ ] **Step 4: 改 `build_rules` 的 L4 规则**

`build_rules` 签名加 `plate_total_fn=None`，函数开头：

```python
plate_total_fn = plate_total_fn or (lambda w: 99)
```

L_UNBLOCK 的 `WashPlate` 分支改成：

```python
(t.TYPE == "WashPlate"
 and ((plate_total_fn(w) == 2 and plate_pressure_fn(w) > 0)
      or (plate_pressure_fn(w) >= 2.0 and not placed_plates_fn(w))))
```

`layer_of` 在 `rules is None` 时本地 import：

```python
from ai.core.planner import _plate_total
```

并把 `plate_total_fn=_plate_total` 传给 `build_rules`。`layer_of_task` 本来就在函数内 import planner，直接传 `plate_total_fn=planner._plate_total`。

`layer_of` 加参数时放在 `usable_plate_fn` 之后、`rules` 之前；现有调用全部用 `rules=` 关键字，不受影响。

同步更新 `ai/core/planner_layers.py` 的 L4 注释和 `tests/test_wash_plate_now.py` 顶部 docstring：现在 2 盘关 1 脏盘也进 L4，不再只靠「0 干净盘」。

- [ ] **Step 5: 跑测试确认通过**

Run: `python3 -m pytest tests/test_planner_layers.py tests/test_wash_plate_now.py tests/test_scoring.py tests/test_score_ordering.py tests/test_rescue_gate.py -q`

Expected: PASS。`test_rescue_gate` 的 3 块盘 + 脏盘世界仍走旧的「0 干净盘」分支，不应改变。

- [ ] **Step 6: Commit**

```bash
git add ai/core/planner.py ai/core/planner_layers.py tests/test_planner_layers.py tests/test_wash_plate_now.py
git commit -m "fix(planner): 2 盘关有脏盘时洗盘升 L4"
```

---

### Task 7: WashPlate 不再选「够不着的脏盘摞」

**Files:**
- Modify: `ai/tasks/plate.py`（新增 `dirty_stack_entry`，复用进 `WashPlate.feasible/build`）
- Test: `tests/test_wash_plate_now.py`

**Interfaces:**
- `dirty_stack_entry(ctx) -> Optional[Dict]`：优先返回与脏盘摞同位置的 `plate_return` 站点，否则返回脏盘 item；没有返回 `None`。
- `WashPlate.feasible(ctx)`：有 `ctx["field"]` 且 `dirty_stack_entry` 非空时，用 `reachability.can_interact(ctx, entry)` 判脏盘摞可达；不可达返回 `False`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_wash_plate_now.py` 加：

```python
def test_wash_infeasible_when_dirty_stack_unreachable(monkeypatch):
    """脏盘摞在对面木筏、field 够不着时，不要选 WashPlate 空跑。"""
    from ai.space import reachability
    w = _wash_world(plates=[], items=[_dirty()])
    ctx = _ctx(w)
    ctx["field"] = {}
    monkeypatch.setattr(
        reachability, "can_interact",
        lambda c, entry: entry.get("t") == "washing")
    assert T.WashPlate(resources=("sink_01",)).feasible(ctx) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_wash_plate_now.py::test_wash_infeasible_when_dirty_stack_unreachable -q`

Expected: FAIL（当前 `feasible` 不查脏盘摞可达性，返回 True）。

- [ ] **Step 3: 重构脏盘目标**

把 `WashPlate.build` 里的内嵌 `dirty(c)` 提成模块级函数：

```python
def dirty_stack_entry(ctx):
    # type: (Dict) -> Optional[Dict]
    """脏盘摞的执行目标：优先同位置的 plate_return 站点，否则脏盘 item。"""
    stack = None
    for it in ctx["w"].items:
        if "dirtyplate" in str(it.get("n") or "").lower():
            stack = it
            break
    if stack is None:
        return None
    sx, sz = stack.get("x"), stack.get("z")
    if sx is None or sz is None:
        return stack
    for st in ctx["w"].stations:
        if not isinstance(st, dict) or st.get("t") != "plate_return":
            continue
        if "drying" in str(st.get("n") or "").lower():
            continue
        if st.get("gx") is None or st.get("gz") is None:
            continue
        if abs(float(st.get("x", 1e9)) - float(sx)) < 0.7 and \
                abs(float(st.get("z", 1e9)) - float(sz)) < 0.7:
            return st
    return stack
```

`build` 里的 `dirty` 直接 `return dirty_stack_entry(c)`。

- [ ] **Step 4: feasible 加可达性**

在 `WashPlate.feasible` 的 sink 可达性检查之后加：

```python
# 池里已经有盘时，直接洗池子即可，不需要够得着脏盘摞；只有「要去捡脏盘」
# 这条路才要求脏盘摞可达，否则会选一个空跑的 WashPlate。
dirty = dirty_stack_entry(ctx)
if (dirty is not None and sink_plate_count(ctx, self.sink_id) <= 0
        and ctx.get("field")):
    from ai.space import reachability
    if not reachability.can_interact(ctx, dirty):
        return False
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python3 -m pytest tests/test_wash_plate_now.py tests/test_scoring.py tests/test_handover.py tests/test_planner_candidates.py -q`

Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add ai/tasks/plate.py tests/test_wash_plate_now.py
git commit -m "fix(tasks): WashPlate 跳过够不着的脏盘摞"
```

---

## Phase 4：整体验证

### Task 8: 全量回归 + 实机 3-5 验证

**Files:** 无新增/修改；只跑命令。

- [ ] **Step 1: 跑全部 Python 测试**

Run: `python3 -m pytest tests/ -q`

Expected: 全部 PASS；若有与本计划无关的既有失败，单独记录，不顺手修。

- [ ] **Step 2: 编译插件**

Run: `./plugin/build.sh`

Expected: 编译成功。

- [ ] **Step 3: 部署并实机验证（需要游戏在主菜单）**

Run: `./restart.sh`

验证清单：
- 进入 `Rapids_3_5_2P` 后，语音/攻略不再要求固定分工；trace 的 `dialog.division` 应为空（或只有 `both`），不应出现 `you: [cook, plate] / player: [chop]`。
- trace 里不应再出现由木筏巡航造成的 `division_review`（真实站点搬家仍可出现）。
- 插件日志出现 `[EXEC] move dash`，且 P2 不再频繁 `respawning`；玩家拿着盘跨缝时不应再掉水。
- `[EXEC] move ledge` 只应在真正的边缘出现；如果每几秒就出现一次，说明 `DASH_PROBE` 太大或路径贴边，先调小探针再继续。
- 2 块盘、其中 1 块脏时，trace 的 WashPlate 候选 `layer` 应为 4，而不是 7；池子应在下一单之前被洗。
- 3 块盘以上的关卡（如 `test_rescue_gate` 的世界）仍按原来的「0 干净盘才进 L4」，不应因为本改动提前抢 wash。
- 全量测试与编译命令仍通过。

- [ ] **Step 4: Commit（只有测试/文档有改动时才提交）**

```bash
git status --short
# 若无本次任务产生的未提交文件，跳过
git add <本次新增的测试或文档>
git commit -m "test: 3-5 dash/wash/division 回归"
```

---

## Out of Scope / Follow-ups

- `layout_review` 在 3-5 仍会随木筏巡航发「路换方向了，我守左边/右边」；本计划只去掉 `division_review` 的误触发。是否对「载有工作站的移动网格」整体关闭 `layout_review`，需要产品决定，建议下一步单独评估。
- `WEIGHT_CLAIM = -6.0` 是否降低：本计划通过「不设分工」绕开；如果要保留固定分工，需另做权重标定。
- `SendAcross(dirty_plate)` 的落点/进池率：本计划只保证 WashPlate 不选够不着的脏盘摞；跨区送盘是否稳定进池，建议用下一局 trace 单独看。

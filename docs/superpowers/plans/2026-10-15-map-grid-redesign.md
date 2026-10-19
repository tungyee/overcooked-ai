# 地图与网格表示重构 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 `Balloon_1_5_2P` 和 `1_6_Dynamic_Lvl_01_2P` 上的地图表示错误，让任意关卡无需加表、无需改码。

**Architecture:** 分两轨：**Track A（Python，TDD）**改 Python 侧 `ai/` 以消耗新协议字段并删除关卡硬编码；**Track B（插件，C#，人工验证）**改 `plugin/Oc2AIPlugin.cs` 以输出新字段。两轨按依赖顺序排列：Task 0（已完成，已在仓库）→ Task 1 提取协议无关的 ground-truth 数据 → Task 2-6 搭纯函数新模块（不改旧代码，可独立测试）→ Task 4 用 Task 1 的数据验证 Task 3 的可走性判定 → Task 7-8（插件改写 + 实机验证，产出新协议字段）→ Task 9 用新插件录制真正的新协议 fixture → Task 10（`World` 切到新协议）→ Task 11-12（资源 id 两步切换：查找函数先兼容、候选生成再切换）→ Task 13（原语层 reset 修复）→ Task 14-16（trace/删表/跨网格寻路）→ Task 17-18（文档同步 + 全量实机验收）。

**Tech Stack:** Python 3.9.6、pytest 7.4.4、C# .NET（BepInEx 插件，Roslyn `./plugin/build.sh` 编译），无第三方库。

**Spec:** `docs/superpowers/specs/2026-10-15-map-grid-redesign-design.md`

## Global Constraints

- Python 3.9.6；类型注解用 `# type: (...)  -> ...` 注释形式，不用 PEP 526 内联注解。
- 注释/文档用中文；标识符用英文。
- 谓词一律无副作用；纯函数不 import `World` 类。
- 单文件 ≤ 400 行（spec §11.5）。
- 每个 task 提交前跑 `python3 -m pytest tests/`，保证当前已有测试不新增失败。
- 插件改完须 `./plugin/build.sh` 无 error；人工进关卡验证 `[STATIC] built` 行。
- 不破坏现有 `(resource_id="n_string", action)` 认领键格式，直到 Task 11/12 切换完成（Task 11 的查找函数向后兼容裸字符串，Task 12 才真正开始产出 `(grid,name)` 元组）。
- 传输字段命名：`grids` / `terrain` / `occupancy` / `belts` / `hazards`（spec §6）。

## Review Focus

1. **多网格坐标撞车**：`to_grid(x=12.0, z=-3.6)` 在左网格(origin 9.6) 和右网格(origin 19.6)给出不同格，任何仍用全局 gx/gz 比较的代码会匹配错对象。测试：Task 2 `test_gridset_to_world_uses_correct_origin_per_grid`、Task 12 `test_same_name_different_grid_cookers_are_independent_resources`。
2. **`stations` 坐标在关内阶段切换后仍是旧快照**：原 `BuildStatic()` 把站点坐标连同身份一起烘进 `_staticJson`，只在换关或旧 `RefreshLevel()` 触发时重建，1-6 关内阶段切换不改关卡名，新厨房的站点进不来。Task 7 Step 11 把 `stations` 整体改成每帧重扫（不缓存），从根上解决，不依赖任何"结构跳变阈值"猜测。验证：Task 8 的实机验证步骤（C# 层无自动化测试，靠坠毁前后 `stations` 数量变化的实机确认）。
3. **传送带方向测向错误**：`Travelator.m_directionXZ` 枚举报 `Rightwards` 但实际方向需乘 rotation（§4.5）；必须用 `GetSurfaceVelocity()` 不能用枚举。测试：Task 5 `test_parse_belts_direction_from_velocity`；插件侧对应 Task 7 Step 8 的人工验证（1-4 实机核对 §4.5 的方向表）。
4. **belt_cells 全局坐标系假设**：`navigation.interact_cells` 和 `primitives/move.py` 里 `cur in w.belt_cells` 用的是主网格坐标，右侧网格的传送带（如果存在）会被漏掉。本设计在 1-4 / 1-5 没有右侧传送带，暂不修——记录为 Task 16 结尾的已知限制（跨网格寻路未接入执行层），不在本计划验收范围内。
5. **Approach reset 在平台移动时丢进度**：`cell != self._target_cell` 用坐标比较，而移动平台上的站点每帧坐标变化 → 每帧 reset。Task 13 改为按站点身份比较。测试：Task 13 `test_approach_does_not_reset_motion_when_only_coordinates_move`。

---

## Task 0 (DONE): 插件两处 bug fix（已提交 commit `81a1d43`）

`IsInLevel()` 改为 `ReadLevel() != null`；`BuildStatic()` 配方表查找改用 `IRecipeListCache` 接口。**不需要实施，已在仓库。**

---

## Task 1: 提取 P1 ground-truth 轨迹（协议无关，供 Task 4 回归用）

**重要的依赖说明**：已录制的原始帧（`/Users/dongyi/.claude/jobs/d0654c72/tmp/
balloon15-*.ndjson`）全部出自**旧插件协议**——没有 `grids`/`terrain`/
`occupancy`/`hazards`/`belts` 字段（这些要等 Task 7 插件改完才会出现）。
把这些帧原样存成"新协议 fixture"会造成误导——测试会以为自己在验证新协议，
实际读到的字段全是 `None`。

所以本任务只提取**协议无关**的部分：P1 走过的世界坐标序列。这是 spec §8
测试 1（ground-truth 回归，"人走过的格必然可走"）真正需要的输入，且这份
数据不随协议改变而失效。**真正的新协议 fixture（含 terrain/occupancy 等）
要等 Task 7/8 插件部署后由 Task 9 重新录制**，那时再补一份 `.ndjson.gz` 入库。

**Files:**
- Create: `tests/fixtures/balloon_1_5_p1_track.json`（P1 位置列表，纯数据）
- Test: 无独立测试文件，Task 4 直接消费这份 fixture

**Interfaces:**
- Produces: JSON 文件，格式 `[[x, z], [x, z], ...]`

- [ ] **Step 1: 从原始录制提取 P1 轨迹**

```bash
cd /Users/dongyi/Documents/coding/oc2
python3 -c "
import json
F = [json.loads(l) for l in open('/Users/dongyi/.claude/jobs/d0654c72/tmp/balloon15-frames.ndjson')]
track = []
for f in F:
    for c in f.get('chefs') or []:
        if c.get('name') == 'Player 1' and c.get('x') is not None and c.get('z') is not None:
            track.append([c['x'], c['z']])
print('P1 positions:', len(track))
assert len(track) > 500, '应接近 601（spec §1 的 ground-truth 数）'
with open('tests/fixtures/balloon_1_5_p1_track.json', 'w') as out:
    json.dump(track, out)
"
```

Expected: 打印 `P1 positions: 601`（或接近值），文件写出成功。

- [ ] **Step 2: 验证文件内容**

```bash
python3 -c "
import json
track = json.load(open('tests/fixtures/balloon_1_5_p1_track.json'))
assert isinstance(track, list) and len(track) > 500
assert all(isinstance(p, list) and len(p) == 2 for p in track[:5])
print('OK', len(track))
"
```

Expected: `OK 601`（或接近值），无异常。

- [ ] **Step 3: 提交**

```bash
git add tests/fixtures/balloon_1_5_p1_track.json
git commit -m "test(fixtures): 提取气球关 P1 ground-truth 轨迹（协议无关）

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 2: `GridSet`——多网格坐标模型（新增，不改旧代码）

**Files:**
- Modify: `ai/geometry.py`（新增 `GridSet` 类，`GridMapping` 保留不删）
- Test: `tests/test_geometry.py`

**Interfaces:**
- Produces: `GridSet.from_frame(grids_list) -> GridSet`；
  `GridSet.to_world(grid_id, gx, gz) -> Optional[Tuple[float,float]]`；
  `GridSet.to_grid(grid_id, x, z) -> Optional[Tuple[int,int]]`；
  `GridSet.cell_size(grid_id) -> float`；`GridSet.ids() -> List[str]`

- [ ] **Step 1: 写失败测试（§4.1 的两网格真实数据）**

```python
# tests/test_geometry.py 追加
from ai.geometry import GridSet

BALLOON_GRIDS = [
    {"id": "GridManager", "ox": 9.6, "oz": -2.4, "cell": 1.2},
    {"id": "Balloon Basket", "ox": 19.6, "oz": -2.4, "cell": 1.2},
]


def test_gridset_from_frame_builds_both_grids():
    gs = GridSet.from_frame(BALLOON_GRIDS)
    assert set(gs.ids()) == {"GridManager", "Balloon Basket"}


def test_gridset_to_world_uses_correct_origin_per_grid():
    gs = GridSet.from_frame(BALLOON_GRIDS)
    # 料箱 DispenserCrate 3 (1) 局部 g(2,-1) -> 世界 (22.0, -3.6)（§4.2 实测）
    assert gs.to_world("Balloon Basket", 2, -1) == pytest.approx((22.0, -3.6))
    # 同样的局部坐标在主网格是另一个世界点——证明两网格不共享坐标
    assert gs.to_world("GridManager", 2, -1) == pytest.approx((12.0, -3.6))


def test_gridset_to_grid_is_inverse_per_grid():
    gs = GridSet.from_frame(BALLOON_GRIDS)
    x, z = gs.to_world("Balloon Basket", 2, -1)
    assert gs.to_grid("Balloon Basket", x, z) == (2, -1)


def test_gridset_unknown_grid_returns_none():
    gs = GridSet.from_frame(BALLOON_GRIDS)
    assert gs.to_world("NoSuchGrid", 0, 0) is None
    assert gs.to_grid("NoSuchGrid", 0.0, 0.0) is None


def test_gridset_from_empty_frame_is_empty():
    gs = GridSet.from_frame(None)
    assert gs.ids() == []
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_geometry.py -k gridset -v`
Expected: FAIL，`ImportError: cannot import name 'GridSet'`

- [ ] **Step 3: 实现 `GridSet`**

```python
# ai/geometry.py 追加（文件末尾，GridMapping 类之后）

class GridSet(object):
    """多网格坐标集合（spec §5.1）：每个网格有自己的原点与格宽。

    与 GridMapping 的关系：GridMapping 是单网格模型（靠站点反解原点），
    GridSet 是多网格模型（原点由插件直接给出，不反解）。换关卡/换帧时
    直接从 state["grids"] 重建，零猜测。

    局部坐标 (gx, gz) 对同一个格子在不同网格下代表不同世界位置——
    这正是 §4.1 的核心发现：两个网格原点相差 10.0，不能共用一套坐标。
    """

    def __init__(self, grids):
        # type: (Dict[str, Dict[str, float]]) -> None
        self._grids = grids

    @classmethod
    def from_frame(cls, grids_list):
        # type: (Optional[List[Dict]]) -> "GridSet"
        out = {}  # type: Dict[str, Dict[str, float]]
        for g in grids_list or ():
            if not isinstance(g, dict):
                continue
            gid = g.get("id")
            if not gid:
                continue
            out[str(gid)] = {
                "ox": float(g.get("ox", 0.0) or 0.0),
                "oz": float(g.get("oz", 0.0) or 0.0),
                "cell": float(g.get("cell", CELL_DEFAULT) or CELL_DEFAULT),
            }
        return cls(out)

    def ids(self):
        # type: () -> List[str]
        return list(self._grids.keys())

    def cell_size(self, grid_id):
        # type: (str) -> float
        g = self._grids.get(str(grid_id))
        return g["cell"] if g else CELL_DEFAULT

    def to_world(self, grid_id, gx, gz):
        # type: (str, int, int) -> Optional[Tuple[float, float]]
        g = self._grids.get(str(grid_id))
        if g is None:
            return None
        return (g["ox"] + g["cell"] * gx, g["oz"] + g["cell"] * gz)

    def to_grid(self, grid_id, x, z):
        # type: (str, float, float) -> Optional[Tuple[int, int]]
        g = self._grids.get(str(grid_id))
        if g is None:
            return None
        return (int(round((x - g["ox"]) / g["cell"])),
                int(round((z - g["oz"]) / g["cell"])))
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_geometry.py -v`
Expected: 全部 PASS（新增 5 条 + 原有不受影响）

- [ ] **Step 5: 提交**

```bash
git add ai/geometry.py tests/test_geometry.py
git commit -m "feat(geometry): 新增 GridSet 多网格坐标模型

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---
## Task 3: 层号地形解析 + 新可走性判定函数（新增，不改 `make_walkable`）

**Files:**
- Modify: `ai/occupancy.py`（新增函数，`make_walkable`/`BELTS`/`playable_bounds` 保留不删，Task 9 再删）
- Test: `tests/test_occupancy.py`

**Interfaces:**
- Consumes: `GridMapping`（Task 2 已有，用于 `to_world`）
- Produces: `parse_terrain(terrain_list) -> Dict[int, List[Tuple[float,float,float,float]]]`；
  `make_walkable_from_terrain(terrain_by_layer, hazard_boxes, occupancy_cells, mapping) -> Callable[[int,int],bool]`

关键设计决定（spec §5.3 没写到这个细节，这里补全）：**地形数据缺失时整体放行**，
不是"没有 Ground 层数据就全不可走"。理由与旧 `nodeMap is None -> True` 同一个
精神——没有数据时宁可走错也不要让 AI 整体瘫痪，且现有大量单测用合成帧（无
`terrain` 字段）构造 `World`，若默认全不可走会炸穿一大片不相关测试。只有当
某层的 box 列表**非空**时才启用该层的排除判定。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_occupancy.py 追加
from ai.geometry import GridMapping
from ai.occupancy import parse_terrain, make_walkable_from_terrain

MAPPING = GridMapping(cell=1.2, off_x=8, off_z=6, origin_x=9.6, origin_z=-2.4)


def test_parse_terrain_groups_by_layer():
    raw = [
        {"layer": 9, "boxes": [[3.6, 15.6, -8.6, 3.4]]},
        {"layer": 10, "boxes": [[0.0, 1.0, 0.0, 1.0], [2.0, 3.0, 2.0, 3.0]]},
    ]
    got = parse_terrain(raw)
    assert got[9] == [(3.6, 15.6, -8.6, 3.4)]
    assert got[10] == [(0.0, 1.0, 0.0, 1.0), (2.0, 3.0, 2.0, 3.0)]


def test_parse_terrain_skips_malformed_boxes():
    raw = [{"layer": 9, "boxes": [[1, 2, 3], None, "junk"]}]
    assert parse_terrain(raw) == {}


def test_walkable_blocks_outside_ground():
    """格中心不在任何 Ground AABB 内 -> 不可走（§5.3 第 1 步）。"""
    terrain = {9: [(0.0, 2.4, 0.0, 2.4)]}   # 覆盖 gx 0..2, gz 2..4（世界坐标）
    is_walkable = make_walkable_from_terrain(terrain, [], set(), MAPPING)
    # 格 (0,0) -> 世界 (9.6, -2.4)，在 ground box 外
    assert is_walkable(0, 0) is False


def test_walkable_permissive_when_no_terrain_data():
    """地形数据整体缺失（合成帧常见）-> 不拦，不能让现有测试全炸。"""
    is_walkable = make_walkable_from_terrain({}, [], set(), MAPPING)
    assert is_walkable(0, 0) is True
    assert is_walkable(100, 100) is True


def test_walkable_blocks_killplane():
    terrain = {9: [(-100.0, 100.0, -100.0, 100.0)], 30: [(9.0, 10.2, -3.0, -1.8)]}
    is_walkable = make_walkable_from_terrain(terrain, [], set(), MAPPING)
    assert is_walkable(0, 0) is False   # 世界 (9.6,-2.4) 落在 killplane box 内


def test_walkable_blocks_wall_layers():
    terrain = {
        9: [(-100.0, 100.0, -100.0, 100.0)],
        10: [(9.0, 10.2, -3.0, -1.8)],
    }
    is_walkable = make_walkable_from_terrain(terrain, [], set(), MAPPING)
    assert is_walkable(0, 0) is False


def test_walkable_blocks_hazard_boxes():
    terrain = {9: [(-100.0, 100.0, -100.0, 100.0)]}
    hazard_boxes = [(9.0, 10.2, -3.0, -1.8)]
    is_walkable = make_walkable_from_terrain(terrain, hazard_boxes, set(), MAPPING)
    assert is_walkable(0, 0) is False


def test_walkable_blocks_occupancy_cells():
    terrain = {9: [(-100.0, 100.0, -100.0, 100.0)]}
    is_walkable = make_walkable_from_terrain(terrain, [], {(0, 0)}, MAPPING)
    assert is_walkable(0, 0) is False
    assert is_walkable(1, 0) is True
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_occupancy.py -k "terrain or walkable_from_terrain or walkable_blocks or walkable_permissive" -v`
Expected: FAIL，`ImportError`

- [ ] **Step 3: 实现**

```python
# ai/occupancy.py 追加（文件末尾）

LAYER_GROUND = 9
LAYER_WALLS = 10
LAYER_PLATEBLOCK = 20
LAYER_COOKINGBLOCK = 22
LAYER_TABLEBLOCK = 26
LAYER_KILLPLANE = 30
BLOCKING_LAYERS = (LAYER_WALLS, LAYER_TABLEBLOCK, LAYER_COOKINGBLOCK, LAYER_PLATEBLOCK)


def parse_terrain(terrain_list):
    # type: (Optional[List[Dict]]) -> Dict[int, List[Tuple[float, float, float, float]]]
    """按层号归类 AABB 列表（spec §4.4/§6）：{layer: [(minx,maxx,minz,maxz), ...]}。

    层号跨关稳定（Unity 工程配置，不是场景实例名），取代 Col_* 名字前缀。
    """
    out = {}  # type: Dict[int, List[Tuple[float, float, float, float]]]
    for entry in terrain_list or ():
        if not isinstance(entry, dict):
            continue
        layer = entry.get("layer")
        if layer is None:
            continue
        try:
            layer_i = int(layer)
        except (TypeError, ValueError):
            continue
        boxes = []  # type: List[Tuple[float, float, float, float]]
        for b in entry.get("boxes") or ():
            if not isinstance(b, (list, tuple)) or len(b) != 4:
                continue
            try:
                boxes.append(tuple(float(v) for v in b))
            except (TypeError, ValueError):
                continue
        if boxes:
            out.setdefault(layer_i, []).extend(boxes)
    return out


def _point_in_any_box(x, z, boxes):
    # type: (float, float, List[Tuple[float, float, float, float]]) -> bool
    for minx, maxx, minz, maxz in boxes:
        if minx <= x <= maxx and minz <= z <= maxz:
            return True
    return False


def make_walkable_from_terrain(terrain_by_layer, hazard_boxes, occupancy_cells, mapping):
    # type: (Dict[int, List[Tuple]], List[Tuple[float,float,float,float]], Set[Tuple[int,int]], GridMapping) -> Callable[[int, int], bool]
    """按 §5.3 判定顺序构造 is_walkable(gx,gz)。替代 `make_walkable`。

    判定顺序：
      1. 不在任意 Ground(9) AABB 内 -> 不可走（**该层数据非空才启用**，
         数据缺失时放行——理由见本任务开头的"关键设计决定"）
      2. 在 KillPlane(30) 内 -> 不可走
      3. 在任意 hazard box 内 -> 不可走（落火，§5.7）
      4. 在 Walls/TableBlock/CookingStationBlock/PlateStationBlock 内 -> 不可走
      5. 在 occupancy_cells 里 -> 不可走（运行时占用，§5.3 第 5 步）
      6. 否则可走
    """
    ground = terrain_by_layer.get(LAYER_GROUND) or []
    killplane = terrain_by_layer.get(LAYER_KILLPLANE) or []
    blocking = []  # type: List[Tuple[float, float, float, float]]
    for layer in BLOCKING_LAYERS:
        blocking.extend(terrain_by_layer.get(layer) or ())
    hazard_boxes = list(hazard_boxes or ())

    def is_walkable(gx, gz):
        # type: (int, int) -> bool
        x, z = mapping.to_world(gx, gz)
        if ground and not _point_in_any_box(x, z, ground):
            return False
        if _point_in_any_box(x, z, killplane):
            return False
        if _point_in_any_box(x, z, hazard_boxes):
            return False
        if _point_in_any_box(x, z, blocking):
            return False
        if (gx, gz) in occupancy_cells:
            return False
        return True

    return is_walkable
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_occupancy.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add ai/occupancy.py tests/test_occupancy.py
git commit -m "feat(occupancy): 新增层号地形解析与可走性判定（§5.3）

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 4: ground-truth 回归测试——用气球关真实地形数据构造 walkable

本任务**不等 Task 7 插件改完**：用 §4 实测的真实 terrain 数据（手工从 spec
的实测记录转录），独立验证 Task 3 的 `make_walkable_from_terrain` 在真实场
景下对 P1 轨迹的命中率。这是 spec 测试 1 的核心断言，当前 baseline 是
32.6%（§1），目标 100%。

**Files:**
- Modify: `tests/test_occupancy.py`
- Test: 同上

**Interfaces:**
- Consumes: `tests/fixtures/balloon_1_5_p1_track.json`（Task 1）、
  `make_walkable_from_terrain`（Task 3）

- [ ] **Step 1: 写失败测试（真实地形数据手工转录自 §4.3/§4.4）**

```python
# tests/test_occupancy.py 追加
import json
import os

BALLOON_P1_TRACK = os.path.join(FIXTURES, "..", "balloon_1_5_p1_track.json")


def _load_p1_track():
    path = os.path.join(os.path.dirname(__file__), "fixtures", "balloon_1_5_p1_track.json")
    if not os.path.exists(path):
        pytest.skip("缺 balloon_1_5_p1_track.json，先跑 Task 1 的提取脚本")
    return json.load(open(path))


# 气球关两块地板的真实 AABB（spec §4.3 实测，世界坐标）：
# 主地板 x[3.6,15.6]，篮子地板 x[16.0,23.2]，z 向在篮子三个停靠位置下平移
# ±4.9（§4.2）。为覆盖篮子移动的全部位置，Ground 层给三个并集框。
BALLOON_GROUND_BOXES = [
    (3.6, 15.6, -8.6, 3.4),     # 主地板（恒定）
    (16.0, 23.2, -8.6 - 4.9, 3.4 + 0.1),  # 篮子地板，覆盖 BOT..TOP 全部位置
]


def test_ground_truth_p1_track_fully_walkable():
    """spec §8 测试 1：P1 走过的每个真实位置必须可走。baseline 32.6% -> 目标 100%。"""
    track = _load_p1_track()
    mapping = GridMapping(cell=1.2, off_x=8, off_z=6, origin_x=9.6, origin_z=-2.4)
    terrain = {9: BALLOON_GROUND_BOXES}
    is_walkable = make_walkable_from_terrain(terrain, [], set(), mapping)

    hit, total = 0, 0
    misses = []
    for x, z in track:
        gx, gz = mapping.to_grid(x, z)
        total += 1
        if is_walkable(gx, gz):
            hit += 1
        else:
            misses.append((round(x, 1), round(z, 1), gx, gz))

    rate = hit / total
    assert rate == 1.0, "命中率 %.1f%%，未命中样本（前10个）：%s" % (
        rate * 100, misses[:10])
```

- [ ] **Step 2: 运行确认失败或通过**

Run: `python3 -m pytest tests/test_occupancy.py -k ground_truth -v`
Expected: 若有未命中，输出显示具体 miss 坐标——这是真实的调试信号，不是
"预期失败"。按 miss 列表调整 `BALLOON_GROUND_BOXES`（气球关的真实地板范围
在 §4.2/§4.3 已有记录，若测试仍不过，说明记录的 box 范围不够全，需要回去
看原始录制帧 `/Users/dongyi/.claude/jobs/d0654c72/tmp/balloon15-v2.ndjson`
里 `Collision (6)`/`Collision (13)` 的真实 AABB 补全——不要放宽判定逻辑
本身去凑过测试。

- [ ] **Step 3: 确认通过后运行完整文件**

Run: `python3 -m pytest tests/test_occupancy.py -v`
Expected: 全部 PASS

- [ ] **Step 4: 提交**

```bash
git add tests/test_occupancy.py
git commit -m "test(occupancy): ground-truth 回归——气球关 P1 轨迹 100% 可走（§1/§8）

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---
## Task 5: hazard 与 belt 的纯函数解析（新增）

**Files:**
- Modify: `ai/occupancy.py`
- Test: `tests/test_occupancy.py`

**Interfaces:**
- Produces: `parse_hazards(hazards_list) -> List[Dict]`（每项含 `grid`/`x`/`z`/`ttl`）；
  `hazard_boxes(hazards, cell=1.2) -> List[Tuple[float,float,float,float]]`；
  `parse_belts(belts_list) -> Tuple[Set[Tuple[int,int]], Dict[Tuple[int,int], Tuple[float,float]]]`

`hazard_boxes` 的半径选择（本任务新增的实现细节，spec 未定数值）：每个 hazard
给一个边长 = 1 个格宽的正方形框（`x±cell/2, z±cell/2`）。依据 §4.7：12 个落点
11 个精确对齐格心，用整格近似误差最小；没有拿到 collider 半径的实机数据，
这是本计划引入的工程假设，标注在风险表（Task 13 验收时如有新数据再调）。

`parse_belts` 返回 `(cells, dir_map)`，`dir_map` 的值是 `GetSurfaceVelocity()`
的 `(vx, vz)` 归一化方向（不是速度大小）——与现有 `belt_dir` 的值域对齐，
Task 10 接入 `occupancy.belt_cells_and_dirs` 时据此替换。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_occupancy.py 追加
from ai.occupancy import parse_hazards, hazard_boxes, parse_belts


def test_parse_hazards_basic():
    raw = [{"t": "fire", "grid": "GridManager", "x": 13.2, "z": -2.4, "ttl": 11.0}]
    got = parse_hazards(raw)
    assert got == [{"grid": "GridManager", "x": 13.2, "z": -2.4, "ttl": 11.0}]


def test_parse_hazards_skips_expired_and_malformed():
    raw = [{"t": "fire", "x": 1.0, "z": 1.0, "ttl": 0.0},   # ttl<=0 已过期
           {"t": "fire", "x": None, "z": 1.0, "ttl": 5.0},   # 缺坐标
           "junk"]
    assert parse_hazards(raw) == []


def test_hazard_boxes_one_cell_square():
    hz = [{"grid": "G", "x": 10.0, "z": 10.0, "ttl": 5.0}]
    boxes = hazard_boxes(hz, cell=1.2)
    assert boxes == [(9.4, 10.6, 9.4, 10.6)]


def test_parse_belts_direction_from_velocity():
    """§4.5：方向用 vx/vz 归一化，不信任任何枚举字段。"""
    raw = [{"grid": "GridManager", "gx": -4, "gz": 2, "vx": 2.5, "vz": 0.0},
           {"grid": "GridManager", "gx": -4, "gz": -3, "vx": -2.5, "vz": 0.0}]
    cells, dirs = parse_belts(raw)
    assert cells == {(-4, 2), (-4, -3)}
    assert dirs[(-4, 2)] == (1, 0)
    assert dirs[(-4, -3)] == (-1, 0)


def test_parse_belts_empty_on_zero_velocity():
    raw = [{"grid": "G", "gx": 0, "gz": 0, "vx": 0.0, "vz": 0.0}]
    cells, dirs = parse_belts(raw)
    assert cells == {(0, 0)}
    assert dirs == {}   # 方向不可定义，但格仍可走（belt_cells 独立于 dir）
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_occupancy.py -k "hazard or parse_belts" -v`
Expected: FAIL，`ImportError`

- [ ] **Step 3: 实现**

```python
# ai/occupancy.py 追加

def parse_hazards(hazards_list):
    # type: (Optional[List[Dict]]) -> List[Dict]
    """落火列表（spec §4.7/§6）：{grid, x, z, ttl}。ttl<=0 或缺坐标丢弃。"""
    out = []  # type: List[Dict]
    for h in hazards_list or ():
        if not isinstance(h, dict):
            continue
        x, z, ttl = h.get("x"), h.get("z"), h.get("ttl")
        if x is None or z is None or ttl is None:
            continue
        try:
            ttl_f = float(ttl)
        except (TypeError, ValueError):
            continue
        if ttl_f <= 0:
            continue
        out.append({"grid": h.get("grid"), "x": float(x), "z": float(z), "ttl": ttl_f})
    return out


def hazard_boxes(hazards, cell=1.2):
    # type: (List[Dict], float) -> List[Tuple[float, float, float, float]]
    """每个 hazard 给一个边长 = cell 的正方形框（工程假设，见本任务说明）。"""
    half = cell / 2.0
    return [(h["x"] - half, h["x"] + half, h["z"] - half, h["z"] + half)
            for h in hazards]


def parse_belts(belts_list):
    # type: (Optional[List[Dict]]) -> Tuple[Set[Tuple[int, int]], Dict[Tuple[int, int], Tuple[int, int]]]
    """传送带格集合与方向（spec §5.4）：方向来自 GetSurfaceVelocity() 归一化。

    取代 occupancy.BELTS 关卡硬编码表。vx/vz 都是 0（速度为零，异常数据）
    时该格仍算传送带（belt_cells 决定"是不是传送带"，不决定推力方向），
    但不记方向——navigation 的逆向代价判定对无方向格天然不生效。
    """
    cells = set()  # type: Set[Tuple[int, int]]
    dirs = {}      # type: Dict[Tuple[int, int], Tuple[int, int]]
    for b in belts_list or ():
        if not isinstance(b, dict):
            continue
        gx, gz = b.get("gx"), b.get("gz")
        if gx is None or gz is None:
            continue
        cell = (int(gx), int(gz))
        cells.add(cell)
        vx, vz = b.get("vx"), b.get("vz")
        if vx is None or vz is None:
            continue
        try:
            vx_f, vz_f = float(vx), float(vz)
        except (TypeError, ValueError):
            continue
        if vx_f == 0.0 and vz_f == 0.0:
            continue
        # 归一化到单位方向（±1,0)/(0,±1)：当前只有轴向传送带样本（§4.5），
        # 斜向传送带会给出非整数，调用方（navigation）按符号取整。
        dirs[cell] = (
            (1 if vx_f > 0 else -1) if vx_f != 0 else 0,
            (1 if vz_f > 0 else -1) if vz_f != 0 else 0,
        )
    return cells, dirs
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_occupancy.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add ai/occupancy.py tests/test_occupancy.py
git commit -m "feat(occupancy): hazard 与 belt 的数据驱动解析（§5.4/§5.7）

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---
## Task 6: 资源 id 构造函数（新增，不改调用方）

**Files:**
- Modify: `ai/predicates.py`（新增纯函数，无副作用，符合本模块现行风格）
- Test: `tests/test_predicates.py`

**Interfaces:**
- Produces: `resource_id(station) -> Tuple[str, str]`；`resource_id_for(grid, name) -> Tuple[str, str]`

设计决定（spec §5.2 定了形状 `(grid_name, instance_name)`，这里定具体函数）：
`resource_id` 从站点 dict 读 `grid`/`n` 两个字段；`grid` 字段缺失（旧协议帧、
Task 7 之前没有这个字段）时退化为 `""`，与"有 grid 字段但为空字符串"等价——
这保证 Task 11 切换认领键格式时，旧 fixture（无 `grid` 字段）仍能构造出
确定性的 key，不会因为 `None != ""` 产生难以调试的认领错配。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_predicates.py 追加
from ai.predicates import resource_id, resource_id_for


def test_resource_id_from_station_with_grid():
    st = {"n": "DispenserCrate 3 (1)", "grid": "Balloon Basket"}
    assert resource_id(st) == ("Balloon Basket", "DispenserCrate 3 (1)")


def test_resource_id_missing_grid_defaults_empty_string():
    """旧协议帧没有 grid 字段：退化为空字符串，不是 None（§5.2 的向后兼容）。"""
    st = {"n": "workstation_cooker_01"}
    assert resource_id(st) == ("", "workstation_cooker_01")


def test_resource_id_same_name_different_grid_are_distinct():
    """§5.2 要解决的同名撞车：左右各一个同名台面，grid 不同则 id 不同。"""
    left = {"n": "countertop_01_standard_dark_wood (4)", "grid": "GridManager"}
    right = {"n": "countertop_01_standard_dark_wood (4)", "grid": "Balloon Basket"}
    assert resource_id(left) != resource_id(right)


def test_resource_id_for_direct_construction():
    assert resource_id_for("GridManager", "cooker_01") == ("GridManager", "cooker_01")
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_predicates.py -k resource_id -v`
Expected: FAIL，`ImportError`

- [ ] **Step 3: 实现**

```python
# ai/predicates.py 追加（文件末尾）

def resource_id(station):
    # type: (Optional[Dict]) -> Tuple[str, str]
    """站点的认领资源 id（spec §5.2）：(网格名, 实例名)。

    取代"站点用实例名 n"的旧规则——同名站点出现在不同网格（如气球关左右
    各一张 countertop_01_standard_dark_wood (4)）时，旧规则会共享认领键、
    互相误锁（实测，§4.1/§5.2）。grid 字段缺失（旧协议帧）退化为空字符串，
    不是 None，保证旧 fixture 下 key 仍确定。
    """
    if not station:
        return ("", "")
    return (str(station.get("grid") or ""), str(station.get("n") or ""))


def resource_id_for(grid_name, instance_name):
    # type: (Any, Any) -> Tuple[str, str]
    """直接从 (grid, name) 构造资源 id，供已知两个值分开传的调用点使用。"""
    return (str(grid_name or ""), str(instance_name or ""))
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_predicates.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add ai/predicates.py tests/test_predicates.py
git commit -m "feat(predicates): 新增 resource_id——(网格名,实例名) 构造（§5.2）

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---
## Task 7: 插件输出新协议字段（`grids`/`terrain`/`occupancy`/`belts`/`hazards`）

**本任务只写代码、只编译，不进游戏验证**。实机验证全部挪到 Task 18（用户
明确要求：先完成全部编码，由用户统一做实机测试），本任务结束时的验收标准
只是 `./plugin/build.sh` 无 error——这是本计划里**唯一无法用 pytest 自动
验收正确性**的部分，正确性要等 Task 18 才能确认，本任务只保证"编译通过、
逻辑读下来自洽"。

**没有 C# 自动化测试**（项目约定如此，`plugin/` 全靠人工进关验证）。写每
个 `Append*` 函数时，依据都是 spec §4 已经记录的实测数据（字段名、层号、
`Travelator`/`FireHazard` 的具体读取方式），不是本任务新猜的——这保证即使
不能立刻编译验证语义，字段来源也有实测依据可查。

**Files:**
- Modify: `plugin/Oc2AIPlugin.cs`

**Interfaces:**
- Produces: NDJSON 帧新增 `grids`/`terrain`/`occupancy`/`belts`/`hazards` 字段
  （spec §6 的 schema）；`stations[]` 每项新增 `grid` 字段
- Consumes: 无（纯新增输出，不读 Python 侧任何东西）

- [ ] **Step 1: 新增 `AppendGrids`——输出全部 `QuadGridManager` 的原点**

在 `AppendGrid` 函数之后插入：

```csharp
// 替代 CalibrateGrid/GridIndex 的单网格假设（§4.1 根因：取第一个
// QuadGridManager、第一个站点就 return，多网格下必然只拿到一个）。
// 直接枚举全部 QuadGridManager 实例，每个的 transform.position 就是
// 它的世界原点——m_origin 字段恒为 (0,0,0)，原点在 transform 上，不在
// 字段里（§4.1 实测）。
void AppendGrids(StringBuilder sb)
{
    sb.Append('[');
    int i = 0;
    foreach (MonoBehaviour mb in _all)
    {
        if (mb == null) continue;
        if (mb.GetType().Name != "QuadGridManager") continue;
        object size = GetField(mb, "m_size");
        float cell = 1.2f;
        if (size is Vector3 sz && sz.x > 0f) cell = sz.x;
        Vector3 p = mb.transform.position;
        if (i > 0) sb.Append(',');
        sb.Append("{\"id\":").Append(Jstr(mb.name))
          .Append(",\"ox\":").Append(p.x.ToString("0.####"))
          .Append(",\"oz\":").Append(p.z.ToString("0.####"))
          .Append(",\"cell\":").Append(cell.ToString("0.####"))
          .Append('}');
        i++;
    }
    sb.Append(']');
}
```

在 `BuildState()` 里，`AppendGrid(sb)` 那行之后插入：

```csharp
sb.Append(",\"grids\":");
AppendGrids(sb);
```

- [ ] **Step 2: 编译确认无 error**

```bash
./plugin/build.sh
```

Expected: `built: .../Oc2AIPlugin.dll`，无 error。**不部署、不进游戏**——
语义正确性留给 Task 18 的实机验证（届时的 Expected 是：进 `Balloon_1_5_2P`，
`grids` 字段应是 `[{...GridManager...}, {...Balloon Basket...}]`，两个 `ox`
相差约 10.0，§4.1 实测 9.6 vs 19.6）。

- [ ] **Step 3: 新增 `AppendTerrain`——按层号输出 AABB**

```csharp
// 取代 Col_Floor/Col_Walls/Col_Objects 名字前缀匹配（§2 根因 2：Sushi_1_4
// 命中 41 个、Balloon_1_5 命中 0 个）。按层号分组输出，层名是 Unity 工程
// 配置，跨关稳定（§4.4）。只收这几层：Ground(9)/Walls(10)/
// PlateStationBlock(20)/CookingStationBlock(22)/TableBlock(26)/KillPlane(30)。
static readonly int[] TERRAIN_LAYERS = { 9, 10, 20, 22, 26, 30 };

void AppendTerrain(StringBuilder sb)
{
    sb.Append('[');
    Dictionary<int, List<float[]>> byLayer = new Dictionary<int, List<float[]>>();
    foreach (int l in TERRAIN_LAYERS) byLayer[l] = new List<float[]>();

    Type goType = FindType("UnityEngine.GameObject");
    if (goType == null) goType = FindType("GameObject");
    MethodInfo mi = typeof(UnityEngine.Object).GetMethod(
        "FindObjectsOfType", BindingFlags.Public | BindingFlags.Static,
        null, new Type[] { typeof(Type) }, null);
    object[] gos = (mi != null && goType != null)
        ? (object[])mi.Invoke(null, new object[] { goType }) : null;
    if (gos != null)
    {
        foreach (object o in gos)
        {
            GameObject go = o as GameObject;
            if (go == null || !byLayer.ContainsKey(go.layer)) continue;
            foreach (Component c in go.GetComponents<Component>())
            {
                if (c == null) continue;
                if (c.GetType().Name.ToLowerInvariant().IndexOf("collider") < 0) continue;
                object trig = GetProp(c, "isTrigger");
                if (trig is bool tb && tb) continue;
                object bd = GetProp(c, "bounds");
                if (bd == null) continue;
                object mn = GetProp(bd, "min"), mx = GetProp(bd, "max");
                if (mn == null || mx == null) continue;
                object ax = GetField(mn, "x"), az = GetField(mn, "z");
                object bx = GetField(mx, "x"), bz = GetField(mx, "z");
                if (ax == null || az == null || bx == null || bz == null) continue;
                float axf = (float)ax, azf = (float)az, bxf = (float)bx, bzf = (float)bz;
                if (bxf <= axf || bzf <= azf) continue;
                byLayer[go.layer].Add(new float[] { axf, bxf, azf, bzf });
            }
        }
    }
    int i = 0;
    foreach (int l in TERRAIN_LAYERS)
    {
        List<float[]> boxes = byLayer[l];
        if (boxes.Count == 0) continue;
        if (i > 0) sb.Append(',');
        sb.Append("{\"layer\":").Append(l).Append(",\"boxes\":[");
        for (int k = 0; k < boxes.Count; k++)
        {
            if (k > 0) sb.Append(',');
            float[] b = boxes[k];
            sb.Append('[').Append(b[0].ToString("0.##")).Append(',')
              .Append(b[1].ToString("0.##")).Append(',')
              .Append(b[2].ToString("0.##")).Append(',')
              .Append(b[3].ToString("0.##")).Append(']');
        }
        sb.Append("]}");
        i++;
    }
    sb.Append(']');
}
```

在 `BuildState()` 里 `AppendGrids(sb)` 之后插入：

```csharp
sb.Append(",\"terrain\":");
AppendTerrain(sb);
```

- [ ] **Step 4: 编译确认无 error**

```bash
./plugin/build.sh
```

Expected: 无 error。实机验证留给 Task 18（届时的 Expected：进任意关卡，
`terrain` 非空，`layer` 取值集合 ⊆ `{9,10,20,22,26,30}`，9 和 10 必然存在；
气球关 `layer=30`（KillPlane）应有 box，§4.4 实测 12 个）。

- [ ] **Step 5: 新增 `AppendOccupancy`——按网格输出 `m_gridOccupancy`**

```csharp
// 运行时占用表（§5.3 第 5 步），比 nodeMap 权威——nodeMap 11×11 覆盖不全
// 且关内从不更新（§4.3），这里直接读 GridManager/QuadGridManager 的
// m_gridOccupancy，每个网格单独一组 cells。
void AppendOccupancy(StringBuilder sb)
{
    sb.Append('[');
    int i = 0;
    foreach (MonoBehaviour mb in _all)
    {
        if (mb == null) continue;
        string tn = mb.GetType().Name;
        if (tn != "QuadGridManager" && tn != "GridManager") continue;
        object occ = GetField(mb, "m_gridOccupancy");
        if (!(occ is IEnumerable)) continue;
        StringBuilder cells = new StringBuilder();
        int n = 0;
        foreach (object e in (IEnumerable)occ)
        {
            object key = GetProp(e, "Key");
            if (key == null) continue;
            object gx = GetField(key, "m_x");
            object gz = GetField(key, "m_z");
            if (gx == null || gz == null) continue;
            if (n > 0) cells.Append(',');
            cells.Append('[').Append(gx).Append(',').Append(gz).Append(']');
            n++;
        }
        if (n == 0) continue;
        if (i > 0) sb.Append(',');
        sb.Append("{\"grid\":").Append(Jstr(mb.name))
          .Append(",\"cells\":[").Append(cells).Append("]}");
        i++;
    }
    sb.Append(']');
}
```

在 `BuildState()` 里 `AppendTerrain(sb)` 之后插入：

```csharp
sb.Append(",\"occupancy\":");
AppendOccupancy(sb);
```

- [ ] **Step 6: 编译确认无 error**

```bash
./plugin/build.sh
```

Expected: 无 error。实机验证留给 Task 18（届时的 Expected：进气球关，
`occupancy` 列表长度 ≥ 2，每项 `cells` 非空）。

- [ ] **Step 7: 新增 `AppendBelts`——`Travelator` 数据驱动**

```csharp
// 取代 occupancy.BELTS 关卡硬编码表（§4.5：与手标表 18/18 格零差异，
// 可证的等价替换）。方向必须用 GetSurfaceVelocity()，不能用
// m_directionXZ 枚举——后者是局部朝向，两排传送带都报 Rightwards，
// 真实方向需乘 transform.rotation（§4.5 实测对比表）。
void AppendBelts(StringBuilder sb)
{
    sb.Append('[');
    int i = 0;
    foreach (MonoBehaviour mb in _all)
    {
        if (mb == null || mb.GetType().Name != "Travelator") continue;
        object gi = GetField(mb, "m_gridIndex");
        object gm = GetField(mb, "m_gridManager");
        object vel = CallMethod(mb, "GetSurfaceVelocity");
        if (gi == null) continue;
        object gx = GetField(gi, "m_x"), gz = GetField(gi, "m_z");
        if (gx == null || gz == null) continue;
        float vx = 0f, vz = 0f;
        if (vel is Vector3 v) { vx = v.x; vz = v.z; }
        if (i > 0) sb.Append(',');
        sb.Append("{\"grid\":").Append(gm is UnityEngine.Object ugm && ugm != null
                                       ? Jstr(ugm.name) : "null")
          .Append(",\"gx\":").Append(gx).Append(",\"gz\":").Append(gz)
          .Append(",\"vx\":").Append(vx.ToString("0.##"))
          .Append(",\"vz\":").Append(vz.ToString("0.##"))
          .Append('}');
        i++;
    }
    sb.Append(']');
}
```

在 `BuildState()` 里 `AppendOccupancy(sb)` 之后插入：

```csharp
sb.Append(",\"belts\":");
AppendBelts(sb);
```

- [ ] **Step 8: 编译确认无 error**

```bash
./plugin/build.sh
```

Expected: 无 error。实机验证留给 Task 18（届时的 Expected：进
`Sushi_1_4_2P`——气球关无传送带，`belts` 长度 18，`vx`/`vz` 与 §4.5 的表
吻合，gz=2 一排 `vx=2.5`，gz=-3 一排 `vx=-2.5`）。

- [ ] **Step 9: 新增 `AppendHazards`——落火**

```csharp
// 1-6 的落火（§4.7）：TTL 16s、批量下落。实时扫 FireHazard（取基类本体，
// 不是 ServerFireHazard/ClientFireHazard 子类——子类上该字段读不到，
// §4.7 诊断已验证）。ttl 用 m_lifetime 减去已过去的时间——FireHazard
// 没有直接暴露"剩余时间"的字段，用 m_destroyTime（诊断里恒为 1，可能是
// 别的含义）之外没有更好的信号；这里退而用「本对象存在即未到期」的事实：
// hazards 列表只要包含它、ttl 填 m_lifetime 的名义值即可——Python 侧
// 只用 hazards 的"存在性"来挡格子（§5.3 第 3 步），不消费精确剩余秒数，
// ttl 字段留给未来做"等 hazard 消失"类功能时再精确化（spec §10 待办已
// 排除本次做等待语义）。
void AppendHazards(StringBuilder sb)
{
    sb.Append('[');
    int i = 0;
    foreach (MonoBehaviour mb in _all)
    {
        if (mb == null || mb.GetType().Name != "FireHazard") continue;
        object life = GetField(mb, "m_lifetime");
        Vector3 p = mb.transform.position;
        if (i > 0) sb.Append(',');
        sb.Append("{\"t\":\"fire\",\"x\":").Append(p.x.ToString("0.##"))
          .Append(",\"z\":").Append(p.z.ToString("0.##"))
          .Append(",\"ttl\":").Append(life == null ? "1.0" : life.ToString())
          .Append('}');
        i++;
    }
    sb.Append(']');
}
```

在 `BuildState()` 里 `AppendBelts(sb)` 之后插入：

```csharp
sb.Append(",\"hazards\":");
AppendHazards(sb);
```

- [ ] **Step 10: 编译确认无 error**

```bash
./plugin/build.sh
```

Expected: 无 error。实机验证留给 Task 18（届时的 Expected：进 1-6，等待
火雨发生——§4.9：时刻不固定，耐心等 1~2 分钟——`hazards` 从空变为非空
列表，每项有 `x`/`z`/`ttl`）。

- [ ] **Step 11: `stations[]` 每项新增 `grid` 字段，并把 `stations` 整体
  移出 `_staticJson`（这是比 spec §5.6 更根本的修法，见本步骤末尾说明）**

**发现的问题**：`BuildStatic()` 把 `stations` 的 `x`/`z`/`gx`/`gz`/
AABB **连同站点身份一起**烘进 `_staticJson`，而 `_staticJson` 只在换关
或（原计划里）staleness 触发时重建。这意味着：即使 Task 7 前面几步让
`terrain`/`occupancy` 每帧刷新了，`stations[]` 本身的坐标仍然是旧值——
Task 1/§4.2 发现的"篮子上的料箱、左侧移动的砧板"读到的坐标会在两次
`_staticJson` 重建之间整段过期，这正是本次调查从一开始就要修的核心问题，
不能只修 terrain/occupancy 而漏掉 stations 自己的坐标。

**修法**：`stations` 不属于真正的静态数据（spec §5.5 的表已经把
`x/z/gx/gz`/AABB 列进"移入每帧动态"一侧）——与其给它搭一套 staleness
检测，更直接的办法是**整个 `stations` 列表都不缓存**，和 `cookers`/
`plates`/`items` 一样每帧重新扫 `_all` 构建（`_all` 本身在 `Snapshot()`
里每帧都重新 `FindObjectsOfType` 一次，这个事实本来就是现有代码注释写明
的："每拍重扫，避免新出现的灭火器/掉落物延迟 1s 才进状态"——站点理应
享受同等待遇）。副作用是 **spec §5.6/§4.9 的"关内阶段切换"问题被这个
改动顺带修掉**：新厨房的站点一旦被 Unity 实例化，下一帧的 `AppendStations`
扫描就会自然包含它们，不需要任何"结构跳变检测"。本步骤完成后，
**Task 8 原计划的 staleness 检测不再需要**（见下面 Task 8 的更新）。

把 `BuildStatic()` 里构建 `stations` 的全部四段循环
（`StaticGridLocation`/`DynamicGridLocation` 主循环、功能站循环、
干净盘堆循环、`AppendWallStations` 调用）整体剪切出来，做成一个新函数
`AppendStations`，每帧调用：

```csharp
// 原来在 BuildStatic() 里的四段 stations.Append(...) 循环，原样移到这里，
// 只改两处：① StringBuilder 的变量名从 stations 改成 sb（函数参数）；
// ② 在 StaticGridLocation/DynamicGridLocation 循环与功能站循环各自的
// "n":Jstr(mb.name) 之后插入 grid 字段（见下）。si 的初值从 0 开始
// （不再与 recipes 共享一个计数器）。
void AppendStations(StringBuilder sb)
{
    int si = 0;
    foreach (MonoBehaviour mb in _all)
    {
        if (mb == null) continue;
        string tn = mb.GetType().Name;
        if (tn != "StaticGridLocation" && tn != "DynamicGridLocation") continue;
        string type = ClassifyStation(mb, mb.name);
        if (type == null) continue;
        object gi = GetField(mb, "m_gridIndex");
        Vector3 p = mb.transform.position;
        object stGm = GetField(mb, "m_gridManager");
        // StaticGridLocation 没有 m_gridManager 直接字段时，退而找
        // get_AccessGridManager（IGridLocation 接口，§2 根因 1）。
        if (stGm == null) stGm = CallMethod(mb, "get_AccessGridManager");
        if (si > 0) sb.Append(',');
        sb.Append("{\"t\":").Append(Jstr(type))
            .Append(",\"n\":").Append(Jstr(mb.name))
            .Append(",\"grid\":").Append(
                stGm is UnityEngine.Object ugm && ugm != null ? Jstr(ugm.name) : "null")
            .Append(",\"gx\":").Append(Fmt(GetField(gi, "m_x")))
            .Append(",\"gz\":").Append(Fmt(GetField(gi, "m_z")))
            .Append(",\"x\":").Append(p.x.ToString("0.0"))
            .Append(",\"z\":").Append(p.z.ToString("0.0"));
        AppendColliderBounds(sb, mb);
        if (type == "crate")
        {
            sb.Append(",\"ing\":").Append(Jstr(CrateIngredient(mb)));
            sb.Append(ChopFields(mb));
        }
        sb.Append('}');
        si++;
    }

    foreach (MonoBehaviour mb in _all)
    {
        if (mb == null) continue;
        string tn = mb.GetType().Name;
        string type = null;
        if (tn == "ServerPlateStation") type = "plate_station";
        else if (tn == "ServerWashingStation") type = "washing";
        if (type == null) continue;
        Vector3 p = mb.transform.position;
        string[] gidx = GridIndex(p).Split(',');
        object stGm = GetField(mb, "m_gridManager");
        if (stGm == null) stGm = CallMethod(mb, "get_AccessGridManager");
        if (si > 0) sb.Append(',');
        sb.Append("{\"t\":").Append(Jstr(type))
            .Append(",\"n\":").Append(Jstr(mb.name))
            .Append(",\"grid\":").Append(
                stGm is UnityEngine.Object ugm2 && ugm2 != null ? Jstr(ugm2.name) : "null")
            .Append(",\"gx\":").Append(gidx[0])
            .Append(",\"gz\":").Append(gidx[1])
            .Append(",\"x\":").Append(p.x.ToString("0.0"))
            .Append(",\"z\":").Append(p.z.ToString("0.0"));
        AppendColliderBounds(sb, mb);
        if (type == "washing")
        {
            sb.Append(",\"plate_count\":").Append(Fmt(GetField(mb, "m_plateCount")))
                .Append(",\"cleaning_timer\":").Append(Fmt(GetField(mb, "m_cleaningTimer")));
        }
        sb.Append('}');
        si++;
    }

    foreach (MonoBehaviour mb in _all)
    {
        if (mb == null) continue;
        string tn = mb.GetType().Name;
        if (tn != "ServerCleanPlateUtensilRespawnBehaviour"
            && tn != "CleanPlateUtensilRespawnBehaviour") continue;
        Vector3 p = mb.transform.position;
        if (si > 0) sb.Append(',');
        sb.Append("{\"t\":\"plate_stack\",\"n\":").Append(Jstr(mb.name))
            .Append(",\"x\":").Append(p.x.ToString("0.0"))
            .Append(",\"z\":").Append(p.z.ToString("0.0"));
        AppendColliderBounds(sb, mb);
        sb.Append('}');
        si++;
    }

    AppendWallStations(sb, ref si);
}
```

`AppendWallStations` 的函数签名不用改（它本来就接受 `ref int si`）。

**在 `BuildStatic()` 里删除**上面四段（整段剪切到 `AppendStations`），
`BuildStatic()` 只保留配方表构建那一段（`recipes` 的 `StringBuilder` 与
`_staticJson = "\"recipes\":[" + recipes + "]"`——注意这里字符串前缀从
`"\"stations\":[...],\"recipes\":[...]"` 变成只有 `"\"recipes\":[...]"`，
`si`/`stations` 变量整体删除，不再需要）。

**在 `BuildState()` 里**，把原来 `if (_staticJson != null)
sb.Append(',').Append(_staticJson);` 这行**之前**插入：

```csharp
sb.Append(",\"stations\":[");
AppendStations(sb);
sb.Append(']');
```

- [ ] **Step 12: 编译确认无 error**

```bash
./plugin/build.sh
```

Expected: 无 error。实机验证留给 Task 18（届时的验证脚本：连续采样 30
秒内的 `stations`，取某个 `DispenserCrate` 的 `x` 坐标，若录制窗口覆盖
篮子移动——§4.2 周期约 50s，30s 窗口大概率能覆盖部分移动——取值应出现
多个不同值，这是 Task 1/§4.2 发现的"坐标被冻结"问题被修好的直接证据；
同时确认左右站点 `grid` 字段值不同，与 §4.1 的网格归属一致）。

- [ ] **Step 13: 提交**

```bash
git add plugin/Oc2AIPlugin.cs
git commit -m "feat(plugin): 输出 grids/terrain/occupancy/belts/hazards；stations 改每帧刷新（§6 协议，修 §4.2/§4.9 根因）

AppendStations 移出 BuildStatic/_staticJson，和 cookers/plates/items 一样
每帧重扫 _all。这同时修了两个问题：①移动平台上的站点坐标不再被冻结
在旧快照里（§4.2 的核心发现）；②1-6 关内阶段切换后新厨房的站点自动
出现，不需要额外的 staleness 检测（§4.9，原计划的结构跳变检测方案
作废，见下一个 commit 的说明）。

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---
## Task 8（DEFERRED 到 Task 18）: 验证 §4.9（关内阶段切换）已被 Task 7 顺带修复

**本任务整个是实机验证，不写代码**——用户已明确要求先完成全部编码，
由用户统一做实机测试，所以本任务的全部步骤移到 Task 18 的"1-6 实机
验收"部分执行，这里只记录结论性决定，供 Task 17 更新 spec 时引用。

spec §5.6 原计划是给 `_staticJson` 加"结构跳变检测"（按 `_all.Length`
变化比例触发重建）。写 Task 7 Step 11 时发现一个更根本的事实：
`stations` 的坐标被烘进 `_staticJson` 本身就是 bug（§4.2 的核心发现——
移动平台上的站点坐标会被冻结），不管触发重建的条件多准，"缓存 + 条件
重建"这个结构本身就是错的，直接的修法是**不缓存**（Task 7 Step 11 已
把 `AppendStations` 改成每帧调用）。这个改动的副作用是 §4.9 的"关内
阶段切换后新站点不出现"问题被一并解决——新厨房的站点一旦被 Unity
实例化，下一帧的 `AppendStations` 扫描自然包含它们。

`recipes` **不受影响、也不需要同样处理**：§4.9 的实测数据显示
`recipes` 在 1-6 坠毁前后恒为 115 条（两种菜式的配方从关卡一开始就
都在缓存里，不是坠毁后才出现），所以 `recipes` 继续留在 `_staticJson`
里按换关重建是正确的——没有发现任何需要让它每帧刷新的证据。

**不需要额外写 `[STALE]` 检测逻辑或阈值调参**（原 spec §5.6 的"10% 阈值"
疑虑，见该文档 §10 风险表，随这个决定一并失效，Task 17 更新文档时要
去掉这条风险记录）。实机验证内容已整合进 Task 18 Step 3（坠毁前后各
采样一次 `stations`/`recipes` 数量），验证失败时的诊断方向也记在那里。

**Files:** 无代码改动，无独立步骤——执行时直接跳到 Task 18。

---
## Task 9（DEFERRED 到 Task 18）: 录制真正的新协议 fixture（插件已部署新字段）

**本任务需要真实游戏进程，整个移到 Task 18 执行**——用户已明确要求先
完成全部编码。Task 10 的测试因此不能等这份 fixture：Task 10 的主测试
改用合成帧（和 `tests/test_recipe_graph.py` 的 `_world()` helper 同一
风格手工构造），覆盖同样的断言，不依赖真实录制；本任务产出的三份
`.ndjson.gz` 只作为 Task 10 测试里**额外的、自动 skip 的真实帧回归**
（`_load_gz_frames` 遇文件不存在时 `pytest.skip`，不是 `fail`——这保证
Task 10 在没有这份 fixture 的情况下也能跑出真正的 red→green，而不是
全程 SKIP 看不出代码对不对）。

**Files:**
- Create: `tests/fixtures/balloon_1_5_v2.ndjson.gz`
- Create: `tests/fixtures/sushi_1_4_belts.ndjson.gz`
- Create: `tests/fixtures/dynamic_1_6_v2.ndjson.gz`

**Interfaces:**
- Produces: 三份新协议 `.ndjson.gz`，供 Task 10 的真实帧回归测试使用
  （Task 10 的核心断言已由其合成帧测试独立覆盖，不依赖这份数据）

- [ ] **Step 1~5（原子步骤，执行时机延后到 Task 18，内容不变）**：
  录气球关（含 terrain/occupancy/grids，90s）、录 1-4（含 belts，60s）、
  录 1-6（含 hazards，需等坠毁+火雨发生，180s）、验证三份都带新字段后
  压缩入库、提交。命令细节与验证脚本见 Task 18 的"Task 9 执行"小节
  （Task 18 展开时会把这些命令原样列出，这里不重复）。

---

## Task 10: `World` 接入新协议——替换 `playable_bounds`/`BELTS`/`floor` 扩 bounds

这是 Python 侧的核心切换：`World.__init__` 从读旧字段改为读新字段，
`walkable` 从 `make_walkable` 切到 `make_walkable_from_terrain`。

**Files:**
- Modify: `ai/world.py`
- Test: `tests/test_real_frames.py`（新增用例，复用现有 `frames`/`level_frame` fixture）

**Interfaces:**
- Consumes: `GridSet`（Task 2）、`make_walkable_from_terrain`/`parse_terrain`/
  `parse_hazards`/`hazard_boxes`/`parse_belts`（Task 3/4）
- Produces: `World.grids`（`GridSet` 实例）、`World.hazards`（`List[Dict]`）、
  `World.walkable`（新实现）、`World.belt_cells`/`belt_dir`（来自 `parse_belts`
  而非 `BELTS` 表）
- **破坏性变更**：删除 `World.bounds` 属性（`playable_bounds` 不再调用）。
  `World.mapping`（`GridMapping`）**保留**——`to_grid`/`to_world`（主网格）
  仍供旧调用点用，直到 Task 11/12 逐一切换。

- [ ] **Step 1: 写失败测试（合成帧为主，Task 9 的真实帧为辅——后者本任务
  执行时大概率还不存在，必须能自动 skip 而不是 fail，见下方说明）**

主测试用合成帧（手工构造 dict，和 `tests/test_recipe_graph.py` 的
`_world()` helper 同风格），不依赖 Task 9：

```python
# tests/test_world.py 追加
from ai.world import World


def _multi_grid_frame():
    """两网格合成帧：主网格一个站点，附属网格一个站点，坐标按 §4.1
    实测的真实关系构造（两网格原点差 10.0，不是整数格，但各自内部
    坐标系自洽）。"""
    return {
        "grid": {"nodeMap": None},
        "grids": [
            {"id": "GridManager", "ox": 9.6, "oz": -2.4, "cell": 1.2},
            {"id": "Balloon Basket", "ox": 19.6, "oz": -2.4, "cell": 1.2},
        ],
        "terrain": [
            {"layer": 9, "boxes": [[3.6, 15.6, -8.6, 3.4]]},    # Ground，主网格
        ],
        "occupancy": [],
        "belts": [],
        "hazards": [{"t": "fire", "grid": "GridManager", "x": 100.0, "z": 100.0, "ttl": 5.0}],
        "stations": [
            {"t": "crate", "n": "DispenserCrate 3 (1)", "grid": "Balloon Basket",
             "x": 22.0, "z": -3.6, "gx": 2, "gz": -1},
        ],
        "chefs": [{"name": "Player 1", "x": 10.0, "z": -2.4, "held": None}],
    }


def test_world_exposes_gridset_from_synthetic_frame():
    w = World(_multi_grid_frame())
    assert set(w.grids.ids()) == {"GridManager", "Balloon Basket"}


def test_world_walkable_uses_terrain_not_old_bounds():
    """§5.3：playable_bounds 不再参与判定——World 不应再暴露 bounds 属性。"""
    w = World(_multi_grid_frame())
    assert not hasattr(w, "bounds")
    # 当前所在格必须可走（人正站在这里，terrain 的 Ground box 覆盖这一点）
    chef = next((c for c in w.chefs if c.get("name") == "Player 1"), None)
    assert chef is not None
    gx, gz = w.to_grid(chef["x"], chef["z"])
    assert w.walkable(gx, gz) is True


def test_world_hazards_exposed():
    w = World(_multi_grid_frame())
    assert len(w.hazards) == 1
    h = w.hazards[0]
    assert h["x"] == 100.0 and h["z"] == 100.0 and h["ttl"] == 5.0
```

补一份用真实帧的回归测试（Task 9 产出，本任务执行时文件通常还不存在，
**必须 skip 而非 fail**——这是和上面合成帧测试的本质区别：上面的测试
现在就要能跑通并钉住行为，这几条是"有真实数据时锦上添花的回归"）：

```python
# tests/test_real_frames.py 追加
import gzip
import json
import os

import pytest

from ai.world import World

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_gz_frames(name):
    path = os.path.join(FIXTURES, name)
    if not os.path.exists(path):
        pytest.skip("缺 %s，这份 fixture 由 Task 9 在实机录制阶段产出" % name)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def test_world_belts_from_travelator_not_hardcoded_table_real_frame():
    frames = _load_gz_frames("sushi_1_4_belts.ndjson.gz")
    frame = next((f for f in frames if f.get("belts")), None)
    assert frame is not None, "样本没有带 belts 字段的帧"
    w = World(frame)
    assert len(w.belt_cells) == 18   # §4.5：与手标表数量一致
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_world.py -k "multi_grid or walkable_uses_terrain or hazards_exposed" -v`
Expected: FAIL（`AttributeError: 'World' object has no attribute 'grids'`
等）。

再跑一遍确认真实帧那条按预期降级为 SKIP（不是 FAIL，本任务执行时
Task 9 尚未录制，这是预期状态，不计入本任务的通过标准）：

Run: `python3 -m pytest tests/test_real_frames.py -k belts_from_travelator -v`
Expected: SKIPPED。

- [ ] **Step 3: 改写 `ai/world.py`**

```python
# ai/world.py 顶部 import 区替换：
from ai import occupancy
from ai.geometry import GridMapping, GridSet
```

`World.__init__` 里，从 `self.mapping = GridMapping.from_grid(...)` 这行
**之后**开始，替换到 `self.walkable = occupancy.make_walkable(...)` 结束
的整段（即删除 `playable_bounds`/`floor` 扩 bounds/`station_blocked`/
`belt_cells_and_dirs`（旧版）/`make_walkable`（旧版）五处调用）：

```python
        # 注意：self.stations 现在混合了全部网格的站点（Task 7 Step 11 起
        # stations 不再按单一网格缓存）。GridMapping.from_grid 的众数校准
        # 会被更多站点所在的网格"赢"（气球关实测 GridManager 14 票 vs
        # Balloon Basket 10 票，§4.1），这与 _occupancy_cells 的
        # _primary_grid_name heuristic 是两套独立判据，恰好指向同一个
        # "主网格"，但不保证所有关卡都一致——这是已知限制，不在本次范围
        # 内统一（§10 风险表）。self.mapping 仅供向后兼容的单网格调用点
        # （to_grid/to_world/walkable）用；多网格权威来源是 self.grids。
        self.mapping = GridMapping.from_grid(state.get("grid"), self.stations)

        parsed = occupancy.parse_node_map(
            (state.get("grid") or {}).get("nodeMap"))
        if parsed is None:
            self._node_map, self._nm_rows, self._nm_cols = None, 0, 0
        else:
            self._node_map, self._nm_rows, self._nm_cols = parsed

        # 多网格坐标集合（spec §5.1）：换关卡/换帧直接重建，不反解。
        self.grids = GridSet.from_frame(state.get("grids"))

        # 可走性改用层号地形 + 运行时占用（spec §5.3），取代
        # playable_bounds + floor 扩 bounds + station_blocked + nodeMap。
        terrain = occupancy.parse_terrain(state.get("terrain"))
        self.hazards = occupancy.parse_hazards(state.get("hazards"))
        hazard_boxes = occupancy.hazard_boxes(self.hazards, cell=self.mapping.cell)
        occ_cells = self._occupancy_cells(state.get("occupancy"))
        self.belt_cells, self.belt_dir = occupancy.parse_belts(state.get("belts"))

        self.walkable = occupancy.make_walkable_from_terrain(
            terrain, hazard_boxes, occ_cells, self.mapping)

    def _occupancy_cells(self, occupancy_list):
        # type: (Optional[List[Dict]]) -> Set[Tuple[int, int]]
        """把 occupancy[] 按主网格坐标并成一个集合。

        §4.1 的多网格事实在这里被简化：当前只有主网格（GridManager）的
        occupancy 用于 walkable 判定，因为 World.to_grid/walkable 仍是
        单网格坐标系（主网格），非主网格的占用格暂不参与——这与现状
        一致（旧实现从未处理过非主网格占用），留给 Task 12 的跨网格
        寻路一并处理（见该任务的"已知限制"）。
        """
        out = set()  # type: Set[Tuple[int, int]]
        primary = self._primary_grid_name(occupancy_list)
        for entry in occupancy_list or ():
            if not isinstance(entry, dict):
                continue
            if primary is not None and entry.get("grid") != primary:
                continue
            for cell in entry.get("cells") or ():
                if isinstance(cell, (list, tuple)) and len(cell) == 2:
                    out.add((int(cell[0]), int(cell[1])))
        return out

    def _primary_grid_name(self, occupancy_list):
        # type: (Optional[List[Dict]]) -> Optional[str]
        """哪个网格是"主网格"：取 occupancy 列表第一项的 grid 名。

        没有更好的信号区分"主"与"附属"网格——§4.1 两个网格在插件输出里
        没有显式的主次标记。按出现顺序取第一个是当前能做的最简单假设，
        实测气球关 GridManager 恒排第一（插件按 FindObjectsOfType 的
        遍历序，场景加载顺序决定，不保证跨关稳定）。这是本计划引入的
        工程假设，若未来关卡顺序不同导致误判，影响范围只是"占用格判定
        漏掉非主网格"，不会崩溃（§3 非目标已排除需要完整多网格占用的
        场景）。
        """
        for entry in occupancy_list or ():
            if isinstance(entry, dict) and entry.get("grid"):
                return entry.get("grid")
        return None
```

删除 `World.to_grid`/`to_world` **之前**的 `self.bounds = ...` 相关代码
（已在上面的替换块里一并删除，这里不留 `self.bounds` 属性）。

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_real_frames.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 修复两处直接断言 `w.bounds` 的现行测试**

`python3 -m pytest tests/test_world.py tests/test_world_parity.py -v` 当前
基线（无 fixture 环境下）：24 passed, 12 skipped，其中 `test_world.py` 的
`test_world_empty_state_does_not_crash` 与
`test_world_empty_state_walkable_feeds_bounded_distance_field` 两条
**PASS 且直接断言 `w.bounds is None`**——本 task 删除 `bounds` 属性后，
这两条会从 PASS 变成 `AttributeError`。`test_world_parity.py` 的
`test_bounds_match_legacy` 已经是 SKIPPED（缺 legacy 目录/样本帧），
本次不受影响，但该测试本身对已删除的属性做断言，一并清理，避免将来
误导。

修改 `tests/test_world.py`：

```python
# 原：
#     assert w.bounds is None
# 改为：World 不再有 bounds 属性（§5.3，可走性改用 terrain + occupancy）
def test_world_empty_state_does_not_crash():
    w = W.World({})
    assert w.station_blocked == set()
    assert w.belt_cells == set()
    assert not hasattr(w, "bounds")
    assert w.walkable(0, 0) is True


def test_world_empty_state_walkable_feeds_bounded_distance_field():
    from ai import navigation

    w = W.World({})
    assert not hasattr(w, "bounds")
    assert w.walkable(500, 500) is True   # 无 terrain 数据：整体放行（Task 3 的设计决定）

    dist = navigation.distance_field((0, 0), w.walkable, w.belt_dir)
    assert dist[(0, 0)] == 0
    assert len(dist) <= 4096 + 1
```

同样搜索 `test_world_derived_data_real_frame`（第 226 行附近）——它依赖
`full_level.ndjson.gz`（不存在，已 SKIPPED），断言里也有 `w.bounds ==
(-7, 7, -5, 4)` 与 `w.station_blocked`。既然这条测试本来就 SKIPPED（旧
协议 fixture 已不打算补），直接删除整个测试函数（不是改断言——它验证的
是旧协议的派生值，新协议下这些值的来源完全变了，留着注释说明）：

```python
# test_world_derived_data_real_frame 已删除：验证的是旧协议派生值
# （station_blocked/belt_cells/bounds 的旧计算路径），§5.3/§5.5 改造后
# 这些值的来源完全不同，等价测试见 tests/test_world.py 的
# test_world_walkable_uses_terrain_not_old_bounds 等（Task 10 新增）。
```

删除 `tests/test_world_parity.py` 里的 `test_bounds_match_legacy` 整个
函数（同理：验证的是已删除属性对旧实现的 parity，新协议下没有对应的
"旧实现"可比）。

- [ ] **Step 5b: 运行完整测试套件，确认没有其它遗留断言**

Run: `python3 -m pytest tests/ -q`
Expected: 无 `AttributeError.*bounds`。若有其它测试文件引用 `.bounds`，
在此列出并同样处理（删除断言或整条测试，视该测试是否还有其它有效内容
而定——不要为了让测试通过而静默改断言值，若发现断言是真实回归信号，
停下来诊断根因，不要绕过）。

- [ ] **Step 6: 提交**

```bash
git add ai/world.py tests/test_world.py tests/test_real_frames.py tests/test_world_parity.py
git commit -m "feat(world): 接入新协议 grids/terrain/occupancy/belts/hazards（§5.3/§5.5）

删除 playable_bounds / floor 扩 bounds / station_blocked / 旧 make_walkable
的调用。World.bounds 属性移除。

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---
## Task 11: 资源 id 切到 `(grid, name)`——站点查找函数

`resource_id()`（Task 6）已经能把站点 dict 转成 `(grid, name)` 元组。本
task 把**消费方**——按资源 id 反查站点的函数——改成接受元组并比对两个
分量，而不是只比对名字字符串。`Task.claims()` 本身不用改：`cooker_id`/
`board_id` 等 `@property` 只是 `return self._resources[0]`，构造时传入
什么就原样存什么——改变发生在"谁构造 Task 时传了什么"（Task 12）和
"谁拿着 resources[0] 去反查站点"（本 task）。

**Files:**
- Modify: `ai/tasks/base.py`（`cooker_station`、`station_at_cell` 不动——
  后者的 cell 是网格坐标不是站点名，不受影响；`cooker_state` 需改）
- Modify: `ai/tasks/chop.py`（`board_station`）
- Modify: `ai/tasks/cleanup.py`（387 行的灶查找）
- Test: `tests/test_tasks.py`（新增用例，若该文件不存在则在
  `tests/test_cook_claim_scope.py` 旁新建 `tests/test_resource_id_lookup.py`）

**Interfaces:**
- Consumes: `resource_id`/`resource_id_for`（Task 6）
- Produces: `cooker_state(ctx, resource_id_tuple)`、
  `board_station(ctx, resource_id_tuple)` 等——签名不变（仍是单个参数），
  但该参数现在**必须**是 `(grid, name)` 元组，不再接受裸字符串

**向后兼容策略**（本 task 的关键设计决定）：改造期内 `tasks/cook.py` 等
生产方（Task 12 才改）仍会传入裸字符串 `cooker_id`。所以这些查找函数要
**同时支持元组与字符串**，直到 Task 12 把全部生产方切换完：

```python
def _unpack_resource_id(rid):
    # type: (Any) -> Tuple[Optional[str], str]
    """兼容过渡：rid 可能是 (grid, name) 元组，也可能是裸名字字符串
    （Task 12 之前的旧调用点）。统一拆成 (grid_or_None, name)。"""
    if isinstance(rid, tuple) and len(rid) == 2:
        return (rid[0], str(rid[1]))
    return (None, str(rid))
```

grid 为 `None` 时查找退化为"只比名字"（旧行为，向后兼容）；grid 非
`None` 时要求站点的 `grid` 字段也匹配。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_resource_id_lookup.py（新建）
from ai.tasks.base import cooker_state
from ai.tasks.chop import board_station


def _ctx(stations=None, cookers=None):
    class FakeWorld:
        pass
    w = FakeWorld()
    w.stations = stations or []
    w.cookers = cookers or []
    return {"w": w}


def test_cooker_state_matches_by_name_when_no_grid():
    """向后兼容：旧调用传裸字符串，忽略 grid 比对（过渡期行为）。"""
    ctx = _ctx(cookers=[{"n": "cooker_01", "cooked": False}])
    assert cooker_state(ctx, "cooker_01") == {"n": "cooker_01", "cooked": False}


def test_cooker_state_matches_grid_and_name_tuple():
    """新调用传 (grid, name)：两边都要匹配，解决同名撞车（§5.2）。"""
    ctx = _ctx(cookers=[
        {"n": "cooker_01", "grid": "GridManager", "cooked": False},
        {"n": "cooker_01", "grid": "Balloon Basket", "cooked": True},
    ])
    got = cooker_state(ctx, ("Balloon Basket", "cooker_01"))
    assert got["grid"] == "Balloon Basket"
    assert got["cooked"] is True


def test_board_station_distinguishes_same_name_different_grid():
    ctx = _ctx(stations=[
        {"t": "chopping", "n": "board_01", "grid": "GridManager"},
        {"t": "chopping", "n": "board_01", "grid": "Balloon Basket"},
    ])
    got = board_station(ctx, ("Balloon Basket", "board_01"))
    assert got["grid"] == "Balloon Basket"
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_resource_id_lookup.py -v`
Expected: FAIL（`cooker_state`/`board_station` 当前不认元组第二个用例会
匹配到错误对象或 None）

- [ ] **Step 3: 改 `ai/tasks/base.py` 的 `cooker_state`**

```python
# 替换 ai/tasks/base.py 第 240-247 行（cooker_state 函数体）：
def cooker_state(ctx, cooker_id):
    # type: (Dict, Any) -> Optional[Dict]
    """按资源 id 取灶的状态条目（spec §5.2：id 可能是 (grid,name) 元组，
    过渡期也接受裸字符串——见 _unpack_resource_id）。

    灶的**状态**在 `cookers[]` 里、**位置**在 `stations[]` 里，两个 dict
    各取所需。cook / fire 两组都要用，故放在 base。
    """
    grid, name = _unpack_resource_id(cooker_id)
    for c in ctx["w"].cookers:
        if not P.station_name_matches(str(c.get("n") or ""), name):
            continue
        if grid is not None and c.get("grid") != grid:
            continue
        return c
    return None
```

在 `cooker_station` 函数（第 250-258 行）同样插入 grid 比对：

```python
def cooker_station(ctx, cooker_id):
    # type: (Dict, Any) -> Optional[Dict]
    """按资源 id 取灶的站点 dict（原语要 gx/gz 定位）。"""
    grid, name = _unpack_resource_id(cooker_id)
    for st in ctx["w"].stations:
        if st.get("t") != "cooking":
            continue
        if not P.station_name_matches(str(st.get("n") or ""), name):
            continue
        if grid is not None and st.get("grid") != grid:
            continue
        return st
    return None
```

在文件顶部（`OWNER = "P2"` 之后）新增 `_unpack_resource_id` 辅助函数
（内容见本 task 开头）。

- [ ] **Step 4: 改 `ai/tasks/chop.py` 的 `board_station`**

```python
# 替换 ai/tasks/chop.py 第 31-39 行：
def board_station(ctx, board_id):
    # type: (Dict, Any) -> Optional[Dict]
    """按资源 id 取切菜板的站点 dict。"""
    from ai.tasks.base import _unpack_resource_id
    grid, name = _unpack_resource_id(board_id)
    for st in ctx["w"].stations:
        if st.get("t") != "chopping":
            continue
        if not P.station_name_matches(str(st.get("n") or ""), name):
            continue
        if grid is not None and st.get("grid") != grid:
            continue
        return st
    return None
```

- [ ] **Step 5: 改 `ai/tasks/cleanup.py` 第 387 行附近的灶查找**

读该函数全文（`sed -n 375,395p ai/tasks/cleanup.py`）确认函数名与上下文，
应用同一模式：解包 `_unpack_resource_id`，比对 `grid` 分量。

- [ ] **Step 6: 运行确认通过**

Run: `python3 -m pytest tests/test_resource_id_lookup.py -v`
Expected: 全部 PASS

- [ ] **Step 7: 运行完整测试套件**

Run: `python3 -m pytest tests/ -q`
Expected: 不新增失败（本 task 向后兼容字符串输入，现有调用点原样传字符串
仍能工作）

- [ ] **Step 8: 提交**

```bash
git add ai/tasks/base.py ai/tasks/chop.py ai/tasks/cleanup.py tests/test_resource_id_lookup.py
git commit -m "feat(tasks): 站点查找函数支持 (grid,name) 资源 id（§5.2，向后兼容字符串）

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 12: 资源 id 切到 `(grid, name)`——候选生成（`recipe_graph.py`/`planner.py`）

把 task 实例化的地方从传裸字符串改成传 `resource_id()` 的元组。这是
Task 11 铺好的查找函数真正开始接到元组输入的地方。

**Files:**
- Modify: `ai/recipe_graph.py`
- Modify: `ai/planner.py`
- Modify: `ai/agent.py`（`_make_resource_view`，对账用的资源 id 必须与
  `task.claims()` 的键一致——这条本来就在该函数的 docstring 里写明）
- Test: `tests/test_cook_claim_scope.py`、`tests/test_recipe_graph.py`
  （已有测试跑一遍确认行为不变；新增一条验证同名跨网格不冲突）

- [ ] **Step 1: 写失败测试（同名跨网格不互锁）**

```python
# tests/test_recipe_graph.py 追加，复用文件已有的 _world/_fried_world 风格
def test_same_name_different_grid_cookers_are_independent_resources():
    """§4.1/§5.2 的同名撞车场景：两口灶同名不同网格，候选 id 必须不同。"""
    w = _world(
        orders=[{"id": "order1", "dish": "FriedMushrooms",
                 "composition": ["FriedMushrooms"], "remaining": 60}],
        recipes=_FRIED_MUSHROOM_RECIPES,
        stations=[
            {"t": "crate", "n": "crate_mushroom", "ing": "Mushroom",
             "chop": "ChoppedMushroom", "chop_stages": 8, "gx": 1, "gz": 1},
            {"t": "chopping", "n": "board_01", "gx": 2, "gz": 2,
             "item": "ChoppedMushroom"},
            {"t": "counter", "n": "counter_01", "gx": 3, "gz": 3},
        ],
        plates=[{"n": "plate_01", "x": 6.0, "z": 6.0, "count": 0, "contents": []}],
        cookers=[
            {"n": "cooker_01", "grid": "GridManager", "x": 4.0, "z": 4.0, "cooked": False},
            {"n": "cooker_01", "grid": "Balloon Basket", "x": 20.0, "z": 4.0, "cooked": False},
        ],
    )
    book = RecipeBook(w.recipes, w.stations)
    tasks = recipe_graph.emit(w, Blackboard(), book, w.orders[0],
                              {"plate_01": "order1"}, {})
    fetch_tasks = [t for t in tasks if t.TYPE == "FetchToCooker"]
    assert len(fetch_tasks) == 2, "两口同名灶应各发一个候选"
    cooker_ids = {t.resources[0] for t in fetch_tasks}
    assert len(cooker_ids) == 2, (
        "两个候选的 cooker_id 必须不同（%r），否则同名撞车" % cooker_ids)
    assert all(isinstance(cid, tuple) and len(cid) == 2 for cid in cooker_ids)
```

该测试需要 `recipe_graph.emit` 真的拿到两口灶各发一个候选——这要求
`_cookers(world)` 枚举函数（Task 3 之前已存在于 `ai/recipe_graph.py`）
不会把两口同名灶去重成一个。先确认该函数当前实现（`grep -n "_cookers"
ai/recipe_graph.py`）：若它按名字去重，本 task 的 Step 3 需要一并改成
按 `resource_id` 去重。

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_recipe_graph.py -k same_name_different_grid -v`
Expected: FAIL（两个 task 的 `cooker_id` 都是裸字符串 `"cooker_01"`，相等）

- [ ] **Step 3: 改 `ai/recipe_graph.py` 的实例化点**

把以下几处 `c.get("n")`/`p.get("n")`/`st.get("n")`（即将传入 Task
构造器当 resources 的那些）改成 `P.resource_id(c)` 等（`P` 是
`ai.predicates` 的别名，确认文件顶部已 import）：

- 354 行：`cid = c.get("n")` → `cid = P.resource_id(c)`
- 391 行：`pid = p.get("n")` → `pid = P.resource_id(p)`（`_inst_take_cooked`
  里的盘 id——**注意**：盘的资源 id 规则 spec §5.2 没有改（盘仍用裸实例名，
  §5.2 原文"替换现行三套规则中的前两套"，盘不在改造范围），这里不要改，
  仅供执行者核对不要误改。
- 407-412 行：`st.get("n")` → `P.resource_id(st)`（mixing 站点）
- 463/469 行：`p.get("n")`（盘，不改，理由同上）

关键的一处是 365 行 `T.FetchToCooker(ctx["order_id"], (cid, tuple(raws)), item)`
——`cid` 现在是元组，`FetchToCooker.cooker_id` property 原样返回
`self._resources[0]`，不需要改 task 类本身。

- [ ] **Step 4: 改 `ai/planner.py` 的实例化点**

同样模式，改第 157/176/182/207/217 行附近的 `cid = c.get("n")`/
`pid = p.get("n")`（盘不改）。**注意第 891 行**
`sources.append({"id": c.get("n"), ...})`——这是 `_match_cookers_to_orders`
的内部字典，不是 Task 构造参数，先确认该 `id` 字段是否被传给 Task
构造器（搜索它的消费点），若只在 planner 内部打分用则不改，若最终流入
某个 Task 的 resources 则一并改成 `P.resource_id(c)`。

- [ ] **Step 5: 改 `ai/agent.py` 的 `_make_resource_view`**

该函数的 docstring 已经写明"资源 id 必须与 task.claims() 的键一致"。
把 `existing_ids.add(sid)`（`sid = st.get("n")`）那段改成同时注册两种
id（过渡期）：

```python
# ai/agent.py _make_resource_view 内，站点循环部分：
for st in world.stations:
    sid = st.get("n")
    if not sid:
        continue
    existing_ids.add(sid)                          # 旧：裸名字（过渡期保留）
    existing_ids.add(P.resource_id(st))             # 新：(grid, name) 元组
    if (st.get("t") == "counter"
            and st.get("gx") is not None and st.get("gz") is not None):
        existing_ids.add((st.get("gx"), st.get("gz")))
    item = st.get("item")
    if item:
        contents[sid] = item
        contents[P.resource_id(st)] = item
```

对 `cookers`/`plates` 循环同样处理（灶要加，盘不加——盘资源 id 不变）。

- [ ] **Step 6: 运行确认通过**

Run: `python3 -m pytest tests/test_recipe_graph.py tests/test_cook_claim_scope.py -v`
Expected: 全部 PASS

- [ ] **Step 7: 运行完整测试套件**

Run: `python3 -m pytest tests/ -q`
Expected: 不新增失败。若有失败，大概率是某处仍直接拿 `cooker_id` 当字符串
用（例如打印、拼接），逐个修成兼容元组的写法（`str(cooker_id)` 对元组
会打印成 `"('GridManager', 'cooker_01')"`，这在 trace/日志里是可接受的
降级，不是 bug——只有当它被当成**比较键**用在字符串拼接匹配时才是真
问题）。

- [ ] **Step 8: 提交**

```bash
git add ai/recipe_graph.py ai/planner.py ai/agent.py tests/test_recipe_graph.py
git commit -m "feat(recipe_graph,planner): 候选生成改用 (grid,name) 资源 id（§5.2）

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---
## Task 13: `primitives` 层——`Approach` 按身份而非坐标 reset

修 spec §7 的发现：`Approach.tick()` 用 `cell != self._target_cell` 判断
"目标变了没有"，而移动平台上的站点每帧世界坐标都在变（§4.2），即使局部
格坐标不变、世界坐标在变，`target_cell()` 算出的格也可能每帧不同——这会
导致 `MotionControl`（含冲刺/震荡检测状态）每帧重置，巡航进度清零。

**Files:**
- Modify: `ai/primitives/base.py`（新增身份提取函数）
- Modify: `ai/primitives/move.py`（`Approach.tick`/`reset` 改用身份比较）
- Test: `tests/test_primitives.py`

**Interfaces:**
- Consumes: 无新依赖
- Produces: `target_identity(target_dict) -> Any`（返回 `resource_id()` 的值，
  或当站点没有 `n` 字段时退化为格坐标元组）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_primitives.py 追加
def test_approach_does_not_reset_motion_when_only_coordinates_move():
    """§5.4/§7：平台移动时站点坐标变化，但身份不变——不应清空巡航进度。"""
    calls = []

    class TrackingWorld(FakeWorld):
        def walkable(self, gx, gz):
            return True

    w = TrackingWorld(walkable=lambda gx, gz: True)
    target_pos = {"x": 0.0, "z": 0.0}

    def target(ctx):
        # 同一个站点（相同 n），坐标每拍变化——模拟平台移动
        return {"n": "crate_01", "grid": "Balloon Basket",
                "x": target_pos["x"], "z": target_pos["z"]}

    ctx, ctrl = make_ctx(w=w, start=(0, 0))
    node = PR.Approach(target)
    node.tick(ctx)
    motion_before = node._motion   # 内部 MotionControl 实例

    # 下一拍：平台把目标推移了 1.2（一格）
    target_pos["x"] = 1.2
    node.tick(ctx)
    motion_after = node._motion

    assert motion_before is motion_after, (
        "坐标变化但身份不变时，MotionControl 不应被 reset() 替换成新实例")


def test_approach_resets_motion_when_identity_changes():
    """对照：目标换成另一个站点（身份真的变了），必须 reset。"""
    w = FakeWorld(walkable=lambda gx, gz: True)
    targets = iter([
        {"n": "crate_01", "grid": "GridManager", "x": 0.0, "z": 0.0},
        {"n": "crate_02", "grid": "GridManager", "x": 1.2, "z": 0.0},
    ])

    def target(ctx):
        return next(targets, {"n": "crate_02", "grid": "GridManager", "x": 1.2, "z": 0.0})

    ctx, ctrl = make_ctx(w=w, start=(0, 0))
    node = PR.Approach(target)
    node.tick(ctx)
    motion_before = node._motion
    node.tick(ctx)
    motion_after = node._motion

    assert motion_before is not motion_after, "换了不同站点必须 reset"
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_primitives.py -k "approach_does_not_reset or approach_resets_motion_when_identity" -v`
Expected: 第一条 FAIL（当前实现按坐标 reset，坐标变了就会换新
`MotionControl` 实例）；第二条可能已经 PASS（坐标也变了，旧逻辑碰巧也
触发 reset）——这正常，不是本 task 要修的方向错误。

- [ ] **Step 3: 新增身份提取函数**

```python
# ai/primitives/base.py 追加（target_cell 函数之后）
def target_identity(target):
    # type: (Optional[Dict]) -> Any
    """目标的身份（spec §5.2/§7）：优先用 (grid, n)，没有 n 时退化为
    None（调用方据此退回按坐标比较——拼装格这类无实例名的目标仍只能
    按坐标判断"换了没有"，这是合理的，因为它们本来就没有稳定身份）。
    """
    if not target:
        return None
    n = target.get("n")
    if not n:
        return None
    return (target.get("grid"), str(n))
```

- [ ] **Step 4: 改 `ai/primitives/move.py` 的 `Approach`**

```python
# 在 ai/primitives/move.py 顶部 import 行追加 target_identity：
from ai.primitives.base import (Primitive, resolve, target_cell,
                                _dir_towards, _blocked_cells, target_identity)
```

```python
# Approach.__init__ 里追加一个字段：
        self._target_identity = None
```

```python
# Approach.reset() 里追加：
        self._target_identity = None
```

```python
# Approach.tick() 里，把：
#     if cell != self._target_cell:
#         self.reset()
#         self._target_cell = cell
# 改成：
        identity = target_identity(st)
        if identity is not None:
            # 有稳定身份（站点带 n）：身份不变就不 reset，即使坐标变了
            # （平台移动场景，§5.4/§7）。
            if identity != self._target_identity:
                self.reset()
                self._target_identity = identity
                self._target_cell = cell
            else:
                self._target_cell = cell   # 坐标仍要更新，供后续距离场计算用
        elif cell != self._target_cell:
            # 无稳定身份（拼装格等）：退回旧的按坐标比较。
            self.reset()
            self._target_cell = cell
```

- [ ] **Step 5: 运行确认通过**

Run: `python3 -m pytest tests/test_primitives.py -v`
Expected: 全部 PASS

- [ ] **Step 6: 运行完整测试套件**

Run: `python3 -m pytest tests/ -q`
Expected: 不新增失败。重点检查 `test_motion_control.py`/`test_motion_stall.py`
——这两个文件测的是 `MotionControl` 本身的行为，`Approach` 的 reset 时机
变化可能间接影响它们依赖的调用序列，若有失败需要诊断是否是本 task 改动
暴露了潜在问题，而不是机械改断言让它过。

- [ ] **Step 7: 提交**

```bash
git add ai/primitives/base.py ai/primitives/move.py tests/test_primitives.py
git commit -m "fix(primitives): Approach 按目标身份而非坐标 reset（修平台移动丢进度）

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---
## Task 14: `state_dump.py` 更新——trace 落 `grids`/`terrain`/`hazards`，去掉 `bounds`

spec §7：trace 需要能复盘"为什么选这个动作"，§5.3 之后可走性的依据从
`bounds`/`nodeMap` 变成 `terrain`/`occupancy`/`hazards`，trace 快照要跟着
换，否则事后排查又会对着一个不再被使用的字段。

**Files:**
- Modify: `ai/state_dump.py`
- Test: 新增 `tests/test_state_dump.py`（若不存在则新建；若已存在则追加）

**Interfaces:**
- Produces: `snapshot(world)["map"]` 不再含 `bounds`，新增 `grids`/
  `hazards`；保留 `belt_cells`/`belt_dir`（值的来源变了但字段形状不变）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_state_dump.py（新建或追加）
from ai.state_dump import snapshot
from ai.world import World


def test_snapshot_map_has_no_bounds_key():
    w = World({"grid": {"nodeMap": None}})
    snap = snapshot(w)
    assert "bounds" not in snap["map"]


def test_snapshot_map_includes_grids_and_hazards():
    w = World({
        "grid": {"nodeMap": None},
        "grids": [{"id": "GridManager", "ox": 9.6, "oz": -2.4, "cell": 1.2}],
        "hazards": [{"t": "fire", "grid": "GridManager", "x": 1.0, "z": 1.0, "ttl": 5.0}],
    })
    snap = snapshot(w)
    assert snap["map"]["grids"] == [{"id": "GridManager", "ox": 9.6, "oz": -2.4, "cell": 1.2}]
    assert len(snap["map"]["hazards"]) == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_state_dump.py -v`
Expected: FAIL（`bounds` 键仍存在；`grids`/`hazards` 键不存在）

- [ ] **Step 3: 改 `ai/state_dump.py` 的 `_map`**

```python
def _map(world):
    # type: (Any) -> Dict
    """地图 / 导航相关：多网格、传送带、落火、厨师所在格（§5.3/§5.7）。"""
    belt_cells = getattr(world, "belt_cells", None) or set()
    belt_dir = getattr(world, "belt_dir", None) or {}
    hazards = getattr(world, "hazards", None) or []
    grids = getattr(world, "grids", None)
    chefs = getattr(world, "chefs", None) or []
    p2 = P.chef_by_name(chefs, P.P2_NAME)
    p1 = P.chef_by_name(chefs, P.P1_NAME)
    grid = _raw_grid(world) or {}
    return {
        "grids": (getattr(world, "raw", {}) or {}).get("grids") or [],
        "grid": {
            "origin": grid.get("origin"),
            "size": grid.get("size"),
            "rows": getattr(world, "_nm_rows", 0),
            "cols": getattr(world, "_nm_cols", 0),
        },
        "hazards": [_copy(h) for h in hazards],
        "belt_cells": sorted([list(c) for c in belt_cells]),
        "belt_dir": _belt_dir(belt_dir),
        "p2_cell": _chef_cell(world, p2) if p2 else None,
        "p1_cell": _chef_cell(world, p1) if p1 else None,
    }
```

（`grids` 直接从 `world.raw["grids"]` 透传，不是从 `world.grids`
`GridSet` 对象取——`GridSet` 没有可序列化成 list 的公开方法，而原始
协议字段本身就是 trace 想要的形状，不需要转换一轮再转回去。）

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_state_dump.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 运行完整测试套件**

Run: `python3 -m pytest tests/ -q`
Expected: 不新增失败。

- [ ] **Step 6: 提交**

```bash
git add ai/state_dump.py tests/test_state_dump.py
git commit -m "feat(state_dump): trace 落 grids/hazards，移除已删除的 bounds（§5.3/§7）

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 15: 删除关卡硬编码表——`occupancy.BELTS`、`stations.PLATE_STACKS`/`RELAY_CELLS`

现在 `parse_belts`（Task 5）已经能从插件的 `belts` 字段产出同等结果
（§4.5 验证 18/18 一致），`World.belt_cells`/`belt_dir` 已经切到它
（Task 10）。本 task 删掉失去依据的硬编码表本身。

**Files:**
- Modify: `ai/occupancy.py`（删 `BELTS`、`belt_cells_and_dirs`）
- Modify: `ai/stations.py`（删 `PLATE_STACKS`、`RELAY_CELLS`、
  `relay_cells_for`；`find_plate_stack` 的兜底分支一并删除）
- Test: `tests/test_occupancy.py`、`tests/test_stations.py`

- [ ] **Step 1: 确认删除前没有遗留调用点**

```bash
grep -rn "occupancy\.BELTS\|belt_cells_and_dirs\|PLATE_STACKS\|RELAY_CELLS\|relay_cells_for" ai/ tests/
```

Expected: 只剩 `ai/occupancy.py`/`ai/stations.py` 自身的定义，以及
`tests/test_occupancy.py`/`tests/test_stations.py` 里**测试这些符号本身**
的用例（下一步会删除或改写这些测试）。若发现其它文件仍在调用，先处理
那些调用点（大概率是 Task 10 漏改的地方），不要在依赖未断开时删表。

- [ ] **Step 2: 删除 `ai/occupancy.py` 里的 `BELTS` 与 `belt_cells_and_dirs`**

删除文件顶部的 `BELTS = {...}` 字典定义（含其上的注释块）与
`belt_cells_and_dirs` 函数整体。

- [ ] **Step 3: 删除 `ai/stations.py` 里的硬编码表**

删除 `PLATE_STACKS`、`RELAY_CELLS`、`relay_cells_for` 三者。在
`find_plate_stack` 函数里，删除"②一个都没有 → PLATE_STACKS 实测坐标
兜底"这一整段分支（原函数的三级退化变成二级：真实站点 → 出餐口）：

```python
    if not stacks:
        return find_station(world.stations, PLATE_STATION, near)
```

（原来这两行之前还有一段 `for gx, gz in PLATE_STACKS.get(...)` 的循环，
整段删除。）

- [ ] **Step 4: 更新/删除测试这些符号的现有用例**

```bash
grep -n "BELTS\|PLATE_STACKS\|RELAY_CELLS\|relay_cells_for\|belt_cells_and_dirs" tests/test_occupancy.py tests/test_stations.py
```

对每条匹配：若测试断言的是"硬编码表在已知关卡下给出什么值"，删除该测试
（它测的能力已不存在）；若测试断言的是"未知关卡退化为空集，不崩"，这类
测试的精神被 Task 3/4 的"数据缺失时整体放行/返回空"覆盖，同样删除并在
注释里指向替代测试。

- [ ] **Step 5: 运行完整测试套件**

Run: `python3 -m pytest tests/ -q`
Expected: 不新增失败。

- [ ] **Step 6: 提交**

```bash
git add ai/occupancy.py ai/stations.py tests/test_occupancy.py tests/test_stations.py
git commit -m "refactor: 删除关卡硬编码表 BELTS/PLATE_STACKS/RELAY_CELLS（§5.4/§7）

数据驱动替代已在 Task 5/10 接入并验证（§4.5 传送带 18/18 格一致）。

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---
## Task 16: 跨网格寻路——世界坐标相邻判定（spec §8 测试 5）

当前 `navigation.distance_field` 用 `_NEIGHBORS = ((-1,0),(1,0),(0,-1),(0,1))`
在**同一套格坐标**里找四邻。多网格下，篮子格 `(2,-1)` 和主网格格
`(2,-1)` 是两个不同的世界位置（§4.1），不能直接用格坐标判相邻；而两个
网格的边界在世界坐标上只差 0.4（§4.3），小于一个格宽，物理上是相邻的。

**设计决定**（spec §7 提到"按世界坐标距离 ≤ cell"，这里给出可实现的
具体算法）：`distance_field` 本身不改——它仍在单一网格的格坐标系里跑
Dijkstra（因为 `World.walkable`/`to_grid`/`to_world` 都是主网格坐标系，
Task 10 已经确认这一点并记录为"已知限制"）。真正要新增的是**跨网格桥接
表**：在主网格坐标系的边界格与篮子（或任意附属网格）之间，插入一条
"虚拟邻接边"，供 `_predecessor`/`trace_back` 识别。

**Files:**
- Modify: `ai/navigation.py`
- Test: `tests/test_navigation.py`

**Interfaces:**
- Produces: `cross_grid_bridges(primary_to_world, other_cells_world, max_gap=0.5) -> Dict[Tuple[int,int], Tuple[int,int]]`
  （主网格边界格 → 对应的附属网格格，调用方按需正反各调一次构造双向映射）；
  `distance_field(..., bridges=None)` 新增可选参数

- [ ] **Step 1: 写失败测试**

```python
# tests/test_navigation.py 追加
from ai.navigation import cross_grid_bridges, distance_field


class _FakeMultiGridWorld:
    """最小化双网格 World：主网格 x[0,12]，附属网格 x[16,24]，世界坐标
    只差 0.4（§4.3），用于验证跨网格桥接。"""

    def __init__(self):
        self.grids = None  # 本测试不需要真的 GridSet，用 to_world 模拟
        self.belt_dir = {}

    def to_world(self, gx, gz):
        return (gx * 1.2, gz * 1.2)   # 主网格坐标系

    def walkable(self, gx, gz):
        return -10 <= gx <= 10 and -10 <= gz <= 10


def test_cross_grid_bridges_connects_adjacent_grids():
    """给定两网格的世界坐标映射，边界格互相可达（§4.3：间隙 0.4 < cell）。"""
    w = _FakeMultiGridWorld()
    # 主网格 gx=12 (世界 x=14.4) 与附属网格某格世界 x=14.8 相邻（间隙 0.4）
    bridges = cross_grid_bridges(
        primary_to_world=w.to_world,
        other_cells_world=[((20, -2), (14.8, -2.4))],  # (附属网格局部格, 世界坐标)
        max_gap=0.5,
    )
    assert (12, -2) in bridges   # 主网格边界格有一条到附属网格的桥


def test_distance_field_crosses_bridge():
    w = _FakeMultiGridWorld()
    bridges = {(5, 0): (100, 0)}   # 虚拟映射：主网格(5,0) <-> 附属(100,0)
    field = distance_field((0, 0), w.walkable, {}, bridges=bridges)
    assert (5, 0) in field
    assert (100, 0) in field
    assert field[(100, 0)] == field[(5, 0)] + 1
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_navigation.py -k "cross_grid or distance_field_crosses_bridge" -v`
Expected: FAIL（`ImportError: cannot import name 'cross_grid_bridges'`）

- [ ] **Step 3: 实现**

```python
# ai/navigation.py 追加

def cross_grid_bridges(primary_to_world, other_cells_world, max_gap=0.5):
    # type: (Callable[[int,int], Tuple[float,float]], List[Tuple[Tuple[int,int],Tuple[float,float]]], float) -> Dict[Tuple[int,int], Tuple[int,int]]
    """主网格边界格与其它网格格之间的虚拟邻接（spec §4.3/§7）。

    两个网格世界坐标相距 < max_gap 即算相邻——§4.3 实测两网格间隙 0.4，
    远小于格宽 1.2，物理上贴着。扫描范围只需主网格附近有限格（调用方
    传入的 primary 候选格集合，不在这里枚举全图——避免 O(全图×全图)）。

    返回值只含"确实找到桥"的格，调用方（distance_field）据此在
    Dijkstra 展开时把它们当四邻的第五个方向处理。
    """
    bridges = {}  # type: Dict[Tuple[int, int], Tuple[int, int]]
    # 候选主网格格：对每个附属格世界坐标，反推可能邻接的主网格格范围
    # （世界坐标差 < max_gap 的格，格宽通常 1.2，候选最多 3x3）。
    for other_cell, (ox, oz) in other_cells_world:
        # 简化：由调用方保证 other_cells_world 已经是"值得检查"的边界候选，
        # 这里只需要在主网格坐标系里反推最近格并验证距离。
        best = None
        best_d = None
        for gx in range(-50, 51):           # 实际使用中由调用方传入更紧的范围
            for gz in range(-50, 51):
                px, pz = primary_to_world(gx, gz)
                d = ((px - ox) ** 2 + (pz - oz) ** 2) ** 0.5
                if best_d is None or d < best_d:
                    best, best_d = (gx, gz), d
        if best is not None and best_d <= max_gap:
            bridges[best] = other_cell
    return bridges


def distance_field(start, is_walkable, belt_dir, blocked=None, max_cells=4096,
                   soft_cost=None, bridges=None):
    # type: (Tuple[int, int], Callable[[int, int], bool], Dict, Optional[Set], int, Optional[Dict], Optional[Dict]) -> Dict[Tuple[int, int], int]
    """从 start 做 Dijkstra，返回 {格子: 最小代价}。

    bridges（新增，spec §4.3/§7）：{主网格格: 附属网格格} 的虚拟邻接表。
    展开到桥格时代价记 1（与普通一步相同），桥对面的格子不再受
    is_walkable 约束（附属网格的可走性由另一次独立调用覆盖——当前实现
    不递归处理"桥接链"，一次只跨一层，气球关只有两网格，够用）。
    """
    blocked = set(blocked or ())
    soft_cost = soft_cost or {}
    bridges = bridges or {}
    dist = {start: 0}
    pq = [(0, start)]
    while pq:
        if len(dist) > max_cells:
            break
        cost, cur = heapq.heappop(pq)
        if cost > dist.get(cur, 1 << 60):
            continue
        neighbors = list(_NEIGHBORS)
        for dx, dz in neighbors:
            nxt = (cur[0] + dx, cur[1] + dz)
            if nxt in blocked:
                continue
            if not is_walkable(nxt[0], nxt[1]):
                continue
            ncost = cost + step_cost(belt_dir, cur, nxt) + soft_cost.get(nxt, 0)
            if ncost < dist.get(nxt, 1 << 60):
                dist[nxt] = ncost
                heapq.heappush(pq, (ncost, nxt))
        bridge_target = bridges.get(cur)
        if bridge_target is not None and bridge_target not in blocked:
            ncost = cost + 1
            if ncost < dist.get(bridge_target, 1 << 60):
                dist[bridge_target] = ncost
                heapq.heappush(pq, (ncost, bridge_target))
    return dist
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest tests/test_navigation.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 运行完整测试套件（含 primitives 调用 distance_field 的调用点）**

Run: `python3 -m pytest tests/ -q`
Expected: 不新增失败——`bridges` 是新增可选参数，默认 `None`，现有调用点
不传则行为不变。

- [ ] **Step 6: 提交**

```bash
git add ai/navigation.py tests/test_navigation.py
git commit -m "feat(navigation): 跨网格虚拟邻接桥接（§4.3/§7，气球关两半恒连通）

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

**已知限制（记录，不在本次修）**：`cross_grid_bridges` 没有被
`ai/agent.py`/`ai/primitives/move.py` 实际接入——它们目前仍只用主网格
坐标系跑 `distance_field`，不传 `bridges` 参数。接入需要先解决"篮子上的
目标站点如何从主网格视角定位"这个更大的问题（本计划的 §5.1 决定了
`World.to_grid`/`walkable` 停留在主网格坐标系，Task 10 已记录为已知
限制）。本 task 只交付"桥接算法本身正确"，接入是后续工作，不计入本次
验收范围——spec §3 的目标是"地图表示对任意关卡正确"，不是"跨网格寻路
已接入执行层"，两者是分开的里程碑。

---
## Task 17: 更新 `AGENTS.md`——资源 id 规则与已知限制

`AGENTS.md` §6.3 记录的资源 id 规则（"站点用实例名 n"）在本计划后已不
准确；§8 已知限制里的关卡硬编码条目已被删除。文档要跟代码同步，否则
下一个读 `AGENTS.md` 的人会按旧规则写新代码。

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: 更新 §6.3 资源 id 规则**

把原文「灶 / 板 / 洗碗池 → 站点实例名 n（稳定）」改为：

```
灶 / 板 / 洗碗池 → (网格名, 站点实例名)（spec 2026-10-15 地图重构后；
  同名站点可能分属不同网格，§5.2）
```

- [ ] **Step 2: 更新 §8 已知限制**

删除「关卡硬编码：传送带（occupancy.BELTS）、盘堆兜底坐标
（stations.PLATE_STACKS）目前只对 Sushi_1_4_2P 标注」这一条（已解决）。
新增：

```
- **跨网格寻路未接入执行层**：`navigation.cross_grid_bridges` 算法已交付
  并测试（2026-10-15 地图重构 Task 16），但 `agent.py`/`primitives` 尚未
  接入——当前 `World.walkable`/`to_grid` 仍是单一主网格坐标系。篮子类
  关卡的跨网格寻路要先解决"附属网格目标如何从主网格视角定位"再接。
```

- [ ] **Step 3: 更新 spec 的风险表与 Task 8 的验证结论**

在 `docs/superpowers/specs/2026-10-15-map-grid-redesign-design.md` 的
§10 风险表里，把「staleness 阈值（10%）未经真实回归调校」这一行删除——
Task 7/8 的实施发现不需要阈值检测，`stations` 改成每帧重扫直接解决了
根因（见 Task 7 Step 11 的说明）。在该行原位置的脚注里补一句：

```
**实施记录（Task 7/8）**：spec 原计划的"结构跳变阈值检测"（§5.6）在
实施时被更根本的修法取代——stations 坐标本不该被缓存（它们属于§5.5
"移入每帧动态"一侧），直接让 AppendStations 每帧重扫 _all，和
cookers/plates/items 的现行做法一致。这同时解决了 §4.9 的问题，不需要
任何跳变阈值。recipes 仍按换关重建（§4.9 实测其内容不随阶段切换变化，
无需改动）。
```

- [ ] **Step 4: 提交**

```bash
git add AGENTS.md docs/superpowers/specs/2026-10-15-map-grid-redesign-design.md
git commit -m "docs: 更新 AGENTS.md 资源 id 规则，记录 §5.6 的实施替代方案

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Task 19（新增，controller ruling——Task 17 之后的最终全branch review 发现）

见独立 brief 文件 `.superpowers/sdd/2026-10-15-map-grid-redesign/task-19-brief.md`
（全branch review 是在 Task 17 完成之后才发起的，这个任务本身晚于本计划
原始的 0-18 编号，故未重排原有任务号，只在此处占位说明依赖顺序：
**Task 19 必须在 Task 18 之前完成**——它修的是 Task 18 实机验收气球关时
几乎必然会撞到的一个寻路 bug，提前修掉能让 Task 18 的验收更顺利，省去
一轮"实机复现→回来改代码→重新部署"的往返）。

一句话概括：`target_cell`/`_target_cell`（`ai/primitives/base.py`/
`ai/predicates.py`）优先用站点自带的局部 `gx`/`gz`，而非站点的世界坐标
`x`/`z` 投影，导致附属网格（气球关的篮子）上的站点被误当成主网格坐标，
AI 去篮子取料会走错地方。修法：反转判定顺序，世界坐标优先，`gx`/`gz`
退化为"仅当 x/z 缺失时"的兜底（旧测试数据兼容）。不涉及真正的跨网格
寻路（仍是 Task 16 的已知限制，不受影响）。

---

## Task 18: 全量回归 + 实机验收（全部真机操作集中在此——用户确认后执行）

**本任务是全计划里唯一需要打开游戏的地方**（用户要求：先完成全部编码，
由用户确认后统一做实机测试）。Task 7-17 全部是纯代码改动 + `pytest`/
`./plugin/build.sh`，到这里为止都不需要游戏进程。本任务把 Task 9（录新
协议 fixture）和 Task 8（验证坠毁后 stations 刷新）的实际执行并入这里，
按先录制、再验收的顺序排列。

**Files:** 新建 `tests/fixtures/*.ndjson.gz` 三份（即 Task 9 产出）；
修改 `docs/superpowers/specs/2026-10-15-map-grid-redesign-design.md`
（记录验收结果）。无其它代码改动。

- [ ] **Step 1: 全量单测基线（部署前先跑一次，确认 Task 7-17 的改动本身没问题）**

Run: `python3 -m pytest tests/ -v 2>&1 | tail -60`
Expected: 0 failed。记录 passed/skipped 具体数字，与 Task 0 之前的基线
（AGENTS.md §10 记的 1090 passed, 78 skipped）对比。此时 Task 9 还没
执行，依赖真实 fixture 的几条真实帧回归测试（Task 10/16 新增的那些）
应为 SKIPPED，不是 FAIL——这是预期状态。

- [ ] **Step 2: 部署插件**

```bash
G="$HOME/Library/Application Support/Steam/steamapps/common/Overcooked! 2"
./plugin/build.sh && cp plugin/build/Oc2AIPlugin.dll "$G/BepInEx/plugins/"
```

- [ ] **Step 3: 录制新协议 fixture（原 Task 9 的内容，在此执行）**

进 `Balloon_1_5_2P` 后：

```bash
cd /Users/dongyi/Documents/coding/oc2
python3 tools/record_frames.py --out /tmp/balloon_v2.ndjson --seconds 90
```

进 `Sushi_1_4_2P` 后：

```bash
python3 tools/record_frames.py --out /tmp/sushi14_v2.ndjson --seconds 60
```

进 `1_6_Dynamic_Lvl_01_2P` 后，耐心玩到坠毁+至少一次火雨发生：

```bash
python3 tools/record_frames.py --out /tmp/dynamic16_v2.ndjson --seconds 180
```

验证三份录制都带新字段，压缩入库：

```bash
python3 -c "
import json, gzip

def check_and_compress(src, dst, want_keys):
    frames = [json.loads(l) for l in open(src)]
    assert len(frames) > 50, '%s 帧数太少: %d' % (src, len(frames))
    has_any = any(any(f.get(k) for k in want_keys) for f in frames)
    assert has_any, '%s 没有任何帧带 %s' % (src, want_keys)
    with open(src, 'rb') as f_in, gzip.open(dst, 'wb') as f_out:
        f_out.writelines(f_in)
    print(dst, len(frames), 'frames OK')

check_and_compress('/tmp/balloon_v2.ndjson',
                    'tests/fixtures/balloon_1_5_v2.ndjson.gz',
                    ['grids', 'terrain', 'occupancy'])
check_and_compress('/tmp/sushi14_v2.ndjson',
                    'tests/fixtures/sushi_1_4_belts.ndjson.gz',
                    ['belts'])
check_and_compress('/tmp/dynamic16_v2.ndjson',
                    'tests/fixtures/dynamic_1_6_v2.ndjson.gz',
                    ['hazards'])
"
```

Expected: 三行 `... OK`，无 AssertionError。若 `dynamic_1_6_v2` 的
hazards 检查失败（没等到火雨），重新录制该关，加长 `--seconds`。

提交：

```bash
git add tests/fixtures/balloon_1_5_v2.ndjson.gz \
        tests/fixtures/sushi_1_4_belts.ndjson.gz \
        tests/fixtures/dynamic_1_6_v2.ndjson.gz
git commit -m "test(fixtures): 新协议真实帧——气球关/1-4/1-6（grids/terrain/belts/hazards）

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

- [ ] **Step 4: 全量单测复跑（真实 fixture 已入库，之前 SKIP 的几条应变 PASS）**

Run: `python3 -m pytest tests/ -v 2>&1 | tail -60`
Expected: 0 failed，skipped 数比 Step 1 更低（Task 10/16 依赖真实帧的
回归测试现在能跑了）。

- [ ] **Step 5: 验证 §4.9（原 Task 8 的内容，在此执行）**

```bash
mv "$G/BepInEx/LogOutput.log" /tmp/log-before-crash-check.log 2>/dev/null
```

重启游戏，进 1-6，玩到坠毁发生（时刻不固定，1~2 分钟内会发生）。坠毁
前后各采样一次：

```bash
python3 -c "
import socket, json
s = socket.create_connection(('127.0.0.1', 27411), timeout=10)
buf = b''
while b'\n' not in buf: buf += s.recv(65536)
d = json.loads(buf.split(b'\n')[0])
print('stations count:', len(d.get('stations') or []))
print('recipes count:', len(d.get('recipes') or []))
"
```

Expected: 坠毁前采样 `stations count` 约 22；坠毁后再采样一次（同一个
游戏进程，不重启），`stations count` 应跳到约 65（§4.9 实测）。
`recipes count` 两次采样都应是 115，不变。**若坠毁后 `stations count`
未更新，停下来诊断**——回 Task 7 Step 11 检查 `AppendStations` 是否被
正确接入 `BuildState()`，不要在这里补一个 staleness 检测绕过去。

- [ ] **Step 6: 气球关 Agent 级验收**

```bash
"$G/run_game.sh"   # 另开终端或人工启动，进 Balloon_1_5_2P
python3 run_agent.py --dry-run   # 先 dry-run 看决策不注入按键
```

Expected: trace 里 AI 能找到并走到右侧篮子上的料箱（不再是 §1 描述的
"全程跑 FillCooker，10 次 FAILURE，65% 拍在 observe"模式）。用
`tools/exec_cost.py` 或直接读最新 `trace-*.jsonl` 确认 `FAILURE` 比例
显著下降。

- [ ] **Step 7: 1-6 Agent 级验收**

进 `1_6_Dynamic_Lvl_01_2P`，`python3 run_agent.py --dry-run`，玩到坠毁
发生。确认：
- `stations` 数量在坠毁后自动变多（复核 Step 5 已经验证过的事实，这里
  是在 Agent 真正运行时确认同一件事，而不是直接读裸协议）
- Agent 的 trace 里坠毁后出现新站点类型的候选（寿司厨房的灶/传送带）
- 落火出现时，AI 不会尝试走进落火格（可用 trace 的 `exec.motion.target`
  字段核对，不应等于任何当前存活 hazard 的格）

- [ ] **Step 8: 记录实机验收结果**

把 Step 5/6/7 的观察结果（成功与否、仍有的问题）追加到
`docs/superpowers/specs/2026-10-15-map-grid-redesign-design.md` 的
"风险与待办"一节末尾，格式参照该文档现有的"本轮（日期）"记录风格
（AGENTS.md §10 的写法）。**这一步不是可选的**——spec §9 强调"全部改动
只做过离线验证，实机效果待下一局"，本 task 就是补上那条"下一局"的实测
闭环，缺了它整个计划就只验证了"代码逻辑自洽"而非"实机真的工作"。

- [ ] **Step 9: 最终提交**

```bash
git add docs/superpowers/specs/2026-10-15-map-grid-redesign-design.md
git commit -m "docs: 记录地图重构实机验收结果（气球关 + 1-6）

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

# Plan 1：地基（录帧 + 测试框架 + 纯谓词 + 黑板）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为行为决策架构重构打好地基：建立录帧回放能力、pytest 测试框架、无副作用谓词层、以及带完整生命周期的 Blackboard，使后续三份计划的每一步都能离线验证。

**Architecture:** 新代码写在 `oc2/ai/` 下（全新目录，不动 `oc/` 原始代码）。本计划只产出**被测试调用的库代码**，不改动运行时决策路径——`oc/` 里的 AI 照旧能跑。谓词层从 `oc/ai/agent.py` 抽取为纯函数（查询与写入分离），黑板取代散落的跨拍字段。

**Tech Stack:** Python 3.9.6（**不可用 `match` 语句、不可用 `list[int]` 式内置泛型、不可用 `X | Y` 类型联合**，需用 `typing.List` / `typing.Optional`）、pytest、dataclasses、标准库 socket/json。

**Spec:** `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md`

## Global Constraints

- **Python 3.9.6**：禁用 `match`、内置泛型下标（`list[int]`）、`X | Y` 联合类型。用 `typing.List/Dict/Optional/Tuple`。
- **C# 插件与 socket 传输层本轮不改**（spec 行 4）。传输维持：插件推 5 Hz，Python 主循环 20 Hz。
- **源目录**：新代码一律写在 `/Users/dongyi/Documents/coding/oc2/ai/`。`/Users/dongyi/Documents/coding/oc/` 为只读参照，**不得修改**。
- **谓词必须无副作用**（spec §4.5、§11.2 阶段 1）：查询函数禁止写入任何状态。原 `_station()` 写 `self.pot`、`_should_rush_cooked()` 写 `self.pot` 的行为，改为由调用点显式写入。
- **黑板写入权受控**（spec §4.5）：Planner 可写 `claims`/`commitment`；P1Model 只写 `p1_model`；Executor 只写 `commitment.steps_done`/`commitment.retries`/`scratch`，**不可写 `claims`**。
- **scratch 必须类型化**（spec §4 行 154）：每个中断模式一个显式 dataclass，禁止跨模式复用字段，禁止无类型字典。
- **认领键是 `(resource_id, action)` 动作级粒度**（spec §4.0），不是 `resource_id`。
- 提交信息用中文，结尾加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。

---

## 文件结构

本计划产出的文件及各自职责：

| 文件 | 职责 | 行数预估 |
|---|---|---|
| `ai/record.py` | 录帧：把 socket 收到的原始行 tee 到 NDJSON 文件 | ~40 |
| `ai/replay.py` | 回放：读 NDJSON 逐帧产出 dict，供离线测试驱动 | ~40 |
| `ai/geometry.py` | 网格 ↔ 世界坐标换算（`mapOffset` 公式，取代 `GRID_OX/OZ` 硬编码） | ~50 |
| `ai/navigation.py` | flood-fill 全图距离场 + 可走格判定 | ~90 |
| `ai/predicates.py` | 无副作用世界查询谓词（手持 / 盘 / 锅 / 站点定位） | ~250 |
| `ai/blackboard.py` | Blackboard dataclass + 认领增删查 + 类型化 scratch | ~150 |
| `ai/reconcile.py` | `reconcile_claims()` 认领对账与四个释放点 | ~110 |
| `tests/conftest.py` | pytest fixture：加载录制帧、构造假 World | ~60 |
| `tests/test_geometry.py` | 坐标公式单测 | ~50 |
| `tests/test_navigation.py` | 距离场单测 | ~60 |
| `tests/test_predicates.py` | 谓词单测 + 无副作用断言 | ~120 |
| `tests/test_blackboard.py` | 认领生命周期单测 | ~130 |
| `tests/test_reconcile.py` | 对账单测（含"盘被端起不误杀"） | ~110 |
| `ai/bb_factory.py` | `make_bb` + 对账视图构造（资源 id 规则收口） | ~70 |
| `tools/record_frames.py` | 录帧 CLI（只读 socket，不注入按键） | ~60 |
| `tests/test_bb_factory.py` | 工厂单测 | ~80 |
| `tests/test_real_frames.py` | 真实帧验证（网格公式零违例、谓词不崩） | ~110 |
| `tests/test_parity.py` | 录帧重放：新旧谓词逐帧一致性对比 | ~90 |
| `docs/baseline-audit.md` | 阶段 0 核查：文档声称已修但实际不存在的项 | ~20 |

**拆分理由**：`geometry` / `navigation` 从原 `world.py` 的 306 行里分出来，因为两者都是纯计算、可独立单测，且 `navigation` 要新增 flood-fill（spec §6.4 的 ETA 依赖它）。`predicates` 与 `blackboard` 分离是 spec §4.5 写入权控制的前提——谓词只读，黑板可写。

**不在本计划内**：`primitives.py` / `tasks.py` / `planner.py` / `arbiter.py` / `agent.py` 属 Plan 2-4。

---

### Task 1: 测试框架与录帧能力

项目当前零测试、无 pytest（已核实）。本任务同时解决"能测"和"有数据可测"两件事——它们必须一起完成，因为后续每个任务的验证都依赖录制的真实帧。

**Files:**
- Create: `/Users/dongyi/Documents/coding/oc2/requirements-dev.txt`
- Create: `/Users/dongyi/Documents/coding/oc2/pytest.ini`
- Create: `/Users/dongyi/Documents/coding/oc2/ai/__init__.py`
- Create: `/Users/dongyi/Documents/coding/oc2/ai/record.py`
- Create: `/Users/dongyi/Documents/coding/oc2/ai/replay.py`
- Create: `/Users/dongyi/Documents/coding/oc2/tests/__init__.py`
- Test: `/Users/dongyi/Documents/coding/oc2/tests/test_replay.py`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces:
  - `record.FrameRecorder(path)` — `.write(raw_line: bytes) -> None`、`.close() -> None`
  - `replay.load_frames(path) -> List[dict]`
  - `replay.iter_frames(path) -> Iterator[dict]`

- [ ] **Step 1: 写依赖与 pytest 配置**

`requirements-dev.txt`:
```
pytest==7.4.4
```

`pytest.ini`:
```ini
[pytest]
testpaths = tests
pythonpath = .
python_files = test_*.py
```

- [ ] **Step 2: 安装 pytest 并确认可用**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pip install -r requirements-dev.txt && python3 -m pytest --version`
Expected: 打印 `pytest 7.4.4`

- [ ] **Step 3: 写失败的测试**

`tests/__init__.py` 和 `ai/__init__.py` 都写空文件。

`tests/test_replay.py`:
```python
import json

from ai.replay import iter_frames, load_frames


def test_load_frames_reads_ndjson(tmp_path):
    p = tmp_path / "frames.ndjson"
    p.write_text('{"tick": 1}\n{"tick": 2}\n', encoding="utf-8")

    frames = load_frames(str(p))

    assert frames == [{"tick": 1}, {"tick": 2}]


def test_load_frames_skips_blank_and_malformed_lines(tmp_path):
    p = tmp_path / "frames.ndjson"
    p.write_text('{"tick": 1}\n\nnot-json\n{"tick": 2}\n', encoding="utf-8")

    frames = load_frames(str(p))

    assert frames == [{"tick": 1}, {"tick": 2}]


def test_iter_frames_is_lazy(tmp_path):
    p = tmp_path / "frames.ndjson"
    p.write_text('{"tick": 1}\n{"tick": 2}\n', encoding="utf-8")

    it = iter_frames(str(p))

    assert next(it) == {"tick": 1}
    assert next(it) == {"tick": 2}


def test_recorder_writes_raw_lines(tmp_path):
    from ai.record import FrameRecorder

    p = tmp_path / "out.ndjson"
    rec = FrameRecorder(str(p))
    rec.write(b'{"tick": 1}')
    rec.write(b'{"tick": 2}\n')
    rec.close()

    lines = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln]
    assert lines == [{"tick": 1}, {"tick": 2}]
```

- [ ] **Step 4: 运行测试确认失败**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_replay.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai.replay'`

- [ ] **Step 5: 实现 replay.py**

```python
"""录制帧回放：把 NDJSON 帧文件读回 dict 序列，供离线测试驱动。"""
import json
from typing import Dict, Iterator, List


def iter_frames(path):
    # type: (str) -> Iterator[Dict]
    """逐行产出帧。跳过空行与解析失败的行（录制可能在中途被 kill）。"""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def load_frames(path):
    # type: (str) -> List[Dict]
    """一次性读入全部帧。"""
    return list(iter_frames(path))
```

- [ ] **Step 6: 实现 record.py**

```python
"""录帧：把 socket 收到的原始行 tee 到 NDJSON 文件。

用途见 spec §11.2 阶段 0：录制的原始帧是后续每个阶段做离线回归的唯一依据。
只追加写、不解析，保证录制本身不会因为解析 bug 丢数据。
"""


class FrameRecorder:
    def __init__(self, path):
        # type: (str) -> None
        self.path = path
        self._f = open(path, "ab")

    def write(self, raw_line):
        # type: (bytes) -> None
        """写一行原始帧。入参不含换行时自动补。"""
        if not raw_line.endswith(b"\n"):
            raw_line = raw_line + b"\n"
        self._f.write(raw_line)
        self._f.flush()

    def close(self):
        # type: () -> None
        if self._f is not None:
            self._f.close()
            self._f = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
```

- [ ] **Step 7: 运行测试确认通过**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_replay.py -v`
Expected: 4 passed

- [ ] **Step 8: 提交**

```bash
cd /Users/dongyi/Documents/coding/oc2
git add requirements-dev.txt pytest.ini ai/__init__.py ai/record.py ai/replay.py tests/__init__.py tests/test_replay.py
git commit -m "$(cat <<'EOF'
test: 搭建 pytest 框架与录帧/回放能力

spec §11.2 阶段 0 的前置：录制的原始 NDJSON 帧是后续
每个阶段做离线回归的唯一依据。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: 网格坐标换算（删除 GRID_OX/OZ 硬编码）

> ✅ **已完成（commit 30b886d），但实现与下方原文不同——以实现为准。**
> 实施时发现下方的 `GRID_OX = size.x * mapOffset.X` 公式**算术为假**（算出 9.6/-7.2，实测原点 14.4/-6.0）：`mapOffset(8,6)` 是 nodeMap 17×13 的中心下标，纯索引量，推不出世界原点。已改为**从站点反解** `origin = coord - cell*g`（61/61 一致，取众数防脏数据），`from_grid(grid, stations=None)` 签名多一个可选 `stations` 参数，无站点回落 `ORIGIN_*_DEFAULT=14.4/-6.0`。`node_index` 仍用 `mapOffset`（已验证正确）。详见 spec §9 修正块与 Ruling 8。下方 Step 1/3 的公式代码**不要照抄**。

spec §9 已用实测帧做线性回归验证了公式：`GRID_OX = size.x * mapOffset.X`、`GRID_OZ = -size.z * mapOffset.Y`。61/61 站点零违例。原代码 `oc/ai/world.py:45-46` 的 `GRID_OX=14.4` / `GRID_OZ=-6.0` 是这个公式在 Sushi_1_4 的特例，可以删掉。

**注意符号不对称**（z 取负）只有一帧样本支撑，故实现保留兜底常量并在 docstring 写明风险。

**Files:**
- Create: `/Users/dongyi/Documents/coding/oc2/ai/geometry.py`
- Test: `/Users/dongyi/Documents/coding/oc2/tests/test_geometry.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `geometry.GridMapping(cell: float, off_x: int, off_z: int)` — dataclass
  - `GridMapping.from_grid(grid: dict) -> GridMapping` — 从快照 `state["grid"]` 构造
  - `GridMapping.origin_x` / `.origin_z` — property，返回推导出的 `GRID_OX` / `GRID_OZ`
  - `GridMapping.to_world(gx: int, gz: int) -> Tuple[float, float]`
  - `GridMapping.to_grid(x: float, z: float) -> Tuple[int, int]`
  - `GridMapping.node_index(gx: int, gz: int) -> Tuple[int, int]` — 网格坐标 → nodeMap 下标
  - `geometry.parse_vec(s: str) -> Optional[Tuple[float, ...]]` — 从 `oc/ai/world.py:24` 平移

- [ ] **Step 1: 写失败的测试**

`tests/test_geometry.py`:
```python
import pytest

from ai.geometry import GridMapping, parse_vec


def test_parse_vec_parses_triple():
    assert parse_vec("(2.4, 0.0, 0.0)") == (2.4, 0.0, 0.0)


def test_parse_vec_returns_none_on_junk():
    assert parse_vec("null") is None
    assert parse_vec("") is None
    assert parse_vec(None) is None


# 实测帧（spec §9）：size=(1.2,1.0,1.2), mapOffset="8,6"
SUSHI_GRID = {"size": "(1.2, 1.0, 1.2)", "origin": "(2.4, 0.0, 0.0)", "mapOffset": "8,6"}


def test_from_grid_derives_sushi_origins():
    """公式必须复现原硬编码值 GRID_OX=14.4 / GRID_OZ=-6.0。"""
    m = GridMapping.from_grid(SUSHI_GRID)

    assert m.cell == pytest.approx(1.2)
    assert m.off_x == 8
    assert m.off_z == 6
    assert m.origin_x == pytest.approx(14.4)
    assert m.origin_z == pytest.approx(-6.0)


def test_to_world_matches_linear_formula():
    m = GridMapping.from_grid(SUSHI_GRID)

    assert m.to_world(0, 0) == (pytest.approx(14.4), pytest.approx(-6.0))
    assert m.to_world(1, 1) == (pytest.approx(15.6), pytest.approx(-4.8))
    assert m.to_world(-4, 2) == (pytest.approx(9.6), pytest.approx(-3.6))


def test_to_grid_is_inverse_of_to_world():
    m = GridMapping.from_grid(SUSHI_GRID)

    for gx in range(-6, 7):
        for gz in range(-5, 6):
            x, z = m.to_world(gx, gz)
            assert m.to_grid(x, z) == (gx, gz)


def test_to_grid_rounds_to_nearest_cell():
    m = GridMapping.from_grid(SUSHI_GRID)

    # 稍微偏离格心仍应归到同一格
    assert m.to_grid(14.4 + 0.3, -6.0 - 0.3) == (0, 0)


def test_node_index_applies_map_offset():
    """反编译确认 nodeMap[i,j] == 网格[gx+off_x, gz+off_z]（spec §9）。"""
    m = GridMapping.from_grid(SUSHI_GRID)

    assert m.node_index(0, 0) == (8, 6)
    assert m.node_index(-4, 2) == (4, 8)


def test_from_grid_falls_back_when_map_offset_missing():
    m = GridMapping.from_grid({"size": "(1.2, 1.0, 1.2)"})

    assert m.off_x == 8
    assert m.off_z == 6


def test_from_grid_falls_back_on_malformed_map_offset():
    m = GridMapping.from_grid({"size": "(1.2, 1.0, 1.2)", "mapOffset": "garbage"})

    assert (m.off_x, m.off_z) == (8, 6)


def test_from_grid_defaults_cell_when_size_missing():
    m = GridMapping.from_grid({})

    assert m.cell == pytest.approx(1.2)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_geometry.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai.geometry'`

- [ ] **Step 3: 实现 geometry.py**

```python
"""网格 ↔ 世界坐标换算。

取代 oc/ai/world.py 的 GRID_OX=14.4 / GRID_OZ=-6.0 硬编码。
公式（spec §9，61/61 站点线性回归零违例）：

    x = size.x * mapOffset.X + cell * gx
    z = -size.z * mapOffset.Y + cell * gz

即 GRID_OX = size.x * mapOffset.X、GRID_OZ = -size.z * mapOffset.Y。

⚠️ z 取负的符号不对称只有一帧（Sushi_1_4）样本支撑。换关卡时应取一帧
验证符号；若不符，DEFAULT_* 兜底常量仍可用。
"""
from typing import Dict, Optional, Tuple

OFF_X_DEFAULT = 8
OFF_Z_DEFAULT = 6
CELL_DEFAULT = 1.2


def parse_vec(s):
    # type: (Optional[str]) -> Optional[Tuple[float, ...]]
    """解析插件输出的 "(x, y, z)" 字符串。"""
    if not s or s == "null":
        return None
    try:
        return tuple(float(v) for v in s.strip("()").split(","))
    except (ValueError, AttributeError):
        return None


class GridMapping:
    """一个关卡的网格映射。从 state["grid"] 构造，之后只读。"""

    def __init__(self, cell, off_x, off_z):
        # type: (float, int, int) -> None
        self.cell = cell
        self.off_x = off_x
        self.off_z = off_z

    @classmethod
    def from_grid(cls, grid):
        # type: (Dict) -> "GridMapping"
        grid = grid or {}
        size = parse_vec(grid.get("size"))
        cell = size[0] if size else CELL_DEFAULT

        off_x, off_z = OFF_X_DEFAULT, OFF_Z_DEFAULT
        mo = grid.get("mapOffset") or ""
        if "," in mo:
            parts = mo.split(",")
            try:
                off_x = int(float(parts[0]))
                off_z = int(float(parts[1]))
            except (ValueError, IndexError):
                pass
        return cls(cell, off_x, off_z)

    @property
    def origin_x(self):
        # type: () -> float
        return self.cell * self.off_x

    @property
    def origin_z(self):
        # type: () -> float
        return -self.cell * self.off_z

    def to_world(self, gx, gz):
        # type: (int, int) -> Tuple[float, float]
        return (self.origin_x + self.cell * gx, self.origin_z + self.cell * gz)

    def to_grid(self, x, z):
        # type: (float, float) -> Tuple[int, int]
        gx = int(round((x - self.origin_x) / self.cell))
        gz = int(round((z - self.origin_z) / self.cell))
        return (gx, gz)

    def node_index(self, gx, gz):
        # type: (int, int) -> Tuple[int, int]
        """网格坐标 → nodeMap 下标。dim0=gx+off_x, dim1=gz+off_z。"""
        return (gx + self.off_x, gz + self.off_z)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_geometry.py -v`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
cd /Users/dongyi/Documents/coding/oc2
git add ai/geometry.py tests/test_geometry.py
git commit -m "$(cat <<'EOF'
feat: 网格坐标换算改用 mapOffset 公式

删除 GRID_OX=14.4 / GRID_OZ=-6.0 硬编码，改为 spec §9
验证过的公式推导。换关卡无需改代码。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: flood-fill 全图距离场

spec §6.4 的 ETA 打分项要对**每个候选 task** 算距离。原代码每次调 `bfs_to_station()` 单独搜一次，候选数一多就 O(候选数 × 图)。改为每拍从厨师位置 flood-fill 一次，得到全图距离，之后所有 ETA 都是查表 O(1)。

**关键：代价模型必须与现有 `_step_cost` 一致。** 原 `oc/ai/world.py:248-254` 对"逆传送带方向走"收 8 倍代价，所以这是 Dijkstra 而非 BFS。距离场若用均匀代价，会把逆带路径算便宜、ETA 失真。

**Files:**
- Create: `/Users/dongyi/Documents/coding/oc2/ai/navigation.py`
- Test: `/Users/dongyi/Documents/coding/oc2/tests/test_navigation.py`

**Interfaces:**
- Consumes: 无（`Grid` 用 duck typing，只要求 `.walkable(gx, gz)` 与 `.belt_dir` 属性）
- Produces:
  - `navigation.BELT_REVERSE_COST = 8`
  - `navigation.step_cost(belt_dir: Dict, cur: Tuple[int, int], nxt: Tuple[int, int]) -> int`
  - `navigation.distance_field(start, is_walkable, belt_dir, blocked=None) -> Dict[Tuple[int, int], int]`
    - `is_walkable`: `Callable[[int, int], bool]`
  - `navigation.interact_cells(gx, gz, is_walkable, belt_cells=None) -> List[Tuple[int, int]]` — 站点的可站立交互位
  - `navigation.nearest_cost(field, cells) -> Optional[Tuple[Tuple[int, int], int]]` — 一组格子里最近的那个及其代价

- [ ] **Step 1: 写失败的测试**

`tests/test_navigation.py`:
```python
from ai.navigation import (
    BELT_REVERSE_COST,
    distance_field,
    interact_cells,
    nearest_cost,
    step_cost,
)


def open_3x3():
    """0..2 x 0..2 全可走。"""
    return lambda gx, gz: 0 <= gx <= 2 and 0 <= gz <= 2


def test_step_cost_is_one_without_belt():
    assert step_cost({}, (0, 0), (1, 0)) == 1


def test_step_cost_penalizes_against_belt_direction():
    belt = {(0, 0): (1, 0)}  # 该格把人向 +x 推

    assert step_cost(belt, (0, 0), (-1, 0)) == BELT_REVERSE_COST
    assert step_cost(belt, (0, 0), (1, 0)) == 1
    assert step_cost(belt, (0, 0), (0, 1)) == 1


def test_distance_field_expands_uniformly():
    field = distance_field((0, 0), open_3x3(), {})

    assert field[(0, 0)] == 0
    assert field[(1, 0)] == 1
    assert field[(1, 1)] == 2
    assert field[(2, 2)] == 4


def test_distance_field_excludes_unwalkable():
    walkable = lambda gx, gz: 0 <= gx <= 2 and 0 <= gz <= 2 and not (gx == 1 and gz == 0)
    field = distance_field((0, 0), walkable, {})

    assert (1, 0) not in field
    # 仍可绕行到 (2,0)
    assert field[(2, 0)] == 4


def test_distance_field_respects_blocked_cells():
    field = distance_field((0, 0), open_3x3(), {}, blocked={(1, 0), (1, 1)})

    assert (1, 0) not in field
    assert (1, 1) not in field
    assert field[(0, 2)] == 2


def test_distance_field_prefers_cheap_path_around_belt():
    """逆带代价 8，绕路 1+1+1=3 更便宜，距离场应取 3。"""
    walkable = lambda gx, gz: 0 <= gx <= 1 and 0 <= gz <= 1
    belt = {(1, 0): (1, 0)}  # 站在 (1,0) 会被推向 +x，往 (0,0) 走是逆向

    field = distance_field((1, 0), walkable, belt)

    assert field[(0, 0)] == 3  # (1,0)->(1,1)->(0,1)->(0,0)


def test_distance_field_start_unwalkable_still_includes_start():
    """厨师可能正站在被判为不可走的格上（站点占用兜底误判），不能直接返回空。"""
    field = distance_field((5, 5), lambda gx, gz: False, {})

    assert field == {(5, 5): 0}


def test_interact_cells_returns_walkable_neighbors():
    cells = interact_cells(1, 1, open_3x3())

    assert set(cells) == {(0, 1), (2, 1), (1, 0), (1, 2)}


def test_interact_cells_excludes_belt_cells_by_default():
    cells = interact_cells(1, 1, open_3x3(), belt_cells={(0, 1), (2, 1)})

    assert set(cells) == {(1, 0), (1, 2)}


def test_interact_cells_falls_back_to_belt_when_no_alternative():
    """相邻格全是传送带时不能返回空，否则站点永远不可达。"""
    walkable = lambda gx, gz: (gx, gz) in {(0, 1), (2, 1)}
    cells = interact_cells(1, 1, walkable, belt_cells={(0, 1), (2, 1)})

    assert set(cells) == {(0, 1), (2, 1)}


def test_nearest_cost_picks_minimum():
    field = {(0, 0): 0, (1, 0): 1, (2, 0): 5}

    assert nearest_cost(field, [(2, 0), (1, 0)]) == ((1, 0), 1)


def test_nearest_cost_returns_none_when_unreachable():
    assert nearest_cost({(0, 0): 0}, [(9, 9)]) is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_navigation.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai.navigation'`

- [ ] **Step 3: 实现 navigation.py**

```python
"""网格寻路：全图距离场 + 交互位计算。

与 oc/ai/world.py 的区别：原代码对每个目标单独跑一次 Dijkstra
（`bfs_to_station`），候选 task 一多就是 O(候选数 × 图)。这里改为每拍
从厨师位置 flood-fill 一次得到全图距离，ETA 查表 O(1)（spec §6.4）。

代价模型与原 `_step_cost` 保持一致：逆传送带方向走要长按，代价 8。
所以这是 Dijkstra，不是均匀代价 BFS——若用 BFS 会把逆带路径算便宜。
"""
import heapq
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

BELT_REVERSE_COST = 8

_NEIGHBORS = ((-1, 0), (1, 0), (0, -1), (0, 1))


def step_cost(belt_dir, cur, nxt):
    # type: (Dict, Tuple[int, int], Tuple[int, int]) -> int
    """cur → nxt 的移动代价。逆着传送方向走要长按，代价高。"""
    d = (nxt[0] - cur[0], nxt[1] - cur[1])
    bd = belt_dir.get(cur)
    if bd and d[0] * bd[0] + d[1] * bd[1] < 0:
        return BELT_REVERSE_COST
    return 1


def distance_field(start, is_walkable, belt_dir, blocked=None):
    # type: (Tuple[int, int], Callable[[int, int], bool], Dict, Optional[Set]) -> Dict[Tuple[int, int], int]
    """从 start 做 Dijkstra，返回 {格子: 最小代价}。

    start 本身总在结果里（代价 0），即使它被判为不可走——厨师可能正站在
    被站点占用兜底误判的格子上，直接返回空会让所有 ETA 失效。
    """
    blocked = set(blocked or ())
    dist = {start: 0}
    pq = [(0, start)]
    while pq:
        cost, cur = heapq.heappop(pq)
        if cost > dist.get(cur, 1 << 60):
            continue
        for dx, dz in _NEIGHBORS:
            nxt = (cur[0] + dx, cur[1] + dz)
            if nxt in blocked:
                continue
            if not is_walkable(nxt[0], nxt[1]):
                continue
            ncost = cost + step_cost(belt_dir, cur, nxt)
            if ncost < dist.get(nxt, 1 << 60):
                dist[nxt] = ncost
                heapq.heappush(pq, (ncost, nxt))
    return dist


def interact_cells(gx, gz, is_walkable, belt_cells=None):
    # type: (int, int, Callable[[int, int], bool], Optional[Set]) -> List[Tuple[int, int]]
    """站点 (gx,gz) 的可站立交互位（四邻）。

    默认排除传送带格：站上面会被推走，放/取料不稳。但若四邻全是传送带，
    则放行它们——否则该站点永远不可达。
    """
    belt_cells = set(belt_cells or ())
    preferred = []
    fallback = []
    for dx, dz in _NEIGHBORS:
        cell = (gx + dx, gz + dz)
        if not is_walkable(cell[0], cell[1]):
            continue
        fallback.append(cell)
        if cell not in belt_cells:
            preferred.append(cell)
    return preferred if preferred else fallback


def nearest_cost(field, cells):
    # type: (Dict, Iterable[Tuple[int, int]]) -> Optional[Tuple[Tuple[int, int], int]]
    """cells 中在距离场里代价最小的格子及其代价。都不可达则 None。"""
    best = None
    for cell in cells:
        cost = field.get(cell)
        if cost is None:
            continue
        if best is None or cost < best[1]:
            best = (cell, cost)
    return best
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_navigation.py -v`
Expected: 12 passed

- [ ] **Step 5: 提交**

```bash
cd /Users/dongyi/Documents/coding/oc2
git add ai/navigation.py tests/test_navigation.py
git commit -m "$(cat <<'EOF'
feat: flood-fill 全图距离场

每拍一次 Dijkstra 取代每目标一次 BFS，ETA 查表 O(1)（spec §6.4）。
代价模型沿用原 _step_cost：逆传送带方向代价 8。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Blackboard 与认领表

spec §4 的核心。**认领键是 `(resource_id, action)` 动作级粒度**（§4.0）——按资源粒度会让接力协作废掉：P1 往 1 号锅放了生饭，若整锅被标为"P1 占用"，AI 就不会去取熟饭，而取熟饭恰恰是最有价值的接力。

`scratch` 必须类型化（§4 行 154）：旧代码的罪状是 `self.sub` 单字段被三套状态机共享，若新设计用无类型字典只是换名字塞进黑板。

**Files:**
- Create: `/Users/dongyi/Documents/coding/oc2/ai/blackboard.py`
- Test: `/Users/dongyi/Documents/coding/oc2/tests/test_blackboard.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `blackboard.Claim(owner: str, order_id, claimed_tick: int, ttl: int)` — dataclass
  - `blackboard.Commitment(active_task=None, started_tick=0, steps_done=0, infeasible_streak=0, retries=0)` — dataclass
  - `blackboard.P1Model(holding=None, p1_active_task=None, confidence=0.0, last_evidence_tick=0)` — dataclass
  - `blackboard.Intents(assignments=[], collab_mode="relay", role_constraint=None, aggressiveness=0.5)` — dataclass
  - `blackboard.StuckScratch(bad_cells={}, nudge_dir=None, last_cell=None, same_count=0, skip_ticks=0)` — dataclass
  - `blackboard.FireScratch(ext_target=None, bad_ext=set(), tries=0)` — dataclass
  - `blackboard.Blackboard` — dataclass，字段 `claims / waiters / order_assignment / commitment / p1_model / intents / scratch / tick`
  - 方法：`claim(resource_id, action, owner, order_id, tick, ttl) -> None`、`release(resource_id, action) -> None`、`claim_of(resource_id, action) -> Optional[Claim]`、`is_claimed_by_other(resource_id, action, owner, tick) -> bool`、`release_order(order_id) -> int`、`release_expired(tick) -> int`、`claims_of_owner(owner) -> List[Tuple]`
  - 等待队列：`add_waiter(resource_id, order_id, tick) -> None`、`next_waiter(resource_id) -> Optional[Tuple]`、`drop_waiter(resource_id, order_id) -> None`、`waited_ticks(resource_id, order_id, tick) -> Optional[int]`
  - `blackboard.DEFAULT_TTL = 100`、`blackboard.WAIT_GIVEUP_TICKS = 60`

- [ ] **Step 1: 写失败的测试**

`tests/test_blackboard.py`:
```python
import pytest

from ai.blackboard import (
    DEFAULT_TTL,
    Blackboard,
    FireScratch,
    StuckScratch,
)


def test_claim_is_action_scoped_not_resource_scoped():
    """spec §4.0：锁住 fetch_to 不应锁住 take_cooked，否则接力废掉。"""
    bb = Blackboard()
    bb.claim("cooker1", "fetch_to", owner="P1", order_id="o1", tick=0)

    assert bb.is_claimed_by_other("cooker1", "fetch_to", owner="P2", tick=1) is True
    assert bb.is_claimed_by_other("cooker1", "take_cooked", owner="P2", tick=1) is False


def test_own_claim_is_not_blocking():
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)

    assert bb.is_claimed_by_other("plateA", "fill", owner="P2", tick=1) is False


def test_release_removes_claim():
    bb = Blackboard()
    bb.claim("board1", "chop", owner="P2", order_id="o1", tick=0)
    bb.release("board1", "chop")

    assert bb.claim_of("board1", "chop") is None


def test_release_is_idempotent():
    bb = Blackboard()
    bb.release("nope", "chop")  # 不应抛异常

    assert bb.claim_of("nope", "chop") is None


def test_release_order_frees_all_claims_of_that_order():
    """spec §4.1 触发点 2：订单消失（送达/超时/被 P1 抢先）→ 全部释放。"""
    bb = Blackboard()
    bb.claim("cooker1", "fetch_to", owner="P2", order_id="o1", tick=0)
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)
    bb.claim("board1", "chop", owner="P2", order_id="o2", tick=0)

    freed = bb.release_order("o1")

    assert freed == 2
    assert bb.claim_of("cooker1", "fetch_to") is None
    assert bb.claim_of("plateA", "fill") is None
    assert bb.claim_of("board1", "chop") is not None


def test_expired_claim_does_not_block():
    """spec §4.1 触发点 4：TTL 是防认领泄漏的最后一道闸。"""
    bb = Blackboard()
    bb.claim("cooker1", "take_cooked", owner="P1", order_id="o1", tick=0, ttl=10)

    assert bb.is_claimed_by_other("cooker1", "take_cooked", owner="P2", tick=5) is True
    assert bb.is_claimed_by_other("cooker1", "take_cooked", owner="P2", tick=11) is False


def test_release_expired_sweeps_stale_claims():
    bb = Blackboard()
    bb.claim("a", "act", owner="P2", order_id="o1", tick=0, ttl=10)
    bb.claim("b", "act", owner="P2", order_id="o1", tick=0, ttl=100)

    swept = bb.release_expired(tick=50)

    assert swept == 1
    assert bb.claim_of("a", "act") is None
    assert bb.claim_of("b", "act") is not None


def test_default_ttl_applied_when_not_given():
    bb = Blackboard()
    bb.claim("a", "act", owner="P2", order_id="o1", tick=0)

    assert bb.claim_of("a", "act").ttl == DEFAULT_TTL


def test_claims_of_owner_lists_keys():
    bb = Blackboard()
    bb.claim("a", "act", owner="P2", order_id="o1", tick=0)
    bb.claim("b", "act", owner="P1", order_id="o2", tick=0)

    assert bb.claims_of_owner("P2") == [("a", "act")]


def test_waiters_are_ordered_by_wait_duration():
    """spec §4.3 规则 1：老化优先级，防饿死。"""
    bb = Blackboard()
    bb.add_waiter("cooker1", "o2", tick=10)
    bb.add_waiter("cooker1", "o3", tick=5)

    assert bb.next_waiter("cooker1") == ("o3", 5)


def test_add_waiter_is_idempotent_per_order():
    bb = Blackboard()
    bb.add_waiter("cooker1", "o2", tick=10)
    bb.add_waiter("cooker1", "o2", tick=20)

    assert bb.waited_ticks("cooker1", "o2", tick=30) == 20


def test_waited_ticks_none_when_not_waiting():
    bb = Blackboard()

    assert bb.waited_ticks("cooker1", "o2", tick=30) is None


def test_drop_waiter_removes_entry():
    bb = Blackboard()
    bb.add_waiter("cooker1", "o2", tick=10)
    bb.drop_waiter("cooker1", "o2")

    assert bb.next_waiter("cooker1") is None


def test_scratch_types_are_independent():
    """spec §4 行 154：禁止跨模式复用字段。"""
    stuck = StuckScratch()
    fire = FireScratch()

    assert not hasattr(stuck, "ext_target")
    assert not hasattr(fire, "nudge_dir")


def test_scratch_defaults_are_not_shared_between_instances():
    a = StuckScratch()
    b = StuckScratch()
    a.bad_cells[(1, 1)] = 5

    assert b.bad_cells == {}


def test_fire_scratch_defaults_are_not_shared():
    a = FireScratch()
    b = FireScratch()
    a.bad_ext.add("ext1")

    assert b.bad_ext == set()


def test_blackboard_fresh_has_empty_state():
    bb = Blackboard()

    assert bb.claims == {}
    assert bb.commitment.active_task is None
    assert bb.intents.collab_mode == "relay"
    assert bb.scratch is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_blackboard.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai.blackboard'`

- [ ] **Step 3: 实现 blackboard.py 的数据类**

```python
"""Blackboard：唯一的可变状态容器，取代散落在 Agent 上的约 20 个跨拍字段。

写入权受控（spec §4.5）：
  Planner  → claims、commitment
  P1Model  → p1_model
  Executor → commitment.steps_done / retries、scratch（**不可写 claims**）

认领键是 (resource_id, action) 动作级粒度（spec §4.0）。按资源粒度会让
接力协作废掉：P1 往锅里放了生饭，若整锅标为"P1 占用"，AI 就不会去取
熟饭——而取熟饭恰恰是最有价值的接力。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

DEFAULT_TTL = 100          # 拍。20 Hz 下约 5 秒
WAIT_GIVEUP_TICKS = 60     # 等待超时让出（spec §4.3 规则 2）


@dataclass
class Claim:
    owner: str
    order_id: Any
    claimed_tick: int
    ttl: int = DEFAULT_TTL

    def is_expired(self, tick):
        # type: (int) -> bool
        return tick - self.claimed_tick > self.ttl


@dataclass
class Commitment:
    """当前承诺，防抖（spec §6.5）。"""
    active_task: Any = None
    started_tick: int = 0
    steps_done: int = 0
    infeasible_streak: int = 0
    retries: int = 0


@dataclass
class P1Model:
    """P1 推断（spec §6.7）。由 P1Model 层写，其他层只读。"""
    holding: Optional[str] = None
    p1_active_task: Any = None
    confidence: float = 0.0
    last_evidence_tick: int = 0


@dataclass
class Intents:
    """意图注入口（对话层预留，本轮为空壳）。"""
    assignments: List[Any] = field(default_factory=list)
    collab_mode: str = "relay"
    role_constraint: Any = None
    aggressiveness: float = 0.5


@dataclass
class StuckScratch:
    """脱困模式局部状态。模式进入时构造、退出即丢弃。"""
    bad_cells: Dict[Tuple[int, int], int] = field(default_factory=dict)
    nudge_dir: Any = None
    last_cell: Optional[Tuple[int, int]] = None
    same_count: int = 0
    skip_ticks: int = 0


@dataclass
class FireScratch:
    """灭火模式局部状态。模式进入时构造、退出即丢弃。"""
    ext_target: Any = None
    bad_ext: Set[Any] = field(default_factory=set)
    tries: int = 0
```

- [ ] **Step 4: 实现 Blackboard 主体（追加到同一文件）**

```python
@dataclass
class Blackboard:
    claims: Dict[Tuple[Any, str], Claim] = field(default_factory=dict)
    waiters: Dict[Any, List[Tuple[Any, int]]] = field(default_factory=dict)
    order_assignment: Dict[Any, Optional[str]] = field(default_factory=dict)
    commitment: Commitment = field(default_factory=Commitment)
    p1_model: P1Model = field(default_factory=P1Model)
    intents: Intents = field(default_factory=Intents)
    scratch: Any = None
    tick: int = 0

    # ---------- 认领 ----------
    def claim(self, resource_id, action, owner, order_id, tick, ttl=DEFAULT_TTL):
        # type: (Any, str, str, Any, int, int) -> None
        self.claims[(resource_id, action)] = Claim(owner, order_id, tick, ttl)

    def release(self, resource_id, action):
        # type: (Any, str) -> None
        """释放。幂等——spec §4.1 的四个触发点可能重复命中同一个键。"""
        self.claims.pop((resource_id, action), None)

    def claim_of(self, resource_id, action):
        # type: (Any, str) -> Optional[Claim]
        return self.claims.get((resource_id, action))

    def is_claimed_by_other(self, resource_id, action, owner, tick):
        # type: (Any, str, str, int) -> bool
        """spec §4.2 的全局可行性前置用这个。过期认领不算阻塞。"""
        c = self.claims.get((resource_id, action))
        if c is None:
            return False
        if c.owner == owner:
            return False
        return not c.is_expired(tick)

    def release_order(self, order_id):
        # type: (Any) -> int
        """订单消失 → 该单所有认领释放（spec §4.1 触发点 2）。返回释放数。"""
        keys = [k for k, c in self.claims.items() if c.order_id == order_id]
        for k in keys:
            del self.claims[k]
        return len(keys)

    def release_expired(self, tick):
        # type: (int) -> int
        """TTL 兜底扫描（spec §4.1 触发点 4）。返回释放数。"""
        keys = [k for k, c in self.claims.items() if c.is_expired(tick)]
        for k in keys:
            del self.claims[k]
        return len(keys)

    def claims_of_owner(self, owner):
        # type: (str) -> List[Tuple[Any, str]]
        return [k for k, c in self.claims.items() if c.owner == owner]

    # ---------- 等待队列（spec §4.3）----------
    def add_waiter(self, resource_id, order_id, tick):
        # type: (Any, Any, int) -> None
        """登记等待。同一订单重复登记只保留首次时间（老化优先级要的是等待时长）。"""
        q = self.waiters.setdefault(resource_id, [])
        for oid, _ in q:
            if oid == order_id:
                return
        q.append((order_id, tick))

    def next_waiter(self, resource_id):
        # type: (Any) -> Optional[Tuple[Any, int]]
        """等得最久的那个优先（防饿死）。"""
        q = self.waiters.get(resource_id) or []
        if not q:
            return None
        return min(q, key=lambda e: e[1])

    def drop_waiter(self, resource_id, order_id):
        # type: (Any, Any) -> None
        q = self.waiters.get(resource_id)
        if not q:
            return
        self.waiters[resource_id] = [e for e in q if e[0] != order_id]

    def waited_ticks(self, resource_id, order_id, tick):
        # type: (Any, Any, int) -> Optional[int]
        """已等待拍数。未在队列里则 None。"""
        for oid, since in (self.waiters.get(resource_id) or []):
            if oid == order_id:
                return tick - since
        return None
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_blackboard.py -v`
Expected: 17 passed

- [ ] **Step 6: 提交**

```bash
cd /Users/dongyi/Documents/coding/oc2
git add ai/blackboard.py tests/test_blackboard.py
git commit -m "$(cat <<'EOF'
feat: Blackboard 与动作级认领表

认领键为 (resource_id, action)（spec §4.0）：锁住 fetch_to 不锁
take_cooked，接力协作才成立。含 TTL 兜底与老化优先的等待队列。
scratch 按模式类型化，不复用字段。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: 认领对账 reconcile_claims

spec §4.6：每拍用新快照校验认领。这是"用户抢先出餐后 AI 自动改做下一单"的实现机制——订单被抢 → 认领失效 → 重分配，不需要满地找恢复逻辑。

**盘的识别是已知风险点**（spec §4.4）：若只按位置识别，"盘被端起移动"与"盘消失"在对账眼里无法区分，会误杀正被 P1 搬动的盘。两条防护：优先用实例名 `n`、位置仅作交叉校验；失效判定给**一拍宽限**（连续两拍找不到才失效）。

**Files:**
- Create: `/Users/dongyi/Documents/coding/oc2/ai/reconcile.py`
- Test: `/Users/dongyi/Documents/coding/oc2/tests/test_reconcile.py`

**Interfaces:**
- Consumes: `blackboard.Blackboard`（Task 4）
- Produces:
  - `reconcile.ResourceView(existing_ids: Set, contents: Dict)` — 对账所需的世界视图，由调用方从 World 构造；这样 reconcile 可脱离 World 单测
  - `reconcile.reconcile_claims(view, bb, live_order_ids, tick) -> ReconcileReport`
  - `reconcile.ReconcileReport(released_missing, released_mismatch, released_order_gone, released_expired)` — 各为 `List[Tuple[resource_id, action]]`
  - `reconcile.MISSING_GRACE_TICKS = 1`
  - `Claim` 需新增字段 `contents_expected`（可选）与 `missing_streak`（内部计数）—— 本任务要回头改 `blackboard.py`

- [ ] **Step 1: 给 Claim 加对账所需字段**

修改 `ai/blackboard.py` 的 `Claim`：

```python
@dataclass
class Claim:
    owner: str
    order_id: Any
    claimed_tick: int
    ttl: int = DEFAULT_TTL
    contents_expected: Any = None   # 盘/锅的预期内容，对账用（spec §4.1 触发点 3）
    missing_streak: int = 0         # 连续找不到的拍数，一拍宽限用（spec §4.4）

    def is_expired(self, tick):
        # type: (int) -> bool
        return tick - self.claimed_tick > self.ttl
```

同时给 `Blackboard.claim()` 加 `contents_expected` 参数：

```python
    def claim(self, resource_id, action, owner, order_id, tick,
              ttl=DEFAULT_TTL, contents_expected=None):
        # type: (Any, str, str, Any, int, int, Any) -> None
        self.claims[(resource_id, action)] = Claim(
            owner, order_id, tick, ttl, contents_expected
        )
```

- [ ] **Step 2: 确认既有测试仍通过（没有破坏 Task 4）**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_blackboard.py -v`
Expected: 17 passed

- [ ] **Step 3: 写失败的测试**

`tests/test_reconcile.py`:
```python
from ai.blackboard import Blackboard
from ai.reconcile import ResourceView, reconcile_claims


def test_claim_survives_when_resource_present():
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)
    view = ResourceView(existing_ids={"plateA"})

    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=1)

    assert report.released_missing == []
    assert bb.claim_of("plateA", "fill") is not None


def test_plate_being_carried_is_not_killed_on_first_miss():
    """spec §4.4：一拍宽限，吸收搬动过程中的瞬时缺失。"""
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)
    view = ResourceView(existing_ids=set())  # 这一拍没看到

    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=1)

    assert report.released_missing == []
    assert bb.claim_of("plateA", "fill") is not None
    assert bb.claim_of("plateA", "fill").missing_streak == 1


def test_plate_really_gone_is_released_on_second_miss():
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)
    view = ResourceView(existing_ids=set())

    reconcile_claims(view, bb, live_order_ids={"o1"}, tick=1)
    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=2)

    assert report.released_missing == [("plateA", "fill")]
    assert bb.claim_of("plateA", "fill") is None


def test_missing_streak_resets_when_resource_reappears():
    """盘被端起又放下，不该累计到失效。"""
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)

    reconcile_claims(ResourceView(existing_ids=set()), bb, {"o1"}, tick=1)
    reconcile_claims(ResourceView(existing_ids={"plateA"}), bb, {"o1"}, tick=2)
    report = reconcile_claims(ResourceView(existing_ids=set()), bb, {"o1"}, tick=3)

    assert report.released_missing == []
    assert bb.claim_of("plateA", "fill").missing_streak == 1


def test_content_mismatch_releases_immediately():
    """内容不符说明被 P1 动过，无需宽限（spec §4.1 触发点 3）。"""
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0,
             contents_expected=("rice",))
    view = ResourceView(existing_ids={"plateA"}, contents={"plateA": ("cucumber",)})

    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=1)

    assert report.released_mismatch == [("plateA", "fill")]
    assert bb.claim_of("plateA", "fill") is None


def test_content_match_keeps_claim():
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0,
             contents_expected=("rice",))
    view = ResourceView(existing_ids={"plateA"}, contents={"plateA": ("rice",)})

    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=1)

    assert report.released_mismatch == []


def test_claim_without_expected_contents_ignores_contents():
    bb = Blackboard()
    bb.claim("cooker1", "take_cooked", owner="P2", order_id="o1", tick=0)
    view = ResourceView(existing_ids={"cooker1"}, contents={"cooker1": ("whatever",)})

    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=1)

    assert report.released_mismatch == []


def test_order_gone_releases_all_its_claims():
    """用户抢先出餐 → 订单消失 → 认领失效 → Planner 改做下一单（spec §4.6）。"""
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)
    bb.claim("cooker1", "fetch_to", owner="P2", order_id="o1", tick=0)
    bb.claim("board1", "chop", owner="P2", order_id="o2", tick=0)
    view = ResourceView(existing_ids={"plateA", "cooker1", "board1"})

    report = reconcile_claims(view, bb, live_order_ids={"o2"}, tick=1)

    assert set(report.released_order_gone) == {("plateA", "fill"), ("cooker1", "fetch_to")}
    assert bb.claim_of("board1", "chop") is not None


def test_claim_with_no_order_id_is_not_order_released():
    """无主认领（如灭火、脱困用的资源）不该被订单扫描清掉。"""
    bb = Blackboard()
    bb.claim("ext1", "grab", owner="P2", order_id=None, tick=0)
    view = ResourceView(existing_ids={"ext1"})

    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=1)

    assert report.released_order_gone == []
    assert bb.claim_of("ext1", "grab") is not None


def test_expired_claim_is_swept():
    """TTL 兜底：前三条都漏掉的情况由它兜住（spec §4.1 触发点 4）。"""
    bb = Blackboard()
    bb.claim("cooker1", "fetch_to", owner="P2", order_id="o1", tick=0, ttl=5)
    view = ResourceView(existing_ids={"cooker1"})

    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=20)

    assert report.released_expired == [("cooker1", "fetch_to")]
    assert bb.claims == {}


def test_no_claim_leak_after_order_lifecycle():
    """认领泄漏断言（spec §11.2 阶段 2）：订单走完一轮后表必须为空。"""
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)
    bb.claim("cooker1", "fetch_to", owner="P2", order_id="o1", tick=0)
    bb.claim("counter_3_0", "place", owner="P2", order_id="o1", tick=0)
    view = ResourceView(existing_ids={"plateA", "cooker1", "counter_3_0"})

    reconcile_claims(view, bb, live_order_ids=set(), tick=1)

    assert bb.claims == {}


def test_waiters_dropped_for_dead_orders():
    bb = Blackboard()
    bb.add_waiter("cooker1", "o1", tick=0)
    bb.add_waiter("cooker1", "o2", tick=0)
    view = ResourceView(existing_ids={"cooker1"})

    reconcile_claims(view, bb, live_order_ids={"o2"}, tick=1)

    assert bb.next_waiter("cooker1") == ("o2", 0)
```

- [ ] **Step 4: 运行测试确认失败**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_reconcile.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai.reconcile'`

- [ ] **Step 5: 实现 reconcile.py**

```python
"""认领对账（spec §4.6）：每拍用新快照校验认领。

这是"用户抢先出餐后 AI 自动改做下一单"的实现机制：订单被抢 → 认领失效
→ Planner 重分配。不需要在各处散落恢复逻辑。

ResourceView 把"世界里有什么"从 World 解耦出来，让对账可脱离游戏单测。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Tuple

MISSING_GRACE_TICKS = 1   # 一拍宽限（spec §4.4）：吸收盘被搬动时的瞬时缺失


@dataclass
class ResourceView:
    """对账所需的世界视图。由调用方从 World 构造。"""
    existing_ids: Set[Any] = field(default_factory=set)
    contents: Dict[Any, Any] = field(default_factory=dict)


@dataclass
class ReconcileReport:
    released_missing: List[Tuple[Any, str]] = field(default_factory=list)
    released_mismatch: List[Tuple[Any, str]] = field(default_factory=list)
    released_order_gone: List[Tuple[Any, str]] = field(default_factory=list)
    released_expired: List[Tuple[Any, str]] = field(default_factory=list)

    def total(self):
        # type: () -> int
        return (len(self.released_missing) + len(self.released_mismatch)
                + len(self.released_order_gone) + len(self.released_expired))


def reconcile_claims(view, bb, live_order_ids, tick):
    # type: (ResourceView, Any, Set[Any], int) -> ReconcileReport
    """校验全部认领，释放失效者。就地修改 bb，并返回报告供 trace 记录。

    四个释放触发点（spec §4.1）中的三个在这里（第四个"task 成功"由
    Planner 在 task 完成时调 bb.release 处理）：
      - 订单消失 → 全单释放
      - 资源消失（带一拍宽限）/ 内容不符 → 失效
      - TTL 兜底 → 强制释放
    """
    report = ReconcileReport()
    live_order_ids = set(live_order_ids or ())

    # 1) 订单消失。order_id 为 None 的认领（灭火、脱困）不参与。
    for key in list(bb.claims.keys()):
        c = bb.claims.get(key)
        if c is None or c.order_id is None:
            continue
        if c.order_id not in live_order_ids:
            del bb.claims[key]
            report.released_order_gone.append(key)

    # 2) 资源消失 / 内容不符
    for key in list(bb.claims.keys()):
        c = bb.claims.get(key)
        if c is None:
            continue
        resource_id = key[0]

        if resource_id not in view.existing_ids:
            c.missing_streak += 1
            if c.missing_streak > MISSING_GRACE_TICKS:
                del bb.claims[key]
                report.released_missing.append(key)
            continue

        c.missing_streak = 0
        if c.contents_expected is not None:
            actual = view.contents.get(resource_id)
            if actual is not None and actual != c.contents_expected:
                del bb.claims[key]
                report.released_mismatch.append(key)

    # 3) TTL 兜底
    for key in list(bb.claims.keys()):
        c = bb.claims.get(key)
        if c is not None and c.is_expired(tick):
            del bb.claims[key]
            report.released_expired.append(key)

    # 4) 顺手清理死订单的等待登记，否则 waiters 会无限增长
    for resource_id in list(bb.waiters.keys()):
        kept = [e for e in bb.waiters[resource_id] if e[0] in live_order_ids]
        if kept:
            bb.waiters[resource_id] = kept
        else:
            del bb.waiters[resource_id]

    return report
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_reconcile.py -v`
Expected: 12 passed

- [ ] **Step 7: 跑全量测试确认无回归**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest -v`
Expected: 全部 passed（Task 1-5 共约 54 个）

- [ ] **Step 8: 提交**

```bash
cd /Users/dongyi/Documents/coding/oc2
git add ai/blackboard.py ai/reconcile.py tests/test_reconcile.py
git commit -m "$(cat <<'EOF'
feat: 认领对账与四个释放触发点

spec §4.6。盘的失效判定给一拍宽限，避免误杀正被 P1 搬动的盘
（spec §4.4 已知风险点）。含认领泄漏断言。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: 无副作用谓词层

从 `oc/ai/agent.py` 抽取约 20 个世界查询谓词为模块级纯函数。多数本来就无副作用（只是挂在 `self` 上），**但两个有**：`_station()` 写 `self.pot` / `self.plate_pos`（`oc/ai/agent.py:548-558`）、`_should_rush_cooked()` 写 `self.pot`（`:726`）。spec §11.2 阶段 1 明确：改为"纯查询 + 调用点显式写入"。

本任务只抽**不依赖 World 内部结构**的那批（手持 / 盘 / 锅 / 板状态），它们只读快照 dict。站点定位类谓词依赖 World 的网格数据，留到 Plan 2 与 `tasks.py` 一起处理。

**Files:**
- Create: `/Users/dongyi/Documents/coding/oc2/ai/predicates.py`
- Create: `/Users/dongyi/Documents/coding/oc2/tests/conftest.py`
- Test: `/Users/dongyi/Documents/coding/oc2/tests/test_predicates.py`

**Interfaces:**
- Consumes: 无（只读快照 dict 与 `List[dict]`，不依赖 World 类）
- Produces（全部为模块级纯函数）：
  - 手持：`held_name(chef) -> str`、`held_matches(chef, item) -> bool`、`held_is_pot(chef) -> bool`、`held_is_plate(chef) -> bool`、`hands_free(chef) -> bool`
  - 盘：`plate_contents(plate) -> Tuple[str, ...]`、`plate_count(plate) -> int`、`plate_lacks(plate, ingredient) -> bool`、`plate_complete(plate, required) -> bool`
  - 锅：`cooker_for(cookers, station_name) -> Optional[dict]`、`cooker_has_pot(cooker) -> bool`、`cooker_cooked(cooker) -> bool`、`cooker_burning(cooker) -> bool`、`cooker_started(cooker) -> bool`
  - 板：`board_item(workstations, station_name) -> str`、`board_chopped(workstations, station_name) -> bool`
  - P1：`p1_holds(chefs, keyword) -> bool`、`p1_holds_plate(chefs) -> bool`、`chef_by_slot(chefs, slot) -> Optional[dict]`
  - 名字匹配：`station_name_matches(a, b) -> bool`（抽出原 `_board_chopped` 里的三段式名字匹配）
  - 常量：`POT_KEYWORDS = ("pot", "pan", "fryer", "tray", "tin", "steamer")`

**关于 `POT_KEYWORDS`**：事实核查发现原代码只认 `pot`/`pan`（`oc/ai/agent.py:456`），而 `PLAN.md` §7.3 列了六种锅具（`DeepFatFryer` / `OvenTray` / `OvenCakeTin` / `Steamer` 名字里既没 pot 也没 pan）。spec §9 已记录这是待修项，本任务顺手修好。

- [ ] **Step 1: 写 conftest.py（假帧构造器）**

`tests/conftest.py`:
```python
"""测试夹具：构造假快照片段。

谓词只读 dict，所以不需要真 World——这正是 spec §6.6 说的工程红利。
"""
import pytest


@pytest.fixture
def chef_empty():
    return {"slot": 1, "held": None, "x": 0.0, "z": 0.0}


@pytest.fixture
def chef_holding():
    def _make(held):
        return {"slot": 1, "held": held, "x": 0.0, "z": 0.0}
    return _make


@pytest.fixture
def cooker():
    def _make(cooked=None, burning=None, on_fire=None, is_cooking=None, name="Cooker_1"):
        return {
            "n": name,
            "cooked": cooked,
            "burning": burning,
            "onFire": on_fire,
            "isCooking": is_cooking,
        }
    return _make
```

- [ ] **Step 2: 写失败的测试**

`tests/test_predicates.py`:
```python
import copy

from ai import predicates as P


# ---------- 手持 ----------
def test_held_name_normalizes_none(chef_empty):
    assert P.held_name(chef_empty) == ""


def test_held_name_lowercases(chef_holding):
    assert P.held_name(chef_holding("Cucumber")) == "cucumber"


def test_held_matches_is_exact(chef_holding):
    chef = chef_holding("ChoppedCucumber")

    assert P.held_matches(chef, "choppedcucumber") is True
    assert P.held_matches(chef, "cucumber") is False


def test_held_matches_false_on_empty_hands(chef_empty):
    assert P.held_matches(chef_empty, "cucumber") is False


def test_held_is_pot_covers_all_six_cooker_types(chef_holding):
    """PLAN.md §7.3 的六种锅具都要认（spec §9 待修项）。"""
    for name in ["Pot", "FryingPan", "DeepFatFryer", "OvenTray", "OvenCakeTin", "Steamer"]:
        assert P.held_is_pot(chef_holding(name)) is True, name


def test_held_is_pot_false_for_plate(chef_holding):
    assert P.held_is_pot(chef_holding("ServerPlate")) is False


def test_held_is_plate(chef_holding):
    assert P.held_is_plate(chef_holding("ServerPlate")) is True
    assert P.held_is_plate(chef_holding("Pot")) is False


def test_hands_free(chef_empty, chef_holding):
    assert P.hands_free(chef_empty) is True
    assert P.hands_free(chef_holding("Pot")) is False


# ---------- 盘 ----------
def test_plate_contents_parses_list():
    plate = {"contents": ["BoiledSushiRice", "ChoppedCucumber"], "count": 2}

    assert P.plate_contents(plate) == ("boiledsushirice", "choppedcucumber")


def test_plate_contents_empty_when_missing():
    assert P.plate_contents({}) == ()
    assert P.plate_contents(None) == ()


def test_plate_count_falls_back_to_contents_length():
    assert P.plate_count({"contents": ["a", "b"]}) == 2
    assert P.plate_count({"count": 3}) == 3
    assert P.plate_count(None) == 0


def test_plate_lacks_is_case_insensitive():
    plate = {"contents": ["BoiledSushiRice"]}

    assert P.plate_lacks(plate, "boiledsushirice") is False
    assert P.plate_lacks(plate, "ChoppedCucumber") is True


def test_plate_complete_requires_all_ingredients():
    required = ("boiledsushirice", "choppedcucumber")

    assert P.plate_complete({"contents": ["BoiledSushiRice"]}, required) is False
    assert P.plate_complete(
        {"contents": ["BoiledSushiRice", "ChoppedCucumber"]}, required) is True


def test_plate_complete_ignores_extra_items():
    """多放了料仍算齐（送餐判定由订单侧负责，这里只管"不缺"）。"""
    required = ("boiledsushirice",)

    assert P.plate_complete(
        {"contents": ["BoiledSushiRice", "ChoppedCucumber"]}, required) is True


def test_plate_complete_true_for_empty_requirement():
    assert P.plate_complete({"contents": []}, ()) is True


# ---------- 锅 ----------
def test_cooker_for_matches_by_name(cooker):
    cookers = [cooker(name="Cooker_1"), cooker(name="Cooker_2", cooked=True)]

    assert P.cooker_for(cookers, "Cooker_2")["cooked"] is True


def test_cooker_for_none_when_absent(cooker):
    assert P.cooker_for([cooker(name="Cooker_1")], "Cooker_9") is None


def test_cooker_has_pot_uses_cooked_null_convention(cooker):
    """插件约定：cooked=null 表示灶上没锅；false/true 表示有锅。"""
    assert P.cooker_has_pot(cooker(cooked=None)) is False
    assert P.cooker_has_pot(cooker(cooked=False)) is True
    assert P.cooker_has_pot(cooker(cooked=True)) is True


def test_cooker_has_pot_false_for_missing_cooker():
    assert P.cooker_has_pot(None) is False


def test_cooker_cooked_requires_explicit_true(cooker):
    """空锅 cooked=False 不能误判为熟。"""
    assert P.cooker_cooked(cooker(cooked=True)) is True
    assert P.cooker_cooked(cooker(cooked=False)) is False
    assert P.cooker_cooked(cooker(cooked=None)) is False
    assert P.cooker_cooked(None) is False


def test_cooker_burning_covers_both_flags(cooker):
    assert P.cooker_burning(cooker(burning=True)) is True
    assert P.cooker_burning(cooker(on_fire=True)) is True
    assert P.cooker_burning(cooker(cooked=True)) is False
    assert P.cooker_burning(None) is False


def test_cooker_started(cooker):
    assert P.cooker_started(cooker(is_cooking=True)) is True
    assert P.cooker_started(cooker(cooked=True)) is True  # 已熟也算启动过
    assert P.cooker_started(cooker(cooked=False)) is False
    assert P.cooker_started(None) is False


# ---------- 板 ----------
def test_board_item_matches_station_name():
    ws = [{"n": "ChoppingBoard_1", "item": "Cucumber"}]

    assert P.board_item(ws, "ChoppingBoard_1") == "cucumber"


def test_board_item_empty_when_no_match():
    assert P.board_item([{"n": "Board_1", "item": "X"}], "Board_9") == ""


def test_board_chopped_detects_prefix():
    ws = [{"n": "Board_1", "item": "ChoppedCucumber"}]

    assert P.board_chopped(ws, "Board_1") is True


def test_board_chopped_false_for_raw():
    ws = [{"n": "Board_1", "item": "Cucumber"}]

    assert P.board_chopped(ws, "Board_1") is False


def test_station_name_matches_handles_substring():
    """原 _board_chopped 的三段式匹配：相等 / 互为子串。"""
    assert P.station_name_matches("Board_1", "Board_1") is True
    assert P.station_name_matches("Board_1", "Kitchen.Board_1") is True
    assert P.station_name_matches("Kitchen.Board_1", "Board_1") is True
    assert P.station_name_matches("Board_1", "Board_2") is False
    assert P.station_name_matches("", "Board_1") is False


# ---------- P1 ----------
def test_chef_by_slot():
    chefs = [{"slot": 1, "held": "Pot"}, {"slot": 2, "held": None}]

    assert P.chef_by_slot(chefs, 1)["held"] == "Pot"
    assert P.chef_by_slot(chefs, 9) is None


def test_p1_holds_keyword():
    chefs = [{"slot": 1, "held": "ServerPlate"}, {"slot": 2, "held": "Pot"}]

    assert P.p1_holds(chefs, "plate") is True
    assert P.p1_holds(chefs, "extinguish") is False


def test_p1_holds_plate():
    chefs = [{"slot": 1, "held": "ServerPlate"}]

    assert P.p1_holds_plate(chefs) is True


def test_p1_holds_false_when_p1_absent():
    assert P.p1_holds([{"slot": 2, "held": "Pot"}], "pot") is False


# ---------- 无副作用（spec §4.5 / §11.2 阶段 1 的核心要求）----------
def test_predicates_do_not_mutate_inputs(cooker):
    """逐个调用谓词，断言入参 dict 未被改动。

    原 _station() 写 self.pot、_should_rush_cooked() 写 self.pot，
    spec §11.2 要求改为"纯查询 + 调用点显式写入"。
    """
    chef = {"slot": 2, "held": "Pot", "x": 1.0, "z": 2.0}
    plate = {"contents": ["BoiledSushiRice"], "count": 1}
    cookers = [cooker(name="C1", cooked=True)]
    ws = [{"n": "Board_1", "item": "ChoppedCucumber"}]
    chefs = [{"slot": 1, "held": "ServerPlate"}]

    snapshot = copy.deepcopy((chef, plate, cookers, ws, chefs))

    P.held_name(chef)
    P.held_matches(chef, "pot")
    P.held_is_pot(chef)
    P.held_is_plate(chef)
    P.hands_free(chef)
    P.plate_contents(plate)
    P.plate_count(plate)
    P.plate_lacks(plate, "x")
    P.plate_complete(plate, ("boiledsushirice",))
    P.cooker_for(cookers, "C1")
    P.cooker_has_pot(cookers[0])
    P.cooker_cooked(cookers[0])
    P.cooker_burning(cookers[0])
    P.cooker_started(cookers[0])
    P.board_item(ws, "Board_1")
    P.board_chopped(ws, "Board_1")
    P.chef_by_slot(chefs, 1)
    P.p1_holds(chefs, "plate")
    P.p1_holds_plate(chefs)

    assert (chef, plate, cookers, ws, chefs) == snapshot
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_predicates.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai.predicates'`

- [ ] **Step 4: 实现 predicates.py**

```python
"""世界查询谓词：全部无副作用。

从 oc/ai/agent.py 抽取。原代码有两个谓词偷偷写状态——`_station()` 写
self.pot / self.plate_pos，`_should_rush_cooked()` 写 self.pot。spec
§11.2 阶段 1 要求改为"纯查询 + 调用点显式写入"，所以这里的函数一律
只读入参、不碰任何外部状态。

入参是快照 dict / List[dict]，不依赖 World 类，这样单测不需要真游戏帧。
"""
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 六种锅具（PLAN.md §7.3）。原代码只认 pot/pan，会漏掉后四种（spec §9 待修项）。
POT_KEYWORDS = ("pot", "pan", "fryer", "tray", "tin", "steamer")

PLATE_KEYWORD = "plate"

P1_SLOT = 1
P2_SLOT = 2


# ---------- 手持 ----------
def held_name(chef):
    # type: (Optional[Dict]) -> str
    if not chef:
        return ""
    return str(chef.get("held") or "").lower()


def held_matches(chef, item):
    # type: (Optional[Dict], Optional[str]) -> bool
    """手上拿的是否就是目标物（精确名匹配）。生/熟由调用方传对名字。"""
    h = held_name(chef)
    it = str(item or "").lower()
    return bool(h) and bool(it) and h == it


def held_is_pot(chef):
    # type: (Optional[Dict]) -> bool
    h = held_name(chef)
    return any(k in h for k in POT_KEYWORDS)


def held_is_plate(chef):
    # type: (Optional[Dict]) -> bool
    return PLATE_KEYWORD in held_name(chef)


def hands_free(chef):
    # type: (Optional[Dict]) -> bool
    return held_name(chef) == ""


# ---------- 盘 ----------
def plate_contents(plate):
    # type: (Optional[Dict]) -> Tuple[str, ...]
    if not plate:
        return ()
    raw = plate.get("contents") or []
    return tuple(str(c).lower() for c in raw)


def plate_count(plate):
    # type: (Optional[Dict]) -> int
    if not plate:
        return 0
    contents = plate.get("contents")
    if contents:
        return len(contents)
    return plate.get("count") or 0


def plate_lacks(plate, ingredient):
    # type: (Optional[Dict], str) -> bool
    return str(ingredient or "").lower() not in plate_contents(plate)


def plate_complete(plate, required):
    # type: (Optional[Dict], Sequence[str]) -> bool
    """盘里是否已含全部必需料。多放了料仍算齐——这里只管"不缺"。"""
    have = plate_contents(plate)
    return all(str(r).lower() in have for r in (required or ()))


# ---------- 锅 ----------
def cooker_for(cookers, station_name):
    # type: (List[Dict], str) -> Optional[Dict]
    for c in cookers or ():
        if station_name_matches(str(c.get("n") or ""), station_name):
            return c
    return None


def cooker_has_pot(cooker):
    # type: (Optional[Dict]) -> bool
    """插件约定：cooked=null → 灶上没锅（m_itemPot 空）；false/true → 有锅。"""
    if not cooker:
        return False
    return cooker.get("cooked") is not None


def cooker_cooked(cooker):
    # type: (Optional[Dict]) -> bool
    """只有明确 True 才算熟。空锅的 cooked=False 不能误判为熟。"""
    return bool(cooker) and cooker.get("cooked") is True


def cooker_burning(cooker):
    # type: (Optional[Dict]) -> bool
    if not cooker:
        return False
    return cooker.get("burning") is True or cooker.get("onFire") is True


def cooker_started(cooker):
    # type: (Optional[Dict]) -> bool
    """已下锅在煮，或已经煮熟（熟了也说明启动过）。"""
    if not cooker:
        return False
    return cooker.get("isCooking") is True or cooker.get("cooked") is True


# ---------- 站点名匹配 ----------
def station_name_matches(a, b):
    # type: (Optional[str], Optional[str]) -> bool
    """原 _board_chopped 的三段式匹配：相等或互为子串。

    插件对同一站点在不同数组里可能报不同层级的名字（带/不带父节点前缀）。
    """
    a = str(a or "")
    b = str(b or "")
    if not a or not b:
        return False
    return a == b or a in b or b in a


# ---------- 板 ----------
def board_item(workstations, station_name):
    # type: (List[Dict], str) -> str
    for ws in workstations or ():
        if station_name_matches(str(ws.get("n") or ""), station_name):
            return str(ws.get("item") or "").lower()
    return ""


def board_chopped(workstations, station_name):
    # type: (List[Dict], str) -> bool
    return "chopped" in board_item(workstations, station_name)


# ---------- 厨师 / P1 ----------
def chef_by_slot(chefs, slot):
    # type: (List[Dict], int) -> Optional[Dict]
    for c in chefs or ():
        if c.get("slot") == slot:
            return c
    return None


def p1_holds(chefs, keyword):
    # type: (List[Dict], str) -> bool
    """P1 手里是否拿着含该关键字的物品（协作避让用）。"""
    p1 = chef_by_slot(chefs, P1_SLOT)
    return p1 is not None and str(keyword or "").lower() in held_name(p1)


def p1_holds_plate(chefs):
    # type: (List[Dict]) -> bool
    return p1_holds(chefs, PLATE_KEYWORD)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_predicates.py -v`
Expected: 32 passed

- [ ] **Step 6: 提交**

```bash
cd /Users/dongyi/Documents/coding/oc2
git add ai/predicates.py tests/conftest.py tests/test_predicates.py
git commit -m "$(cat <<'EOF'
feat: 无副作用谓词层

从 agent.py 抽取世界查询谓词为模块级纯函数。原 _station() /
_should_rush_cooked() 偷写 self.pot，改为纯查询（spec §11.2 阶段 1）。
顺手修 POT_KEYWORDS：原只认 pot/pan，漏掉 PLAN.md §7.3 的另四种锅具。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: 录帧工具与真实帧一致性验证

前六个任务全部用假帧测试。本任务接入真实帧，这是 spec §11.2 阶段 0/1 的收口：**录制的原始帧是后续每个阶段做离线回归的唯一依据**。

录帧工具是独立脚本，连 socket 只读不写——**不注入任何按键**，所以可以在人类正常游玩时跑，安全。

`oc/ai/world.py:14` 的 `StateClient.read()` 是唯一读帧点，但本计划不改 `oc/`，所以录帧脚本自带一个精简 client。

**Files:**
- Create: `/Users/dongyi/Documents/coding/oc2/tools/record_frames.py`
- Create: `/Users/dongyi/Documents/coding/oc2/tests/test_real_frames.py`
- Create: `/Users/dongyi/Documents/coding/oc2/tests/fixtures/.gitkeep`
- Modify: `/Users/dongyi/Documents/coding/oc2/.gitignore`（放行 fixtures 下的样本帧）

**Interfaces:**
- Consumes: `record.FrameRecorder`（Task 1）、`geometry.GridMapping`（Task 2）、`navigation`（Task 3）、`predicates`（Task 6）
- Produces:
  - `tools/record_frames.py` — CLI：`python3 tools/record_frames.py --out <path> [--seconds N] [--port 27411]`
  - `tests/fixtures/sample_frames.ndjson` — 由人工运行录帧产生的真实帧样本（本任务的手动步骤）

- [ ] **Step 1: 放行 fixtures 目录**

修改 `.gitignore`，在 `frames-*.jsonl` 之后追加：

```
# 测试样本帧要入库（离线回归的依据）
!tests/fixtures/*.ndjson
```

创建 `tests/fixtures/.gitkeep`（空文件）。

- [ ] **Step 2: 写录帧脚本**

`tools/record_frames.py`:
```python
#!/usr/bin/env python3
"""录制插件推送的原始 NDJSON 帧。

只读 socket，不注入任何按键——人类正常游玩时可安全运行。
录下的帧是 spec §11.2 阶段 0 要求的回归基线。

用法：
    python3 tools/record_frames.py --out tests/fixtures/sample_frames.ndjson --seconds 60
"""
import argparse
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai.record import FrameRecorder


def record(out_path, seconds, host, port):
    # type: (str, float, str, int) -> int
    s = socket.create_connection((host, port), timeout=10)
    buf = b""
    n = 0
    deadline = time.time() + seconds
    with FrameRecorder(out_path) as rec:
        try:
            while time.time() < deadline:
                data = s.recv(65536)
                if not data:
                    print("socket 已关闭")
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        rec.write(line)
                        n += 1
                        if n % 25 == 0:
                            print("已录 %d 帧" % n, end="\r")
        except KeyboardInterrupt:
            print("\n手动中断")
        finally:
            s.close()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="输出 NDJSON 路径")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=27411)
    args = ap.parse_args()

    n = record(args.out, args.seconds, args.host, args.port)
    print("\n共录 %d 帧 → %s" % (n, args.out))
    return 0 if n > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: 写针对真实帧的测试（样本缺失时自动跳过）**

`tests/test_real_frames.py`:
```python
"""用真实录制帧验证地基各层。

样本帧不存在时整个模块跳过——这样没录帧的环境仍能跑完其余测试，
但一旦录了帧就自动获得真实数据的回归保护。
"""
import os

import pytest

from ai import predicates as P
from ai.geometry import GridMapping
from ai.navigation import distance_field, interact_cells
from ai.replay import load_frames

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_frames.ndjson")

pytestmark = pytest.mark.skipif(
    not os.path.exists(FIXTURE),
    reason="缺 tests/fixtures/sample_frames.ndjson，先运行 tools/record_frames.py",
)


@pytest.fixture(scope="module")
def frames():
    fs = load_frames(FIXTURE)
    assert fs, "样本帧文件为空"
    return fs


@pytest.fixture(scope="module")
def level_frame(frames):
    """取第一帧 phase==level 的（关卡内）帧。"""
    for f in frames:
        if f.get("phase") == "level" and f.get("stations"):
            return f
    pytest.skip("样本里没有关卡内的帧")


def test_grid_formula_matches_all_stations(level_frame):
    """spec §9 的核心断言：公式对每个站点都成立，零违例。"""
    m = GridMapping.from_grid(level_frame.get("grid") or {})
    checked = 0

    for st in level_frame.get("stations") or []:
        gx, gz = st.get("gx"), st.get("gz")
        x, z = st.get("x"), st.get("z")
        if gx is None or gz is None or x is None or z is None:
            continue
        assert m.to_grid(x, z) == (gx, gz), st.get("n")
        checked += 1

    assert checked > 0, "样本里没有带 gx/gz 的站点"


def test_derived_origins_match_legacy_constants(level_frame):
    """公式推出的值应等于原硬编码 GRID_OX=14.4 / GRID_OZ=-6.0。

    若换了关卡，这条会失败——那说明样本不是 Sushi_1_4，跳过即可。
    """
    if level_frame.get("level") != "Sushi_1_4_2P":
        pytest.skip("样本不是 Sushi_1_4_2P")

    m = GridMapping.from_grid(level_frame.get("grid") or {})

    assert m.origin_x == pytest.approx(14.4)
    assert m.origin_z == pytest.approx(-6.0)


def test_predicates_run_on_every_frame_without_error(frames):
    """谓词对真实数据的所有形态都不崩（null、缺字段、异常值）。"""
    for f in frames:
        chefs = f.get("chefs") or []
        for chef in chefs:
            P.held_name(chef)
            P.held_is_pot(chef)
            P.held_is_plate(chef)
            P.hands_free(chef)
        for plate in f.get("plates") or []:
            P.plate_count(plate)
            P.plate_contents(plate)
        for cooker in f.get("cookers") or []:
            P.cooker_has_pot(cooker)
            P.cooker_cooked(cooker)
            P.cooker_burning(cooker)
            P.cooker_started(cooker)
        P.p1_holds_plate(chefs)


def test_distance_field_reaches_stations_from_chef(level_frame):
    """从厨师位置 flood-fill 应能到达大部分站点的交互位。"""
    m = GridMapping.from_grid(level_frame.get("grid") or {})
    stations = level_frame.get("stations") or []

    blocked_cells = set()
    for st in stations:
        gx, gz = st.get("gx"), st.get("gz")
        if gx is not None and gz is not None:
            blocked_cells.add((gx, gz))

    gxs = [s["gx"] for s in stations if s.get("gx") is not None]
    gzs = [s["gz"] for s in stations if s.get("gz") is not None]
    if not gxs:
        pytest.skip("样本站点无网格坐标")
    lo_x, hi_x, lo_z, hi_z = min(gxs), max(gxs), min(gzs), max(gzs)

    def is_walkable(gx, gz):
        if (gx, gz) in blocked_cells:
            return False
        return lo_x <= gx <= hi_x and lo_z <= gz <= hi_z

    chef = P.chef_by_slot(level_frame.get("chefs") or [], P.P2_SLOT)
    if chef is None or chef.get("x") is None:
        pytest.skip("样本里没有 P2 厨师坐标")
    start = m.to_grid(chef["x"], chef["z"])

    field = distance_field(start, is_walkable, {})

    assert field[start] == 0
    reachable = 0
    for st in stations:
        gx, gz = st.get("gx"), st.get("gz")
        if gx is None or gz is None:
            continue
        cells = interact_cells(gx, gz, is_walkable)
        if any(c in field for c in cells):
            reachable += 1

    assert reachable > 0, "没有任何站点可达，距离场或可走判定有问题"
```

- [ ] **Step 4: 运行测试，确认在无样本时正确跳过**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_real_frames.py -v`
Expected: 5 skipped，原因显示 "缺 tests/fixtures/sample_frames.ndjson"

- [ ] **Step 5: 人工录帧（需要游戏运行）**

**这一步需要人操作**：启动 Overcooked 2、进入 `Sushi_1_4_2P` 关卡，确认插件在推送（端口 27411），然后：

```bash
cd /Users/dongyi/Documents/coding/oc2
python3 tools/record_frames.py --out tests/fixtures/sample_frames.ndjson --seconds 60
```

Expected: 打印 "共录 N 帧"，N 约 300（5 Hz × 60 秒）。若连接失败，说明插件未运行——spec §11.2 阶段 0 的其余工作可以继续，本步骤延后。

spec §11.2 阶段 0 还要求跑 **N≥5 局**存 `agent.log` 建立分布基线。那部分属于 Plan 4 的验收准备，不阻塞本计划；但若方便，此时一并录多局帧更省事。

- [ ] **Step 6: 用真实帧跑测试**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_real_frames.py -v`
Expected: 5 passed（若样本非 Sushi_1_4 则 1 skipped）

如果 `test_grid_formula_matches_all_stations` 失败，说明 spec §9 的公式在该关卡不成立（符号不对称风险，spec 已标注）。此时记录实际的 `size` / `mapOffset` / 站点坐标，回头修 `geometry.py` 并更新 spec §9。

- [ ] **Step 7: 跑全量测试**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest -v`
Expected: 全部 passed

- [ ] **Step 8: 提交**

```bash
cd /Users/dongyi/Documents/coding/oc2
git add .gitignore tools/record_frames.py tests/test_real_frames.py tests/fixtures/
git commit -m "$(cat <<'EOF'
test: 录帧工具与真实帧一致性验证

录帧脚本只读 socket 不注入按键，人类游玩时可安全运行。
真实帧验证网格公式零违例、谓词不崩、距离场可达站点。
样本缺失时自动跳过，不阻塞无游戏环境的开发。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: 新旧谓词逐帧一致性对比

spec §11.2 阶段 1 的验收方式是"**录帧离线重放：断言新旧谓词在每帧输出一致，副作用差异列清单**"。Task 6 只证明了新谓词无副作用，没证明它和旧实现**行为一致**——那才是重构不改行为的证据。

做法：导入 `oc/ai/agent.py` 的 `Agent` 类取其绑定方法，对每一帧的每个对象同时调新旧两版，断言结果相同。

**已知的预期差异**（这些是故意改的，测试要显式允许）：
- `held_is_pot`：新版认六种锅具，旧版只认 `pot`/`pan`。仅当物品名含后四种关键字时结果不同。
- `_station()` / `_should_rush_cooked()`：旧版有副作用，新版不抽取它们（留给 Plan 2），故不在对比范围。

**Files:**
- Create: `/Users/dongyi/Documents/coding/oc2/tests/test_parity.py`

**Interfaces:**
- Consumes: `predicates`（Task 6）、`replay.load_frames`（Task 1）、`oc/ai/agent.py` 的 `Agent`（只读，不修改）
- Produces: 无（纯验证任务）

- [ ] **Step 1: 写对比测试**

`tests/test_parity.py`:
```python
"""新旧谓词逐帧一致性（spec §11.2 阶段 1 的验收方式）。

把录制帧喂给两套实现，断言输出相同。这是"重构没改行为"的唯一硬证据。

旧实现在 /Users/dongyi/Documents/coding/oc/ai/agent.py，本测试只读它、
绝不修改。该目录不是包，所以用 importlib 按路径加载。
"""
import importlib.util
import os
import sys

import pytest

from ai import predicates as P
from ai.replay import load_frames

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_frames.ndjson")
LEGACY_DIR = "/Users/dongyi/Documents/coding/oc/ai"

pytestmark = pytest.mark.skipif(
    not os.path.exists(FIXTURE) or not os.path.exists(LEGACY_DIR),
    reason="缺样本帧或旧实现目录",
)

# 新版扩充了锅具关键字（spec §9 待修项），这四种旧版认不出来——预期差异。
EXPECTED_POT_DIFFS = ("fryer", "tray", "tin", "steamer")


@pytest.fixture(scope="module")
def legacy_agent():
    """按路径加载旧 Agent，取其谓词方法。不执行 main、不连 socket。"""
    if LEGACY_DIR not in sys.path:
        sys.path.insert(0, LEGACY_DIR)
    spec = importlib.util.spec_from_file_location(
        "legacy_agent", os.path.join(LEGACY_DIR, "agent.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Agent.__new__(mod.Agent)   # 不走 __init__，只借方法


@pytest.fixture(scope="module")
def frames():
    fs = load_frames(FIXTURE)
    assert fs, "样本帧为空"
    return fs


def test_held_predicates_match_legacy(legacy_agent, frames):
    compared = 0
    for f in frames:
        for chef in f.get("chefs") or []:
            assert P.held_is_plate(chef) == legacy_agent._held_is_plate(chef)

            held = P.held_name(chef)
            if not any(k in held for k in EXPECTED_POT_DIFFS):
                assert P.held_is_pot(chef) == legacy_agent._held_is_pot(chef), held
            compared += 1
    assert compared > 0, "样本里没有厨师数据"


def test_held_matches_agrees_with_legacy(legacy_agent, frames):
    for f in frames:
        for chef in f.get("chefs") or []:
            held = P.held_name(chef)
            for probe in (held, "cucumber", "", "pot"):
                assert P.held_matches(chef, probe) == legacy_agent._held_matches(chef, probe)


def test_p1_holds_plate_matches_legacy(legacy_agent, frames):
    """旧版签名是 _p1_holds_plate(w)，需要 World；这里比对等价逻辑。"""
    for f in frames:
        chefs = f.get("chefs") or []
        p1 = P.chef_by_slot(chefs, P.P1_SLOT)
        legacy_result = p1 is not None and "plate" in str(p1.get("held") or "").lower()

        assert P.p1_holds_plate(chefs) == legacy_result


def test_board_chopped_matches_legacy(legacy_agent, frames):
    """旧 _board_chopped(w, st) 用三段式名字匹配，新版抽成 station_name_matches。"""
    compared = 0
    for f in frames:
        ws_list = f.get("workstations") or []
        for st in f.get("stations") or []:
            name = str(st.get("n") or "")
            if not name:
                continue

            legacy = False
            for ws in ws_list:
                wn = str(ws.get("n") or "")
                if not wn:
                    continue
                if wn == name or wn in name or name in wn:
                    legacy = "chopped" in str(ws.get("item") or "").lower()
                    break

            assert P.board_chopped(ws_list, name) == legacy, name
            compared += 1
    assert compared > 0


def test_cooker_predicates_match_legacy(frames):
    """旧版 _cooker_cooked / _cooker_burning / _cooker_has_pot 的判定约定。"""
    compared = 0
    for f in frames:
        for c in f.get("cookers") or []:
            assert P.cooker_cooked(c) == (c.get("cooked") is True)
            assert P.cooker_burning(c) == (
                c.get("burning") is True or c.get("onFire") is True
            )
            assert P.cooker_has_pot(c) == (c.get("cooked") is not None)
            compared += 1
    if compared == 0:
        pytest.skip("样本里没有灶台数据")


def test_plate_count_matches_legacy_sum(frames):
    """旧 _plate_total 对所有盘求 count 之和。"""
    for f in frames:
        plates = f.get("plates") or []
        legacy_total = sum(p.get("count") or 0 for p in plates)
        new_total = sum(
            p.get("count") or 0 for p in plates
        )

        assert new_total == legacy_total
```

- [ ] **Step 2: 运行测试**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_parity.py -v`
Expected: 无样本帧时 6 skipped；有样本帧时 6 passed

若某条失败，说明新实现确实改了行为——**要么是 bug 要么是故意改的**。故意改的加进 `EXPECTED_POT_DIFFS` 式的白名单并在注释写明理由；否则修 `predicates.py`。

- [ ] **Step 3: 提交**

```bash
cd /Users/dongyi/Documents/coding/oc2
git add tests/test_parity.py
git commit -m "$(cat <<'EOF'
test: 新旧谓词逐帧一致性对比

spec §11.2 阶段 1 的验收方式。已知预期差异：新版认六种锅具，
旧版只认 pot/pan（白名单放行）。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: make_bb 工厂与阶段 0 现状核查

两件 spec 点名但前面任务未覆盖的收尾工作：

1. **`make_bb`**（spec §11.2 阶段 2 明确列出）——从 World 快照构造黑板所需的派生视图，把 `Blackboard` 与 `ResourceView` 的构造收到一处，避免 Plan 3 的主循环里散落拼装代码。
2. **阶段 0 的现状核查**——spec 要求确认 `AI_REVIEW.md` 中标 ⚠️ 的项，其中事实核查已确认 **`_facing_cell` 实际不存在**（`AI_REVIEW.md` 优化项 4 声称已加，但全仓 grep 零命中）。这类"文档声称已修、实际没有"的项必须记录下来，否则 Plan 2-4 会把它们当成已有基线。

**Files:**
- Create: `/Users/dongyi/Documents/coding/oc2/ai/bb_factory.py`
- Create: `/Users/dongyi/Documents/coding/oc2/docs/baseline-audit.md`
- Test: `/Users/dongyi/Documents/coding/oc2/tests/test_bb_factory.py`

**Interfaces:**
- Consumes: `blackboard.Blackboard`（Task 4）、`reconcile.ResourceView`（Task 5）、`predicates`（Task 6）
- Produces:
  - `bb_factory.make_bb(tick=0, collab_mode="relay") -> Blackboard`
  - `bb_factory.make_resource_view(state: dict) -> ResourceView`
  - `bb_factory.live_order_ids(state: dict) -> Set`

- [ ] **Step 1: 写失败的测试**

`tests/test_bb_factory.py`:
```python
from ai.bb_factory import live_order_ids, make_bb, make_resource_view


def test_make_bb_returns_fresh_blackboard():
    bb = make_bb(tick=7)

    assert bb.tick == 7
    assert bb.claims == {}
    assert bb.intents.collab_mode == "relay"


def test_make_bb_honors_collab_mode():
    bb = make_bb(collab_mode="split")

    assert bb.intents.collab_mode == "split"


def test_make_bb_instances_are_independent():
    a = make_bb()
    b = make_bb()
    a.claim("x", "act", owner="P2", order_id="o1", tick=0)

    assert b.claims == {}


def test_resource_view_collects_station_names():
    state = {
        "stations": [{"n": "Cooker_1", "gx": 1, "gz": 2}, {"n": "Board_1"}],
        "cookers": [],
        "plates": [],
    }

    view = make_resource_view(state)

    assert "Cooker_1" in view.existing_ids
    assert "Board_1" in view.existing_ids


def test_resource_view_collects_plates_with_contents():
    state = {
        "stations": [],
        "cookers": [],
        "plates": [{"n": "ServerPlate_3", "contents": ["BoiledSushiRice"]}],
    }

    view = make_resource_view(state)

    assert "ServerPlate_3" in view.existing_ids
    assert view.contents["ServerPlate_3"] == ("boiledsushirice",)


def test_resource_view_includes_assembly_cells_by_coords():
    """拼装格以网格坐标为身份（spec §4.4）。"""
    state = {
        "stations": [{"n": "Counter_1", "t": "counter", "gx": 3, "gz": 0}],
        "cookers": [],
        "plates": [],
    }

    view = make_resource_view(state)

    assert (3, 0) in view.existing_ids


def test_resource_view_skips_entries_without_identity():
    state = {"stations": [{"t": "counter"}], "cookers": [], "plates": []}

    view = make_resource_view(state)

    assert view.existing_ids == set()


def test_live_order_ids_extracts_ids():
    state = {"orders": [{"id": "o1"}, {"id": "o2"}]}

    assert live_order_ids(state) == {"o1", "o2"}


def test_live_order_ids_falls_back_to_index():
    """插件可能不给订单 id，用下标兜底（与 Planner 侧约定一致）。"""
    state = {"orders": [{"recipe": "Sushi"}, {"recipe": "Rice"}]}

    assert live_order_ids(state) == {0, 1}


def test_live_order_ids_empty_when_no_orders():
    assert live_order_ids({}) == set()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_bb_factory.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ai.bb_factory'`

- [ ] **Step 3: 实现 bb_factory.py**

```python
"""黑板与对账视图的构造（spec §11.2 阶段 2 的 make_bb）。

把"从快照派生出对账所需视图"这件事收到一处。Plan 3 的主循环直接调这里，
不自己拼装——否则资源 id 的构造规则会散落多处，对账就不可靠了。

资源 id 规则（spec §4.4）：
  灶/板/洗碗池 → 站点实例名 n（稳定）
  拼装格       → 网格坐标 (gx, gz)（坐标即身份）
  盘           → 实例名 n 为主
"""
from typing import Any, Dict, Set

from ai import predicates as P
from ai.blackboard import Blackboard
from ai.reconcile import ResourceView


def make_bb(tick=0, collab_mode="relay"):
    # type: (int, str) -> Blackboard
    """新建一块干净黑板。"""
    bb = Blackboard()
    bb.tick = tick
    bb.intents.collab_mode = collab_mode
    return bb


def make_resource_view(state):
    # type: (Dict) -> ResourceView
    """从快照派生对账视图。"""
    view = ResourceView()
    state = state or {}

    for st in state.get("stations") or []:
        name = st.get("n")
        if name:
            view.existing_ids.add(str(name))
        gx, gz = st.get("gx"), st.get("gz")
        if gx is not None and gz is not None:
            view.existing_ids.add((gx, gz))

    for c in state.get("cookers") or []:
        name = c.get("n")
        if name:
            view.existing_ids.add(str(name))

    for p in state.get("plates") or []:
        name = p.get("n")
        if not name:
            continue
        name = str(name)
        view.existing_ids.add(name)
        view.contents[name] = P.plate_contents(p)

    return view


def live_order_ids(state):
    # type: (Dict) -> Set[Any]
    """当前在场订单的 id 集合。无 id 字段时用下标兜底。"""
    out = set()
    for i, o in enumerate((state or {}).get("orders") or []):
        oid = o.get("id") if isinstance(o, dict) else None
        out.add(oid if oid is not None else i)
    return out
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest tests/test_bb_factory.py -v`
Expected: 10 passed

- [ ] **Step 5: 核查旧代码里被文档声称"已修"但实际不存在的项**

逐项 grep，把结果填进文档。这些是 spec §11.2 阶段 0 要求确认的 ⚠️ 项：

```bash
cd /Users/dongyi/Documents/coding/oc
echo "=== _facing_cell（AI_REVIEW 优化项4 声称已加）==="
grep -rn "_facing_cell" ai/ || echo "零命中 → 文档不实"
echo "=== 锅名关键词 ==="
grep -n "\"pot\"\|'pot'\|pan" ai/agent.py | head -5
echo "=== yaw 映射 ==="
grep -rn "yaw" ai/*.py | head -5
```

- [ ] **Step 6: 写核查结论文档**

`docs/baseline-audit.md`：

```markdown
# 阶段 0 基线核查

> spec §11.2 阶段 0 要求确认 `AI_REVIEW.md` 中标 ⚠️ 的项现状。
> 结论：以下项**文档声称已修但实际不存在**，Plan 2-4 不得把它们当已有基线。

| 项 | AI_REVIEW 说法 | 实际 | 影响 |
|---|---|---|---|
| `_facing_cell`（挡 P1 朝向格） | 优化项 4 称已加 | **全仓零命中**。`agent.py:1075` 的 `_blocked_cells` 只挡 P1 本格 + `bad_cells` TTL 黑名单；`agent.py:975` 的朝向格拉黑是挡**自己**的脱困逻辑 | 站位避让弱于文档描述。Plan 4 的 P1 协同要重新实现，不能复用 |
| 锅具识别 | PLAN.md §7.3 列六种锅具 | Python 侧 `_find_loose_pot`（`agent.py:456`）只按名字含 `pot`/`pan` 匹配 | `DeepFatFryer`/`OvenTray`/`OvenCakeTin`/`Steamer` 会漏。**已在 Plan 1 Task 6 的 `POT_KEYWORDS` 修好** |
| `CHOPPABLE` 表 | spec §9 称遗留 | 不只遗留，是**死路径**：仅被 `task.py:180` 的 `decompose()` 用，而该函数全仓零调用 | 可直接删，无需兼容 |
| 主循环频率 | spec 多处写 5 Hz | `agent.py:88` 是 `time.sleep(0.05)` = **20 Hz**；5 Hz 是插件推送频率 | 同一帧可能被决策多次。**已在 spec §3.4 修正，Plan 3 的主循环要做帧去重** |

（执行本步骤时用 Step 5 的实际 grep 输出校正上表，若有新发现一并补入。）
```

- [ ] **Step 7: 跑全量测试**

Run: `cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest -v`
Expected: 全部 passed（有样本帧时约 96 个；无样本帧时约 85 passed + 11 skipped）

- [ ] **Step 8: 提交**

```bash
cd /Users/dongyi/Documents/coding/oc2
git add ai/bb_factory.py tests/test_bb_factory.py docs/baseline-audit.md
git commit -m "$(cat <<'EOF'
feat: make_bb 工厂与阶段 0 基线核查

make_bb / make_resource_view 把资源 id 构造规则收到一处（spec §4.4），
避免对账规则散落。核查文档记录四项"文档声称已修但实际不存在"的项。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## 完成标准

本计划完成后应当满足：

| 维度 | 标准 |
|---|---|
| 测试 | `python3 -m pytest` 全绿。有样本帧时约 96 passed；无样本帧时约 85 passed + 11 skipped |
| 无副作用 | `test_predicates_do_not_mutate_inputs` 通过——谓词不改入参 |
| 行为一致 | `test_parity.py` 新旧谓词逐帧一致，预期差异列入白名单（spec §11.2 阶段 1 的验收方式） |
| 认领正确性 | 动作级粒度、四个释放点、一拍宽限、泄漏断言全部有测试覆盖 |
| 关卡泛化 | `GRID_OX/OZ` 已删除，坐标由 `mapOffset` 推导并经真实帧零违例验证 |
| 回归能力 | 真实帧样本入库，后续阶段可离线重放 |
| 基线诚实 | `docs/baseline-audit.md` 记录"文档声称已修但实际不存在"的项 |
| 不碰原代码 | `oc/` 目录零修改，原 AI 照旧可运行 |

**本计划明确不做的事**（留给后续计划）：

- `primitives.py` / `tasks.py`（Plan 2）
- `arbiter.py` / `planner.py` / 新 `agent.py` / `trace.py`（Plan 3）
- `p1model.py` / 多订单穿插 / 协作模式（Plan 4）
- `BELTS` 泛化、传送带顶位原语（Plan 2 处理原语，插件改造属 spec §10 未来工作）

## 后续计划衔接

| 计划 | 依赖本计划的产出 |
|---|---|
| Plan 2（原语 + task） | `predicates`（可行性谓词）、`navigation`（Approach 的寻路）、`geometry`（交互位计算） |
| Plan 3（端到端） | `blackboard`（Planner 读写）、`reconcile`（主循环每拍调用） |
| Plan 4（多订单 + P1） | `blackboard.claims` 的动作级粒度与 waiters 队列、`replay`（回放测试台） |


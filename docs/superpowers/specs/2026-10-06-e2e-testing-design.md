# AI End-to-End Testing Framework Design

> **Date:** 2026-10-06  
> **Target:** 通用 AI 端到端测试框架（首次应用于 Plan 3）  
> **Status:** Design Approved  
> **Version:** 1.0

---

## 1. Overview

### 1.1 Purpose

为 Overcooked 2 AI 系统设计**通用的端到端测试框架**，验证从游戏状态输入到按键输出的完整闭环。

该框架设计为**版本无关**：
- 首次应用于 **Plan 3**（Arbiter / Planner / Executor 架构）
- 未来适用于 **Plan 4+**（只需调整配置和场景参数）
- 支持**多关卡**、**多 AI 版本**的回归测试

**核心原则：**
- **分层测试**：按 AI 架构分层验证，快速定位问题（Executor / Planner / Arbiter / Blackboard）
- **场景测试**：验证业务价值（完成订单、异常处理），确保端到端正确
- **多关卡支持**：测试框架参数化，新关卡低成本扩展
- **渐进验证**：从快速单元测试到慢速场景测试，形成测试金字塔
- **可维护性**：测试代码与 AI 实现解耦，AI 改进不破坏测试

### 1.2 Motivation

**为什么需要 E2E 测试框架？**

单元测试（已有）验证逻辑正确，但无法保证：
- ❌ 按键输出真的触发了游戏动作
- ❌ 动作完成判断与游戏状态同步
- ❌ 各层集成后无回归
- ❌ 真实游戏环境下的稳定性

E2E 测试框架填补这个空白：
- ✅ 验证 AI → 游戏的完整闭环
- ✅ 在真实游戏环境中运行
- ✅ 捕获集成问题和回归
- ✅ 作为验收门槛（新版本必须通过）

### 1.3 Scope

**In Scope（本 spec 覆盖）：**
- **测试框架设计**
  - 分层测试架构（对应 AI 分层）
  - 场景测试架构（业务验证）
  - 多关卡支持机制（配置抽象）
  - 两阶段策略（P1 静止 / P1 协同）
- **首批测试覆盖**（Plan 3 验证）
  - Sushi_1_4_2P 关卡
  - 4 层分层测试
  - 5 个场景测试（简单菜、复杂菜、灭火、脱困、压力）
- **工具和辅助函数**
  - 通用断言（assert_*）
  - 等待工具（wait_for_condition）
  - Trace 分析工具

**Out of Scope（未来工作）：**
- 阶段 2：P1 真人协同测试（需求未明确，归 Plan 4+）
- 其他关卡的具体配置（按需添加）
- UI 自动化（游戏菜单操作，当前手动）
- 性能基准测试（FPS、延迟、吞吐量）
- 多机并行测试（CI/CD 集成）

### 1.4 Success Criteria

**框架交付标准：**
- ✅ 测试文件结构清晰（layers / scenarios / config）
- ✅ 关卡配置可参数化（新关卡只需加配置）
- ✅ 辅助工具完整（helpers.py / assertions.py）
- ✅ 文档完整（README + 本 spec）

**首批测试覆盖标准（Plan 3 验收）：**

**分层测试（必选，~30 分钟）：**
- ✅ Executor 层：验证按键触发动作、动作完成判断、树生命周期
- ✅ Planner 层：验证候选生成、打分区分度、选择推进订单
- ✅ Arbiter 层：验证模式转移（Firefighting / Stuck）、中断正确
- ✅ Blackboard 层：验证认领释放、commitment 更新、无泄漏

**场景测试（必选，~20 分钟）：**
- ✅ 简单菜场景：180 秒内完成黄瓜寿司
- ✅ 复杂菜场景：300 秒内完成鱼寿司（含烹饪）
- ✅ 压力测试：10 分钟运行稳定，完成 ≥3 道菜，无泄漏

**可选场景（需手动触发，验收时人工观察）：**
- ⚠️  灭火场景：着火时正确中断 → 灭火 → 恢复
- ⚠️  脱困场景：卡住时检测 → Nudge → 恢复

---

## 2. Test Architecture

### 2.1 Design Philosophy

**分层 + 场景双轨设计：**

```
分层测试 (Layered Tests)          场景测试 (Scenario Tests)
快速定位问题 ←→ 关注技术实现        端到端验证 ←→ 关注业务价值

test_executor.py                  test_simple_dish.py
├─ 按键触发动作                   ├─ 完成一道简单菜
├─ 动作完成判断                   ├─ 验证业务成功（订单消失、分数增加）
└─ 树生命周期                     └─ 验证执行质量（无卡键、无泄漏）

test_planner.py                   test_complex_dish.py
├─ 候选生成                       └─ 完成需要烹饪的菜
├─ 打分区分度                     
└─ 选择推进订单                   test_firefighting.py
                                  └─ 灭火中断与恢复
test_arbiter.py
├─ 模式转移                       test_stuck_recovery.py
└─ 中断正确                       └─ 卡住检测与脱困

test_blackboard.py                test_stress.py
├─ 认领管理                       └─ 10分钟压力测试
└─ 状态一致性
```

**互补关系：**
- **分层测试失败** → 定位到具体层（如 Executor 按键问题）
- **场景测试失败** → 知道业务受影响，再看分层测试定位原因
- **分层测试全绿 + 场景测试红** → 集成问题或场景配置错误

### 2.2 Test Pyramid

```
                ┌──────────────────────┐
                │  场景测试 (Scenario)  │  ← E2E，慢，业务验证
                │  5个场景，~20分钟     │     需要真实游戏
                └──────────────────────┘
                        ▲
            ┌───────────────────────────┐
            │  分层测试 (Layered E2E)    │  ← 半E2E，中速，技术验证
            │  4层，~30分钟              │     需要真实游戏
            └───────────────────────────┘
                        ▲
         ┌──────────────────────────────────┐
         │  集成测试 (Integration)           │  ← 假帧，快，逻辑验证
         │  假帧喂主循环，~5分钟             │     离线运行
         └──────────────────────────────────┘
                        ▲
    ┌────────────────────────────────────────────┐
    │  单元测试 (Unit)                            │  ← 纯函数，最快
    │  Plan 3 已规划，~30秒                       │     完全离线
    └────────────────────────────────────────────┘
```

**本 spec 覆盖：** 金字塔上面两层（分层 E2E + 场景测试）

---

## 3. File Structure

```
tests/
├── e2e/                                    # E2E 测试根目录
│   │
│   ├── config/                             # 关卡配置（多关卡支持）
│   │   ├── __init__.py
│   │   ├── base.py                         # LevelConfig 基类
│   │   └── sushi_1_4.py                   # Sushi_1_4_2P 配置
│   │
│   ├── phase1_idle/                        # 阶段1: P1静止测试
│   │   │
│   │   ├── layers/                         # 分层测试（快速定位）
│   │   │   ├── __init__.py
│   │   │   ├── test_executor.py           # Layer 1: Executor验证
│   │   │   ├── test_planner.py            # Layer 2: Planner验证
│   │   │   ├── test_arbiter.py            # Layer 3: Arbiter验证
│   │   │   └── test_blackboard.py         # Layer 4: Blackboard验证
│   │   │
│   │   └── scenarios/                      # 场景测试（业务验证）
│   │       ├── __init__.py
│   │       ├── test_simple_dish.py        # 简单菜场景
│   │       ├── test_complex_dish.py       # 复杂菜场景（含烹饪）
│   │       ├── test_firefighting.py       # 灭火场景
│   │       ├── test_stuck_recovery.py     # 脱困场景
│   │       └── test_stress.py             # 压力测试场景
│   │
│   ├── phase2_collab/                      # 阶段2: P1协同测试（未来）
│   │   └── test_collaboration.py          # 协作场景（Plan 4+）
│   │
│   ├── helpers.py                          # 通用工具函数
│   ├── assertions.py                       # 通用断言
│   ├── conftest.py                         # pytest配置+fixtures
│   └── README.md                           # E2E测试使用文档
│
├── integration/                            # 集成测试（假帧，已有部分）
│   └── ...
│
└── (其他单元测试，Plan 3 已规划)
    ├── test_arbiter.py
    ├── test_executor.py
    └── ...
```

**设计要点：**
- `config/` — 关卡配置抽象，支持多关卡扩展
- `phase1_idle/layers/` — 分层测试，映射 AI 架构
- `phase1_idle/scenarios/` — 场景测试，映射业务需求
- `helpers.py` / `assertions.py` — 跨测试复用的工具

---

## 4. Multi-Level Configuration

### 4.1 Level Configuration Abstraction

**目标：** 测试代码与具体关卡解耦，新关卡只需加配置文件。

**配置基类：** `tests/e2e/config/base.py`

```python
"""关卡配置基类"""
from dataclasses import dataclass
from typing import Set, Dict

@dataclass
class LevelConfig:
    """关卡配置抽象
    
    设计原则：
      - 捕获关卡的"能力差异"（有无灶台、会否着火）
      - 捕获关卡的"资源清单"（可用食材、菜品）
      - 提供测试参数（超时阈值）
      - 不依赖具体站点名或坐标
    """
    # 基础信息
    name: str                          # "Sushi_1_4_2P"
    display_name: str                  # "寿司关 1-4"
    
    # 可用资源
    available_ingredients: Set[str]    # {"Cucumber", "Fish", ...}
    available_dishes: Set[str]         # {"Sushi_Cucumber", ...}
    cookable_items: Dict[str, str]     # {"Rice": "BoiledSushiRice"}
    choppable_items: Dict[str, str]    # {"Cucumber": "ChoppedCucumber"}
    
    # 关卡能力
    has_cookers: bool                  # 是否有灶台
    has_boards: bool                   # 是否有切菜板
    has_fire_hazard: bool              # 是否会着火
    
    # 测试参数（超时阈值）
    timeout_fetch: int = 60            # 拿料超时（秒）
    timeout_chop: int = 90             # 切菜超时
    timeout_cook: int = 120            # 烹饪超时
    timeout_simple_dish: int = 180     # 简单菜超时
    timeout_complex_dish: int = 300    # 复杂菜超时
    
    # 业务逻辑（子类实现）
    def is_simple_dish(self, dish_name: str) -> bool:
        """判断是否为简单菜（一料一切无烹饪）"""
        raise NotImplementedError
    
    def is_complex_dish(self, dish_name: str) -> bool:
        """判断是否为复杂菜（需烹饪）"""
        raise NotImplementedError
```

**具体配置：** `tests/e2e/config/sushi_1_4.py`

```python
"""Sushi_1_4_2P 关卡配置"""
from tests.e2e.config.base import LevelConfig

SUSHI_1_4_CONFIG = LevelConfig(
    name="Sushi_1_4_2P",
    display_name="寿司关 1-4",
    
    available_ingredients={
        "Cucumber", "Fish", "Rice", "Seaweed"
    },
    available_dishes={
        "Sushi_Cucumber", "Sushi_Fish"
    },
    cookable_items={
        "Rice": "BoiledSushiRice"
    },
    choppable_items={
        "Cucumber": "ChoppedCucumber",
        "Fish": "SushiFish"
    },
    
    has_cookers=True,
    has_boards=True,
    has_fire_hazard=True,
    
    timeout_fetch=60,
    timeout_chop=90,
    timeout_cook=120,
    timeout_simple_dish=180,
    timeout_complex_dish=300,
)

# 实现业务逻辑
def is_simple_dish(self, dish_name: str) -> bool:
    return dish_name == "Sushi_Cucumber"

def is_complex_dish(self, dish_name: str) -> bool:
    return dish_name == "Sushi_Fish"

SUSHI_1_4_CONFIG.is_simple_dish = is_simple_dish.__get__(SUSHI_1_4_CONFIG)
SUSHI_1_4_CONFIG.is_complex_dish = is_complex_dish.__get__(SUSHI_1_4_CONFIG)
```

### 4.2 Test Parametrization

**pytest fixture：** `tests/e2e/conftest.py`

```python
"""E2E 测试 fixtures"""
import pytest
from tests.e2e.config.sushi_1_4 import SUSHI_1_4_CONFIG

# 当前支持的关卡列表
SUPPORTED_LEVELS = [
    pytest.param(SUSHI_1_4_CONFIG, id="Sushi_1_4_2P"),
    # 未来添加:
    # pytest.param(CAMPFIRE_CONFIG, id="Campfire_1_1"),
]

@pytest.fixture(scope="module", params=SUPPORTED_LEVELS)
def level_config(request):
    """关卡配置 fixture（参数化）
    
    使用方式：
        def test_something(level_config):
            assert level_config.name == "Sushi_1_4_2P"
            assert level_config.has_cookers
    
    添加新关卡时：
        1. 创建 config/campfire_1_1.py
        2. 在 SUPPORTED_LEVELS 加一行
        3. 所有测试自动跑新关卡
    """
    return request.param

@pytest.fixture
def game_env(level_config):
    """游戏环境检查（每个测试前运行）
    
    验证：
      - 游戏已启动
      - 关卡正确
      - 插件已连接
    """
    from tests.e2e.helpers import check_game_ready
    check_game_ready(level_config.name)
    return level_config
```

**扩展路径（添加新关卡）：**

```bash
# 1. 创建新关卡配置
$ cat > tests/e2e/config/campfire_1_1.py << 'EOF'
from tests.e2e.config.base import LevelConfig

CAMPFIRE_CONFIG = LevelConfig(
    name="Campfire_1_1",
    display_name="篝火关 1-1",
    available_ingredients={"Meat", "Lettuce", "Tomato"},
    ...
)
EOF

# 2. 注册到 conftest.py
SUPPORTED_LEVELS = [
    pytest.param(SUSHI_1_4_CONFIG, id="Sushi_1_4_2P"),
    pytest.param(CAMPFIRE_CONFIG, id="Campfire_1_1"),  # ← 加这行
]

# 3. 运行测试（自动跑两个关卡）
$ pytest tests/e2e/phase1_idle/ -v
# test_simple_dish.py::test_complete[Sushi_1_4_2P] PASSED
# test_simple_dish.py::test_complete[Campfire_1_1] PASSED
```

---

## 5. Layered E2E Tests

### 5.1 Overview

**目标：** 验证 AI 各层在真实游戏环境中正确工作。

**特点：**
- 每层独立测试，快速定位问题
- 不关心"完成了几道菜"，只关心该层逻辑正确
- 测试时长：每层 5-10 分钟，总计 ~30 分钟

**四层映射：**

| 测试文件 | AI 层 | 验证重点 |
|---------|-------|---------|
| `test_executor.py` | Executor | 按键触发动作、动作完成判断、树生命周期 |
| `test_planner.py` | Planner | 候选生成、打分区分度、选择推进订单 |
| `test_arbiter.py` | Arbiter | 模式转移、中断正确、Stuck/Firefighting |
| `test_blackboard.py` | Blackboard | 认领释放、commitment更新、无泄漏 |

### 5.2 Layer 1: Executor Tests

**文件：** `tests/e2e/phase1_idle/layers/test_executor.py`

**验证点：**

1. **按键触发动作**
   - 测试：`test_pickup_key_triggers_pickup`
   - 验证：AI 按下 pickup 键 → 游戏反馈 held 变化
   - 检查：时序合理（1-5 拍内生效）

2. **移动按键生效**
   - 测试：`test_movement_keys_move_character`
   - 验证：AI 持续输出移动按键 → 角色位置变化
   - 检查：5 秒移动 >3 格

3. **切菜完成判断**
   - 测试：`test_chop_action_completes_correctly`
   - 验证：AI 执行切菜 → board.item 从 "Tomato" → "ChoppedTomato"
   - 检查：Executor 正确判断 SUCCESS

4. **树生命周期**
   - 测试：`test_tree_lifecycle_on_task_change`
   - 验证：task 变化时旧树被 halt，新树被构建
   - 检查：按键正确释放

**示例测试：**

```python
def test_pickup_key_triggers_pickup(game_env):
    """验证：按下 pickup 键真的触发了拿料动作
    
    流程：
      1. 等待 AI 走到 crate 旁
      2. 记录按下 pickup 的拍号
      3. 等待 held 变化
      4. 验证响应时间（1-5拍）
    """
    agent = start_ai()
    
    # 等待 AI 走到 crate 附近且准备拿料
    world_before = wait_for_condition(
        lambda w: (
            _near_any_crate(w) and 
            w.p2_chef.get("held") is None
        ),
        agent=agent,
        timeout=30
    )
    
    # 等待 trace 显示即将按 pickup
    wait_for_condition(
        lambda w: "pickup" in _get_recent_keys(),
        agent=agent,
        timeout=5
    )
    
    pickup_tick = _get_latest_key_tick("pickup")
    
    # 等待 held 变化
    world_after = wait_for_condition(
        lambda w: w.p2_chef.get("held") is not None,
        agent=agent,
        timeout=1
    )
    
    # 验证时序
    response_ticks = world_after.tick - pickup_tick
    assert 1 <= response_ticks <= 5, \
        f"pickup 响应应在 1-5 拍，实际 {response_ticks}"
    
    print(f"✅ pickup 触发正确，响应时间 {response_ticks} 拍")
    agent.stop()
```

### 5.3 Layer 2: Planner Tests

**文件：** `tests/e2e/phase1_idle/layers/test_planner.py`

**验证点：**

1. **候选数量合理**
   - 测试：`test_candidates_count_reasonable`
   - 验证：每拍候选数在 3-20 之间
   - 检查：不会出现 0 候选（死锁）或 >50 候选（爆炸）

2. **打分有区分度**
   - 测试：`test_scoring_has_differentiation`
   - 验证：分数最高和最低有显著差距（>0.5）
   - 检查：terms 拆解非空

3. **选择推进订单**
   - 测试：`test_chosen_task_advances_order`
   - 验证：选中的 task 有 order_id（不是 Idle）
   - 检查：订单 remaining 在下降

**示例测试：**

```python
def test_candidates_count_reasonable(game_env):
    """验证：候选数量合理（不死锁、不爆炸）"""
    agent = start_ai()
    time.sleep(30)  # 收集 30 秒数据
    
    trace = load_trace_log()
    candidate_counts = [
        len(entry.get("candidates", []))
        for entry in trace
    ]
    
    avg = sum(candidate_counts) / len(candidate_counts)
    min_count = min(candidate_counts)
    max_count = max(candidate_counts)
    
    assert min_count >= 1, "出现 0 候选，决策死锁"
    assert max_count <= 50, f"候选数爆炸: {max_count}"
    assert 3 <= avg <= 20, f"平均候选数应在 3-20，实际 {avg}"
    
    print(f"✅ 候选数合理: 平均 {avg:.1f}, 范围 [{min_count}, {max_count}]")
    agent.stop()
```

### 5.4 Layer 3: Arbiter Tests

**文件：** `tests/e2e/phase1_idle/layers/test_arbiter.py`

**验证点：**

1. **Firefighting 模式转移**
   - 测试：`test_firefighting_mode_on_fire` (manual)
   - 验证：着火时模式从 Normal → Firefighting
   - 检查：当前 task 被 halt

2. **Stuck 模式转移**
   - 测试：`test_stuck_mode_on_blocked` (manual)
   - 验证：卡住时进入 Stuck 模式
   - 检查：执行 Nudge 脱困

3. **模式转移清理**
   - 测试：`test_mode_transition_halts_tree`
   - 验证：模式转移时按键被释放
   - 检查：trace 显示 exec 状态重置

**示例测试：**

```python
@pytest.mark.manual
def test_firefighting_mode_on_fire(game_env):
    """验证：着火时进入 Firefighting 模式
    
    ⚠️  需要手动触发着火或等待随机着火
    """
    agent = start_ai()
    
    print("⏳ 等待着火事件（最多 5 分钟）...")
    
    # 等待着火
    fire_detected = wait_for_condition(
        lambda w: any(c.get("onFire") for c in w.cookers),
        agent=agent,
        timeout=300
    )
    
    fire_tick = fire_detected.tick
    print(f"🔥 检测到着火: tick {fire_tick}")
    
    # 检查模式转移
    mode_switched = wait_for_condition(
        lambda w: _get_current_mode() == "Firefighting",
        agent=agent,
        timeout=5
    )
    
    response_time = mode_switched.tick - fire_tick
    print(f"✅ 模式已切换到 Firefighting，响应时间 {response_time} 拍")
    
    agent.stop()
```

### 5.5 Layer 4: Blackboard Tests

**文件：** `tests/e2e/phase1_idle/layers/test_blackboard.py`

**验证点：**

1. **认领写入和释放**
   - 测试：`test_claims_written_and_released`
   - 验证：task SUCCESS 后 claims 被释放
   - 检查：无永久泄漏

2. **commitment 更新**
   - 测试：`test_commitment_updates_correctly`
   - 验证：选中新 task 时 commitment 更新
   - 检查：task 完成时 commitment 清空

3. **长时间运行无泄漏**
   - 测试：`test_no_memory_leak_in_long_run`
   - 验证：运行 10 分钟，认领数稳定
   - 检查：无增长趋势（线性回归斜率 <0.5）

**示例测试：**

```python
def test_claims_written_and_released(game_env):
    """验证：认领被正确写入和释放"""
    agent = start_ai()
    time.sleep(60)
    
    trace = load_trace_log()
    claim_counts = [
        len(entry.get("bb_claims", {}))
        for entry in trace
    ]
    
    # 检查：有认领数回到 0 的时刻（说明有释放）
    assert 0 in claim_counts, "认领应该被释放"
    
    # 检查：最大认领数不应超过 10
    max_claims = max(claim_counts)
    assert max_claims <= 10, f"认领数异常: {max_claims}"
    
    print(f"✅ 认领管理正常，最大 {max_claims} 个")
    agent.stop()
```

---

## 6. Scenario Tests

### 6.1 Overview

**目标：** 验证 AI 能完成业务目标（订单、异常处理）。

**特点：**
- 端到端完整流程
- 关注业务成功（订单完成、分数增加）
- 验证执行质量（无卡键、无泄漏、时间合理）
- 测试时长：每个场景 3-10 分钟，总计 ~20 分钟

**五个场景：**

| 场景 | 目标 | 复杂度 | 时长 | 手动? |
|-----|------|-------|------|------|
| 简单菜 | 完成黄瓜寿司 | 低 | <180s | 否 |
| 复杂菜 | 完成鱼寿司（含烹饪） | 中 | <300s | 否 |
| 灭火 | 检测着火→灭火→恢复 | 中 | ~60s | 是 |
| 脱困 | 检测卡住→Nudge→恢复 | 低 | ~30s | 是 |
| 压力 | 10分钟稳定运行 | 高 | 600s | 否 |

### 6.2 Scenario 1: Simple Dish

**文件：** `tests/e2e/phase1_idle/scenarios/test_simple_dish.py`

**场景定义：**
- 目标订单：Sushi_Cucumber（黄瓜寿司）
- 预期流程：取盘 → 取黄瓜 → 切 → 取米饭 → 取海苔 → 装盘 → 送餐
- 成功标准：180 秒内完成，订单消失，分数增加

**验证维度：**
- ✅ 业务成功：订单完成
- ✅ 执行正确：每个里程碑达成
- ✅ 时间合理：< 180 秒
- ✅ 无异常：无卡键、无认领泄漏

**关键代码：**

```python
def test_complete_cucumber_sushi(game_env):
    """完整场景：从空手到送出黄瓜寿司"""
    
    agent = start_ai()
    
    # 找到目标订单
    target_order = _find_simple_dish_order(agent.client.receive())
    if not target_order:
        pytest.skip("当前无简单菜订单")
    
    order_id = target_order["id"]
    print(f"🎯 目标订单: {target_order['dish']} (ID: {order_id})")
    
    # 设置里程碑
    milestones = {
        "plate_placed": False,
        "ingredient_chopped": False,
        "plate_assembled": False,
    }
    
    start_time = time.time()
    
    # 等待订单完成
    result = wait_for_condition(
        condition=lambda w: order_id not in [o["id"] for o in w.orders],
        agent=agent,
        timeout=game_env.timeout_simple_dish,
        on_tick=lambda w: _check_milestones(w, milestones)
    )
    
    elapsed = time.time() - start_time
    
    # 验证
    assert_order_completed(result, order_id)
    assert_score_increased(initial_score, result.score.get("base"))
    assert all(milestones.values()), "未完成的里程碑"
    
    agent.stop()
    verify_no_stuck_keys(agent)
    
    print(f"✅ 场景测试通过: 简单菜，耗时 {elapsed:.1f}s")
```

### 6.3 Scenario 2: Complex Dish

**文件：** `tests/e2e/phase1_idle/scenarios/test_complex_dish.py`

**场景定义：**
- 目标订单：Sushi_Fish（鱼寿司，需烹饪）
- 关键差异：需要煮米饭
- 预期流程：取生米 → 下锅 → 等待煮熟 → 取熟米饭 → 取鱼切 → 装盘 → 送餐
- 成功标准：300 秒内完成

**额外验证：**
- ✅ Planner 识别需要烹饪
- ✅ Executor 正确等待烹饪完成（不提前拿）
- ✅ 烹饪期间能并发处理其他任务（如切菜）

**关键代码：**

```python
def test_complete_fish_sushi_with_cooking(game_env):
    """完整场景：需要烹饪的寿司"""
    
    # 扩展里程碑
    milestones = {
        "rice_to_cooker": False,
        "rice_cooking": False,
        "rice_cooked": False,
        "rice_taken": False,
        "fish_chopped": False,
    }
    
    # ... 等待订单完成 ...
    
    # 验证烹饪里程碑
    assert milestones["rice_cooked"], "应该完成烹饪"
    
    # 验证并发能力
    cooking_period = _find_cooking_period(trace)
    if cooking_period:
        tasks_during_cooking = _get_tasks_in_period(trace, cooking_period)
        assert len(tasks_during_cooking) > 1, \
            "烹饪期间应执行其他任务（如切鱼）"
    
    print(f"✅ 场景测试通过: 复杂菜（含烹饪）")
```

### 6.4 Scenario 3: Firefighting

**文件：** `tests/e2e/phase1_idle/scenarios/test_firefighting.py`

**场景定义：**
- 触发：灶台着火（手动触发或等待随机着火）
- 预期流程：检测着火 → 切换模式 → 中断当前 task → 拿灭火器 → 灭火 → 恢复工作
- 成功标准：火被扑灭，AI 恢复正常工作

**验证维度：**
- ✅ 模式转移：Normal → Firefighting → Normal
- ✅ 中断正确：当前 task 被 halt
- ✅ 灭火成功：onFire 从 true → false
- ✅ 恢复工作：灭火后继续做菜

**关键代码：**

```python
@pytest.mark.manual
def test_firefighting_interrupt_and_resume(game_env):
    """完整场景：检测着火 → 灭火 → 恢复工作"""
    
    agent = start_ai()
    
    print("⏳ 等待着火事件（最多 5 分钟）...")
    
    # 等待着火
    fire_detected = wait_for_condition(
        lambda w: any(c.get("onFire") for c in w.cookers),
        agent=agent,
        timeout=300
    )
    
    fire_tick = fire_detected.tick
    
    # 验证模式转移
    mode_switched = wait_for_condition(
        lambda w: _get_current_mode() == "Firefighting",
        agent=agent,
        timeout=5
    )
    
    # 等待灭火完成
    fire_out = wait_for_condition(
        lambda w: not any(c.get("onFire") for c in w.cookers),
        agent=agent,
        timeout=60
    )
    
    # 验证模式恢复
    mode_restored = wait_for_condition(
        lambda w: _get_current_mode() == "Normal",
        agent=agent,
        timeout=10
    )
    
    # 验证恢复工作
    working_again = wait_for_condition(
        lambda w: _get_current_task() not in [None, "Idle"],
        agent=agent,
        timeout=10
    )
    
    agent.stop()
    
    print(f"✅ 场景测试通过: 灭火中断与恢复")
```

### 6.5 Scenario 4: Stuck Recovery

**文件：** `tests/e2e/phase1_idle/scenarios/test_stuck_recovery.py`

**场景定义：**
- 触发：用 P1 堵住 AI（手动触发）
- 预期流程：检测卡住 → 进入 Stuck 模式 → 执行 Nudge → 移动到新位置 → 恢复任务
- 成功标准：脱困后能继续工作

**关键代码：**

```python
@pytest.mark.manual
def test_stuck_detection_and_recovery(game_env):
    """完整场景：被堵住 → 检测 → 脱困 → 恢复"""
    
    agent = start_ai()
    
    print("⚠️  需要手动触发:")
    print("   1. 等待 AI 开始移动")
    print("   2. 用 P1 堵住 AI")
    print("   3. 保持 3-5 秒")
    
    input("按任意键继续...")
    
    # 等待检测到 Stuck
    stuck_detected = wait_for_condition(
        lambda w: _get_current_mode() == "Stuck",
        agent=agent,
        timeout=30
    )
    
    stuck_pos = (stuck_detected.p2_chef.get("x"), stuck_detected.p2_chef.get("z"))
    
    # 等待脱困
    recovered = wait_for_condition(
        lambda w: _distance(_get_pos(w), stuck_pos) > 1.0,
        agent=agent,
        timeout=30
    )
    
    print(f"✅ 场景测试通过: 卡住脱困")
```

### 6.6 Scenario 5: Stress Test

**文件：** `tests/e2e/phase1_idle/scenarios/test_stress.py`

**场景定义：**
- 持续运行：10 分钟
- 验证维度：无崩溃、无死锁、无泄漏、帧率稳定、完成 ≥3 道菜

**监控指标：**
- 每 30 秒检查一次：AI 活跃、认领数、内存、订单完成
- 最终分析：认领趋势、内存趋势

**关键代码：**

```python
def test_10_minute_continuous_run(game_env):
    """压力测试：连续运行 10 分钟"""
    
    agent = start_ai()
    
    metrics = {
        "completed_orders": 0,
        "claim_snapshots": [],
        "memory_snapshots": [],
    }
    
    # 每 30 秒检查一次，共 20 次
    for i in range(20):
        time.sleep(30)
        
        # 检查 AI 是否还活着
        current_world = World(agent.client.receive(timeout=5))
        
        # 检查认领泄漏
        trace = load_trace_log()
        claims_count = len(trace[-1].get("bb_claims", {}))
        metrics["claim_snapshots"].append(claims_count)
        
        if claims_count > 20:
            pytest.fail(f"❌ 认领泄漏: {claims_count}")
        
        # 检查内存
        memory_mb = psutil.Process().memory_info().rss / 1024 / 1024
        metrics["memory_snapshots"].append(memory_mb)
    
    agent.stop()
    
    # 验证
    assert metrics["completed_orders"] >= 3, "应完成至少 3 道菜"
    assert max(metrics["claim_snapshots"]) < 15, "认领数异常"
    
    print(f"✅ 场景测试通过: 10 分钟压力测试")
```

---

## 7. Helper Functions & Assertions

### 7.1 Core Helpers

**文件：** `tests/e2e/helpers.py`

```python
"""E2E 测试通用工具函数"""

def start_ai():
    """启动 AI（后台线程）
    
    返回: Agent 实例
    """
    from ai.agent import Agent
    agent = Agent()
    agent.start_async()
    time.sleep(1)
    return agent


def check_game_ready(expected_level):
    """检查游戏环境是否就绪
    
    验证：
      - 游戏已启动
      - 关卡正确
      - 插件已连接
    
    失败时 pytest.skip()
    """
    client = StateClient()
    try:
        frame = client.receive(timeout=5)
        if not frame:
            pytest.skip("❌ 游戏未运行或插件未连接")
        
        level = frame.get("level", "")
        if expected_level not in level:
            pytest.skip(f"❌ 关卡不对：期望 {expected_level}，实际 {level}")
    
    except Exception as e:
        pytest.skip(f"❌ 游戏环境检查失败: {e}")


def wait_for_condition(condition, agent, timeout, message, on_tick=None):
    """等待条件满足，返回满足时的 World 对象
    
    参数:
        condition: (World) -> bool
        agent: AI 实例
        timeout: 超时秒数
        message: 超时时的错误信息
        on_tick: 可选回调 (World) -> None，每拍调用
    
    返回:
        满足条件时的 World 对象
    
    抛出:
        TimeoutError: 超时
    """
    deadline = time.time() + timeout
    client = agent.client
    
    print(f"⏳ {message}... (超时 {timeout}s)")
    
    while time.time() < deadline:
        try:
            frame = client.receive(timeout=0.05)
            if not frame:
                continue
            
            world = World(frame)
            
            # 可选回调
            if on_tick:
                on_tick(world)
            
            # 检查条件
            if condition(world):
                return world
        
        except Exception as e:
            continue
    
    raise TimeoutError(f"超时 {timeout}s: {message}")


def load_trace_log():
    """加载 trace 日志
    
    返回: List[Dict] - JSONL 解析后的列表
    """
    import json
    from ai.trace import TRACE_PATH
    
    if not os.path.exists(TRACE_PATH):
        return []
    
    with open(TRACE_PATH) as f:
        return [json.loads(line) for line in f if line.strip()]


def verify_no_stuck_keys(agent):
    """验证无卡键
    
    检查: agent.controller 的所有按键都已释放
    """
    # 简化：检查最近 trace 的 keys 字段
    trace = load_trace_log()
    if not trace:
        return
    
    recent_keys = trace[-5:]  # 最近 5 拍
    for entry in recent_keys:
        keys = entry.get("keys", [])
        if keys:
            pytest.fail(f"检测到卡键: {keys}")
```

### 7.2 Common Assertions

**文件：** `tests/e2e/assertions.py`

```python
"""E2E 测试通用断言"""

def assert_holding_something(world, player="P2"):
    """断言玩家手上有东西
    
    返回: 手持物品名
    """
    chef = world.p2_chef if player == "P2" else world.p1_chef
    held = chef.get("held")
    assert held is not None, f"{player} 应该手持物品，实际为空"
    return held


def assert_ingredient_on_board(world):
    """断言任意切菜板上有食材
    
    返回: 该切菜板对象
    """
    boards = [st for st in world.stations if st.get("t") == "board"]
    occupied = [b for b in boards if b.get("item")]
    assert len(occupied) > 0, "应有切菜板上放了食材"
    return occupied[0]


def assert_order_completed(world, order_id):
    """断言订单完成（已从订单列表消失）"""
    current_ids = [o.get("id") for o in world.orders]
    assert order_id not in current_ids, f"订单 {order_id} 应该已完成"


def assert_score_increased(initial_score, final_score, expected_min=50):
    """断言分数增加
    
    参数:
        expected_min: 最低增加分数（默认 50）
    """
    gain = final_score - initial_score
    assert gain >= expected_min, \
        f"分数应增加至少 {expected_min}，实际增加 {gain}"


def assert_no_claim_leak(trace):
    """断言无认领泄漏
    
    检查: trace 中认领数应有回到 0 的时刻
    """
    claim_counts = [
        len(entry.get("bb_claims", {}))
        for entry in trace
    ]
    
    assert 0 in claim_counts, "认领应该被释放（未检测到认领数回到 0）"
    
    max_claims = max(claim_counts)
    assert max_claims <= 15, f"认领数异常: {max_claims}"


def assert_decision_efficiency(trace, min_efficiency=0.6):
    """断言决策效率
    
    效率定义: 非 Idle 任务的比例
    
    返回: 效率值 (0-1)
    """
    tasks = [e.get("commit", {}).get("task", "") for e in trace]
    non_idle = [t for t in tasks if t and "Idle" not in t]
    
    efficiency = len(non_idle) / len(tasks) if tasks else 0
    
    assert efficiency >= min_efficiency, \
        f"决策效率应 ≥{min_efficiency:.0%}，实际 {efficiency:.0%}"
    
    return efficiency
```

---

## 8. Two-Phase Strategy

### 8.1 Phase 1: P1 Idle (Current Spec)

**定义：** P1 物理静止（控制器不输入），AI 完全自主运行。

**适用场景：**
- 验证 AI 基础能力
- 快速迭代开发
- 回归测试

**测试覆盖：**
- 分层测试（4 层）
- 场景测试（5 个）

**运行方式：**

```bash
# 1. 启动游戏，进入 Sushi_1_4_2P
# 2. P1 控制器不动（物理 idle）
# 3. 运行测试

# 所有分层测试
pytest tests/e2e/phase1_idle/layers/ -v

# 所有场景测试（跳过需要手动触发的）
pytest tests/e2e/phase1_idle/scenarios/ -v -m "not manual"

# 完整 Phase 1 测试
pytest tests/e2e/phase1_idle/ -v
```

### 8.2 Phase 2: P1 Collaboration (Future Work)

**定义：** P1 由真人玩家控制，测试 AI 与真人协作。

**适用场景：**
- 验证协作逻辑（Plan 4: 接力、分单）
- 验证冲突处理（P1 拿走 AI 想要的料）
- 最终验收

**测试覆盖：** (待 Plan 4 设计)
- 协作送餐
- 资源竞争处理
- 站位协调

**文件占位：** `tests/e2e/phase2_collab/test_collaboration.py`

```python
"""阶段 2: P1 协同测试（Plan 4+）

⚠️  本文件为占位符，待 Plan 4 设计后实现
"""
import pytest

@pytest.mark.skip(reason="Phase 2 待 Plan 4 设计")
def test_collaboration_placeholder():
    pass
```

---

## 9. Running Tests

### 9.1 Prerequisites

**环境要求：**
- Overcooked 2 已安装
- C# 插件已加载（socket 27411 监听）
- Python 3.9.6 + pytest
- 依赖：`pip install pytest psutil scipy`

**游戏准备：**
1. 启动 Overcooked 2
2. 选择关卡：Sushi_1_4_2P
3. P1 控制器保持静止（物理 idle）
4. 等待倒计时结束，游戏开始

### 9.2 Running Layered Tests

```bash
# 单独运行某一层
pytest tests/e2e/phase1_idle/layers/test_executor.py -v -s
pytest tests/e2e/phase1_idle/layers/test_planner.py -v -s
pytest tests/e2e/phase1_idle/layers/test_arbiter.py -v -s
pytest tests/e2e/phase1_idle/layers/test_blackboard.py -v -s

# 运行所有分层测试（~30 分钟）
pytest tests/e2e/phase1_idle/layers/ -v

# 跳过需要手动触发的测试
pytest tests/e2e/phase1_idle/layers/ -v -m "not manual"
```

### 9.3 Running Scenario Tests

```bash
# 单个场景
pytest tests/e2e/phase1_idle/scenarios/test_simple_dish.py -v -s
pytest tests/e2e/phase1_idle/scenarios/test_complex_dish.py -v -s
pytest tests/e2e/phase1_idle/scenarios/test_stress.py -v -s

# 所有自动场景（跳过手动触发的）
pytest tests/e2e/phase1_idle/scenarios/ -v -m "not manual"

# 包含手动场景（需要人工触发）
pytest tests/e2e/phase1_idle/scenarios/ -v
```

### 9.4 Running Full E2E Suite

```bash
# 完整 Phase 1 测试（分层 + 场景，~50 分钟）
pytest tests/e2e/phase1_idle/ -v

# 生成 HTML 报告
pytest tests/e2e/phase1_idle/ -v \
    --html=e2e_report.html \
    --self-contained-html

# 只跑关键验收测试
pytest tests/e2e/phase1_idle/ -v -m "not manual and not stress"
```

### 9.5 Test Markers

| Marker | 用途 | 示例 |
|--------|------|------|
| `@pytest.mark.e2e` | 标记 E2E 测试 | 过滤出所有 E2E 测试 |
| `@pytest.mark.scenario` | 标记场景测试 | 只跑场景测试 |
| `@pytest.mark.manual` | 需要手动触发 | 跳过需要人工介入的测试 |
| `@pytest.mark.stress` | 压力测试 | 跳过耗时长的压力测试 |
| `@pytest.mark.timeout(N)` | 设置超时 | 防止测试卡死 |

**用法：**

```bash
# 只跑自动测试（跳过 manual）
pytest -m "not manual"

# 只跑场景测试
pytest -m scenario

# 跳过压力测试（加快验收）
pytest -m "not stress"
```

---

## 10. Troubleshooting

### 10.1 Common Issues

**问题 1: 游戏未准备好**
```
SKIPPED: ❌ 游戏未运行或插件未连接
```
**解决：**
- 检查游戏是否已启动
- 检查插件是否加载（socket 27411 监听）
- 检查关卡是否正确

**问题 2: 测试超时**
```
TimeoutError: 超时 180s: 等待订单完成
```
**解决：**
- 检查 AI 是否卡住（看日志）
- 检查 P1 是否挡路（P1 应保持静止）
- 增加超时阈值（如订单复杂度提高）

**问题 3: 认领泄漏**
```
AssertionError: 认领数异常: 25
```
**解决：**
- 查看 trace 日志，定位哪些认领未释放
- 检查 Blackboard 的认领释放逻辑
- 检查 Executor 的 SUCCESS/FAILURE 处理

**问题 4: 按键卡死**
```
pytest.fail: 检测到卡键: ['move']
```
**解决：**
- 检查 Executor 的 halt 逻辑
- 检查模式转移时的清理
- 检查原语的 halt 实现

### 10.2 Debugging Tips

**查看实时 trace：**
```bash
# 另一个终端
tail -f /tmp/agent.log | jq '.mode, .commit.task, .keys'
```

**查看决策统计：**
```bash
cat /tmp/agent.log | jq -r '.commit.task' | sort | uniq -c
```

**检查模式转移：**
```bash
cat /tmp/agent.log | jq -r '.mode' | uniq -c
```

**检查认领泄漏：**
```bash
cat /tmp/agent.log | jq '.bb_claims | length'
```

---

## 11. Future Extensions

### 11.1 Adding New Levels

**步骤：**

1. **创建关卡配置**
   ```python
   # tests/e2e/config/campfire_1_1.py
   CAMPFIRE_CONFIG = LevelConfig(
       name="Campfire_1_1",
       display_name="篝火关 1-1",
       available_ingredients={"Meat", "Lettuce", "Tomato"},
       ...
   )
   ```

2. **注册到 conftest**
   ```python
   # tests/e2e/conftest.py
   SUPPORTED_LEVELS = [
       pytest.param(SUSHI_1_4_CONFIG, id="Sushi_1_4_2P"),
       pytest.param(CAMPFIRE_CONFIG, id="Campfire_1_1"),
   ]
   ```

3. **运行测试**
   ```bash
   pytest tests/e2e/phase1_idle/ -v
   # 自动跑两个关卡的所有测试
   ```

### 11.2 Adding New Scenarios

**步骤：**

1. **创建场景文件**
   ```python
   # tests/e2e/phase1_idle/scenarios/test_new_scenario.py
   @pytest.mark.e2e
   @pytest.mark.scenario
   def test_new_scenario(game_env):
       # ... 测试逻辑
       pass
   ```

2. **复用辅助函数**
   ```python
   from tests.e2e.helpers import wait_for_condition, start_ai
   from tests.e2e.assertions import assert_order_completed
   ```

3. **运行测试**
   ```bash
   pytest tests/e2e/phase1_idle/scenarios/test_new_scenario.py -v
   ```

### 11.3 CI/CD Integration (Future)

**目标：** 自动化测试集成到 CI 流水线。

**挑战：**
- 游戏需要手动启动（无 headless 模式）
- P1 需要保持 idle（无法通过 API 控制）

**可能方案：**
1. 修改 C# 插件，支持 headless 模式 + P1 bot
2. 使用专用测试机器，游戏常驻运行
3. 只在 CI 跑单元测试 + 集成测试，E2E 手动验收

---

## 12. Acceptance Checklist

### 12.1 Framework Delivery

- [ ] 文件结构完整（config / layers / scenarios / helpers / assertions）
- [ ] 关卡配置抽象完成（base.py + sushi_1_4.py）
- [ ] pytest fixtures 配置完成（conftest.py）
- [ ] 辅助函数完整（helpers.py）
- [ ] 通用断言完整（assertions.py）
- [ ] README 文档完成（使用说明）
- [ ] 本 spec 文档完成

### 12.2 Plan 3 Acceptance (First Application)

**分层测试（必选，全绿）：**
- [ ] test_executor.py - Executor 层验证通过
- [ ] test_planner.py - Planner 层验证通过
- [ ] test_arbiter.py - Arbiter 层验证通过（跳过 manual）
- [ ] test_blackboard.py - Blackboard 层验证通过

**场景测试（必选，全绿）：**
- [ ] test_simple_dish.py - 简单菜场景通过（<180s）
- [ ] test_complex_dish.py - 复杂菜场景通过（<300s）
- [ ] test_stress.py - 压力测试通过（10分钟，≥3道菜）

**可选场景（人工验收）：**
- [ ] test_firefighting.py - 灭火场景观察正常
- [ ] test_stuck_recovery.py - 脱困场景观察正常

**执行质量（分层+场景全过程）：**
- [ ] 无卡键（verify_no_stuck_keys 通过）
- [ ] 无认领泄漏（assert_no_claim_leak 通过）
- [ ] 无崩溃（所有测试正常结束）
- [ ] trace 完整（/tmp/agent.log 格式正确）

---

## 13. Glossary

| 术语 | 定义 |
|-----|------|
| E2E | End-to-End，端到端测试，验证完整闭环 |
| 分层测试 | 按 AI 架构分层验证（Executor/Planner/Arbiter/Blackboard） |
| 场景测试 | 验证完整业务流程（订单完成、异常处理） |
| Phase 1 | P1 静止测试阶段 |
| Phase 2 | P1 协同测试阶段（未来） |
| 里程碑 | 关键检查点（如"盘子就位"、"食材切好"） |
| 认领泄漏 | Blackboard claims 不释放导致资源楔死 |
| trace | 决策日志（/tmp/agent.log，JSONL 格式） |
| manual | 需要手动触发的测试（@pytest.mark.manual） |

---

## Appendix A: References

- **Plan 3 Spec:** `docs/superpowers/plans/2026-10-05-plan3-end-to-end.md`
- **AI Behavior Architecture:** `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md`
- **Existing Test Infrastructure:** `tests/test_*.py`, `tests/conftest.py`
- **Pytest Documentation:** https://docs.pytest.org/

---

## Appendix B: Change Log

| Date | Version | Changes |
|------|---------|---------|
| 2026-10-06 | 1.0 | Initial design approved |

---

**END OF SPECIFICATION**


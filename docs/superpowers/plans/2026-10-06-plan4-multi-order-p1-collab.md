# Plan 4：多订单并行 + P1 协同实施计划

> **状态**: 🔵 待实施 (2026-10-06)  
> **前置**: Plan 3 已完成 (端到端闭环)  
> **测试**: 0/X 通过

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在 Plan 3 的单订单闭环基础上，实现**多订单并行穿插**（煮饭空档切下一单的菜）、**P1 协同推断**（避让 + 接力）、**收尾 task**（RescueCooked / ResetPlate / Prefetch），并补全打分权重（异步资源优先、接力加成、完成度、料复用、combo、截止前瞻），使 AI 在实机表现超过 Plan 3 基线，30 分钟浸泡稳定。

**Architecture:** 扩展 Planner 支持多订单候选生成（不再限定 `current_order_id`）、多盘多灶认领（claims 同时持有多个订单的资源）、承诺穿插（一个 task RUNNING 时可选另一订单的 task）。新增 `p1model.py` 做三层推断（事实 / 短程意图 / 显式告知），协作过滤从空壳变为实装（接力模式做分数惩罚、分单模式按归属筛选）。打分权重从 3 项扩展到 9 项。

**Tech Stack:** Python 3.9.6、pytest。依赖 Plan 1-3 的全部模块。

**Spec:** `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md`（§4 Blackboard 多订单认领、§6.0 候选生成反向展开、§6.3 协作过滤、§6.4 打分完整六项、§6.7 P1Model、§11.2 阶段 5-7、§11.5 完成标准）

## Global Constraints

- **Python 3.9.6**：类型标注用注释形式。
- **不改 `oc/`**：旧实现只读参考。
- **不改 C# 插件**：帧格式按现状。
- **多订单模式**：Plan 4 移除 `current_order_id` 限制，候选生成枚举所有订单的未满足需求，Planner 可同时持有多个订单的认领。
- **P1 协同默认开启**：`p1model.py` 默认运行，`--no-p1model` 可关闭。
- **接力模式为默认**：`--collab=relay`（默认），`--collab=split` 切换分单。
- **打分权重渐进上线**（§11.4）：先跑通多订单 + 三项权重，再逐项加入其余六项，每加一项录基线。
- **注释用中文**。
- **测试命令**：`cd /Users/dongyi/Documents/coding/oc2 && python3 -m pytest <path> -v`
- **分支**：本计划在 `plan4-multi-order-collab` 分支，不直接提交 main。

## Review Focus

Plan 4 是从"能跑"到"跑得好"的关键一跳，七类高风险输入：

1. **多订单候选生成的反向展开**（spec §6.0）。从需求反向展开而非对资源做笛卡尔积，候选数上限约 50。`for order in orders: for req in diff(order.recipe, plate_contents): 候选 += 能推进该需求的 task`。Task 1 断言。

2. **认领粒度 = (resource, action)**（spec §4.0）。P1 往锅里放生饭，该锅的 `(cooker1, "fetch_to")` 被占，但 `(cooker1, "take_cooked")` 开放——这就是接力。Task 1 断言。

3. **P1Model 三层推断 + confidence 衰减**（spec §6.7）。事实（holding）、短程意图（p1_active_task，置信度 0~1）、显式告知（对话层，本计划为空）。无新证据则 confidence 递减，低于阈值清空推断。Task 2 断言。

4. **协作过滤的两种模式**（spec §6.3）。接力：对 p1_active_task 做分数惩罚（不硬 veto），置信度越高惩罚越重；分单：只留 `order_assignment == "P2"` 的 task，归属为空立刻退回接力。Task 3 断言。

5. **异步资源优先权重**（spec §6.4）。FetchToCooker（下锅后自己跑 20 秒）比 FetchToPlate（需人等）分数更高，这是穿插涌现的核心。Task 4 断言"下锅后趁煮去切下一单"。

6. **承诺穿插的条件**（新增设计点）。当前 task 是 RUNNING 且剩余步数 > 阈值（如 est_steps - steps_done > 5）时，允许 Planner 选另一订单的 task（穿插）。穿插后原 task 进入"暂停"状态，完成穿插 task 后恢复。Task 5 断言。

7. **截止前瞻的时长估算**（spec §6.4 后三项）。每个 task 有粗粒度 `est_ticks`（常量 + ETA），打分时惩罚"会错过已知截止"的选择（锅烧糊时刻、订单到期）。Task 4 断言。

---

## 文件结构

| 文件 | 职责 | 产出任务 | 预估行数 |
|---|---|---|---|
| `ai/planner.py` | 扩展：多订单候选生成、承诺穿插、完整打分权重 | Task 1, 4 | +300 |
| `ai/p1model.py` | P1 推断：三层（事实 / 短程意图 / 显式告知）+ confidence 衰减 | Task 2 | ~250 |
| `ai/blackboard.py` | 扩展：order_assignment、p1_model 字段、waiters 队列 | Task 1, 3 | +100 |
| `ai/arbiter.py` | 微调：读取 p1_model（P1 持灭火器判断） | Task 2 | +30 |
| `ai/navigation.py` | P1 软代价图：P1 周边格子加通行代价 | Task 2 | +80 |
| `ai/tasks/*.py` | 新增：RescueCooked / ResetPlate / Prefetch | Task 6 | ~150 |
| `ai/agent.py` | 接入 p1model.update()（步骤 ④）、移除 current_order_id | Task 1, 2 | +50 |
| `tests/test_planner_multi.py` | 多订单候选、穿插、打分 | Task 1, 4, 5 | |
| `tests/test_p1model.py` | P1 推断、confidence 衰减、站位软代价 | Task 2 | |
| `tests/test_collab.py` | 协作过滤：接力 / 分单模式 | Task 3 | |
| `tests/test_rescue_reset.py` | 收尾 task：RescueCooked / ResetPlate / Prefetch | Task 6 | |
| `tests/test_integration.py` | 集成测试：30 分钟浸泡、基线对比 | Task 7 | |

依赖顺序：Task 1（多订单 Planner）→ Task 2（P1Model）→ Task 3（协作过滤）→ Task 4（打分权重完整集）→ Task 5（承诺穿插）→ Task 6（收尾 task）→ Task 7（浸泡测试）。

---

### Task 1: 多订单候选生成 + 多资源认领

**Files:**
- Modify: `ai/planner.py`（移除 `current_order_id` 参数，候选生成枚举所有订单）
- Modify: `ai/blackboard.py`（添加 `order_assignment` 字段、waiters 队列）
- Modify: `ai/agent.py`（移除 `self.current_order_id`，步骤 ⑧ 不再选单订单）
- Test: `tests/test_planner_multi.py`

**Interfaces:**
- `planner.plan(world, bb)` —— 移除 `current_order_id` 参数
- Blackboard 新增字段：
  - `order_assignment: Dict[str, str]` —— `{order_id → "P1" | "P2" | None}`
  - `claims` 的 waiters: `Dict[str, List[Tuple[str, int]]]` —— `{resource_id → [(order_id, since_tick)]}`

**Spec References:**
- §6.0 task 身份与候选生成：反向展开，不做笛卡尔积
- §6.1 候选 task 全集（16 个 task）
- §4.0 认领粒度 = (resource, action)
- §4.3 waiters 队列 + 超时让出

实现多订单并行的核心机制：候选生成从"只看一个订单"改为"枚举所有订单的未满足需求"，认领表可同时持有多个订单的资源。

**Key Design Points:**

1. **反向展开枚举**（spec §6.0）：
   ```python
   for order in world.orders:
       for req in _diff_requirements(order, world, bb):
           candidates.extend(_generate_tasks_for_requirement(req, order, world, bb))
   ```
   候选数上限：`订单数(≤4) × 未满足需求(≤4) × 可用资源(≤3)` ≈ 50。

2. **认领粒度的接力语义**（spec §4.0）：
   - `(cooker1, "fetch_to")` 被 P1 占用 → AI 的 FetchToCooker(cooker1) 不可行
   - `(cooker1, "take_cooked")` 未被占 → AI 的 TakeCookedToPlate(cooker1) 可行
   - **这就是接力**：P1 下锅煮饭，AI 去取熟饭

3. **task 身份绑定参数**（spec §6.0）：
   - `FetchToPlate(Cucumber → plate_A, order#1)` 与 `FetchToPlate(Cucumber → plate_B, order#2)` 是不同 task
   - 完成判定必须绑定具体盘：P1 把黄瓜放进盘 B 不会让盘 A 的 task 误报成功

4. **waiters 队列**（spec §4.3）：
   - 想用但被占时登记 `(order_id, since_tick)`
   - 资源释放时按等待时长优先给（老化优先级，防饿死）
   - 等待超过 `WAIT_GIVEUP_TICKS`（默认 100 拍 = 5 秒）则放弃该 task 并释放自己的认领

- [ ] **Step 1: 写失败测试**

创建 `tests/test_planner_multi.py`：

```python
"""Planner 多订单模式单测。

重点验证：
  1. 候选生成枚举所有订单（不限 current_order_id）。
  2. 认领粒度 = (resource, action)，接力语义正确。
  3. task 身份绑定参数，不误报完成。
  4. waiters 队列：等待 → 老化优先 → 超时让出。
"""
import pytest
from ai import planner
from ai.blackboard import Blackboard
from ai.tasks import TaskType
# ...

def test_generate_candidates_for_all_orders():
    """多订单模式：候选包含所有订单的 task。"""
    # 构造：两个订单 A（黄瓜寿司）、B（鱼寿司）
    # 断言：候选同时含 A 和 B 的 task
    pass

def test_claim_granularity_enables_relay():
    """认领粒度 = (resource, action)：P1 占 fetch_to，AI 仍可 take_cooked。"""
    # 构造：cooker1 的 (cooker1, "fetch_to") 被 P1 认领
    # 断言：TakeCookedToPlate(cooker1) 可行
    pass

def test_task_identity_binds_resources():
    """task 身份绑定参数：P1 完成盘 B 不会让盘 A 误报。"""
    # 构造：两个 FetchToPlate 任务，绑定不同盘
    # P1 完成盘 B → 只有盘 B 的 task 完成，盘 A 的仍 RUNNING
    pass

def test_waiters_queue_aging_priority():
    """waiters 队列：等待时长越长优先级越高。"""
    # 构造：cooker1 被占，order#1 等了 50 拍，order#2 等了 20 拍
    # cooker1 释放 → order#1 获得
    pass

def test_wait_timeout_releases_own_claims():
    """等待超时 → 放弃 task 并释放自己的认领（打断 hold-and-wait）。"""
    # 构造：AI 为 order#1 持有 plate + assembly，等 cooker 超过 WAIT_GIVEUP_TICKS
    # 断言：plate + assembly 认领被释放
    pass
```

运行 `pytest tests/test_planner_multi.py -v`，全红。

- [ ] **Step 2: 扩展 Blackboard**

修改 `ai/blackboard.py`，添加字段：

```python
@dataclass
class Blackboard:
    # ... 既有字段 ...
    
    # 多订单并行
    order_assignment: Dict[str, Optional[str]] = field(default_factory=dict)
    # {order_id → "P1" | "P2" | None}
    # 分单模式用：新订单到达时按负载分配，持久化不每拍重算
    
    # waiters 队列（spec §4.3）
    waiters: Dict[str, List[Tuple[str, int]]] = field(default_factory=dict)
    # {resource_id → [(order_id, since_tick), ...]}
    # 想用但被占时登记，资源释放时按等待时长优先给
```

- [ ] **Step 3: 实现多订单候选生成**

修改 `ai/planner.py` 的 `plan` 签名和 `_generate_candidates`：

```python
def plan(world, bb):
    # type: (World, Blackboard) -> Tuple[Task, Dict, Dict]
    """规划一拍（多订单模式）。
    
    移除 current_order_id 参数，候选生成枚举所有订单。
    
    返回: (chosen_task, claims', decision_record)
    """
    # ① 候选生成（枚举所有订单）
    candidates = _generate_candidates(world, bb)
    # ... 后续步骤不变 ...

def _generate_candidates(world, bb):
    # type: (World, Blackboard) -> List[Task]
    """生成候选 task（多订单模式）。
    
    反向展开：从订单需求出发，不对资源做笛卡尔积。
    """
    candidates = []
    
    # 枚举所有订单
    for order in world.orders:
        order_id = order.get("id")
        
        # 计算该订单的未满足需求
        requirements = _order_requirements(order, world, bb)
        
        # 为每个需求生成能推进它的 task
        for req in requirements:
            candidates.extend(_generate_tasks_for_requirement(req, order, world, bb))
    
    # 加入清理 task（无 order_id）
    candidates.extend(_generate_cleanup_tasks(world, bb))
    
    return candidates
```

- [ ] **Step 4: 实现 waiters 队列**

在 `ai/planner.py` 的可行性过滤中：

```python
def _claims_available(task, bb, ctx):
    # type: (Task, Blackboard, dict) -> bool
    """检查 task 所需的认领是否可得。
    
    不可得时登记到 waiters 队列。
    """
    required_claims = task.claims()
    for res_id, action in required_claims:
        claim_key = (res_id, action)
        if claim_key in bb.claims:
            claim = bb.claims[claim_key]
            # 被他人持有 且 未过 TTL
            if claim["owner"] != "P2" and not _claim_expired(claim, ctx["tick"]):
                # 登记等待
                if res_id not in bb.waiters:
                    bb.waiters[res_id] = []
                
                # 检查是否已在等待
                order_id = task.order_id or "cleanup"
                existing = [w for w in bb.waiters[res_id] if w[0] == order_id]
                if not existing:
                    bb.waiters[res_id].append((order_id, ctx["tick"]))
                
                # 检查是否等待超时
                wait_time = ctx["tick"] - existing[0][1] if existing else 0
                if wait_time > WAIT_GIVEUP_TICKS:
                    # 超时让出：放弃该 task 并释放自己的认领
                    _release_own_claims(task, bb)
                    return False
                
                return False
    return True

def _release_own_claims(task, bb):
    # type: (Task, Blackboard) -> None
    """释放 task 持有的认领（超时让出时调用）。"""
    for res_id, action in task.claims():
        claim_key = (res_id, action)
        if claim_key in bb.claims and bb.claims[claim_key]["owner"] == "P2":
            del bb.claims[claim_key]
```

- [ ] **Step 5: 修改 agent.py 主循环**

移除 `self.current_order_id`，步骤 ⑧ 不再选单订单：

```python
class Agent:
    def __init__(self):
        # ... 既有字段 ...
        # 移除：self.current_order_id = None
    
    def run(self):
        while True:
            # ...
            # ⑧ 移除订单选择逻辑（_update_current_order）
            
            # ⑨ 规划（移除 current_order_id 参数）
            if new_mode == "Normal":
                chosen_task, claims_new, record = self.planner.plan(world, self.bb)
            # ...
```

- [ ] **Step 6: 跑通多订单测试**

运行 `pytest tests/test_planner_multi.py -v`，全绿。确认：
- 候选包含所有订单
- 接力语义正确（P1 占 fetch_to，AI 可 take_cooked）
- task 身份绑定参数，不误报
- waiters 队列生效

---

### Task 2: P1Model（人类意图推断）

**Files:**
- Create: `ai/p1model.py`
- Modify: `ai/navigation.py`（添加 `p1_soft_cost_map`）
- Modify: `ai/arbiter.py`（Firefighting 判断读 p1_model.holding）
- Modify: `ai/agent.py`（步骤 ④ 调用 `p1model.update`）
- Test: `tests/test_p1model.py`

**Interfaces:**
- `p1model.update(world, bb)` —— 每拍更新 P1 推断
- Blackboard 的 `p1_model` 字段：
  ```python
  {
    "holding": str | None,           # 事实：P1 手持何物
    "p1_active_task": str | None,    # 短程意图：推断的 task 身份
    "confidence": float,              # 0~1，置信度
    "last_evidence_tick": int,        # 最近一次支持证据的拍号
  }
  ```
- `navigation.p1_soft_cost_map(world, p1_model)` —— 返回 `{cell: extra_cost}`

**Spec References:**
- §6.7 P1Model：三层推断（事实 / 短程意图 / 显式告知）
- §6.3 协作过滤：站位软代价图

实现对人类玩家 P1 的行为推断，目的是让 Planner 避开撞车、接管下游。

**Key Design Points:**

1. **三层输出**（spec §6.7）：
   - **事实**：`holding`（直接读 `chefs[P1].held`），置信度 1.0
   - **短程意图**：`p1_active_task`（从手持 + 位置 + 朝向 + 最近交互推断），置信度 0~1
   - **显式告知**：`intents.assignments`（对话层，本计划为空）

2. **推断规则**（spec §6.7）：
   - 正在交互某站点 → `confidence = 1.0`（观测到的事实）
   - 手持 X 且朝目标站点移动 → 推断"要把 X 送去该站点"，`confidence ≈ 0.7`
   - 手持 X 但静止 / 朝向不明 → 只登记事实，不推断动作，`confidence = 0`
   - **宁可不推**：置信度低时只登记事实、不预测动作

3. **confidence 衰减**（spec §6.7）：
   - 每拍无新证据则 `confidence *= DECAY`（默认 0.95）
   - 低于阈值（默认 0.3）清空 `p1_active_task`
   - `last_evidence_tick` 记录最近一次支持证据的拍号

4. **站位软代价图**（spec §6.3）：
   - P1 周边格子（曼哈顿距离 ≤2）加通行代价（默认 +3）
   - 由 `navigation.py` 的 flood-fill 统一消费
   - 所有 task 的 ETA 自动绕开 P1 周边

- [ ] **Step 1: 写失败测试**

创建 `tests/test_p1model.py`：

```python
"""P1Model 人类意图推断单测。

重点验证：
  1. 事实层：holding 直接读取，置信度 1.0。
  2. 短程意图：手持 + 移动 → 推断 task，置信度 0.7。
  3. confidence 衰减：无新证据则递减，低于阈值清空。
  4. 站位软代价图：P1 周边格子加通行代价。
"""
import pytest
from ai import p1model
from ai.blackboard import Blackboard
# ...

def test_p1model_facts_holding():
    """事实层：直接读 P1 手持，置信度 1.0。"""
    # 构造：P1 手持 Cucumber
    # 断言：bb.p1_model["holding"] == "Cucumber", confidence == 1.0
    pass

def test_p1model_short_term_intent_moving_to_station():
    """短程意图：P1 手持黄瓜朝板移动 → 推断 FetchToBoard，置信度 0.7。"""
    # 构造：P1 持 Cucumber，位置朝 board 移动，距离减小
    # 断言：p1_active_task == "FetchToBoard(...)", confidence ≈ 0.7
    pass

def test_p1model_no_intent_when_stationary():
    """P1 静止 → 不推断动作，只登记事实。"""
    # 构造：P1 持 Cucumber，位置不变
    # 断言：p1_active_task == None, confidence == 0
    pass

def test_p1model_confidence_decay():
    """无新证据 → confidence 衰减，低于阈值清空。"""
    # 构造：初始 confidence = 0.7，连续 10 拍无新证据
    # 断言：confidence 递减，低于 0.3 后 p1_active_task 清空
    pass

def test_p1_soft_cost_map():
    """P1 周边格子加通行代价。"""
    # 构造：P1 在 (5, 5)
    # 断言：(4,5) / (5,4) / (6,5) / (5,6) 等周边格 extra_cost = 3
    pass
```

运行 `pytest tests/test_p1model.py -v`，全红。

- [ ] **Step 2: 实现 `ai/p1model.py` 骨架**

```python
# -*- coding: utf-8 -*-
"""P1Model：人类意图推断。

职责：
  推断 P1 在做什么，目的是避开撞车、接管下游。

三层输出（spec §6.7）：
  1. 事实：holding（手持何物），置信度 1.0
  2. 短程意图：p1_active_task（推断的 task），置信度 0~1
  3. 显式告知：intents.assignments（对话层，本计划为空）

Spec: §6.7 P1Model
"""
from typing import Optional, Dict
from ai.blackboard import Blackboard

# 常量
CONFIDENCE_DECAY = 0.95  # 每拍无新证据时的衰减系数
CONFIDENCE_THRESHOLD = 0.3  # 低于此值清空推断
MOVING_CONFIDENCE = 0.7  # 手持 + 移动推断的置信度
INTERACTING_CONFIDENCE = 1.0  # 正在交互的置信度

def update(world, bb):
    # type: (World, Blackboard) -> None
    """更新 P1 推断。
    
    每拍调用，更新 bb.p1_model。
    """
    if not bb.p1_model:
        bb.p1_model = {
            "holding": None,
            "p1_active_task": None,
            "confidence": 0.0,
            "last_evidence_tick": 0
        }
    
    p1_chef = _find_p1_chef(world)
    if not p1_chef:
        return
    
    # ① 事实层：holding
    bb.p1_model["holding"] = p1_chef.get("held")
    
    # ② 短程意图推断
    new_task, new_confidence = _infer_short_term_intent(p1_chef, world, bb)
    
    if new_confidence > 0:
        # 有新证据
        bb.p1_model["p1_active_task"] = new_task
        bb.p1_model["confidence"] = new_confidence
        bb.p1_model["last_evidence_tick"] = world.tick
    else:
        # 无新证据，衰减
        bb.p1_model["confidence"] *= CONFIDENCE_DECAY
        if bb.p1_model["confidence"] < CONFIDENCE_THRESHOLD:
            bb.p1_model["p1_active_task"] = None
            bb.p1_model["confidence"] = 0.0

def _find_p1_chef(world):
    # type: (World) -> Optional[dict]
    """找到 P1 厨师。"""
    for chef in world.chefs:
        if chef.get("name") == "Player 1":
            return chef
    return None

def _infer_short_term_intent(p1_chef, world, bb):
    # type: (dict, World, Blackboard) -> Tuple[Optional[str], float]
    """推断 P1 的短程意图。
    
    返回: (p1_active_task, confidence)
    """
    # ① 正在交互某站点 → confidence = 1.0
    interactions = p1_chef.get("interactions", {})
    if interactions:
        station_id = interactions.get("station")
        if station_id:
            # 推断正在交互该站点
            task = _infer_task_from_interaction(station_id, p1_chef, world)
            return task, INTERACTING_CONFIDENCE
    
    # ② 手持 + 移动中 → confidence = 0.7
    holding = p1_chef.get("held")
    if holding:
        target_station = _infer_target_from_movement(p1_chef, world, bb)
        if target_station:
            task = _infer_task_from_holding_and_target(holding, target_station, p1_chef, world)
            return task, MOVING_CONFIDENCE
    
    # ③ 其他情况：不推断
    return None, 0.0

def _infer_task_from_interaction(station_id, p1_chef, world):
    # type: (str, dict, World) -> Optional[str]
    """从交互站点推断 task。
    
    例如：P1 在板上 → 可能是 FetchToBoard 或 Chop
    """
    # 简化：返回通用描述，具体 task 由协作过滤匹配
    pass

def _infer_target_from_movement(p1_chef, world, bb):
    # type: (dict, World, Blackboard) -> Optional[str]
    """从移动方向推断目标站点。
    
    需要跨拍记录位置变化，判断是否在靠近某个站点。
    """
    # 简化：读 yaw + 最近站点
    pass

def _infer_task_from_holding_and_target(holding, target, p1_chef, world):
    # type: (str, str, dict, World) -> Optional[str]
    """从手持物 + 目标站点推断 task。
    
    例如：持 Cucumber + 朝 board → FetchToBoard(Cucumber, board)
    """
    pass
```

- [ ] **Step 3: 实现 confidence 衰减逻辑**

已在 `update` 函数中实现：无新证据时 `confidence *= DECAY`，低于阈值清空。

- [ ] **Step 4: 实现站位软代价图**

修改 `ai/navigation.py`，添加函数：

```python
def p1_soft_cost_map(world, p1_model):
    # type: (World, dict) -> Dict[Tuple[int, int], float]
    """生成 P1 周边的软代价图。
    
    P1 周边格子（曼哈顿距离 ≤2）加通行代价。
    
    返回: {(gx, gz): extra_cost}
    """
    if not p1_model or not p1_model.get("holding"):
        return {}
    
    p1_chef = _find_p1_chef(world)
    if not p1_chef:
        return {}
    
    p1_x, p1_z = p1_chef.get("x"), p1_chef.get("z")
    p1_gx, p1_gz = world.geometry.to_grid(p1_x, p1_z)
    
    soft_cost = {}
    P1_SOFT_COST = 3.0  # 周边格子的额外代价
    P1_RADIUS = 2  # 曼哈顿距离
    
    for dx in range(-P1_RADIUS, P1_RADIUS + 1):
        for dz in range(-P1_RADIUS, P1_RADIUS + 1):
            if abs(dx) + abs(dz) <= P1_RADIUS:
                neighbor = (p1_gx + dx, p1_gz + dz)
                soft_cost[neighbor] = P1_SOFT_COST
    
    return soft_cost
```

修改 `distance_field` 函数消费软代价图：

```python
def distance_field(world, start, p1_soft_cost=None):
    # type: (World, Tuple[int, int], Optional[Dict]) -> Dict[Tuple[int, int], float]
    """BFS 全图距离场 + P1 软代价。"""
    if p1_soft_cost is None:
        p1_soft_cost = {}
    
    # ... BFS 逻辑 ...
    # 在 step_cost 计算时加上 p1_soft_cost.get(neighbor, 0)
```

- [ ] **Step 5: 接入主循环**

修改 `ai/agent.py`，步骤 ④ 调用 `p1model.update`：

```python
from ai import p1model

class Agent:
    def run(self):
        while True:
            # ...
            # ④ P1 推断
            p1model.update(world, self.bb)
            # ...
```

- [ ] **Step 6: 修改 Arbiter 读 p1_model**

修改 `ai/arbiter.py` 的 Firefighting 判断：

```python
def _should_enter_firefighting(world, bb):
    """检查是否应进入 Firefighting。
    
    考虑 P1 持灭火器的情况（spec §5.3）。
    """
    # ...
    # 检查灭火器是否被 P1 持有
    if bb.p1_model and bb.p1_model.get("holding") == "Extinguisher":
        # P1 持唯一灭火器，AI 退回 Normal
        return None
    # ...
```

- [ ] **Step 7: 跑通 P1Model 测试**

运行 `pytest tests/test_p1model.py -v`，全绿。

---

### Task 3: 协作过滤（接力 / 分单模式）

**Files:**
- Modify: `ai/planner.py`（实装 `_filter_collaboration`）
- Test: `tests/test_collab.py`

**Spec References:**
- §6.3 协作过滤：两种模式共用同一套 task，区别只是打分前的一道筛

实现协作过滤从 Plan 3 的空壳变为实装：接力模式做分数惩罚，分单模式按归属筛选。

**Key Design Points:**

1. **接力模式**（默认，`--collab=relay`）：
   - `p1_active_task` 按 `confidence` 缩放为**分数惩罚**（不硬 veto）
   - 硬 veto 只保留给**物理争抢同一具体物件**
   - 惩罚公式：`score -= W_P1_CONFLICT * confidence * 5.0`

2. **分单模式**（`--collab=split`）：
   - 只留 `order_assignment == "P2"` 的 task
   - 归属为空 → **立刻退回接力**（这是必须的保护）
   - 新订单到达时按负载分配：`各自在手 task 数 + 已认领资源数`

3. **模式切换**：
   - `bb.intents.collab_mode` 控制（`"relay"` / `"split"`）
   - 默认 `"relay"`，`--collab=split` 可切换

- [ ] **Step 1: 写失败测试**

创建 `tests/test_collab.py`：

```python
"""协作过滤单测。

重点验证：
  1. 接力模式：p1_active_task 做分数惩罚，置信度越高惩罚越重。
  2. 分单模式：只留 order_assignment == "P2" 的 task。
  3. 分单模式归属为空 → 退回接力。
"""
import pytest
from ai import planner
# ...

def test_relay_mode_penalizes_p1_active_task():
    """接力模式：p1_active_task 做分数惩罚。"""
    # 构造：p1_active_task = "FetchToBoard(Cucumber, board1)", confidence = 0.8
    #       候选含 FetchToBoard(Cucumber, board1)
    # 断言：该 task 分数被扣 W_P1_CONFLICT * 0.8 * 5.0
    pass

def test_relay_mode_hard_veto_physical_conflict():
    """接力模式：物理争抢同一物件 → 硬 veto。"""
    # 构造：P1 正在拾取 cucumber_crate#1 的黄瓜（interactions.station）
    #       AI 的 FetchToBoard 也要从 cucumber_crate#1 取
    # 断言：该 task 被 veto，不进候选
    pass

def test_split_mode_filters_by_assignment():
    """分单模式：只留 order_assignment == "P2" 的 task。"""
    # 构造：order#1 归 P1，order#2 归 P2
    # 断言：候选只含 order#2 的 task
    pass

def test_split_mode_fallback_to_relay_when_no_assignment():
    """分单模式归属为空 → 退回接力。"""
    # 构造：所有订单归属为 None
    # 断言：候选包含所有订单（退回接力模式）
    pass
```

运行 `pytest tests/test_collab.py -v`，全红。

- [ ] **Step 2: 实现接力模式的分数惩罚**

修改 `ai/planner.py` 的 `_filter_collaboration`：

```python
# 常量
W_P1_CONFLICT = 1.0  # P1 冲突惩罚权重

def _filter_collaboration(feasible, world, bb):
    # type: (List[Task], World, Blackboard) -> List[Task]
    """协作过滤：接力 / 分单模式。
    
    接力模式：对 p1_active_task 做分数惩罚（在打分阶段处理，此处做硬 veto 检查）。
    分单模式：只留 order_assignment == "P2" 的 task。
    """
    collab_mode = bb.intents.get("collab_mode", "relay") if bb.intents else "relay"
    
    if collab_mode == "split":
        return _filter_split_mode(feasible, world, bb)
    else:
        return _filter_relay_mode(feasible, world, bb)

def _filter_relay_mode(feasible, world, bb):
    # type: (List[Task], World, Blackboard) -> List[Task]
    """接力模式：硬 veto 物理争抢，其余在打分阶段惩罚。"""
    if not bb.p1_model or not bb.p1_model.get("p1_active_task"):
        return feasible
    
    filtered = []
    p1_task = bb.p1_model["p1_active_task"]
    p1_interactions = _get_p1_interactions(world)
    
    for task in feasible:
        # 硬 veto：物理争抢同一具体物件
        if _physical_conflict(task, p1_task, p1_interactions):
            continue
        filtered.append(task)
    
    return filtered

def _physical_conflict(task, p1_task, p1_interactions):
    # type: (Task, str, dict) -> bool
    """检查是否物理争抢同一物件。
    
    例如：P1 正在从 crate#1 取黄瓜，AI 也要从 crate#1 取。
    """
    # 简化：检查 task 的资源是否与 P1 当前交互的站点重叠
    pass

def _filter_split_mode(feasible, world, bb):
    # type: (List[Task], World, Blackboard) -> List[Task]
    """分单模式：只留 order_assignment == "P2" 的 task。
    
    归属为空 → 退回接力。
    """
    # 检查是否有任何订单归属 AI
    has_assignment = any(
        bb.order_assignment.get(order.get("id")) == "P2"
        for order in world.orders
    )
    
    if not has_assignment:
        # 归属为空，退回接力
        return _filter_relay_mode(feasible, world, bb)
    
    # 只留归属 P2 的 task
    filtered = []
    for task in feasible:
        if task.order_id:
            assignment = bb.order_assignment.get(task.order_id)
            if assignment == "P2":
                filtered.append(task)
        else:
            # 无 order_id 的 task（清理类）保留
            filtered.append(task)
    
    return filtered
```

- [ ] **Step 3: 在打分阶段加入 P1 冲突惩罚**

修改 `ai/planner.py` 的 `_score_tasks`，加入 P1 冲突项：

```python
def _score_tasks(tasks, world, bb):
    # type: (List[Task], World, Blackboard) -> List[Tuple[Task, float, Dict]]
    """打分（含 P1 冲突惩罚）。"""
    scored = []
    p1_task = bb.p1_model.get("p1_active_task") if bb.p1_model else None
    p1_confidence = bb.p1_model.get("confidence", 0.0) if bb.p1_model else 0.0
    
    for task in tasks:
        terms = {}
        score = 0.0
        
        # ... 既有打分项（订单紧迫 / 时间压力 / ETA）...
        
        # ⑦ P1 冲突惩罚（接力模式）
        if p1_task and _task_conflicts_with_p1(task, p1_task):
            penalty = W_P1_CONFLICT * p1_confidence * 5.0
            terms["p1_conflict"] = -penalty
            score -= penalty
        
        scored.append((task, score, terms))
    
    return scored

def _task_conflicts_with_p1(task, p1_task):
    # type: (Task, str) -> bool
    """检查 task 是否与 P1 推断的 task 冲突。"""
    # 简化：字符串匹配 task 类型
    pass
```

- [ ] **Step 4: 实现分单模式的订单分配**

在 `ai/planner.py` 添加订单分配逻辑（新订单到达时调用）：

```python
def assign_new_order(order, world, bb):
    # type: (dict, World, Blackboard) -> None
    """分配新订单（分单模式用）。
    
    按负载分配：各自在手 task 数 + 已认领资源数。
    """
    order_id = order.get("id")
    
    # 计算 P1 / P2 负载
    p1_load = _compute_load("P1", bb)
    p2_load = _compute_load("P2", bb)
    
    # 分配给负载较低的一方
    if p1_load <= p2_load:
        bb.order_assignment[order_id] = "P1"
    else:
        bb.order_assignment[order_id] = "P2"

def _compute_load(player, bb):
    # type: (str, Blackboard) -> int
    """计算玩家负载：在手 task 数 + 已认领资源数。"""
    # 在手 task 数
    task_count = 1 if bb.commitment and bb.commitment.get("active_task") else 0
    
    # 已认领资源数
    claim_count = sum(
        1 for claim in bb.claims.values()
        if claim.get("owner") == player
    )
    
    return task_count + claim_count
```

- [ ] **Step 5: 跑通协作过滤测试**

运行 `pytest tests/test_collab.py -v`，全绿。

---

### Task 4: 打分权重完整集

**Files:**
- Modify: `ai/planner.py`（添加六项新权重）
- Test: `tests/test_planner_multi.py`（打分部分）

**Spec References:**
- §6.4 打分：九项加权（Plan 3 实现了三项，Plan 4 补全六项）

Plan 4 补全的六项权重：
4. **异步资源优先** ⭐：FetchToCooker（下锅后自己跑）比 FetchToPlate（需人等）分数高
5. **接力加成**：承接 P1 上游的下游 task（P1 下锅，AI 取熟饭）
6. **完成度**：快装齐的盘优先推完
7. **料复用 / 批量**：该次取料同时满足 ≥2 单则加成
8. **combo 延续**：与上一次出餐同菜则加成
9. **截止前瞻**：启动后会错过已知截止则惩罚

- [ ] **Step 1: 写打分测试**

扩展 `tests/test_planner_multi.py`：

```python
def test_score_async_resource_priority():
    """异步资源优先：FetchToCooker（下锅）比 FetchToPlate 分数高。"""
    # 构造：cooker 空闲，plate 已就位，两个 task 都可行
    # 断言：FetchToCooker 分数 > FetchToPlate
    pass

def test_score_relay_bonus():
    """接力加成：P1 下锅煮饭，AI 的 TakeCookedToPlate 加分。"""
    # 构造：P1 刚完成 FetchToCooker，cooker 正在 cooking
    # 断言：TakeCookedToPlate 有 relay_bonus
    pass

def test_score_progress_bonus():
    """完成度：盘里已有 2/3 料，FetchToPlate 最后一料分数高。"""
    pass

def test_score_ingredient_reuse():
    """料复用：取一次黄瓜同时满足两单，加成。"""
    pass

def test_score_combo_continuation():
    """combo 延续：上次送出 Sushi_Fish，这次送同类加分。"""
    pass

def test_score_deadline_forecast():
    """截止前瞻：启动后会错过锅烧糊时刻，惩罚。"""
    pass
```

运行 `pytest tests/test_planner_multi.py -k score -v`，全红。

- [ ] **Step 2: 实现六项新权重**

修改 `ai/planner.py` 的 `_score_tasks`：

```python
# 新增权重常量
W_ASYNC_RESOURCE = 4.0  # 异步资源优先
W_RELAY_BONUS = 2.0  # 接力加成
W_PROGRESS = 1.5  # 完成度
W_REUSE = 2.0  # 料复用
W_COMBO = 1.0  # combo 延续
W_DEADLINE = 3.0  # 截止前瞻（惩罚）

def _score_tasks(tasks, world, bb):
    """打分（完整九项）。"""
    scored = []
    last_delivered_dish = bb.get("last_delivered_dish")  # 上次出餐的菜名
    
    for task in tasks:
        terms = {}
        score = 0.0
        
        # ①②③ 既有三项（订单紧迫 / 时间压力 / ETA）
        # ...
        
        # ④ 异步资源优先
        if task.TYPE in [TaskType.FETCH_TO_COOKER, TaskType.FETCH_TO_BOARD]:
            # 下锅 / 切菜后自己跑，启动越早并行度越高
            terms["async"] = W_ASYNC_RESOURCE
            score += terms["async"]
        
        # ⑤ 接力加成
        if _is_relay_task(task, world, bb):
            terms["relay"] = W_RELAY_BONUS
            score += terms["relay"]
        
        # ⑥ 完成度
        progress_ratio = _order_progress(task, world, bb)
        terms["progress"] = W_PROGRESS * progress_ratio
        score += terms["progress"]
        
        # ⑦ 料复用 / 批量
        reuse_count = _ingredient_reuse_count(task, world, bb)
        if reuse_count >= 2:
            terms["reuse"] = W_REUSE * (reuse_count - 1)
            score += terms["reuse"]
        
        # ⑧ combo 延续
        if task.TYPE == TaskType.DELIVER_PLATE and task.order_id:
            order = _find_order_by_id(world, task.order_id)
            if order and order.get("name") == last_delivered_dish:
                terms["combo"] = W_COMBO
                score += terms["combo"]
        
        # ⑨ 截止前瞻
        deadline_penalty = _deadline_forecast(task, world, bb)
        if deadline_penalty > 0:
            terms["deadline"] = -W_DEADLINE * deadline_penalty
            score += terms["deadline"]
        
        scored.append((task, score, terms))
    
    return scored

def _is_relay_task(task, world, bb):
    # type: (Task, World, Blackboard) -> bool
    """检查是否是接力 task（承接 P1 上游）。"""
    # 例如：P1 刚完成 FetchToCooker，AI 的 TakeCookedToPlate 是接力
    # 简化：检查 task 的前置是否被 P1 完成
    pass

def _order_progress(task, world, bb):
    # type: (Task, World, Blackboard) -> float
    """计算订单完成度（0~1）。"""
    if not task.order_id:
        return 0.0
    
    order = _find_order_by_id(world, task.order_id)
    if not order:
        return 0.0
    
    # 已就位的料数 / 总需求数
    requirements = order.get("composition", [])
    plate = _find_plate_for_order(task.order_id, world, bb)
    if not plate:
        return 0.0
    
    filled = sum(1 for req in requirements if _plate_has(plate, req, world))
    return float(filled) / len(requirements) if requirements else 0.0

def _ingredient_reuse_count(task, world, bb):
    # type: (Task, World, Blackboard) -> int
    """计算该料同时满足几个订单。"""
    if task.TYPE not in [TaskType.FETCH_TO_PLATE, TaskType.TAKE_COOKED_TO_PLATE]:
        return 0
    
    ingredient = task.resources.get("item")
    if not ingredient:
        return 0
    
    # 统计有几个订单缺这个料
    count = 0
    for order in world.orders:
        if ingredient in order.get("composition", []):
            plate = _find_plate_for_order(order.get("id"), world, bb)
            if plate and not _plate_has(plate, ingredient, world):
                count += 1
    
    return count

def _deadline_forecast(task, world, bb):
    # type: (Task, World, Blackboard) -> float
    """截止前瞻：task 执行时长是否会错过已知截止。
    
    已知截止：
      - 锅烧糊时刻（cooked → burning 的倒计时）
      - 订单到期（order.remaining）
    
    返回：惩罚系数（0 = 不会错过，>0 = 会错过）
    """
    est_ticks = task.est_ticks(world, bb)  # task 粗粒度时长估算
    
    # ① 检查锅烧糊
    if task.order_id:
        cookers_for_order = _find_cookers_for_order(task.order_id, world, bb)
        for cooker in cookers_for_order:
            if cooker.get("cooked") and not cooker.get("burning"):
                # 粗略估算：cooked → burning 约 10 秒 = 200 拍
                time_to_burn = 200  # 简化
                if est_ticks > time_to_burn:
                    return 1.0  # 会错过
    
    # ② 检查订单到期
    if task.order_id:
        order = _find_order_by_id(world, task.order_id)
        if order:
            remaining_ticks = order.get("remaining") * 20  # 秒 → 拍
            if est_ticks > remaining_ticks:
                return 1.0  # 会错过
    
    return 0.0
```

- [ ] **Step 3: 为 Task 添加 `est_ticks` 方法**

在 `ai/tasks/` 的 base task 类中添加：

```python
class Task:
    # ... 既有方法 ...
    
    def est_ticks(self, ctx):
        # type: (dict) -> int
        """粗粒度时长估算（常量 + ETA）。
        
        用于截止前瞻。
        """
        base_ticks = self._base_ticks()  # task 类型的基础时长
        eta = self.eta(ctx)  # 到达成本
        return base_ticks + eta
    
    def _base_ticks(self):
        # type: () -> int
        """task 类型的基础时长（常量）。"""
        # 示例：
        # FetchToCooker: 20 拍（拿料 + 放锅）
        # Chop: 60 拍（切菜按压）
        # DeliverPlate: 30 拍（端盘 + 送出）
        pass
```

- [ ] **Step 4: 跑通打分测试**

运行 `pytest tests/test_planner_multi.py -k score -v`，全绿。

---

### Task 5: 承诺穿插

**Files:**
- Modify: `ai/planner.py`（扩展 `_apply_commitment_inertia`，允许穿插）
- Modify: `ai/executor.py`（支持 task 暂停 / 恢复）
- Test: `tests/test_planner_multi.py`（穿插部分）

**Spec References:**
- 新增设计点（spec 未明确，补充如下）

实现承诺穿插：当前 task 是 RUNNING 且剩余步数 > 阈值时，允许 Planner 选另一订单的 task（穿插）。

**Key Design Points:**

1. **穿插条件**：
   - 当前 task 状态 = RUNNING
   - 剩余步数 > `INTERLEAVE_THRESHOLD`（默认 5 步）
   - 新 task 的分数显著更高（超过阈值 + 穿插惩罚）

2. **暂停 / 恢复**：
   - 穿插时：当前 task 进入"暂停"状态（`bb.paused_task`）
   - 完成穿插 task 后：恢复 paused_task

3. **限制**：
   - 只允许一层穿插（不递归穿插）
   - 暂停的 task 必须是可恢复的（不涉及时效性强的动作，如灭火）

- [ ] **Step 1: 写穿插测试**

扩展 `tests/test_planner_multi.py`：

```python
def test_commitment_interleave_when_running_long_task():
    """承诺穿插：正在煮饭（长 task），穿插去切下一单的菜。"""
    # 构造：当前 task = FetchToCooker（已下锅，cooking），剩余 15 步
    #       候选中有 FetchToBoard（下一单的菜），分数更高
    # 断言：选 FetchToBoard（穿插），FetchToCooker 进入 paused_task
    pass

def test_interleave_resume_after_completion():
    """穿插完成后恢复原 task。"""
    # 构造：paused_task = FetchToCooker，当前 task = FetchToBoard 完成
    # 断言：下一拍选 FetchToCooker（恢复）
    pass

def test_no_interleave_when_remaining_steps_low():
    """剩余步数低（≤5），不允许穿插。"""
    # 构造：当前 task 剩余 3 步
    # 断言：即使有更高分 task，也不穿插
    pass
```

运行 `pytest tests/test_planner_multi.py -k interleave -v`，全红。

- [ ] **Step 2: 扩展 Blackboard**

添加字段：

```python
@dataclass
class Blackboard:
    # ... 既有字段 ...
    
    # 承诺穿插
    paused_task: Optional[Dict] = None
    # {"task_id": ..., "tree": ..., "steps_done": ...}
    # 当前 task 被穿插时，暂存在此
```

- [ ] **Step 3: 修改 `_apply_commitment_inertia`**

```python
# 常量
INTERLEAVE_THRESHOLD = 5  # 剩余步数 > 此值才允许穿插
INTERLEAVE_PENALTY = 3.0  # 穿插的分数门槛惩罚

def _apply_commitment_inertia(scored, world, bb):
    """承诺惯性 + 穿插。"""
    if not scored:
        return _idle_task(), {}
    
    scored.sort(key=lambda x: x[1], reverse=True)
    top_task, top_score, top_terms = scored[0]
    
    # ① 检查是否有暂停的 task（优先恢复）
    if bb.paused_task:
        paused_id = bb.paused_task["task_id"]
        paused = _find_task_in_scored(scored, paused_id)
        if paused:
            # 恢复暂停的 task
            bb.paused_task = None
            return paused[0], _compute_claims(paused[0])
    
    # ② 检查当前承诺
    if bb.commitment and bb.commitment.active_task:
        current_task_id = bb.commitment.active_task
        current = _find_task_in_scored(scored, current_task_id)
        
        if current:
            current_task, current_score, current_terms = current
            
            # ③ 不可行连续性检查（同 Plan 3）
            # ...
            
            # ④ 穿插检查
            remaining_steps = current_task.est_steps() - bb.commitment.steps_done
            if remaining_steps > INTERLEAVE_THRESHOLD:
                # 允许穿插
                threshold = COMMITMENT_MARGIN + INTERLEAVE_PENALTY
                if top_score - current_score > threshold:
                    # 穿插：暂停当前 task
                    bb.paused_task = {
                        "task_id": current_task_id,
                        "steps_done": bb.commitment.steps_done
                    }
                    bb.commitment = None
                    return top_task, _compute_claims(top_task)
            
            # ⑤ 进度加成（同 Plan 3）
            # ...
    
    # 无承诺 或 改选
    return top_task, _compute_claims(top_task)
```

- [ ] **Step 4: 修改 Executor 支持恢复**

```python
class Executor:
    def tick(self, task, ctx):
        """执行一拍（支持恢复暂停的 task）。"""
        task_id = task.identity()
        
        # ① 检查是否恢复暂停的 task
        if ctx["bb"].paused_task and ctx["bb"].paused_task["task_id"] == task_id:
            # 恢复：复用暂停时的 tree + steps_done
            # （简化：重建树，由 bt.py 的 RUNNING 状态自动续跑）
            pass
        
        # ② 正常流程（同 Plan 3）
        if task_id != self.active_task_id:
            self._rebuild_tree(task, ctx, current_tick)
        
        status = self.active_tree.tick(ctx)
        # ...
        return status
```

- [ ] **Step 5: 跑通穿插测试**

运行 `pytest tests/test_planner_multi.py -k interleave -v`，全绿。

---

### Task 6: 收尾 task（RescueCooked / ResetPlate / Prefetch）

**Files:**
- Create: `ai/tasks/rescue_cooked.py`
- Create: `ai/tasks/reset_plate.py`
- Create: `ai/tasks/prefetch.py`
- Test: `tests/test_rescue_reset.py`

**Spec References:**
- §6.1 候选 task 全集：第 6、14、15 号
- §6.1 设计点：RescueCooked / ResetPlate 的必要性

实现三个收尾 task，补全 16 个 task 的完整集合。

**Key Design Points:**

1. **RescueCooked**（spec §6.1 第 6 号）：
   - 可行性：该灶 `cooked` 且 无盘需要它
   - 完成判定：锅已离灶（烧糊计时停止）
   - 动作：端起锅放到就近台面（spec §6.1 确认的游戏机制：端起即停烧糊）
   - 价值：同时完成"救料"（现成食材）和"腾资源"（释放灶台）

2. **ResetPlate**（spec §6.1 第 14 号）：
   - 可行性：盘内容与**任何**现存订单需求都不匹配
   - 完成判定：盘空 或 已移出拼装格
   - 动作：倒进垃圾桶 或 移走盘子
   - 必要性：两个场景会楔死拼装格（盘里被放错料、满盘在手时订单过期）

3. **Prefetch**（spec §6.1 第 15 号）：
   - 可行性：存在后续订单必然需要、且可提前取的料
   - 完成判定：料已在台面
   - 动作：从 crate 取料放到台面
   - 价值：无其他 task 可做时（Idle）的生产性待命

- [ ] **Step 1: 写收尾 task 测试**

创建 `tests/test_rescue_reset.py`：

```python
"""收尾 task 单测。

重点验证：
  1. RescueCooked：熟料在锅里但无单需要 → 端离灶台。
  2. ResetPlate：盘内容与所有订单不匹配 → 清空或移走。
  3. Prefetch：后续订单必然需要的料 → 提前取。
"""
import pytest
from ai.tasks import RescueCooked, ResetPlate, Prefetch
# ...

def test_rescue_cooked_saves_cooked_ingredient():
    """RescueCooked：锅里有熟饭但无单需要 → 端离灶台救料。"""
    # 构造：cooker1 cooked（含 BoiledSushiRice），无订单需要
    # 断言：RescueCooked(cooker1) 可行
    pass

def test_rescue_cooked_releases_cooker():
    """RescueCooked 同时腾出灶台。"""
    # 构造：两口锅都 cooked
    # 断言：RescueCooked 完成后，cooker 认领释放
    pass

def test_reset_plate_when_content_mismatch():
    """ResetPlate：盘里放错料 → 清空盘子。"""
    # 构造：盘里有 Cucumber，但所有订单都不需要 Cucumber
    # 断言：ResetPlate(plate) 可行
    pass

def test_prefetch_when_idle():
    """Prefetch：无其他 task 可做 → 预取常用料。"""
    # 构造：当前无订单，但历史订单常用 Rice
    # 断言：Prefetch(Rice) 可行
    pass
```

运行 `pytest tests/test_rescue_reset.py -v`，全红。

- [ ] **Step 2: 实现 RescueCooked**

创建 `ai/tasks/rescue_cooked.py`：

```python
# -*- coding: utf-8 -*-
"""RescueCooked task：端走熟料锅，救料 + 腾灶台。

Spec: §6.1 第 6 号
"""
from ai.tasks.base import Task, TaskType
from ai.bt import Sequence
from ai.primitives import InteractWith, Approach

class RescueCooked(Task):
    """端走熟料锅。"""
    
    TYPE = TaskType.RESCUE_COOKED
    
    def __init__(self, cooker_id, order_id=None):
        self.cooker_id = cooker_id
        self.order_id = order_id  # 通常为 None（不绑定订单）
    
    def feasible(self, ctx):
        """可行性：cooker cooked 且 无盘需要它。"""
        cooker = _find_cooker(ctx["world"], self.cooker_id)
        if not cooker or not cooker.get("cooked"):
            return False
        
        # 检查是否有订单需要该锅的熟料
        # （简化：遍历订单，查 composition 与锅内容匹配）
        cooked_item = cooker.get("contents")
        for order in ctx["world"].orders:
            if cooked_item in order.get("composition", []):
                # 有订单需要，不 rescue
                return False
        
        return True
    
    def build(self, ctx):
        """构建执行树。"""
        cooker = _find_cooker(ctx["world"], self.cooker_id)
        return Sequence([
            InteractWith(cooker, "pickup", expect=lambda: ctx["hands"].holding()),
            Approach(_find_drop_cell(ctx), "placement"),
            InteractWith("drop", expect=lambda: not ctx["hands"].holding())
        ])
    
    def claims(self):
        """认领：灶台。"""
        return [(self.cooker_id, "rescue")]
    
    def est_steps(self):
        return 3
```

- [ ] **Step 3: 实现 ResetPlate**

创建 `ai/tasks/reset_plate.py`：

```python
# -*- coding: utf-8 -*-
"""ResetPlate task：清空错误的盘子，解锁拼装格。

Spec: §6.1 第 14 号
"""
from ai.tasks.base import Task, TaskType
from ai.bt import Sequence
from ai.primitives import InteractWith, Approach

class ResetPlate(Task):
    """清空盘子。"""
    
    TYPE = TaskType.RESET_PLATE
    
    def __init__(self, plate_id, assembly_pos):
        self.plate_id = plate_id
        self.assembly_pos = assembly_pos  # 拼装格位置
        self.order_id = None  # 无绑定订单
    
    def feasible(self, ctx):
        """可行性：盘内容与所有订单都不匹配。"""
        plate = _find_plate(ctx["world"], self.plate_id)
        if not plate:
            return False
        
        contents = plate.get("contents", [])
        if not contents:
            return False  # 空盘不需要 reset
        
        # 检查是否匹配任何订单
        for order in ctx["world"].orders:
            if _plate_matches_order(plate, order, ctx):
                return False  # 匹配，不 reset
        
        return True
    
    def build(self, ctx):
        """构建执行树：拿盘 → 倒垃圾桶。"""
        plate = _find_plate(ctx["world"], self.plate_id)
        bin_station = _find_station(ctx["world"], "bin")
        
        return Sequence([
            InteractWith(self.assembly_pos, "pickup", expect=lambda: ctx["hands"].holding(self.plate_id)),
            InteractWith(bin_station, "discard", expect=lambda: _plate_empty(plate))
        ])
    
    def claims(self):
        """认领：盘 + 拼装格 + 垃圾桶。"""
        return [
            (self.plate_id, "reset"),
            (self.assembly_pos, "assembly"),
            ("bin", "discard")
        ]
    
    def est_steps(self):
        return 3
```

- [ ] **Step 4: 实现 Prefetch**

创建 `ai/tasks/prefetch.py`：

```python
# -*- coding: utf-8 -*-
"""Prefetch task：预取常用料，生产性待命。

Spec: §6.1 第 15 号
"""
from ai.tasks.base import Task, TaskType
from ai.bt import Sequence
from ai.primitives import InteractWith, Approach

class Prefetch(Task):
    """预取料。"""
    
    TYPE = TaskType.PREFETCH
    
    def __init__(self, item):
        self.item = item
        self.order_id = None
    
    def feasible(self, ctx):
        """可行性：后续订单必然需要、且可提前取。"""
        # 简化：检查该料是否在常见订单的 composition 中
        # （可用历史订单统计，或硬编码常用料）
        common_items = ["Rice", "SushiFish", "Seaweed", "Cucumber"]
        return self.item in common_items
    
    def build(self, ctx):
        """构建执行树：从 crate 取料 → 放台面。"""
        crate = _find_crate(ctx["world"], self.item)
        drop_cell = _find_drop_cell(ctx)
        
        return Sequence([
            InteractWith(crate, "pickup", expect=lambda: ctx["hands"].holding(self.item)),
            Approach(drop_cell, "placement"),
            InteractWith("drop", expect=lambda: not ctx["hands"].holding())
        ])
    
    def claims(self):
        """认领：crate + 落点。"""
        return [(f"crate_{self.item}", "prefetch")]
    
    def est_steps(self):
        return 3
```

- [ ] **Step 5: 集成到候选生成**

修改 `ai/planner.py` 的 `_generate_cleanup_tasks`：

```python
def _generate_cleanup_tasks(world, bb):
    """生成清理 task（含 RescueCooked / ResetPlate / Prefetch）。"""
    tasks = []
    
    # StashHeld（Plan 3 已有）
    # ...
    
    # RecoverLoosePot（Plan 3 已有）
    # ...
    
    # Discard（Plan 3 已有）
    # ...
    
    # RescueCooked（新增）
    for cooker in world.cookers:
        if cooker.get("cooked"):
            tasks.append(RescueCooked(cooker.get("id")))
    
    # ResetPlate（新增）
    for plate in world.plates:
        if _plate_content_mismatch(plate, world):
            assembly_pos = _find_assembly_for_plate(plate, world, bb)
            if assembly_pos:
                tasks.append(ResetPlate(plate.get("id"), assembly_pos))
    
    # Prefetch（新增）
    if not world.orders:
        # 无订单时才 prefetch
        for item in ["Rice", "SushiFish", "Seaweed", "Cucumber"]:
            tasks.append(Prefetch(item))
    
    return tasks
```

- [ ] **Step 6: 跑通收尾 task 测试**

运行 `pytest tests/test_rescue_reset.py -v`，全绿。

---

### Task 7: 浸泡测试 + 基线对比

**Files:**
- Test: `tests/test_integration.py`

**Goal:** 30 分钟实机运行 + 基线对比（出餐数 / 得分中位数），验证认领泄漏 / 模式死锁 / 承诺抖动。

- [ ] **Step 1: 编写集成测试框架**

创建 `tests/test_integration.py`：

```python
"""集成测试：浸泡 + 基线对比。

验收标准（spec §11.5）：
  1. 30 分钟浸泡：零模式锁死、零楔死认领、零残留按键、零认领泄漏。
  2. 基线对比：出餐数 / 得分中位数不低于 Plan 3 基线 −X%（N≥5 局）。
  3. 多订单穿插：日志可见"下锅 → 期间推进别单 → 回头取熟料"。
"""
import pytest
import time
from ai.agent import Agent
# ...

@pytest.mark.slow
def test_30min_soak():
    """30 分钟浸泡测试。
    
    运行方式：pytest tests/test_integration.py::test_30min_soak -v --log-cli-level=INFO
    """
    agent = Agent()
    start_time = time.time()
    duration = 30 * 60  # 30 分钟
    
    while time.time() - start_time < duration:
        # 跑一局，记录指标
        result = _run_one_game(agent)
        
        # 检查健康指标
        assert result["mode_deadlock"] == 0, "模式死锁"
        assert result["claim_leak"] == 0, "认领泄漏"
        assert result["key_stuck"] == 0, "残留按键"
    
    print("✅ 30 分钟浸泡通过")

def test_baseline_comparison():
    """基线对比：与 Plan 3 比较。"""
    # 读取 Plan 3 基线（docs/baseline-plan3.json）
    plan3_baseline = _load_baseline("plan3")
    
    # 跑 N≥5 局
    results = []
    for i in range(5):
        result = _run_one_game(Agent())
        results.append(result)
    
    # 计算中位数
    plan4_dishes = _median([r["dishes_delivered"] for r in results])
    plan4_score = _median([r["score"] for r in results])
    
    # 断言：不低于 Plan 3 −10%
    assert plan4_dishes >= plan3_baseline["dishes"] * 0.9, "出餐数退化"
    assert plan4_score >= plan3_baseline["score"] * 0.9, "得分退化"
    
    print(f"✅ 基线对比通过：Plan4={plan4_dishes:.1f}菜/{plan4_score:.0f}分, Plan3={plan3_baseline['dishes']}菜/{plan3_baseline['score']}分")

def test_multi_order_interleaving():
    """多订单穿插：日志验证。"""
    # 跑一局，解析 /tmp/agent.log
    agent = Agent()
    _run_one_game(agent)
    
    log = _parse_trace_log("/tmp/agent.log")
    
    # 验证穿插模式：
    # 1. 选中 FetchToCooker（下锅）
    # 2. 几拍后选中 FetchToBoard（别单）
    # 3. FetchToBoard 完成后恢复 TakeCookedToPlate（取熟料）
    interleave_detected = _detect_interleave_pattern(log)
    assert interleave_detected, "未检测到多订单穿插"
    
    print("✅ 多订单穿插验证通过")
```

- [ ] **Step 2: 实现健康指标检测**

```python
def _run_one_game(agent):
    """跑一局游戏，返回健康指标。"""
    # 启动 agent，等待游戏结束（订单队列空 / 超时）
    # 收集指标：
    #   - dishes_delivered: 出餐数
    #   - score: 得分
    #   - mode_deadlock: 模式死锁次数
    #   - claim_leak: 认领泄漏次数
    #   - key_stuck: 残留按键次数
    pass

def _detect_claim_leak(bb):
    """检测认领泄漏。
    
    规则：认领的 order_id 不在当前订单表 → 泄漏
    """
    leaks = []
    for claim_key, claim in bb.claims.items():
        order_id = claim.get("order_id")
        if order_id:
            if not _order_exists(world, order_id):
                leaks.append(claim_key)
    return len(leaks)
```

- [ ] **Step 3: 录制 Plan 3 基线**

```bash
# 切换到 Plan 3 分支
git checkout plan3-end-to-end

# 跑 5 局，记录指标
python3 scripts/run_baseline.py --plan=3 --games=5 --output=docs/baseline-plan3.json
```

- [ ] **Step 4: 跑通集成测试**

```bash
# 30 分钟浸泡（慢测试，标记 @pytest.mark.slow）
pytest tests/test_integration.py::test_30min_soak -v --log-cli-level=INFO

# 基线对比
pytest tests/test_integration.py::test_baseline_comparison -v

# 多订单穿插
pytest tests/test_integration.py::test_multi_order_interleaving -v
```

全绿 → Plan 4 交付。

---

## 验收标准（Plan 4）

按 spec §11.5 完成标准：

1. **不退化**（阶段 5 之后）：
   - [ ] 出餐数 / 得分中位数不低于 Plan 3 基线 −10%（N≥5 局）

2. **多订单穿插**：
   - [ ] 日志可见"下锅 → 期间推进别单 task → 回头取熟料"

3. **协同质量**（阶段 6）：
   - [ ] P1Model 推断测试通过
   - [ ] 站位软代价图生效（ETA 自动绕开 P1 周边）
   - [ ] 协作过滤测试通过（接力 / 分单模式）

4. **可解释**：
   - [ ] 任一拍的决策都能从 `trace` 回放出完整九项打分 terms

5. **健壮**（30 分钟浸泡）：
   - [ ] 零模式锁死
   - [ ] 零楔死认领
   - [ ] 零残留按键
   - [ ] 零认领泄漏

6. **代码**：
   - [ ] 无单个文件 > 600 行
   - [ ] 打分权重渐进上线（每加一项录基线，spec §11.4）

---

## 已知缺口与后续工作

Plan 4 完成后，以下能力归未来工作：

1. **对话协作层**（spec §10.7）：自然语言 → 结构化意图的解析器。
2. **关卡机制**（spec §10）：传送带泛化、冰面、移动地板、传送门、老鼠等。
3. **方案 B（进程内控制）**：把决策搬进 Unity 进程。
4. **实时 TUI / Web 面板**：在 JSONL 基础上叠加实时可视化。

Plan 4 交付的是**接近人类水平的多订单协同 AI**，为对话层与复杂关卡打下基线。

---

## Ledger

| Task | Status | Commit | Notes |
|---|---|---|---|
| Task 1: 多订单候选生成 + 多资源认领 | ⬜ Not started | | 反向展开、认领粒度、waiters 队列 |
| Task 2: P1Model | ⬜ Not started | | 三层推断 + confidence 衰减 + 站位软代价 |
| Task 3: 协作过滤 | ⬜ Not started | | 接力 / 分单模式 |
| Task 4: 打分权重完整集 | ⬜ Not started | | 补全六项新权重 |
| Task 5: 承诺穿插 | ⬜ Not started | | RUNNING 时穿插别单 |
| Task 6: 收尾 task | ⬜ Not started | | RescueCooked / ResetPlate / Prefetch |
| Task 7: 浸泡测试 + 基线对比 | ⬜ Not started | | 30 分钟 + 不退化断言 |

---

**Plan 4 总行数估计**: ~1200 行代码 + ~600 行测试 = 1800 行

**预计工期**: 7 个任务 × 平均 2 小时 ≈ 14-16 小时（含调参）

**风险**: 多订单并行与 P1 协同交互复杂，承诺穿插引入新状态。缓解：逐任务测试，Task 1-3 各自独立验证，Task 4-5 在前三者基础上叠加，Task 7 集成验证。

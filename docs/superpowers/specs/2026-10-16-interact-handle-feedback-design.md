# 交互句柄反馈闭环 设计

> 2026-10-16。根因来自 `trace-20261016-220409`（P2 在格 (1,-4) 钉住 287 拍
> 直到收盘）+ `Assembly-CSharp.dll` 反编译（ilspy 11.1）。

## 1. 现象

实机 trace-20261016-220409：P2 手持 `utensil_pot_01 (1)`，从 t=9513 到
t=9800（287 拍 ≈ 14 秒，直到关卡结束）停在格 (1,-4) 一动不动、零按键。

trace 里能看到的链条：

```
t=9521  DeliverPlate(-14092, order#4)        failure
t=9523  PlaceCleanPlate(-39196, (2,-4))      failure
t=9526  PlaceCleanPlate(-37268, (1,-2))      failure
t=9529  PlaceCleanPlate(-37038, (3,-4))      failure
t=9532  （无候选可选）                        idle × 269 拍
```

那 269 拍里 `PlaceCleanPlate` 被 veto `abandoned` 共 14160 次（候选 × 拍）。

## 2. 根因

### 2.1 直接原因：`Tap` 的前置校验与 `Face` 的到位判据读的不是同一个字段

离线在 t=9523 那一帧复现（`/tmp/f9523.json`）：

```
EnsureHandsFree → FAILURE
  _lock_target  → countertop_01_standard_wood (36) @ 格 (2,-4)   （P2 右手边）
  Approach      → SUCCESS
  Face          → SUCCESS        ← interact_ready(kind=None)  读通用 gx/gz = (2,-4) ✓
  TapWhileHolding → FAILURE      ← interact_ready(kind="placement") 读 placement_gx/gz = (0,-5) ✗
```

游戏那一帧上报：

| 字段 | 值 | 含义 |
|---|---|---|
| `interactions.gx/gz` | (2,-4) 台面 | 我正对着的那一格 |
| `interactions.placement_gx/gz` | (0,-5) 出餐口 | 弧内最近的可放置物 |

两个字段都是真实的，但回答的是两个不同问题。`Face` 用前者判定「转好了」，
`Tap` 用后者判定「能按了」—— **`Face` SUCCESS 不蕴含 `Tap` 能按**。

### 2.2 为什么两个字段会不一致（反编译结论）

`PlayerControls.FindNearbyObjects()` 做两次**独立**扫描：

```csharp
// 扫描 A：弧内最近可拾取/放置物 → m_iHandlePickup / m_iHandlePlacement
GameObject gameObject2 = InteractWithItemHelper.ScanForObject(
    m_colliders, collidersInArc, m_PickupOrPlaceCondition);

// 扫描 B：我正对着的那一格 → m_gridLocation
m_tmpInteractionObjects.m_gridLocation = InteractWithItemHelper.ScanForComponent(
    m_colliders, collidersInArc, m_GridLocationCondition);
```

- **扫描 A**（`ScanForObject`）：半径 1.0、180° 弧内，取 `transform.position`
  距基准点 `position + 0.5*radius*forward` **最近**的那一个。纯距离，不看朝向。
- **扫描 B** 走 `_gridSelection=true` 分支，内部 `GetFacingGridOccupant` 按
  `ScoreFacingGridCollider`（`dot(forward, offset)`，135° 截断）挑**朝向最优**的格。

所以在「两个可交互物都在弧内」的位置上，A 可能选 X 而 B 选 Y。

t=9523 的实测几何（用站点 AABB 近似 collider 复算）：

```
P2 实际位置 (15.30, -10.97)，所在格 (1,-4) 的格心是 (15.6, -10.8)
偏离格心 (-0.30, -0.17)

弧内候选（按到基准点距离）：
  d=1.1041  workstation_plate_station        @ (0,-5)   ← 扫描 A 选中
  d=1.1204  countertop_01_standard_wood (36) @ (2,-4)   ← 扫描 B 选中
```

两者差 **0.016 世界单位（1.4%）**。

### 2.3 关键观察：这是站位精度问题，不是站位选择问题

同样用复算，固定 yaw 扫一遍：

| 位置 | yaw=90 | yaw=116（实际） | yaw=130 |
|---|---|---|---|
| 实际 (15.30,-10.97) | 台面 ✓ | **出餐口 ✗** | 出餐口 ✗ |
| 格心 (15.6,-10.8) | 台面 ✓ | 台面 ✓ | 台面 ✓ |

**站在格心时，任何朝向都是台面赢。** 是那 0.3 格的偏离把出餐口推成了更近者。

再扫四个交互位（都按朝向目标的 yaw、都站格心）：

```
stand (1,-4) → 台面 ✓     stand (3,-4) → 台面 ✓
stand (2,-5) → 台面 ✓     stand (2,-3) → 台面 ✓
```

四个位置全部可用。**换格子解决不了问题，因为当前这格本来就是好的。**

这推翻了「挑一个能摸到目标的落脚点」那条思路：没有坏格子可排除。

## 3. 非目标

- **不读高亮**。`ClientAnticipateInteractionHighlight.OnInteractionAnticipationStart(type, _player)`
  收到 `_player` 但实现直接丢弃，只 `IncrementHighlight()`。`m_highlightCount`
  是「有几个来源在高亮我」的总数，P1 站在同一台面前也会 +1，且 Interact /
  Pickup / Placement / Catch / GridOccupant 五类共享同一个计数。分屏下无法
  区分「这是给 P2 的吗」，而那正是唯一需要的那维信息。
- **不在 Python 侧复刻 `ScanForObject` 几何做预测**。胜负 margin 实测 1.4%，
  而 collider 真实形状、trace 坐标两位小数精度、浮点噪声都大于这个余量。
  复刻会时对时错，比直接读真实句柄更差。
- **不放宽 `interact_ready` 的出餐口闸**。闸的判断是**正确**的：
  `PlayerControlsHelper.PlaceHeldItem_Client` 只会把东西放到
  `m_iHandlePlacement` 指向的物体上，那一帧它指着出餐口。真按下去锅会塞向
  出餐口（然后 `CanHandlePlacement` 拒绝），**不会**落到台面。放宽闸=制造误放。

## 4. 设计

### 4.1 核心：把「句柄不对」变成可纠正的站位误差

现状是「句柄不对 → FAILURE → 树塌 → 重建 → 同样的位置 → 再塌」，
信息完全丢失。要做的是让它变成「句柄不对 → 往格心收紧 → 重试」。

游戏每帧都在算权威答案，我们只需要在它说「不行」时做一个能改变输入的动作，
而不是放弃。往格心走正是那个动作：§2.3 证明格心对四个交互位都有效。

### 4.2 新原语 `AlignToCell`

```
AlignToCell(target_kind)
  若 interact_ready(chef, target, kind) 成立         → SUCCESS
  若已在格心容差内（|pos - cell_center| <= ALIGN_EPSILON）→ FAILURE（收紧到位仍不行，真不可交互）
  否则 → 朝格心方向短按一步，RUNNING
```

放在 `Face` 与 `Tap` 之间：`Approach → Face → AlignToCell → Tap → Confirm`。

为什么不改 `Approach` 的到位判据（让它一路收到格心）：`Approach` 的判据是
**位置**而非朝向，这是 spec §7.2 的定稿，且注释记着「用朝向敏感的
interact_ready 当到位判据会在面向相邻目标时反复翻转，左右摇摆停不下来
（实机：两个灶之间横跳）」。那条教训不能退。`AlignToCell` 只在 `Face` 之后
介入——此时朝向已稳定，不存在翻转源。

为什么不塞进 `Face`：`Face` 的职责是转向，它的 `arrived` 读 `kind=None`
（通用网格）是对的。混进 placement 语义会让它对「朝向对了但句柄被抢」的
情况反复按方向键转圈（它只会转，不会平移）。

### 4.3 容差

`ALIGN_EPSILON` 取半格的一半 = `cell/4 = 0.3`。依据：t=9523 的偏离恰是
0.30/0.17，而格心处全朝向可用。取 0.3 意味着「偏离超过 1/4 格就先收紧」，
比 §2.3 实测的失效点（0.30）保守一档。

这个值没有第二个实机样本支撑，所以它是**待标定**的（见 §6）。

### 4.4 失败仍要有出路

`AlignToCell` 收紧到位仍 FAILURE 时，当前位置对这个目标确实不可用。此时
应落到既有的 L3 脱困：把 **(目标资源, 落脚格)** 这一对记入
`scratch.bad_cells`，让 `Approach` 下次换格。

注意粒度：不能只拉黑格子（那格对别的目标是好的），也不能只拉黑资源（会把
整个台面废掉）。需要 pair 粒度。现有 `scratch.bad_cells` 是
`Dict[(gx,gz), expire_tick]`，不带目标维度——这是本设计要扩的地方。

## 5. 影响面

`interact_ready(kind="placement"/"pickup")` 的调用点全部受益：
`Tap`、`TapWhileHolding`、`InteractWith`、`EnsureHandsFree`。即所有
19 个 task 的放/取动作。

实机统计（trace-20261016-220409，3964 拍）：`placement` 句柄指向出餐口
且手里有东西共 288 拍，其中 261 拍 P2 在格 (1,-4)。出餐口旁的台面是送餐
路线必经，端着东西路过就可能踩到——不是罕见巧合。

## 6. 待标定 / 风险

- **`ALIGN_EPSILON = 0.3` 只有一个实机样本**。下一局实机后用
  `tools/exec_cost.py` 看 `AlignToCell` 的介入频次与成功率；若频繁介入但
  常常收紧到位仍失败，说明阈值太松（该更早介入）或根因另有其它。
- **收紧动作本身耗时**。一次短按 + 观察 ≈ 8 拍（AGENTS.md §10.1：observe
  至少等 4 拍，插件 5 Hz）。若介入频次高，这是新的时间成本。预期它远小于
  现在「树塌 + 重建」的代价（本局 287 拍彻底卡死），但要测。
- **只验证了 placement 一侧**。pickup 侧（`ScanForObject` 同一次扫描，
  `m_iHandlePickup` 与 `m_iHandlePlacement` 来自同一个 `gameObject2`）理论
  同构，但没有实机样本。
- **`m_bGridSelection` 来自 `GameUtils.GetLevelConfig().m_gridSelection`**，
  是关卡配置。若某关为 false，扫描 B 不走 `GetFacingGridOccupant`，通用
  gx/gz 的语义会变。本关为 true（实测两个字段不一致即证明 B 分支生效）。
  换关卡需复验。

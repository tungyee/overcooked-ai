# 地图与网格表示重构（多网格 + 动态地形 + 去关卡硬编码）

2026-10-15 · 分支 `feat/bt-opt`

## 1. 问题

地图表示建立在三条**在 `Sushi_1_4_2P` 成立、但不是游戏事实**的假设上。换到
`Balloon_1_5_2P` 三条全破，AI 无法完成任何一单。

| 假设 | 游戏事实 | 实测证据 |
|---|---|---|
| 全图一个网格、一个原点 | `QuadGridManager*2`，原点 9.6 与 19.6 | 站点 origin 反解裂成两簇（14 票 / 10 票） |
| 地形静态 | 两块地形周期移动 | 篮子 z 有 34 种状态；左侧占用表 7 种状态 |
| 站点坐标恒定 | 4 个站点的局部格坐标自己在变 | `DynamicGridLocation*4`：3 砧板 + 垃圾桶 |

**后果量化**：以 P1 走过的 601 个位置为 ground truth（人站过 = 必然可走），现有
`world.walkable` 只有 **32.6% 命中（405/601 帧判成墙）**。主要伤害来自
`playable_bounds` 把右半边整个夹掉——它取站点 gx/gz 的极值界定可玩区，而右簇
站点的 gx 因原点算错而全错。

`trace-20261015-191205` 的直接表现：AI 全程跑 `FillCooker`，10 次 FAILURE，
**65% 的拍停在 `observe` 相位**（418/643），cruise 仅 3 拍——一直在错误目标点
附近 2~3 格内反复微调。两个料箱都在移动的篮子上，缓存的坐标从一开始就是错的。

**第四条假设（1-6 调查新增）**：插件把「静态数据何时需要重建」收窄成了
「关卡名字符串是否变了」。`1_6_Dynamic_Lvl_01_2P` 关内有一次「阶段切换」
（沙拉厨房→坠毁→寿司厨房），`level` 字段全程不变，但场上实体结构整体换了一批
（§4.9）。`_staticJson` 只在换关才重建，于是这次切换对 `stations`/`recipes`
完全不可见——AI 在后半局看到的"地图"永远是前半局的快照。

## 2. 根因：抽象层级错位

插件在三处用「名字 / 关卡名」逼近游戏本来就有的语义：

1. **原点靠站点反解**（`CalibrateGrid`）——取第一个 `QuadGridManager`、第一个
   站点就 `return`，多网格下必然只拿到一个。而 `IGridLocation` 接口有
   `get_AccessGridManager`：**每个站点自己就带着归属网格**，一直没读。
2. **地形靠名字前缀**（`AppendWallStations` 匹配 `Col_Floor` / `Col_Walls` /
   `Col_Objects`）——`Sushi_1_4` 命中 41 个，`Balloon_1_5` 命中 **0 个**
   （`[STATIC] built stations=71` vs `=30`）。`Col_` 在整个 `Assembly-CSharp.dll`
   的字符串堆里**零命中**：它是场景实例名，不是代码常量，没有跨关一致的保证。
3. **传送带靠关卡表**（`occupancy.BELTS`）——手标 18 格，未知关卡退化为空集。

游戏自己从不靠名字，它用**层掩码**（`GroundCast.m_landscapeMask`、
`s_staticCollisionLayerMask`）。层名是 Unity 工程配置，跨关稳定。

4. **「是否在关卡内」靠单一类名**（`IsInLevel()` 原实现只认
   `ServerCampaignFlowController`）——`1_6_Dynamic_Lvl_01_2P` 走
   `ServerDynamicFlowController`，场上永远没有前者，`IsInLevel()` 恒为 false，
   整关不进 `Discover()`/`BuildState()`/`Push()`，Python 侧收不到任何帧。
5. **配方表靠三个具体类名枚举**（`BuildStatic()` 硬编码
   `ServerCampaignFlowController`/`ClientCampaignFlowController`/
   `CampaignFlowController` 找 `m_cachedRecipeList`）——同样只覆盖 Campaign
   系列，`recipes` 在 1-6 整局 1851 次重试一次未成功（§4.8）。
6. **静态数据重建靠关卡名**（`RefreshLevel()` 只在 `level` 字符串变化时清空
   `_staticJson`）——1-6 关内阶段切换不改变 `level`，但场上实体整体换了一批
   （§4.9），`stations` 永远停在第一次构建时的快照。

4/5/6 和 1/2/3 是同一类错误的不同实例：**用「某个具体的名字/字符串」逼近
一个本来有通用判据的概念**（归属网格、关卡内、配方表就绪、场上结构是否变了）。
4/5 已在插件侧修复并验证（§4.8），6 的修复方向见 §5.6。

## 3. 目标与非目标

**目标**

- 地图表示对任意关卡正确，新关卡**无需加表、无需改码**。
- 删除全部关卡硬编码：`BELTS`、`PLATE_STACKS`、`Col_*` 前缀、`floor` 扩 bounds、
  「nodeMap 下沿以下放行」、`playable_bounds`。
- 资源 id 在站点移动时保持稳定。
- 可走性感知带 TTL 的临时障碍（落火，§4.7）。
- 静态数据（`stations`/`recipes`）在关内阶段切换时能重建（§4.9、§5.6）。

**非目标**

- **不做「等平台」语义**。实测两半恒连通（§4.3），没有需要等待的局面。
- **不做「等火点熄灭」语义**。落火只占几格，绕路总是可行；且 TTL 短于多数
  绕路成本，等待不划算（§4.7 的取舍，不是「恒不需要等待」的推广）。
- 不改 planner 分层打分、不改 task 契约、不改收益模型。
- 不追求与旧 `.ndjson.gz` fixture 的协议兼容（§9）。
- 不实现传送带（真实运输型）的物料推送模拟——1-6 的 `ConveyorStation` 坠毁后
  才存在，本设计只保证插件把它纳入动态站点枚举，物料是否被推送由游戏自己算，
  Python 侧照常每帧读位置（§4.9 的教训是「别漏报」，不是「要重新实现物理」）。

## 4. 实测依据

全部数据来自一次性诊断补丁（已 revert，不在仓库）+ `tools/record_frames.py` 录的
真实帧。下列每条都是本设计某个决定的唯一依据，改设计前先回来看这节。

### 4.1 两个网格

```
#0 QuadGridManager go="Balloon Basket"  pos=(19.6, 0, -2.4)  size=(1.2,1,1.2)  occupancy=11
#1 QuadGridManager go="GridManager"     pos=( 9.6, 0, -2.4)  size=(1.2,1,1.2)  occupancy=14
```

`m_origin` **两个都是 (0,0,0)**——原点不在字段里，而在 `transform.position`。
两原点差 10.0，格宽 1.2，**10.0 / 1.2 = 8.333 不是整数**：统一全局整数格在
数学上无解。站点世界坐标对 1.2 取余有 5 个相位（x 轴）、3 个（z 轴），要全部
对齐需 0.1 格宽（格数膨胀 144 倍）。**结论：放弃统一格，采用多网格模型。**

### 4.2 两种不同的动态

**右侧篮子 = 整块网格平移。** 11 个站点局部坐标恒定，只有世界坐标变：

```
DispenserCrate 3 (1)  g(2,-1) 恒定   w(22.0, -6.1 … -1.2)   ← 摆幅约 4 格
```

z 轴三个停靠点，x 恒定：TOP 0.0（停 21~22 s）、MID −2.45（仅过路 1 s）、
BOT −4.85（停 23~25 s），单程 2~3 s，周期约 50 s。整条篮子（地板 + 7 面围墙）
同步移动。玩家站上后 `玩家z − 篮子z ≡ −1.1`（537/602 采样）——篮子载人。

**左侧 = 4 个站点在静止网格内移动。** 局部格坐标自己在变：

```
DYN workstation_bin_01                g(-4,-3) → g(-4,-2) → …
DYN countertop_01_chopping_dark_wood  ×3（全部砧板）
```

左侧占用表 7 种状态（格数恒 14），`gx ∈ {−1,−2,−3,−4}` 这一列在 gz 上整列平移：
`0 → +1 → +2 → +3（停 23s）→ … → −3（停 23s）→ …`，每格过渡 1 s，周期约 56 s。

**这一条否证了「(gx,gz) 即身份」**（AGENTS.md §6.3）：砧板的格坐标每 8 秒变一次，
认领键会随之漂移，同一块砧板在移动前后成为两个资源 id。

> 保留的不确定：用户观察到「放盘子的柜台」也在移动。`plate_stack` 无
> `GridLocation` 组件（`gx/gz` 为 None）、AABB 在 `_staticJson` 缓存里，诊断探针
> 看不到它。本设计不依赖该项结论——「坐标一概不缓存」对两种情况都正确。

### 4.3 两半恒连通（这是砍掉等待语义的依据）

```
主地板   Collision (13)  x[ 3.6 .. 15.6]
篮子地板 Collision (6)   x[16.0 .. 23.2]

x 间隙 = 0.4（格宽 1.2 的 1/3）
z 重叠最小值 = 7.0（约 6 格），重叠 ≤ 0 的帧数 = 0 / 270
```

篮子在任何位置都与主地板相邻，从不断开。P1 上下船 5 次，发生在篮子处于 MID
（4 次）和 TOP（1 次）——不限特定停靠点。**所以不需要「等船」。**

附带解释：nodeMap 只有 11×11 覆盖 x 3.6~15.6，而篮子地板到 x=23.2，
**整块右半边从来不在 nodeMap 里**。601 帧里 P1 有 101 帧落在表外。

### 4.4 层号方案（取代名字前缀）

```
9=Ground(3)   10=Walls(19)   11=Worktops(26)   26=TableBlock(24)
22=CookingStationBlock(4)    20=PlateStationBlock(1)
30=KillPlane(12)   8=Players(4)   14=Attachments(7)
```

气球关地形的 GameObject 叫 `Collision (6)` / `Collision (13)` / `Mesh` /
`Worktop` / `Block`——**无一带 `Col_` 前缀**。层名是工程配置，与实例名无关。

`KillPlane`（12 个）当前完全未建模，气球关有坠落风险，本次纳入障碍。

### 4.5 传送带：与手标表零差异

```
Travelator 实例数 18          手标 BELTS 18 格
gz = -3   gx = [-4..4]  9 格连续
gz =  2   gx = [-4..4]  9 格连续
集合完全一致: True
```

方向要用 `GetSurfaceVelocity()`，**不能用 `m_directionXZ`**：后者两排都报
`Rightwards`（局部朝向），真实方向需乘 `transform.rotation`。

| 行 | eulerAngles | GetTravelDirection() | GetSurfaceVelocity() | 手标表 |
|---|---|---|---|---|
| gz = 2 | (0, **180**, 0) | (+1,0,0) | (+2.5,0,0) | `(1,0)` ✅ |
| gz = −3 | (0, **0**, 0) | (−1,0,0) | (−2.5,0,0) | `(−1,0)` ✅ |

手标表是对的，枚举是错的来源。全部 18 格 `speed=2.5`、`grid=GridManager`。
`GetSurfaceVelocity()` 一次给出方向与速率，无需合成旋转——速度是手标表没有的
信息，`navigation` 给逆向通行加代价时可用。

### 4.6 本关不存在的类型（修正早期误判）

```
MovingPlatform*0   LinearPath*0   Travelator*0   GridNavigator*0
```

早期我引用 `MovingPlatform` / `GridNavigator.m_isMoving` 作为「动态可观测」依据，
**那是错的**：这些类在气球关未实例化。真正在动的是
`DynamicLandscapeParenting*11` / `SurfaceMovable*11` / `GroundCast*11`，配
`EmptyLerp*18` + Animator——**平台运动由动画片段驱动，时间线烘在 AnimationClip
里，没有字段可读**。

因此「平台时刻表」读不到。但也不必硬编码：位置每帧可读，规律可在线测量
（本设计不需要，因 §4.3 已排除等待语义）。

### 4.7 落火（1-6）：TTL 16 秒，批量下落，未必严格对齐格

```
FireHazard.m_lifetime = 15（配置值）  实测存活 16s（+1s 颗粒度）
每次事件一次性砸落 7~12 个点，不是逐个随机
```

两局独立验证一致。12 个落点中 11 个精确对齐 1.2 格宽，1 个（z=-3.0）疑似采样到
下落轨迹中的瞬时值——**不能假定落火恒落在格心**，可走性判定要用 AABB/半径，
不能像 §5.1 的站点那样直接取整数格。`FireHazardSpawner.m_alignToGrid` 字段存在，
暗示游戏内部也是按格对齐生成的，但没有在本轮数据里拿到足够样本证实"永远对齐"，
所以协议按半径处理更安全。

**用户纠正（关键）：这类落火不需要、也不能灭火。** 它自带 TTL、到期自动熄灭、
不会扩散——与灶台糊锅着火（无 TTL、不会自己灭、会扩散，必须用 `Extinguish`
处理）是两种不同的机制，不能共用同一套"灭火"语义。所以这里**只影响可走性
（§5.3 第 3 步），不涉及 `Extinguish`／灭火目标**：AI 对落火的正确反应是
"这几格现在不能走，绕开，等 TTL 过了自己通"，不是"去扑灭它"。`tasks/fire.py`
不需要任何改动。

### 4.8 两个新发现的插件 bug，已在本轮修复并验证

**`IsInLevel()` 恒为 false（1-6 整关失效）**。原实现 `FindMB("ServerCampaignFlowController") != null`。
诊断绕开这道闸门直接问场上实况：

```
[GATE] oldIsInLevel=False newIsInLevel=False totalMB=1807
       候选类型: ClientDynamicFlowController,ServerDynamicFlowController,
                 CampaignKitchenLoaderManager,CampaignAudioManager,
                 DynamicFlowController,CampaignAudioComponent
```

场上有 `ServerDynamicFlowController`，没有 `ServerCampaignFlowController`。
修复：`IsInLevel()` 改为 `ReadLevel() != null`（复用已有的、跨两套框架都成立的
`PlayerControls.m_levelConfigBase` 读取）。validated：修复后 `[GATE] 新判据
IsInLevel()=true`，整局正常推帧（17696 行日志 vs 修复前 19 行）。

**配方表 `recipes` 永远为空（`BuildStatic()` 1851 次重试一次未成功）**。
原实现只在 `ServerCampaignFlowController`/`ClientCampaignFlowController`/
`CampaignFlowController` 三个具体类名上找 `m_cachedRecipeList`。元数据反查
确认该字段声明在共享基类 `ClientKitchenFlowControllerBase` 上，且存在专用接口
`IRecipeListCache`（`GetCachedRecipeList`/`GetCachedAssembledRecipes`/
`GetCachedCookingSteps`）。诊断验证：

```
[RECIPE-FIX] iface命中 type=ClientDynamicFlowController GetCachedRecipeList.count=115
```

修复：按接口 `IRecipeListCache` 查找，不枚举具体类名。validated：修复后
`stations=22 recipes=115` 全程稳定（551 帧）。

两处修复已应用到 `plugin/Oc2AIPlugin.cs`（本文档写作时尚未提交，随本设计
一并进实施计划的第一步——这两条独立于地图重构本身，可以单独先行合入）。

### 4.9 关内阶段切换：实体整体换一批，`level` 不变，静态数据不跟着重建

1-6 wiki 描述：「前半段在气球上做沙拉，气球坠毁后落地进入寿司餐厅，后半段做
寿司」（来源：overcooked.fandom.com）。本轮用全程「每秒全局类型计数 diff」
抓到了切换瞬间（坠毁时刻不固定，两局分别在 ~45s / ~131s，疑似玩家行为触发
而非定时）：

```
t=127 (坠毁前)                      t=128 (坠毁后，同一帧内)
ClientConveyorStation:            0  ->  27
ClientConveyorBeltCosmeticDecisions: 0 -> 27
ClientCookingStation:             0  ->  2
ClientWashingStation:             1  ->  2
ClientTabletopConveyenceReceiver: 9  ->  53
StaticGridLocation:               10 ->  65
RendererInfo:                     24 ->  863
```

右侧寿司厨房的全部组件（含真正的传送带 `ConveyorStation`——不是沙拉厨房那种
静止的 `TabletopConveyenceReceiver`）在坠毁瞬间才被 Unity 实例化。但：

```
level 字段：901 帧全程恒为 "1_6_Dynamic_Lvl_01_2P"（没有变化）
stations 数量：901 帧全程恒为 22（没有变化）
[STATIC] built stations=22 recipes=115（只打印了一次）
```

`RefreshLevel()` 只在 `level` 字符串变化时清空 `_staticJson`，而这次切换
`level` 不变——`_staticJson` 永远停在坠毁前的快照，右侧厨房（含传送带、新灶、
新洗碗池）对 Python 侧完全不可见。这解释了用户观察到的「传送带载物料」现象
为什么前几轮录帧都没抓到：**真正的传送带坠毁前根本不存在**，前几局观测窗口
里能摸到的只是静止的 `TabletopConveyenceReceiver`。

元数据里有专门描述阶段的类型，可用于通用检测（不是只对 1-6 管用的信号）：

```
DynamicRoundData.Phases              GetCurrentPhaseDuration()  GetNextRecipe()
DynamicRoundInstanceData.CurrentPhase
```

本设计不实现「阶段感知」的完整语义（那是游戏流程层面的概念，超出地图表示的
范围），只要求 §5.6 的 staleness 检测能让 `_staticJson` 在结构变化后自愈，
不依赖 `DynamicRoundInstanceData` 这个信号本身。

## 5. 核心设计

一条原则贯穿全部改动：**身份与位置彻底分离**。身份永久稳定，几何每帧重取。

| 概念 | 来源 | 稳定性 |
|---|---|---|
| 站点身份 | 实例名 + 归属网格名 | 永久 |
| 站点位置 | 每帧 `transform.position` | 每帧可变 |
| 可走性 | 每帧层号扫 collider + `m_gridOccupancy` | 每帧可变 |

这样 §4.2 的两种动态是同一机制的自然结果，**不需要区分「平移型」与「格内移动型」**。

### 5.1 坐标系：多网格，局部坐标为权威

格坐标一律是 `(grid_id, gx, gz)` 三元组。世界坐标是派生值：

```
world_xz = grid_origin[grid_id](每帧读) + (gx, gz) × cell
```

- 局部 `(gx, gz)` 对篮子站点恒定（§4.2），对左侧 4 个动态站点会变——两者都由
  「每帧重取」覆盖。
- `grid_origin` 取 `QuadGridManager.transform.position`，**不是 `m_origin`**
  （后者恒为 0，§4.1）。
- 删除 `geometry.py` 的众数校准与 `ORIGIN_X_DEFAULT = 14.4` 兜底常量：原点由
  插件直接给出，不再反解。

**为什么不用统一全局格**：§4.1 证明无解（8.333 格，非整数）。

### 5.2 资源 id：实例名 + 网格名

```
resource_id = (grid_name, instance_name)
```

替换现行三套规则（站点用 `n`、拼装格用 `(gx,gz)`、盘用 `n`）中的前两套。解决：

- **同名撞车**：左右各一个 `countertop_01_standard_dark_wood (4)`（x=10.8 与
  x=17.2），现行规则下共享认领键，会互相误锁。
- **坐标漂移**：砧板的 `(gx,gz)` 每 8 秒变一次（§4.2），用它当身份会让认领、
  承诺惯性、`abandoned` 拉黑全部错乱。

拼装格（counter 落点）仍需按位置区分同一张台面的不同格位时，用
`(grid_name, instance_name, slot_index)`，其中 slot 由 AABB 内的格序号派生——
**但 slot 不参与跨帧身份比较**，因为台面本身可能移动。当前关卡每张台面一格，
`slot_index` 恒为 0；多格台面留待有样本时再定（见 §10 待办）。

### 5.3 可走性：层号 + 占用表，每帧重算

判定顺序（替换 `make_walkable` 现行五条优先级）：

1. 格中心不在任何 **Ground(9)** AABB 内 → 不可走（没地板站不住）
2. 格中心在 **KillPlane(30)** AABB 内 → 不可走（坠落区，新增）
3. 格在任意存活的 hazard AABB/半径内 → 不可走（落火，新增，见 §5.7/§4.7）
4. 格被 **Walls(10) / TableBlock(26) / CookingStationBlock(22) /
   PlateStationBlock(20)** AABB 覆盖 → 不可走
5. 格在 `m_gridOccupancy` 里 → 不可走（运行时占用，权威）
6. 否则可走

**nodeMap 退为校验用途，不再参与判定**：它 11×11 覆盖不全（§4.3）、关内从不
更新（601 帧零变化）。保留解析是为了在测试里对比「层号方案 vs nodeMap」的差异，
不进 `walkable`。

删除随之失去依据的三处补丁：`floor` 扩 bounds、「下沿以下放行」、
`playable_bounds`（可玩区由 Ground AABB 并集直接给出）。

### 5.4 传送带：`Travelator` 数据驱动

`occupancy.BELTS` 整表删除。插件输出每格 `(grid_id, gx, gz)` +
`GetSurfaceVelocity()`（含方向与速率，§4.5）。`belt_dir` 由速度向量归一化得到，
新增的 `belt_speed` 供 `navigation` 逆向代价使用（当前可先忽略速率，保持行为不变）。

### 5.5 `_staticJson` 拆分

现行缓存把真静态与动态混在一起，是 §1 全部症状的放大器。拆成两段：

| 留在静态缓存（换关才重建） | 移入每帧动态 |
|---|---|
| 配方表 `recipes` | 站点坐标 `x/z`、`gx/gz` |
| 箱子产出 `ing` / `chop` / `chop_stages` | collider AABB |
| 站点身份 `n` / `t` / `grid` | 占用表、传送带速度 |

### 5.6 静态数据 staleness 检测（修 §4.9）

`RefreshLevel()` 只在 `level` 字符串变化时清空 `_staticJson`，这对「同关内
阶段切换」（§4.9）失效。改成按**结构信号**触发重建，不依赖关卡名：

- 每帧廉价地数一次场上 MonoBehaviour 总数（`_all.Length`，已经在扫，不是新开销）
- 若与上次 `BuildStatic()` 时记录的总数相差超过阈值（建议 10%，气球关坠毁时
  变化幅度是数量级的，远超噪声范围），标记 `_staticJson = null` 触发重建
- 重建不只追加，而是整体重算——坠毁后旧厨房的站点可能同时失效（本轮未观测到
  1-6 旧站点消失，但不能假设换阶段只会新增不会减少）

这是本设计里**唯一**没有被 §4 数据直接验证到「阈值取多少最合适」的参数（变化
幅度本身已验证：22→65+ 的 `StaticGridLocation` 跳变远超任何合理阈值）。10%
是保守选择，实施时用两局 1-6 数据回放调整。

### 5.7 临时障碍（落火）纳入可走性——仅此而已，不碰灭火

判定顺序已并入 §5.3（第 3 步）。Hazard 没有实例名（它是生灭的，不是移动的），
资源 id 用 `(grid_name, gx, gz)`——这是 §5.2「实例名+网格名」规则的例外：
火点没有稳定实例可认领，身份只能是「这一刻在这个格子上着火」，TTL 到期后
同一个 id 不再存在，不需要跨帧保持。

**不扩展 `Extinguish`**（修正早先的错误设计）。两种"火"是不同机制，不能共用
语义：落火自带 TTL、到期自动熄灭、不扩散（§4.7）；灶台糊锅着火无 TTL、不会
自愈、会扩散（`ai/tasks/fire.py` 现行逻辑针对的正是后者）。AI 对落火的正确
反应是「绕开，等它自己过去」，不是「跑去扑灭」——`claims`/`feasible` 等
task 契约、`Extinguish.build()` 的执行树都不改。落火只通过 §5.3 的可走性
拒绝进入格子，`Approach`/`navigation` 自然绕路，不需要新 task 或新目标源。

## 6. 传输协议变更

```jsonc
{
  "grids": [                        // 新增：每帧
    {"id": "GridManager",     "ox": 9.6,  "oz": -2.4, "cell": 1.2},
    {"id": "Balloon Basket",  "ox": 19.6, "oz": -2.4, "cell": 1.2}
  ],
  "stations": [                     // 身份静态，几何每帧
    {"t": "crate", "n": "DispenserCrate 3 (1)", "grid": "Balloon Basket",
     "gx": 2, "gz": -1, "x": 22.0, "z": -3.6,
     "minx": ..., "maxx": ..., "minz": ..., "maxz": ...,
     "ing": "Pasta", "chop": null, "chop_stages": null}
  ],
  "terrain": [                      // 新增：按层分组的 AABB，每帧
    {"layer": 9,  "boxes": [[3.6, 15.6, -8.6, 3.4], ...]},
    {"layer": 10, "boxes": [...]},
    {"layer": 30, "boxes": [...]}
  ],
  "occupancy": [                    // 新增：每帧，按网格
    {"grid": "GridManager", "cells": [[1, 0], [2, 0], ...]}
  ],
  "belts": [                        // 替代 occupancy.BELTS
    {"grid": "GridManager", "gx": -4, "gz": 2, "vx": 2.5, "vz": 0.0}
  ],
  "hazards": [                      // 新增：落火（§4.7/5.7），无实例名
    {"t": "fire", "grid": "GridManager", "x": 13.2, "z": -2.4, "ttl": 11.0}
  ]
}
```

`map.grid.nodeMap` 保留（校验用），`bounds` 字段删除。

**体积估算**：气球关 terrain 约 20 个 box、occupancy 约 25 格、belts 0 条，
相比现有帧（约 8 KB）增加约 1 KB。5 Hz 下无压力。1-4 有 18 条 belts、
71 个站点，增量相近。1-6 坠毁后 `stations` 从 22 跳到约 65（§4.9），单帧体积
相应跳升，仍在 5 Hz 可接受范围（量级与 1-4 的 71 站点相近）。

## 7. Python 侧改动

| 模块 | 改动 |
|---|---|
| `geometry.py` | `GridMapping` → `GridSet`（多网格）；删除众数校准与兜底原点 |
| `occupancy.py` | 删 `BELTS`、`playable_bounds`；`make_walkable` 按 §5.3 重写；新增 `terrain_blocked_cells` |
| `world.py` | 删 `floor` 扩 bounds；坐标访问走 `GridSet`；暴露 `grids` / `terrain` / `occupancy` |
| `stations.py` | 删 `PLATE_STACKS` 兜底表；`RELAY_CELLS` 已空置（注释保留），一并清掉表结构 |
| `navigation.py` | 距离场跨网格：相邻判定按世界坐标距离 ≤ cell，而非格差 1 |
| `tasks/base.py` 等 | 资源 id 改 `(grid, name)`（§5.2）；`tasks/fire.py` 不改（§5.7） |
| `placement.py` | 落点约束按新 id 与每帧坐标 |
| `state_dump.py` | trace 落 `grids` / `terrain` / `hazards` 摘要，便于复盘 |
| `primitives/base.py` | `target_cell`/`_dir_towards` 改按世界坐标索引，不返回裸 `(gx,gz)`——多网格下同一格坐标在不同网格里会撞车（§4.1 的 (2,-1) 实例） |
| `primitives/move.py` | `Approach` 的 reset 判据从「目标格坐标变化」改为「目标身份变化」（§5.2 的 id）；平台移动时同一站点的坐标会变，但身份不变，不应清空 `MotionControl` 已有的巡航进度 |

`navigation` 那条是唯一的新算法：两网格边界格世界距离 0.4（§4.3），小于 cell
1.2，所以按世界距离判相邻时自然连通，无需特例。

**插件侧**（`plugin/Oc2AIPlugin.cs`）：
- `IsInLevel()` 改用 `ReadLevel() != null`（§4.8，已验证）
- `BuildStatic()` 配方表查找改用 `IRecipeListCache` 接口（§4.8，已验证）
- `RefreshLevel()` 加结构变化检测触发 `_staticJson` 重建（§5.6，待实现）
- `AppendStaticStations`/`AppendWallStations` 按层号重写（§5.3）
- 新增 `AppendHazards`（§5.7）、`AppendGrids`/`AppendOccupancy`（§5.1/5.3）
- `Travelator` 替代 `BELTS`（§5.4）

## 8. 测试策略

**先写测试**（项目实践是 TDD）。关键是 §4 的实测数据可直接转成断言：

1. **ground truth 回归**（最重要）：P1 走过的 601 个位置必须全部 `walkable`。
   当前 32.6%，目标 100%。这条测试直接钉住 §1 的核心缺陷。
2. **传送带等价**：`Travelator` 推导出的格集合与方向 == 旧 `BELTS` 表
   （§4.5 已验证 18/18 一致）。这是删表的安全网。
3. **资源 id 稳定性**：构造砧板移动前后两帧，断言资源 id 不变、认领不漂移。
4. **多网格坐标**：篮子站点在 TOP/MID/BOT 三个位置下，局部坐标恒定、世界坐标
   按原点偏移。
5. **跨网格寻路**：主厨房任一格 → 篮子任一格可达，且在篮子三个位置下均可达。
6. **KillPlane**：坠落区格不可走。
7. **Hazard 可走性**：落火格不可走，TTL 到期后格复活（§5.7）；`Extinguish`
   的 `claims()`/`feasible()`/`build()` 不读 hazard，行为与改造前一致
   （回归断言，钉住"不碰灭火"这条决定）。
8. **Approach 跨拍不丢进度**：构造同一站点身份、坐标变化（模拟平台移动）的
   连续两帧，断言 `MotionControl` 状态保留，不因坐标变化触发 reset（§5.4 的
   `primitives/move.py` 改动）。
9. **staleness 检测**：构造 MonoBehaviour 总数跳变超过阈值的两帧（模拟 1-6
   坠毁），断言 `_staticJson` 被标记重建（§5.6）。这条测试在插件侧（C#），
   不在 Python 测试套件里。

新 fixture 用本轮录的帧（`Balloon_1_5_2P` 502 帧 + 1-4 带传送带的帧 +
`1_6_Dynamic_Lvl_01_2P` 坠毁前后各一段），压成 `.ndjson.gz` 入库。

## 9. 迁移与兼容

**旧 fixture 处理**：实际入库的真实帧只有
`tests/fixtures/motion/trace-stall-20261010.ndjson.gz`（3.8 KB，移动控制专用）。
`full_level.ndjson`（108 MB）**未入库**（`.gitignore:16` 排除），是本地文件。
因此「旧 fixture 大面积失效」不成立——`conftest.py` 的 `frames` fixture 在
`.ndjson.gz` 缺失时已走 skip 路径（当前 78 skipped 即此）。

实际动作：

- `tests/fixtures/motion/` 那份只喂 `motion` 相关测试（不含地图判定），**保留**。
- 新增 `tests/fixtures/balloon_1_5.ndjson.gz`（本轮 502 帧）与
  `sushi_1_4_belts.ndjson.gz`（1-4 传送带帧），作为新的地图回归基线。
- `test_parity.py` / `test_world_parity.py` 当前已因无帧而 skip，改造后按新
  协议重建其地图部分断言。

**插件与 Python 必须同步部署**：协议破坏性变更。本轮已踩过一次——部署的 DLL
比源码旧 20 小时，导致 `wall`/`floor` 缺失、诊断结论一度错误。实施时先改插件、
录新帧、再改 Python。

**`IsInLevel()`/配方表两处修复可独立先行合入**：它们不依赖本设计的任何协议
变更，是纯 bug fix（§4.8）。写作本文档时改动在工作区、未提交。建议作为实施
计划的第一个任务单独提交，和地图重构解耦——这样 1-6 的基本可玩性不用等整个
重构完成。

## 10. 风险与待办

| 风险 | 缓解 |
|---|---|
| 每帧扫全部 collider 的开销 | 气球关约 100 个 collider、5 Hz。若掉帧则按层缓存静态项（Walls 中 11/19 静止），只重算移动的 |
| `navigation` 跨网格相邻判定引入回归 | 测试 5 覆盖；两网格间隙 0.4 << cell 1.2，无边界歧义 |
| 多格台面的 `slot_index` 未定 | 当前关卡每台面一格，恒为 0；有样本再定（§5.2） |
| `plate_stack` 是否移动未确认 | 「坐标不缓存」对两种情况都正确（§4.2 注） |
| 落火是否严格对齐格未 100% 确认 | §4.7：11/12 对齐，1 个样本存疑；协议按 AABB/半径处理，不假设格对齐 |

**实施记录（Task 7/8）**：spec 原计划的"结构跳变阈值检测"（§5.6）在
实施时被更根本的修法取代——stations 坐标本不该被缓存（它们属于§5.5
"移入每帧动态"一侧），直接让 AppendStations 每帧重扫 _all，和
cookers/plates/items 的现行做法一致。这同时解决了 §4.9 的问题，不需要
任何跳变阈值。recipes 仍按换关重建（§4.9 实测其内容不随阶段切换变化，
无需改动）。

**待办（不在本次范围）**

- 平台时刻表在线估计 + 「等平台」语义：§4.3 证明当前不需要，但若后续关卡出现
  真正断开的平台，这是正确的扩展点。届时在线测量（§4.6）优于硬编码。
- `nodeMap` 与层号方案的差异审计：保留 nodeMap 解析正是为此。
- 传送带速率进入 `navigation` 代价：数据已有（`speed=2.5`），当前先不改行为。
- **真正的传送带（`ConveyorStation`）物料推送建模**：1-6 坠毁后出现的传送带
  会推动台面上的物料（§4.9），本设计只保证插件把这类站点纳入动态枚举、Python
  侧每帧读到正确坐标；是否需要专门的「料在传送带上会移动」谓词（例如让
  `FetchToPlate` 的目标跟着料走）留待下一轮——需要先在传送带存在的窗口内录到
  物料实际移动的帧，本轮录制窗口只覆盖到传送带刚生成，没有录到推送行为本身。
- `DynamicRoundInstanceData.CurrentPhase` 是否可作为比「全局类型计数变化超过
  阈值」更精确的 staleness 信号：§4.9 发现了这个字段但本设计未采用（§5.6 选了
  更通用、不依赖该类型是否存在的检测方式），值得在下一关卡验证后重新评估。

# 分层打分设计（Planner 仲裁机制改造）

2026-10-15 · 分支 `feat/planner-ai`

## 1. 问题

`ai/planner.py` 的 `_score_candidate` 把 17 项加权求和成一个标量，用它排序全部候选。
跨类优先级因此由一组浮点数的相对大小维持，而那组数是一个**手工解**。

权重注释里记的全是**序关系约束**，不是数值依据：

- `W_COOKER_IDLE`：「必须压过备菜的实际合计，但不能压过端刚熟的锅」
- `W_NO_PLATE_PLACED`：「要压过备菜与填灶，但不能压过快糊的锅」
- `W_DELIVER_BONUS`：「要压过 async+completion，但低于快糊锅的 pressure」
- `W_COMBO_HEAD`：「必须显著高于新鲜度项」

n 项加权和有 O(n²) 对交互，单组测试结构上查不出其中任何一对。后果已经发生
（AGENTS.md §10.2）：urgency 从 `W_URGENCY/remaining`（中段恒 ~0.1）改成读 slack
（中段 3~4 分）后，另一组验证过的七个「灶空转该填灶」的拍**全部回退**，982 个
测试一个都没报警。约束本身一个字没变，只是那组解失效了。

同一性质的另三处后果：`cf2fc64` 修的手写加法（16 项重抄一遍、漏掉求和那处的症状
是「trace 里有这一项但它不影响选择」）、`W_DEADLINE_LOOKAHEAD = 10.0` 定义后从未
被引用（实际量级 1.0，写本节时尚未修——本轮随后用 `a883ba8` + `0213d06` 补上了
权重与锅查找，它现在是 L6 白名单内的真实项，见 §7.1）、`W_DELIVER_BONUS` 4.0→3.0
同时在调「类别优先级」和「改选频率」两件事。

## 2. 目标与非目标

**目标** —— 让跨类优先级由控制流隔离保证，不再依赖浮点数的相对大小。

**非目标**：

- 不改任何单项曲线的形态。`_completion_bonus` 的凸性、`_time_pressure` 的分段、
  `_credited_order_id` 的 combo 建模（55/0 实测）、`_urgency_from_slack` 的分段线性
  全部原样保留。
- 不动 BT 引擎、不动 Task 四元身份契约、不动 `recipe_graph` 的发射逻辑。
- **不动承诺惯性**。分层管「挑哪个」，抢占仍由 `COMMITMENT_MARGIN` 与
  `pressure >= 1.0 跳过惯性` 负责，`_apply_commitment_inertia` 一行都不改（见 §8）。
- 不引入目标层 / 选项链（那是独立的后续工作，见 §9）。

## 3. 方案选型

三个候选，差别在「跨类优先级靠什么保证不被顶翻」：

| 方案 | 保证机制 | 还会静默回退吗 | 要重标权重吗 | 丢信号吗 |
|---|---|---|---|---|
| 软分层（层给大偏移量，仍是单一标量） | 大常量的余量 | **会**（余量被吃穿时） | 不用 | 不丢 |
| **混合（层间词典序，层内加权和）** | **控制流隔离** | 层内可能，跨层不会 | 层内要微调 | 不丢 |
| 纯词典序（层内也只比单一主键） | 单一主键 | 不会 | 基本全删 | **丢**完成度/复用/combo |

**选混合。** 软分层只是把问题推后——层内分数上限 < 层间间距这个保证没有任何机制
强制，跟今天靠 `4.8 < 5.0` 维持顺序是同一回事，只是余量从 0.2 换成 100。某层加了
第 18 项、或某项曲线从 0~1 改成 0~3（`_completion_bonus` 就这么改过），穿透就回来，
而且仍然是静默的。

纯词典序会扔掉有实测依据的次要信号：完成度（`37a0edc` 改凸曲线解掉的「五张盘各做
一半全部烂尾」）、食材复用、combo 偏好。

混合方案下，跨层候选**根本不进同一次比较**——有救锅候选时备菜候选压根没参与排序。
这不是「余量够大」，是「没有这条通路」。

## 4. 层序

### 4.1 判据

**有倒计时的压过没倒计时的，倒计时短的压过长的。**

| 层 | 名称 | 正在流失的东西 |
|---|---|---|
| L0 | 火情 | 灶在烧，会蔓延 |
| L1 | 不可逆损失 | 锅快糊/已糊：料会毁、还要灭火 |
| L2 | 救料 | 刚熟的锅：12 秒不可压缩的时钟在走 |
| L3 | 兑现 | 订单寿命 + 立即到账的 60 分 base |
| L4 | 解卡 | 产能（拼装链无落点，但洗完就恢复） |
| L5 | 异步产能 | 灶空转的机会成本 |
| L6 | 备菜 | 无时钟 |
| L7 | 清理 | 无时钟 |

这条原则比「哪个权重大」好论证，也给以后加 task 留了依据：新 task 问一句
「它在抢救什么正在流失的东西」就知道该进哪层。

### 4.2 层内容

```
L0 火情        Extinguish
L1 不可逆损失   锅快糊/已糊（_time_pressure >= 1.5）
L2 救料        刚熟的锅（_time_pressure 1.0~1.5）
L3 兑现        DeliverPlate（盘料齐，feasible 已保证）
L4 解卡        WashPlate / PlaceCleanPlate（_plate_pressure > 0）
L5 异步产能     FillCooker（shortfall > 0）
L6 备菜        FetchToPlate / Chop / FetchToBoard / FetchToCooker / Mix / PlaceToRelay
L7 清理        StashHeld / Discard / ReturnPot / RecoverLoosePot / ResetPlate / Prefetch
             + WashPlate / PlaceCleanPlate（盘充足时）
```

### 4.3 两处定过的边界

**兑现 > 产能（L3 > L5）。** 依据是实测分布：4577 拍里「料齐的盘」0 盘占 4279 拍、
1 盘占 298 拍——从没有两盘同时料齐。所以 L3 几乎永远至多一个候选，而 `DeliverPlate`
实测 50 拍。它堵不住填灶，而 60 分 base 立即到账。

反向排法（填灶 > 兑现）的风险：开局三口空灶 + 5 单要米时，L5 里堵着三个
`FillCooker`（每个实测 120 拍），料齐的盘要等 360 拍。这正是
`COOKER_SHORTFALL_SATURATION` 当年要防的局面。

**救料 > 解卡（L2 > L4）。** 两者大多数情况下不冲突：`TakeCookedToPlate` 的身份里
绑着 `plate_id`，`feasible` 要求那只盘已存在且 `plate_lacks` 为真——它用的是已匹配给
订单的盘，不是干净盘堆里的盘。而 `WashPlate` 补充的是 `clean_plate_count`。0 干净盘
时救料照样可行，只要那只绑定盘还在台面上等着接饭。

真冲突只在「有一只就位的盘能接饭，同时干净盘只剩 0~1 块」。此时救料优先：锅上时钟
在走，`TakeCookedToPlate` 实测 125 拍；洗盘 150 拍、产出的是「下一单的盘」，没有任何
东西在倒计时。先救料再洗盘损失 125 拍，先洗盘再救料让那锅多烧 150 拍——而
`_time_pressure` 的曲线说明 ratio 从 0.5 到 1.0 只有 12 秒（240 拍），150 拍吃掉大半。

**不要误用 `W_NO_PLATE_PLACED` 的定标数据反驳这条。** 那组数据（W=5.0 时救锅保住
0 拍）测的是「摆盘压过 progress=23/24 的快糊锅」，对应现在的 L1——分层后 L1 > L4
已经保证了它。同一组注释里「W=3.0 下刚熟的锅让位摆盘」描述的是当时的既有行为，
从未验证过「让刚熟的锅等 170 拍」是不是好交易。按时钟推理，不是。

## 5. 层判定

新模块 `ai/planner/layers.py`。纯函数、无副作用、只读 world/bb。

```python
L_FIRE, L_IRREVERSIBLE, L_RESCUE, L_REDEEM = 0, 1, 2, 3
L_UNBLOCK, L_CAPACITY, L_PREP, L_CLEANUP = 4, 5, 6, 7

PRESSURE_IRREVERSIBLE = 1.5    # ratio 0.75：烧糊路程走了四分之三
PRESSURE_RESCUE = 1.0          # ratio 0.50：刚熟

def layer_of(task, world, bb, ctx):
    t = task.TYPE
    if t == "Extinguish":                  return L_FIRE
    p = _time_pressure(task, world)        # 沿用现函数，0~2.0
    if p >= PRESSURE_IRREVERSIBLE:         return L_IRREVERSIBLE
    if p >= PRESSURE_RESCUE:               return L_RESCUE
    if t == "DeliverPlate":                return L_REDEEM
    if t in ("WashPlate", "PlaceCleanPlate") and _plate_pressure(world) > 0:
                                           return L_UNBLOCK
    if t == "FillCooker":                  return L_CAPACITY
    if t in LAYER_PREP_TYPES:              return L_PREP
    return L_CLEANUP

LAYER_PREP_TYPES = ("FetchToPlate", "FetchToBoard", "Chop",
                    "FetchToCooker", "Mix", "PlaceToRelay")
```

上面是语义的参考写法。**实际实现要把层序做成数据表**，见 §5.1。

**`LAYER_PREP_TYPES` 不是现有的 `_COMBO_PREP_TYPES`**（`planner.py:530`），两者
必须分开：后者含 `TakeCookedToPlate` 与 `PlaceCleanPlate`（它们吃 combo 偏好），
而这两个在分层后分属 L2 与 L4，不在备菜层。名字刻意取得不相近，避免误用。

按顺序短路，第一个命中即为层。三个要点：

**复用两条已定标的曲线当层阈值。** `_time_pressure` / `_plate_pressure` 不改一行，
只是输出从「乘权重」变成「切层」。`PRESSURE_RESCUE = 1.0` 正是
`planner.py:1542` 今天跳过承诺惯性用的同一个阈值，语义一致。

**层判定只认 `_time_pressure` 认的那三个 task**（`TakeCookedToPlate` /
`RescueCooked` / `Discard`，见 `planner.py:1360`）。别的 task 该函数返回 0.0，
自然落不进 L1/L2。现有行为，不动。

**`WashPlate` / `PlaceCleanPlate` 在盘充足时掉到 L7**，不留在 L4。符合「没有正在
流失的东西」的原则，也避免盘够用时还去洗盘占住解卡层。

一处已知的交互，这次不碰：`Discard` 吃灶压力，所以倒糊锅会进 L1/L2（对的——糊锅
占着灶，产能在流失）。但 AGENTS.md §10.1 记着「`Discard.feasible` 要求灶还在烧，
火灭后的糊锅没人倒」，那是 feasible 的问题，不是层的问题。

### 5.1 层序是数据，不是控制流

`layer_of` 不写成 `if` 链，而是遍历一张可替换的规则表，取第一个命中：

```python
LAYER_RULES = (
    (L_FIRE,         lambda t, w: t.TYPE == "Extinguish"),
    (L_IRREVERSIBLE, lambda t, w: _time_pressure(t, w) >= PRESSURE_IRREVERSIBLE),
    (L_RESCUE,       lambda t, w: _time_pressure(t, w) >= PRESSURE_RESCUE),
    (L_REDEEM,       lambda t, w: t.TYPE == "DeliverPlate"),
    (L_UNBLOCK,      lambda t, w: t.TYPE in _PLATE_TYPES and _plate_pressure(w) > 0),
    (L_CAPACITY,     lambda t, w: t.TYPE == "FillCooker"),
    (L_PREP,         lambda t, w: t.TYPE in LAYER_PREP_TYPES),
)

def layer_of(task, world, rules=LAYER_RULES):
    for layer, pred in rules:
        if pred(task, world):
            return layer
    return L_CLEANUP
```

两条约束：

**表可替换，谓词不可替换。** 调用方可以传入改了**层号映射**或删掉若干条的
`rules`（这是后续 LLM 策略层的接入点，见 §9.1），但谓词本身是实测定标过的
曲线，不开放给策略改。

注意可替换的是层号映射与规则集合，**不是行顺序**：`layer_of` 返回命中规则
自带的层号，所以重排行顺序不改变任何结果。要让填灶压过送餐，得传一张把它
映射到更高层号的表（`tests/test_planner_layers.py::test_rules_table_is_replaceable_by_remapping_layers`
是这个用法的例子）。

**`layer_of` 保持纯函数，不读 `bb.intents`。** 策略覆盖由调用方决定传哪张表，
层判定本身只认 `(task, world)`。这样它可以脱离黑板单测，也不会让「当前策略」
悄悄渗进一个看起来是纯查询的函数。

成本几乎为零，但事后抽要动所有调用点，所以本轮就这么写。

## 6. 选择流程

`plan()` 的 ③~⑤ 之间插一步，改动集中在一处：

```python
# ③ 可行性过滤（不变）
feasible = [...]

# ③.5 分层：取最高层，只有该层候选进入打分
layers = {c.task.key: layer_of(c.task, world) for c in feasible}
top_layer = min(layers.values())              # 数字小 = 层高
in_layer = [c for c in feasible if layers[c.task.key] == top_layer]

# ④ 打分（只对 in_layer，且只算该层白名单内的项）
scored = _score_tasks(in_layer, ..., layer=top_layer)

# ⑤ 承诺惯性：一行都不改
chosen = _apply_commitment_inertia(scored, world, bb)
```

**trace 记全量。** `record_cands` 仍遍历**全部**候选，每条多两个字段：`layer` 与
`in_top_layer`。这样「为什么没选救锅」在 trace 里能一眼区分「被否决」与「层不够高」
——分层最大的可观测性收益，不能丢。`trace.py` 的 `_format_candidates` 同步加两列。

## 7. 层内打分白名单

不是所有 17 项都参与每一层。每层一张白名单：

| 层 | 参与的 terms | 排序含义 |
|---|---|---|
| L0 火情 | 无（单候选） | — |
| L1 不可逆 | `pressure` | 越接近糊越先救 |
| L2 救料 | `pressure` | 同上（纯词典序，见下） |
| L3 兑现 | `freshness`、`combo_head`、`combo_break`、`urgency` | 送哪种菜（combo 是这层主角） |
| L4 解卡 | `plate_pressure`、`no_plate_placed` | 0 盘 > 无盘就位 > 1 盘 |
| L5 产能 | `cooker_idle`、`eta` | 缺口大的先填，近的先填 |
| L6 备菜 | `urgency`、`completion_ratio`、`async_resource`、`ingredient_reuse`、`combo_*×0.25`、`deadline_lookahead`、`hands`、`move_penalty`、`eta`、`relay_bonus`、`p1_conflict` | 现有大部分项都在这层 |
| L7 清理 | `eta`、`hands` | 就近 |

**L2 只留 `pressure`。** 锅的时钟不管盘满不满——盛饭这个动作的紧迫度完全由锅决定，
`completion_ratio` 在这里没有发言权。只留一项让 L2 退化成纯词典序（按 pressure
降序），最可预测。

**关键性质：`W_*` 之间不再需要跨类可比。** `W_COOKER_IDLE` 只跟 `W_ETA` 比（同在
L5），`W_ASYNC_RESOURCE` 只在 L6 内部有意义。约束表里的跨层断言因此从「分数比较」
变成「层比较」，层内断言保持分数比较。

### 7.1 逐项处置

**删（恒等加常数的类别标记，且该层白名单内无需与之可比的项）**

- `W_DELIVER_BONUS = 3.0` —— 实现是 `if TYPE == "DeliverPlate": terms[...] = 3.0`，
  对 L3 全部候选加同一常数，恒等变换。它原本在调两件事（类别优先级 + 改选频率，
  见 4.0→3.0 那段注释），现在类别优先级归层序、改选频率归 `COMMITMENT_MARGIN`。
  删掉它是解开一处耦合，不只是省一项。

**曾打算删，实现过程中复活（见下）**

- `W_DEADLINE_LOOKAHEAD = 10.0` —— 本节原计划说它「从未被引用、顺带清掉」，
  但那正是本轮顺带修的 bug（`a883ba8`：`_score_candidate` ⑩ 补上了
  `*W_DEADLINE_LOOKAHEAD`）；同一轮又把它读的锅查找从恒 `None` 的
  `bb.cooker_assignments` 改成实际可用的 `cooker_match`（`0213d06`）。两处修完后
  它不再是零调用，语义收窄为「这一步会不会错过锅烧糊的窗口」（订单到期那支已被
  Ruling 10 移除，见下）。结论改为**保留**，在 L6 白名单内（§7 表格）。

**保留为恒等缩放（曲线标尺，层内唯一项）**

- `W_PRESSURE = 5.0` —— L1/L2 内唯一项，系数不改变排序。保留是因为它同时是
  `_time_pressure` 的量纲声明，删掉会让该曲线失去标尺。注释改为「仅 L1/L2 层内
  有效，当前为恒等缩放」。

**保留并需在层内重标（曲线标尺，层内有同伴）**

- `W_PLATE_PRESSURE = 5.0` 与 `W_NO_PLATE_PLACED = 3.0` —— 同在 L4，必须可比。
  原值是为了跟备菜/填灶可比而定的，现在只需表达「0 盘洗盘 > 无盘就位摆盘 >
  1 盘洗盘」。重标后要跑 `tests/test_plate_supply_guard.py`。
- `W_COOKER_IDLE = 4.8` 与 `W_ETA` —— 同在 L5。4.8 这个值的来历是「必须压过备菜的
  实际合计 2.7~3.3」（§10.2 那次回退的产物），这个理由随分层消失。重标为只表达
  「缺口大的先填」。
- `W_URGENCY = 5.0` —— 同时出现在 L3 与 L6，两层的同伴不同，可能需要分层取值。
  优先尝试单值，约束表不绿再拆。

**原始理由消失但仍有第二作用（保留，改注释）**

- `COMBO_PREP_DISCOUNT = 0.25` —— 原因是「0.5 时 FetchToPlate 以 2.98 压过
  FillCooker 的 2.50」，两者现在分属 L6/L5、不再同层比较。但它在 L6 内部仍有用
  （区分订单先后时别盖过 `completion_ratio`），保留。
- `COOKER_SHORTFALL_SATURATION = 3` —— 原因是「不能无限加分，否则 5 单全要米时
  填灶盖过送餐和灭火」。分层后填灶在 L5、送餐 L3、灭火 L0，这个保险不再被需要；
  但它仍影响 L5 层内排序（缺 1 份 vs 缺 5 份），保留、改注释说明新用途。
- `W_ASYNC_RESOURCE = 2.0` —— 原本让 FetchToCooker 压过 FetchToPlate，两者都在
  L6，**仍然有效**，不动。

## 8. 承诺惯性：不改

**分层只管「从可行候选里挑哪个」，不管「要不要打断正在做的事」。**
`_apply_commitment_inertia` 一行都不改。

分层解决的是**选择**问题——§10.2 那次回退是「该选填灶时选了备菜」，发生在挑新
任务的那一刻。而抢占是另一回事，现有机制（`COMMITMENT_MARGIN` +
`pressure >= 1.0` 跳过惯性）是实机调过的，没出过问题。把两者绑在一起会凭空造出
一个抖动面，还要靠迟滞、宽限分叉之类的补丁去兜。

### 8.1 层是派生值，不是状态

关键性质：`layers` 每拍**从 `feasible` 重新构造**，所以层会自己掉下来，不需要任何
「降层」动作。

走一遍「盘被 P1 拿走」：

1. `FetchToPlate.feasible` 的 `plate_by_id(...) is None` → False
2. 该候选不进 `feasible`
3. `layers` 是从 `feasible` 构造的 —— 消失的候选压根不在里面
4. `top_layer = min(layers.values())` 自动变成剩下候选里最高的那层

**候选消失本身就是信号。** 同理锅被端走、料被取走、灶被点火——一切「高层动作不
可达」的情形都走这条路，不需要额外的失效检测机制。

### 8.2 当前承诺总在候选里

`generate_candidates`（`planner.py:68-70`）已经把当前承诺无条件塞回候选，注释里的
理由是防止「planner 自己饿死」。所以当前承诺只要仍可行，它所在的层就一直有候选，
不会因为分层而被饿死。

当前承诺若落在比 `top_layer` 低的层，它不在 `in_layer` → 不在 `scored` →
`_apply_commitment_inertia` 的 ③ 把它当「不在可行候选中」，走 `infeasible_streak`
的 3 拍宽限再改选。**这个行为恰好是想要的**：出现高层候选时宽限几拍再切，而不是
立即切。白捡一个防抖。

L1（锅快糊）不受这 3 拍影响：`pressure >= 1.0 跳过惯性`（`planner.py:1542`）在
`_apply_commitment_inertia` 的开头、早于 ③，救锅仍是立即的。

### 8.3 因此保留（不要删）

- `planner.py:1542` 的 `pressure >= 1.0 → 跳过惯性` —— 它承担的是**抢占**，分层
  不接这个职责。注释里记的实机 bug（「米熟了 P2 还在给下一单下锅」）仍靠它挡着。
- `_task_is_interruptible` 的 `["Discard"]` —— 同上，与分层正交。

### 8.4 设计过程中被否掉的三个机制

记下来是因为它们看着都合理，而否掉它们的理由同一条：**层是派生值。**

- **降层迟滞**（`LAYER_EXIT_TICKS`）—— 为防「`pressure` 在阈值附近抖动导致来回
  跳层」。不需要：没有「跳层」这个动作，`top_layer` 每拍算出来即用。另外
  `cooking_progress` 在一个烹饪周期内单调递增，L2→L1 本身就是单向的。
- **`invalidated(ctx)` 第五契约** —— 为了统一回答「我绑定的资源还在不在」。
  不需要：`feasible` 转假已经让候选消失，而候选消失就是信号。
- **跳层立即改选** —— 把分层和抢占绑在一起。否掉的理由见 §8 开头。

## 9. 为后续 LLM 策略层留的接口

后续计划接入 LLM 与用户对话、据此调整策略。LLM 接入本身是独立一轮设计，这里只
记录**分层为它留了什么**，以及为什么分层比扁平加权和更适合承载它。

**扁平加权和没有给策略留位置。** 用户说「先把米都煮上」，LLM 唯一能动的是
`W_COOKER_IDLE` 的数值——而 §10.2 的经验正是「改一个权重会让另一组行为静默回退」。
让 LLM 去调一个它无法预测后果的浮点数，是最糟的接口。

分层给出三个离散、可解释、可验证的旋钮：

| 旋钮 | 用户话术 | 实现 |
|---|---|---|
| 层序调整 | 「优先保连击」 | 传一张改了**层号映射**的 `LAYER_RULES`（§5.1；不是重排行顺序） |
| 层的启用/禁用 | 「别管脏盘了我来洗」 | 删掉 L_UNBLOCK 那条规则，`WashPlate` 落到 L7 |
| 层内权重 | 「下锅比摆盘急」 | 调 L6 内的 `W_ASYNC_RESOURCE`，**结构上不可能影响救锅** |

第三行是扁平加权和给不了的保证：影响范围被层封死。

**但分层只覆盖「偏好」类话术，不覆盖「指派」类。** 用户说的话大多不是层序：

| 用户说的 | 落到哪 | 现状 |
|---|---|---|
| 「先煮米」「别管脏盘」 | 层序 / 层启用 | 本次提供 |
| 「这单先做」「你做寿司我做虾」 | `bb.order_assignment` | 结构已有，过滤逻辑是死代码（见 §11 末） |
| 「去把那个锅端下来」 | Arbiter 的 `Assigned` 模式 | 空壳（`arbiter.py:52` 返回模式，但 `agent.py:220` 的分支 `chosen_task = None`） |

三者正交：`Assigned` 在模式层，层序在模式内部。分层不妨碍后两条，但也不实现它们。

**时间尺度约束（给后续设计的前提）**：planner 20 Hz，LLM 一次往返 1~3 秒 =
20~60 拍。LLM 绝不能在决策回路里，只能异步写策略、planner 每拍读。`bb.intents`
就是这个位置，但策略写入必须原子 + 带版本号，否则 planner 会读到半套层序。

## 10. 不在本次范围

- **目标层 / 选项链**（planning 单元 ≠ execution 单元）。瓶颈在 observe（占 task
  拍数 25%，按精确步次数计费），解法是让一次承诺覆盖多个 task。独立工作。
- **`recipe_graph` 的 cost-to-go**。DAG 已编译但 `emit()` 只取前沿，路径代价未进
  打分（`estimated_steps` 仍是按 TYPE 查的常数）。独立工作。
- **两套耗时常数统一**（`EST_* = 10/20/15/5` vs `eta.COST_* = 125/30/120/50`）。
  这个会改变 L5/L6/L7 的 `eta` 项量级，但它是独立 bug，应在分层**之前**单独修并
  单独验证，否则两个改动的效果无法区分。
- **两处「按拍计数」应按新观测计数**（设计过程中发现的既有 bug，与分层无关）：
  ① `INFEASIBLE_THRESHOLD = 3` 在 `planner.py:1554` 每个 Python 拍 +1，但实测每
  3.5 拍才来一个新帧——3 拍宽限最坏情况下跨不到一个新帧，消耗的是重复的同一帧；
  本意显然是「连续 3 次新观测」。同款坑 `Confirm` 原语已修过（`retries` 按新快照
  消耗，见 AGENTS.md §6.2），照搬即可。② `done()` 为真导致的不可行和「暂时够不到」
  走同一条宽限路径，而后者已有 `reconcile.MISSING_GRACE_TICKS = 1` 兜着，两道宽限
  叠加；而「取熟饭是最有价值的接力」（§6.3），给它加滞后方向是反的。
  这两条改变既有实机行为，各自单独提交带测试。
- **插件提频（5 Hz → 10 Hz）**。observe 占 task 拍数 25%，其中至少 4 拍是纯等新帧，
  提频能砍掉一半且不改任何决策逻辑。但 `Snapshot()` 第一行是
  `FindObjectsOfType<MonoBehaviour>()`（全场扫描），而注释记着它「每 0.2s 跑一次会
  把游戏卡到选关页掉帧」——当时的解法是关卡外不推帧，所以关卡内的开销从未被测量。
  直接改 `0.2f` 风险不可控（游戏掉帧影响的是人类玩家）。本轮不做；真要做应先加
  `Stopwatch` 量出单帧耗时，再决定是否需要先把全场扫描改成按具体类型扫。

## 11. 实施顺序与验收

分层有若干前置，都独立可验证。顺序：

1. **A 组 bug**（`W_DEADLINE_LOOKAHEAD` 未引用、`effective_order_id` 漏传完成度/
   async、`_extract_task_resources` 改读 `claims()`）。纯 bug。
2. **权重约束表** `tests/test_score_ordering.py`。声明式地写下「局面 S 下 A 必须
   优于 B」。**这是本次改造的安全网**：分层后这张表应当原样通过，否则说明分层
   改变了既有序关系。
3. **拆 `planner.py` 成包**（1959 行 → `weights/candidates/matching/feasibility/
   terms/scoring/inertia/orders` + 新增 `layers`）。保留 `ai.planner` 路径与全部
   私有名（19 个测试文件伸手进去，`_score_candidate` 28 处、`_feasibility_of`
   16 处），零测试改动。
4. **分层本身**（§5~§8）。

一笔顺带的决定（第 3 步时做）：`_filter_collaboration` / `_filter_split_mode`
（47 行，只被 `tests/test_collab.py` 调用，`plan()` 里没有协作过滤这一步）要么接线
要么删。它是 §9 表格第二行的现状，LLM 那轮会需要它——建议**保留并在 AGENTS.md §8
记一句「未接线」**，别让测试继续制造「split 可用」的错觉。

**验收**：

- 全量测试原样通过（当前 982 passed / 78 skipped）。
- 约束表全绿。
- `tools/replay_urgency.py` 的倒挂率仍为 0%。
- 重放一局真实 trace，比对选择分布，**逐处解释差异**。
- 确认一个旧 bug 没以新形式复活：L4 > L5 意味着 0 盘时洗盘压过填灶，而注释里
  记的「没盘 → 不煮 → 米不够 → 单过期」正是这个形状。那个 bug 的根因是
  `async_resource` 的收紧语义（`planner.py:1105` 要求订单已有盘才给下锅加分），
  而填灶现在走订单无关的 `FillCooker`、不经过那一项——需要在重放里确认。
- 实机验证另算，不属于本设计的验收条件。

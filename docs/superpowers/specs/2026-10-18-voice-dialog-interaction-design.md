# 语音分工协商 设计

> 2026-10-18。目标：给 P2 加一个「像真人队友一样说话、协商分工」的交互层。
> 调研素材：`docs/notes/multiplayer_comm_research_20261018.md`；公开资料见 §13。
>
> **2026-10-18 修订**：修正截止闸的 slack 定义、补齐动作族与分支映射、L2 救援豁免、
> 半双工与打断的一致性、事件投递、`cancel_p2` 反转、`resume_order`、TTL 续期基准等。

## 0. 规划概览

**对话层不替代现有决策，只调整权重**：

- P2 从一开始就按分层规则（L0灭火 > L1快糊 > L2刚熟 > L3送餐 > L4洗盘 > L6备菜 > L7清洁）正常工作，不会等对话层才开始干活；
- 对话层的偏好权重（role ±1.2、assign -4、claim -8、direct +4）是**层内打分的加法项**，不是开关；
- 层序完全不变；截止闸只在最后窗口允许无视认领抢单，平时尊重玩家认领；
- LLM 只做「理解 + 措辞 + 在有限动作里选」，不做规划、不算 ETA、不进入 20 Hz 控制循环。

**延迟预算与模型选择**：

- 端到端目标 P95 <2.5s = VAD 尾段 400ms + ASR 300ms + LLM（首 token <400ms + 生成 800ms）+ TTS 首包 300ms + 播放；
- 候选模型：豆包低延迟非推理模型（`lite`/`flash` 一类，按方舟控制台实际可调型号实测选型）；不要用深度思考模型；
- 单句 8–20 字，`max_tokens=200`，温度 0.3。

## 1. 意图

现在 P2 的决策全部来自规则和收益模型：行为正确，但不像一个一起玩的人。真人玩胡闹厨房时会不断说话——报需求、分工、催、救急。本设计加的不是聊天机器人，而是**分工协商**：

- P2 能听懂玩家自由说的话（含转折、指代）；
- 把话落成对分工的修改，而不是只回一句漂亮话；
- 用自己的话确认、解释、派活；
- 说的话和正在做的事保持一致。

吐槽、甩锅、闲聊明确不做。

## 2. 用户已定的决策

这些是对话里定下来的，不再作为开放问题：

1. **核心是协商分工、派活**；吐槽暂不做。
2. **语音输入输出**：TTS + ASR，玩家直接开口，不用按键。
3. **一直听**：无唤醒词、无按住说话；本地 VAD 守候，检测到人声才送 ASR。
4. **宁可漏接**：拿不准是不是对 P2 说的，就不回应；是否回应、怎么回，由 LLM 决定。
5. **自由语言**：要能听懂「你先别煮饭，去把盘子洗了，我来切」这类带转折和指代的话。
6. **LLM 在云端**，TTS/ASR 用火山引擎，玩家有额度。
7. **优先级不变**：送餐高于洗碗；玩家的话不推翻层序。
8. **开局提案默认生效**：P2 提出分工后，除非玩家明确说不行，否则直接按这个分工走。
9. **分工与认领都用权重表达**：角色是小权重，明确认领是大负分；订单要丢时才允许无视认领抢单。
10. **P2 手里端着东西被认领时立即让开**，由行为树的 `EnsureHandsFree` 就近放下，不做特殊交接。
11. **认领按分支作用域**：说「我来煮饭」压的是整条米饭生产链（取米、下锅、端饭上盘），不是某一个具体候选；菜谱级整单分工（你做黄瓜寿司、我做鱼寿司）第一版不做。
12. **LLM 只回一句**：按它拿到的那份快照回答；拿不准就用条件句（「等有盘子我洗」「先等我送完这单」），不预写多套回复。
13. **音频先使用 Mac 内置麦克风和扬声器**，不额外要求耳机；v1 接受半双工限制（TTS 播放期间不打断）。

## 3. 一局里的体验

### 3.1 开局与协商

开局第 3 秒内：

```
P2：这单要寿司，我煮饭，你切黄瓜。
```

玩家改主意：

```
玩家：你先别煮饭，去把盘子洗了，我来切。
P2：行，我洗盘子。
```

如果此刻还有没被认领的送餐单（L3 高于 L4）：

```
玩家：你先别煮饭，去把盘子洗了，我来切。
P2：先等我送完这单，马上洗。
```

玩家认领整条米链：

```
玩家：我来煮饭。
P2：好，饭你来。
```

P2 立刻让开米链上所有候选；如果手上正端着米锅，`EnsureHandsFree` 就近放下。

玩家认领了煮饭，但一直没做，订单进入最后窗口：

```
P2：饭一直没煮，这单要超时了，我先去煮；海苔你铺一下。
```

突发（走短句模板，不等 LLM）：

```
P2：有锅着火了，我去灭。
```

没听懂或不是在跟 P2 说：

```
玩家：这锅怎么又糊了……
（P2 不回应，继续干活）
```

### 3.2 用户说话触发的典型回合

| 玩家说 | LLM 命令 | 主循环结果（内部） | P2 实际动作 / 回复 |
|---|---|---|---|
| 我来切 | `claim_task`，`family=chop` | accepted | 类型级 -8 存住；黄瓜上板后不碰，回「行，你切。」 |
| 你去洗碗（还有送餐） | `direct_p2`，`family=wash` | deferred | +4 存 30 秒；先送餐，回「先等我送完这单，马上洗。」 |
| 你去洗碗（现在没脏盘子） | `direct_p2`，`family=wash` | pending | +4 先存住；回「现在没盘子，等有了我洗。」 |
| 你先别煮饭，去把盘子洗了，我来切 | `cancel_p2 cook` + `direct_p2 wash` + `claim_task chop` | accepted / deferred | 停煮、记住洗和切；有送餐时回「先等我送完这单，马上洗。」 |
| 你煮饭，我切 | `set_role` P1=cook、P2=chop | accepted | 对调动作族权重，回「行，你煮，我切。」 |
| 这单不要了 | `abandon_order` | accepted | 该单硬排除 60 秒，回「行，不管鱼那单了。」 |
| 这单还是要吧 | `resume_order` | accepted | 取消硬排除，回「行，那继续做鱼那单。」 |
| 现在最急的是哪单 | `noop` | accepted | 纯回复「黄瓜那单，剩 20 秒。」；上下文没有就回「我看不到」 |
| 好 / 不用 | `noop` / `cancel_assign` | accepted | 结合上一轮提案落命令，回「行，那我煮饭。」或「行，那我来切。」 |

「我来煮饭」这种 branch 级认领见 §4.5。

## 4. 对话语义

### 4.1 输入

每段话由本地 VAD 切出，送火山流式 ASR，得到最终文本。对话层同时拿到一份**精简的决策上下文**（见 §4.2），不是完整 World。

### 4.2 LLM 的输入

上下文由主循环每 10 拍（2 Hz）发布一次，只读快照：

```text
level: 关卡名 / 剩余时间
p2: 当前 task 类型、order_id、branch、步骤、模式（Normal/Firefighting）、手持物
preferences: 当前有效的角色/认领/指令权重
p1: 手持物、所在格、当前有效的认领
orders: [{id, dish, remaining, solo_slack, eta}]
candidates: 最多 12 条 [{key, type, order_id, branch, layer, resources, item, score}]
dialog: 最近 6 轮对话、P2 正在等回答的问题、上次主动开口时间
event: speech / new_order / rescue / help
```

`branch` 用生料名（`SushiRice`、`Cucumber`、`SushiFish`、`Seaweed`），熟料名和切好形态由 `RecipeBook` 归一到生料。只给候选和状态，不给整张地图。LLM 不需要重新规划，它只需要在有限动作里做选择。

`event` 的来源：

- `speech`：VAD + ASR 最终文本；
- `new_order`：主循环比较订单 id，发现新单；1 秒内的多张新单合并；
- `rescue`：planner 的截止闸开闸时，主循环把事件放入 `dialog_events` 队列；
- `help`：P2 连续 40 拍（2 秒）没有可行候选，且 P1 手部空闲；主循环放入 `dialog_events`。

`dialog_events` 是 `Queue(maxsize=1)`：同一时刻只保留最新事件；worker 取走后由主循环按 seq 清空。P2 是否正在说话**不放进上下文**：v1 是半双工，TTS 播放期间 VAD 暂停，LLM 不会在这种情况下被调用。

### 4.3 LLM 的输出

强约束 JSON，只回一句：

```json
{
  "addressed": true,
  "reply": "行，我洗盘子。",
  "commands": [
    {"type": "direct_p2", "family": "wash", "ttl_s": 30}
  ],
  "confidence": 0.86
}
```

命令类型：

| type | 含义 | 落成的权重/状态 | 关键字段 |
|---|---|---|---|
| `claim_task` | 玩家明确认领 | `claim_penalty = -8`，带 TTL | `branch` / `family` / `candidate_key` |
| `release_claim` | 玩家放弃认领 | 删除对应负分 | 同上 |
| `set_role` | 设定动作族分工 | `role = ±1.2`，整关 | `owner` / `family` |
| `clear_role` | 取消角色分工 | 删除对应权重 | `owner` / `family` |
| `assign_p1` | P2 派某趟活给玩家 | `assign_penalty = -4`，带 TTL | `branch` / `family` / `candidate_key` / `ttl_s` |
| `cancel_assign` | 撤回派活 | 删除对应负分 | 同上 |
| `direct_p2` | 玩家让 P2 做某事 | `direct_bonus = +4`，带 TTL | `branch` / `family` / `candidate_key` / `ttl_s` |
| `cancel_p2` | 玩家叫停 P2 | 该动作族硬压制 8 秒 | `task_type` / `family` |
| `abandon_order` | 玩家明确放弃某单 | 该订单硬排除，带 TTL | `order_id` |
| `resume_order` | 恢复被放弃的订单 | 取消该订单的硬排除 | `order_id` |
| `noop` | 只听懂了，不改分工 | 无 | 无 |

`addressed=false` 时 `reply` 为 `null`、`commands` 为空。

**单回复怎么保证一致**：

- LLM 按它拿到的快照回答；如果当前状态不能立刻做，就用条件句说清楚： 「现在没盘子，等有了我洗」「先等我送完这单，马上洗」「来不及了，我先煮」。
- 主循环仍然做权威校验，返回内部的 ack：`accepted / pending / deferred / rejected / overridden`。ack 不用于在几句话里选，只用于两件事：
  1. 记日志、算延迟；
  2. 如果结果是 `rejected`（这关根本做不了），**抑制这句回复**，改成一句通用短句或静默，避免承诺一个不存在的事。
- `pending` / `deferred` 一般不额外处理：只要 LLM 按快照说了「等有了」「等我送完」，它本来就是对的。
- 如果 ack 和回复明显矛盾（例如回复说「马上去洗」但结果是 `deferred`），worker 可以用一条短模板纠正（「等下，先送这单」）；这是兜底，不是主路径。

**多命令回复的一致性风险**：

复合命令（如 `cancel_p2 cook` + `direct_p2 wash` + `claim_task chop`）提交后，主循环可能部分接受、部分拒绝。规则：

1. 有效命令照常应用；被 `rejected` 的命令不产生任何状态；
2. 如果 LLM 的回复承诺了被拒的命令，整句抑制，改播通用纠正句（「这个现在做不了，我先做别的」）；
3. 多命令时优先保证「不承诺做不到的事」，不要求回复覆盖每一条命令；
4. 第一版优先覆盖单命令场景；复合命令在测试中观察。

### 4.4 校验层

LLM 的输出不能直接进规划器。`schema.py` 做四件事：

1. `task_type` / `family` 必须合法；`branch` 必须是当前订单配方里出现过的生料名。
2. `candidate_key` 必须出现在本轮上下文提供的候选里；没有 `candidate_key` 就是**类型/分支级**命令。
3. **类型/分支级命令不要求此刻存在候选**。开局说「我来切」时黄瓜还没上砧板；说「去洗碗」时还没有脏盘子。命令仍然生效，存成 Preference，等目标出现再兑现；ack 是 `pending`，不是 `rejected`。
4. 只有**这关根本做不了**的情况才 `rejected`：没有洗碗池、没有这种食材、订单不存在、要求洗锅/扫地。此时丢弃命令，并抑制 LLM 的回复（通用句或静默）。

绑定到具体候选的命令如果当前找不到：目标还在但被占着 → 降级成类型/分支级，ack `pending`；目标已经不存在（盘子没了/订单没了）→ `rejected`。

`abandon_order` 是硬排除，TTL 默认 60 秒；`resume_order` 立即取消该排除。被放弃订单的候选在 `generate_candidates` 阶段整体过滤，不只过滤盘匹配。

### 4.5 偏好权重与分支作用域

不引入「可靠度」状态，也不把认领从候选里永久删掉。所有分工都落成候选上的一个加法项：

```python
@dataclass
class Preference:
    kind: str          # role / claim / assign / direct
    branch: Any = None # 生料名；None = 不限分支
    family: Any = None # chop / cook / wash / deliver；None = 不限
    task_type: Any = None
    candidate_key: Any = None
    owner: str = "P1"  # P1 / P2
    weight: float = 0.0
    expires_tick: int = 0
    base_ttl: int = 0  # 续期时回到这个值，不写死 240 拍
```

默认量级（实施时用真实帧校准）：

| kind | 默认权重 | 生命期 | 作用 |
|---|---|---|---|
| `role`（P1 负责的族） | -1.2 | 整关 | P2 平时避开，但 urgency 很容易盖过 |
| `role`（P2 负责的族） | +1.2 | 整关 | 同层内稍微优先 |
| `assign`（P2 派给 P1） | -4.0 | 8–20 秒 | 比角色强，但仍可能被紧急任务盖过 |
| `claim`（玩家明确认领） | -8.0 | 12 / 30 秒 | 平时完全不碰，只有截止闸能抢 |
| `direct`（玩家让 P2 做） | +4.0 | 30 秒 | 同层内明显优先，但不会越过更高层 |

匹配规则（从窄到宽）：

1. `candidate_key` 非空时只命中该候选；
2. 否则 `task_type` 非空时按 task 类型匹配；
3. `branch` / `family` 非空时，先按分支过滤，再按动作族过滤；
4. 全部为空时不匹配任何候选（不会退化成「影响全部」）。

动作族是 `schema.py` 里手工维护的小表：

| 玩家说 | 族 | 覆盖的 task 类型 |
|---|---|---|
| 我来切 | `chop` | `Chop` + 同 item 的 `FetchToBoard` / `FetchToPlate` / `Prefetch` |
| 我来洗 | `wash` | `WashPlate` + 同 sink 的 `PlaceCleanPlate` |
| 我来送 | `deliver` | `DeliverPlate` |
| 我来煮 | `cook` | `FetchToCooker`、`FillCooker`、`TakeCookedToPlate`、同分支的 `Prefetch` |

**救援类不参与认领**：`RescueCooked`、`ReturnPot`、`RecoverLoosePot` 以及 L0/L1/L2 的 `TakeCookedToPlate` 不受任何 `claim`/`assign` 负分约束。玩家说「我来煮饭」不等于「饭要糊了也别管」。

**分支是关键**：说「我来煮饭」时，`branch=SushiRice` + `family=cook` 会命中：

- `FetchToCooker(order#1 / order#2, ..., "BoiledSushiRice")`
- `TakeCookedToPlate(..., "BoiledSushiRice")`
- 还没发射的未来候选（米还没拿到、灶还没空）
- `FillCooker` / `Prefetch` 里 item 归一到 `SushiRice` 的那些

黄瓜链上的 `FetchToPlate`、`Chop` 不受影响。说「饭我来」时不带 `family`，整条米链都调低（取米、下锅、端饭上盘）。

`Candidate.branch` 由 `RecipeBook` 归一：

- `Chop` / `FetchToBoard` / `FetchToPlate` / `Prefetch` / `FillCooker` 的 item → 生料名；
- `FetchToCooker` / `TakeCookedToPlate` 的 item（熟料名）→ 生料名；
- `RescueCooked` / `ReturnPot` / `RecoverLoosePot` 的 item → 生料名（仅用于 trace，不参与认领）；
- `Mix` 的 item → 生料/熟料名；当前关卡没有 mixing 站点；
- `WashPlate` / `PlaceCleanPlate` / `DeliverPlate` / `Discard` / `ResetPlate` / `StashHeld` / `Extinguish` 没有 branch（共享/整单/清理动作）。

多生料的熟料节点（如未来的混合菜）取熟料名本身作 branch，不强行拆成多个生料；当前关卡不涉及。归一到 `None` 的 task 不会被 branch 级认领命中，trace 里记 `branch=None`。

### 4.6 分层、权重与截止闸

层序完全不变（`planner_layers.py`）：L0 灭火 > L1 快糊 > L2 刚熟 > L3 送餐 > L4 洗盘/放盘 > L5 空层 > L6 备菜 > L7 清洁。

选择顺序：

1. 生成候选、可行性过滤、`layer_filter` 取最高层；
2. 层内打分 = 现有分 + `role` + `assign` + `claim` + `direct`；
3. 如果本层**所有**候选都带有效的 `claim` / `assign` 负分：
   - 先看截止闸。有单进入最后窗口 → 允许无视负分，抢关键路径上的那个；
   - 没有单要丢 → 掉到下一层；下一层仍全被认领就继续向下，直到找到未认领层或无候选；
4. 如果本层还有未认领候选 → 正常比分数；
5. 如果选中的候选带 `claim` / `assign` 负分，但截止闸没开 → 把它从本轮排除，重新选；不行就继续掉层/空转。

**截止闸（rescue gate）**：

```text
cushion = ETA.solo_slack_by_order[order_id]   # 剩余时间 - 该单完整 solo ETA，单位拍
D = 从当前状态做完这个动作、再送到出餐口还需要的 ETA（拍）
margin = max(5 秒, min(0.3 × D, 10 秒))       # 5 秒起步，长链封顶 10 秒
```

- `cushion >= margin` → 还等得起，尊重认领；
- `0 <= cushion < margin` → 最后窗口，开闸抢；
- `cushion < 0` → 已经来不及，按必死单处理，不为了它抢。

**注意**：`cushion` 用的是项目现有的 `solo_slack`（剩余时间减完整 solo ETA），**不是剩余时间本身**；不要再减一次 `D`，否则会把 ETA 重复扣掉，闸永远不会开。

例：某单 `remaining=21s`、`solo_eta=17s`，则 `cushion=4s`；`D=17s`，`margin=max(5, min(5.1, 10))=5.1s`；`4 < 5.1`，开闸。`cushion=9s` 时不开闸；`cushion=-8s` 时已必死，不开闸。

**margin 说明**：原版 `max(5 秒, 0.3×D)` 在长链上（D=60s）会到 18 秒，开闸过早；改成 `max(5 秒, min(0.3×D, 10 秒))`，既保留 5 秒起步（3 秒太紧），又把长链封在 10 秒。实施时用真实帧校准 0.3 和 10 秒这两个数。

其他规则：

- 只对 `claim` / `assign` 生效，不对 `role` 生效；
- L0/L1/L2（灭火、快糊、刚熟）不受任何认领负分约束；`RescueCooked` / `ReturnPot` / `RecoverLoosePot` 也不参与认领；
- 抢过一次后，`rescue_cooldown` 默认 100 拍（5 秒）；冷却期内不再因为同一单来回切；
- 复用现有 `commitment` 惯性，避免「让开—抢回—再让开」的抖动。

**分支与行为树**：

- 调低的是**候选分**，不是 BT 内部步骤。BT 是每个 task 一棵，没有跨 task 优先级；规划器不选它，树就不会建。
- 如果 P2 正在执行这条分支上的 task，认领到了就立刻清 `commitment`、halt 当前树，`EnsureHandsFree` 按现有规则放下手里东西，然后重新选。
- 未来候选也覆盖：认领存的是 branch/Preference，不是某一帧的候选，所以米还没拿到、灶还没空时也不会漏；没有脏盘子时说「去洗碗」也同理，先存着，盘子出现再生效。

**明确否决**：

- 玩家说「别管这单」→ `abandon_order`，该订单硬排除，带 TTL（默认 60 秒）；`resume_order` 可立即恢复。
- 玩家说「先别煮饭」→ `cancel_p2(family="cook")`，该族硬压制 8 秒；这也不是权重，是明确的停手指令。
- **反转**：8 秒内玩家又说「你去煮」/「我来煮」→ 新的 `direct_p2` / `claim_task` 命中同族时，先删除 `p2_suppressed_families[family]`，再按新命令生效。

### 4.7 有效期与接手

- 玩家认领，绑具体候选：默认 12 秒（240 拍），有证据则续期。
- 玩家认领，类型/分支级：默认 30 秒（600 拍）。黄瓜 20 秒后才上砧板也不会过期。
- P2 派给玩家的活：默认取 `max(8 秒, 0.5 × 该 task 的 ETA)`，上限 20 秒。
- 玩家给 P2 的指令：默认 30 秒，完成即结束；没有目标时也保留 30 秒，等目标出现。
- 角色分工：活到关卡结束；玩家明确改口或关卡切换时清除。
- `abandon_order`：默认 60 秒；`resume_order` 立即恢复。
- 到期后先看证据：玩家手持相关物、站在目标附近、朝目标移动 → 续期；否则失效。
- P2 派活到期失效 → 负分消失，P2 可以自己接手；`assign_expired` 只走低优模板，不调 LLM。
- 玩家认领到期失效 → 负分消失，P2 重新把该层纳入正常打分。
- 每拍由主循环扫一遍 `Preference.expires_tick`，过期即删；对话层通过上下文看到 `claim_expired` 事件。

**证据续期机制**（每 10 拍 = 500ms 检查一次）：

玩家认领到期前，检查以下证据：

1. **手持物匹配**：认领送餐时 P1 手持盘、认领煮饭时 P1 拿米/锅 → 续期到 `base_ttl`；
2. **站位与移动方向**：P1 当前格与分支对应的资源格相邻（曼哈顿距离 ≤2）且相比上次检查的格子更接近目标 → 续期到 `base_ttl`；
3. **反向证据**：P1 连续 8 拍（400ms）手持无关物且相比初始认领时的格子更远离目标 → 不续期，让 TTL 自然到期。

`base_ttl` 按认领类型存：绑候选 240 拍、类型/分支级 600 拍、`assign` 160–400 拍、`direct` 600 拍。续期时写回 `current_tick + base_ttl`，**不要统一写 240**，否则分支级 30 秒认领会越续越短。

证据检查的"到期前"定义：TTL 剩余 ≤ 120 拍（6 秒）时触发检查；检查通过则续期，否则让 TTL 继续倒计时直到归零。

**明确续期的边界场景**：

- 场景 A：玩家认领「我来切」后站在砧板旁 11 秒不动、不拿黄瓜，第 12 秒才拿起黄瓜 → 第 6 秒触发证据检查时 P1 不持有相关物、未朝目标移动，不续期；第 12 秒 TTL 已归零，认领失效，P2 已接手。
- 场景 B：玩家认领「我来切」，先去送了一个盘（8 秒），送完后朝砧板走（第 9 秒开始接近） → 第 6 秒触发检查时 P1 正在朝砧板移动（虽然手持的是盘，但移动方向匹配），续期；P1 到达砧板后有充足时间完成。

**P2 接手时的通知**：

- P2 派活到期失效 → 负分消失，P2 可以自己接手；`assign_expired` 只走低优模板，不调 LLM。
  - **"明显空着"的定义**：P1 手部空闲（`hands_free`）且连续 4 拍（200ms）未朝该分支的资源移动。
  - 满足条件时，P2 说一句低优模板（「那我切了啊」「我来洗了」）；否则静默接手。
- 玩家认领到期失效 → 负分消失，P2 重新把该层纳入正常打分；静默接手，不通知。

**关卡切换**：收到新的 `level` 帧（关卡名或 phase 变化）时，清空 `kind=role` 的偏好、`dialog` 状态、`p2_suppressed_families`、`abandoned_orders`。`rescue_cooldown` 也归零。

### 4.8 对账

口头认领不能只靠 TTL。每拍用现有 P1 事实做一次轻量对账：

- **手持物**：认领送餐时 P1 手里端着盘 → 续期；认领煮饭时 P1 拿着米/锅 → 续期；
- **站位与移动**：P1 的格子已经在 `p1model.p1_soft_cost_map()` 里从 world x/z 算出，`state_dump` 也输出 `p1_cell`；用它判断 P1 是否在朝分支对应的资源移动；
- **明确反悔**：「我不送了」「你来」→ 删除对应负分；
- **反向证据**：P1 连续 4 秒手持无关物且离目标更远 → 不续期，让 TTL 自然到期。

不需要插件新增字段，也不做可靠度衰减。

### 4.9 第一版支持范围

**支持**：

- 认领、派活、叫停、放弃/恢复订单；
- 动作族角色分工；
- 分支级认领（「饭我来」「黄瓜我来」）；
- 复合指令（一句话里多个命令）；
- 能从上文/候选里回答的询问；
- 「好」「不用」「不是」这类回应，由 LLM 结合最近一轮对话推断；
- 指代只要能落到候选、分支或动作族上；
- 暂时没有目标的命令（先 pending，目标出现再生效）。

**第一版不做**（识别到也不硬猜，忽略或回通用句）：

- 菜谱级整单分工（「你做黄瓜寿司，我做鱼寿司」）；
- 交接/递东西（现有 19 个 task 没有 hand-off）；
- 暂停、等待、让位、「站远点」；
- 条件与通知（「等饭熟了叫我」「锅开了告诉我」）；
- 世界查询超出上下文（库存、锅状态、盘子位置、刚才为什么那么选）；
- 屏幕方位指代（「左边那个」「你旁边那个」）；
- 具体格子微动作（「放这儿」「拿这个」）；
- 洗锅、扫地等没有对应 task 的动作；
- TTS 播放期间打断（v1 半双工；第二版再支持 barge-in）。

## 5. 说话策略

### 5.1 什么时候调 LLM

**只有四个入口**：

| 入口 | 触发 | LLM 做什么 |
|---|---|---|
| `speech` | 一段人声的 ASR 最终文本出来 | 判断是否对 P2 说、落命令、回一句 |
| `new_order` | 新订单出现；开局第一批也算 | 决定沿用/微调/重新分工 |
| `rescue` | 截止闸把一个认领抢回来 | 说一句「来不及了，我先煮」 |
| `help` | P2 连续 40 拍无可行候选且 P1 手部空闲 | 请 P1 接手一个关键步骤 |

`new_order` 会合并：1 秒内出现的多张新单算一次事件；开局第一批订单也走同一条路径，不再单独有 `level_start`。

**事件投递**：主循环维护 `dialog_events: Queue(maxsize=1)`，把 `new_order` / `rescue` / `help` 放进去；worker 每轮取最新事件，和 2 Hz 上下文一起用。`speech` 由 worker 自己从 ASR 产生，不走这个队列。事件带 seq，worker 取走后主循环清空；同一 seq 只处理一次。

**不调 LLM**：

- 每一帧、每一拍、ASR 中间结果、候选变化、分数变化、P2 换任务；
- 火、快糊、订单快超时：走模板 + TTS；
- `assign_expired`：低优模板，玩家明显空着才说一句「那我切了啊」；
- `conflict`：不是单独事件，回复里直接说原因（「先等我送完这单」）。

三道闸：

- 主动开口最短间隔 4 秒（`speech` 是玩家触发，不受这条限制）；
- 同一意图状态没变不重复；
- 同一时刻最多一个 LLM 请求在飞；玩家开口就取消它。

按这个算，4 分钟一关大约 20–40 次调用。

### 5.2 频率与长度

- 一句话，目标 8–20 个汉字；超过 30 字视为失败，实施时在 prompt 里硬约束。
- 玩家刚说完时优先回应，不被主动播报挤掉；玩家说话时立刻停 TTS（见 §5.4）。
- 不报流水账（「我现在去拿盘子」「我拿到盘子了」）；只在**分工变化**或**需要玩家知道**时开口。

### 5.3 紧急线

火、快糊、订单快超时走**模板 + 火山 TTS**，不经过 LLM：

- 事件检测到 → 直接合成固定短句 → 播放；
- 目标首包 < 800ms；
- 模板只描述事实和 P2 的动作，不评价玩家。

命名规则：**不发明锅号/订单号**。仓库里没有面向玩家的编号约定，随便叫「二号锅」会让玩家看错。初版用「有锅着火了」「寿司那单快超时了」这种以菜名/事实指代的说法；只有关卡数据里补了屏幕相对方位表，才允许说「右边那口锅」。

### 5.4 打断

**v1 是半双工**：

- TTS 播放期间暂停 VAD，不处理玩家插话；
- TTS 播放结束后恢复 VAD；
- 第二版再支持 barge-in（需要 AEC / 双工语音模型）。

这样和 §8.2 的「TTS 播放期间暂停 VAD」一致，不会出现「暂停了 VAD 却还能检测插话」的矛盾。

### 5.5 漏接

- `addressed=false` 或 `confidence < 0.6` → 不播报、不改分工，只记日志；
- 不追问问「你是在跟我说话吗」；
- 玩家重复一遍或叫名字时再处理。

## 6. 架构

### 6.1 新模块

```text
ai/dialog/
  __init__.py
  schema.py      # Preference、DialogCommand、DialogContext、JSON 校验、动作族
  audio.py       # sounddevice 采集/播放、设备选择、RMS VAD、分帧
  asr.py         # 火山流式 ASR 客户端（WSS）
  tts.py         # 火山流式 TTS 客户端、播放队列
  llm.py         # 火山方舟 chat 客户端（SSE）、prompt、JSON 解析
  policy.py      # 主动触发、频率限制、紧急模板、assign_expired 模板
  worker.py      # 后台线程：VAD→ASR→LLM→TTS，命令/ack/事件队列
```

不放进 `ai/core/`：对话层不参与 20 Hz 决策，放进 core 会让人误以为它能改规划。不放 `ai/output/`：它不只是输出，还读上下文、写偏好。

### 6.2 线程与队列

- 主循环（20 Hz）仍是唯一碰 `World` / `Blackboard` 的地方。
- `DialogWorker` 跑独立 daemon 线程，内部可以用 asyncio；它**不读 World、不写 Blackboard**。
- 主循环 → worker：`publish_context(dict)`，2 Hz，`Queue(maxsize=1)`，新快照覆盖旧快照。
- 主循环 → worker：`dialog_events: Queue(maxsize=1)`，`new_order` / `rescue` / `help` 事件。
- worker → 主循环：`commands: Queue`，每项是一条 `DialogCommand` + 时间戳。
- 主循环 → worker：`acks: Queue`，每项是 `(command_id, outcome, reason)`，outcome 为 `accepted/pending/deferred/rejected/overridden`。
- 主循环每拍开头 drain 命令队列，权威校验后应用；ack 回给 worker。
- worker 拿到 ack 后：`rejected` 抑制原回复（通用句/静默）；其余情况播原回复；只有明显矛盾时才用短模板纠正。
- 关闭顺序：先停 worker（关流、停播放），再退游戏，沿用现有「先停 Agent 再退游戏」的约定。

### 6.3 主循环接入

`Agent.tick()` 增加五步：

1. drain `worker.commands` → 用当前 World 做权威校验 → 写 `bb.preferences` / 硬否决 → 回 ack；
2. 过期 `Preference` / `abandon_order`；
3. 发现新订单 → `dialog_events.put(("new_order", ids))`；planner 开闸 → `dialog_events.put(("rescue", order_id))`；连续 40 拍无候选且 P1 空闲 → `dialog_events.put(("help", None))`；
4. 每 10 拍 `worker.publish_context(_build_dialog_context(...))`；
5. trace 记 `dialog` 事件（命令、ack、权重变化、`rescue_override`、`fallthrough`、`branch`、`event`）。

**播报顺序**：worker 拿到 LLM JSON 后先发命令，等主循环 ack；ack 只决定「播不播」，不选多套回复。ack 只需 ≤50ms，TTS 首包本来就要 300ms，不增加可感知延迟。

**关卡切换**：收到新的 `level` 帧时，清空角色偏好、dialog 状态、压制/放弃订单表、rescue 冷却。

`run_agent.py` 增加：

```text
--voice            启用语音（默认：有 key 则启用，否则告警并继续）
--no-voice         强制关闭
--voice-debug      打印 ASR 文本、LLM JSON、TTS 文本、延迟，不静音
--voice-log PATH   对话日志（默认 voice-<时间戳>.jsonl，已 gitignore）
```

### 6.4 Planner 接入

- `Candidate` 新增 `branch` 字段，由 `RecipeBook` 归一；`_score_candidate()` 新增 `pref` 项，按 §4.5 汇总命中的 `role` / `assign` / `claim` / `direct` 权重。
- **`pref` 必须加入各层 `TERM_WHITELIST`**（`planner_layers.filter_terms` 会按层白名单过滤未知项；漏了这一步，权重会被静默丢掉）。
- `Blackboard` 新增 `preferences: List[Preference]`、`p2_suppressed_families: Dict[str, int]`、`abandoned_orders: Dict[Any, int]`、`dialog: DialogState`、`rescue_cooldown_until: int`。
- `layer_filter()` 不改；层内打分后做 §4.6 的 fallthrough / rescue gate；fallthrough 要**逐层向下**，不是只掉一层。
- `plan()` 里增加：
  - 本层全是 `claim`/`assign` 负分时，先算截止闸；
  - 闸不开 → 掉层；闸开 → 无视负分选关键路径；
  - 选中的候选带负分且闸不开 → 排除后重新选。
- `commitment`：新 `claim` / `assign` 命中当前 `active_task`（按 branch/family/candidate 匹配）时，立即清空承诺（清 `active_task`、`infeasible_streak=0`），**不要把 `Commitment` 置 None**；当前 BT 由 Executor halt。
- `p2_suppressed_families` 与 `abandoned_orders` 在可行性/候选阶段硬过滤；它们不是权重。`abandon_order` 要在订单级过滤**所有**该单候选，不只 `_match_plates_to_orders`。
- `cancel_p2` 命中同族的新 `direct_p2` / `claim_task` 时，先删除压制。
- `plan_firefighting()` 不应用任何 `claim`/`assign` 负分（火情优先）；L0/L1/L2 的救援任务也不受认领负分约束。
- trace 的 `veto` 字典新增 `p2_suppressed`、`abandoned_order`；打分项新增 `pref`；候选记录新增 `branch`；记录 `fallthrough`、`rescue_override`、`rescue_cooldown`、`help`。

### 6.5 配置

全部走环境变量，不写进仓库：

```text
VOLC_ASR_API_KEY / VOLC_ASR_RESOURCE_ID
VOLC_TTS_API_KEY / VOLC_TTS_VOICE / VOLC_TTS_RESOURCE_ID
ARK_API_KEY / ARK_MODEL / ARK_BASE_URL
OC2_VOICE=1/0
OC2_VOICE_NAME=小厨
OC2_VOICE_RATE=1.05
OC2_VOICE_VOLUME=0.7
```

密钥只从环境读；`.gitignore` 已有 `.env`，不新增明文配置。

## 7. 云服务

### 7.1 ASR

- 火山「大模型流式语音识别」，双向流式优化版 `bigmodel_async`；16 kHz、16 bit、单声道 PCM；200 ms 一包。
- 官方给的尾字延迟 300–400 ms（5 秒音频）；首字更低。
- VAD 切段后开一次识别，说完关流；不在静音期持续计费。
- 识别结果取 `definite=true` 的最终文本；中间结果只用于调试。

### 7.2 TTS

- 火山流式 TTS，优先双向流式（文本边出边合成），首包 300–400 ms。
- 输出 PCM 16k/24k；`sounddevice` 直接播放，不落盘。
- 音色实施时在控制台选一个自然、偏年轻的中文音色；默认女声，语速 1.05；名字默认「小厨」，可配置。
- 紧急短句也走同一 TTS，但文本来自模板，不等 LLM。

### 7.3 LLM

- 火山方舟 OpenAI 兼容接口 `https://ark.cn-beijing.volces.com/api/v3/chat/completions`，`stream=true`。
- **必须选非深度思考的快模型**；实施时用 3 个候选实测首 token，选 P50 最低的。不要用 reasoning 模型做实时对话。
  - **候选模型**：豆包低延迟非推理模型（`lite`/`flash` 一类），以方舟控制台实际可调型号为准；Qwen-Turbo 只有在方舟可调时才作为候选。
  - **延迟预算**：目标 P95 端到端 <2.5s = VAD 尾段 400ms + ASR 300ms + **LLM 首 token + 生成 1.2s** + TTS 首包 300ms + 播放。LLM 部分预算约 1.2–1.4s（首 token <400ms，生成 20 字约 800ms）。
  - **实测方法**：用 50 条真实上下文（包含 12 条候选、6 轮对话历史）测 P50/P95 首 token 延迟，在控制台测试接口直接发送，记录时间戳。如果 P95 >500ms 则换模型。
- 输出短（一句话 + 命令），`max_tokens=200`（约 130 汉字，含 JSON；150 对多命令偏紧）；温度 0.3，保证措辞稳定。
- prompt 见 §7.3.1；要求只输出 JSON；状态不确定时用条件句。
- LLM 只做「理解 + 措辞 + 在有限动作里选」，不做规划、不算 ETA、不读原始帧。

### 7.3.1 LLM Prompt（草稿）

```text
你是 P2（2P 玩家），正在和 P1（1P 真人玩家）一起玩 Overcooked 2 合作烹饪游戏。你的任务是理解 P1 的话，决定如何调整分工，并用一句简短自然的话回应（8–20 字）。

## 当前状态
{上下文 JSON：level, p2, preferences, p1, orders, candidates, dialog, event}

## 你能做的事
1. 认领/放弃：玩家说"我来X"→ claim_task；"你来X" → direct_p2
2. 角色分工：玩家说"你煮饭我切菜" → set_role
3. 叫停：玩家说"别煮了" → cancel_p2
4. 放弃/恢复订单：玩家说"这单不管了" → abandon_order；"这单还是要" → resume_order
5. 只听懂、不改分工 → noop

## 命令类型
{命令表：type, 字段, 含义}

## 动作族
- chop: 切菜（Chop + FetchToBoard + FetchToPlate + Prefetch）
- cook: 煮（FetchToCooker + FillCooker + TakeCookedToPlate + Prefetch）
- wash: 洗盘（WashPlate + PlaceCleanPlate）
- deliver: 送餐（DeliverPlate）
- 救援类（RescueCooked/ReturnPot/RecoverLoosePot）不参与认领

## 分支
- SushiRice / Cucumber / SushiFish / Seaweed（用生料名）

## 回复规则
1. **只回一句**，8–20 字，不要解释、不要多句；
2. 如果状态不确定，用条件句：「等有了我洗」「先送完这单再说」；
3. 如果听不懂或不是在跟你说，输出 addressed=false；
4. 回复要和你正在做的事、将要做的事一致；
5. 不要发明锅号/订单号，用菜名或事实指代。

## 输出格式（纯 JSON，不要 markdown 代码块）
{"addressed": true/false, "reply": "...", "commands": [...], "confidence": 0.0–1.0}

现在玩家说："{玩家的话}"
```

**待完善**：实施时补 3–5 个 few-shot（简单认领、复合指令、条件句、放弃/恢复订单）。

### 7.4 失败回退

- LLM 超时（>3 s）或返回非法 JSON → 不改分工，不播报；记日志。
- ASR 失败 → 忽略这段话。
- TTS 失败 → 紧急线退回本地 `say -v Tingting`；非紧急线静默。
- 断网 → 对话层整体降级：紧急模板仍可用（本地 `say`），协商功能暂停，主循环不受影响，游戏继续跑。
  - **自动恢复**：worker 线程每 30 秒重试一次健康检查（发一条 noop 请求）；网络恢复后下一次 `speech` / `new_order` 事件自动重新启用 LLM。
  - 降级期间不抑制 VAD 和 ASR，玩家的话仍然被转写，只是不送 LLM、不生成回复。
- 所有网络调用都在 worker 线程，主循环不做任何阻塞 I/O。

## 8. 音频

### 8.1 设备

**使用 Mac 自带麦克风和扬声器（v1 的取舍）**：

- 输入：Mac 内置麦克风（或外接 USB 麦克风），16 kHz 单声道；
- 输出：Mac 扬声器，同时播放游戏声和 TTS；
- **回声消除的现实**：`sounddevice` 走 PortAudio/CoreAudio，**不会自动获得** macOS VoiceProcessingIO 的 AEC。内置麦克风会收到扬声器里的游戏声和 TTS，这是 v1 的已知限制。
- v1 的缓解策略：
  1. **TTS 播放时暂停 VAD**：P2 说话期间麦克风输入不送 ASR，避免自听；代价是 v1 不支持 TTS 期间插话（半双工，见 §5.4）；
  2. **VAD 阈值调高**：RMS 阈值从 6 dB 调到 9 dB，降低误触发；
  3. **TTS 音量适中**：默认相对音量 0.7（而非 1.0），减少泄漏到麦克风的能量；
  4. **游戏音量适中**：演示前把游戏音量调到中等，减少游戏声触发 ASR。
- 如果实测仍有严重回声（游戏声被当成玩家输入、触发循环），第二版再引入软件 AEC（WebRTC AEC / speexdsp）或改用带硬件 AEC 的会议全向麦；这不在 v1 范围内。
- 设备按名字选，不假设默认设备；`audio.py` 提供 `list_input_devices()` / `list_output_devices()`。
- 输出按设备采样率，必要时用 `numpy.interp` 重采样。
- macOS 首次运行需要给终端/Python 麦克风权限；键盘注入仍需辅助功能权限。

### 8.2 VAD

- 初版用 RMS + 自适应噪声底 + 300 ms 拖尾，不引额外依赖。
- 开口判定：连续 3 帧超过噪声底 9 dB（比耳机方案的 6 dB 更高，降低误触发）；结束判定：连续 400 ms 低于阈值。
- 最短话长 300 ms，过滤咳嗽/键盘；最长 8 秒，超时强制切段。
- **TTS 播放期间暂停 VAD**：P2 说话时麦克风输入不进 VAD 判断，避免自听触发 ASR；这与 §5.4 的半双工一致。
- 如果实测误切多或回声严重，再考虑：
  1. 调整 RMS 阈值（9 dB → 12 dB）；
  2. 引入 WebRTC VAD 或 Silero VAD；
  3. 引入软件 AEC（WebRTC AEC / speexdsp）。

### 8.3 播放

- TTS 音频块进播放队列，`sounddevice` 输出流连续播放。
- **TTS 播放期间暂停 VAD**：不处理玩家插话；播放结束后恢复 VAD（v1 半双工）。
- **音量平衡**（Mac 扬声器外放方案）：
  - 默认 TTS 相对音量为系统音量的 **0.7**（而非 1.0），给游戏声留出空间，也减少泄漏到麦克风的能量；
  - 可通过环境变量 `OC2_VOICE_VOLUME=0.5` 或 `1.0` 调整（范围 0.3–1.2）；
  - 不做自动音量检测或动态 ducking（会增加复杂度且效果不稳定）。
- 播放实现用输出流/回调，不用阻塞式 `sounddevice.wait()`；这样 worker 在播放期间仍能处理其他队列（事件、ack、关闭）。

## 9. 依赖与权限

新增运行时依赖：

```text
sounddevice   # 采集/播放（自带 PortAudio）
numpy         # 分帧、RMS、重采样
websockets    # 火山 ASR/TTS 的 WSS
```

现有项目运行时只有 Quartz，这是一次明确的依赖扩张，需要写进 `AGENTS.md` 和 `docs/agent/architecture.md`。`requests` 已在环境里，方舟的 SSE 可以直接用。

权限：

- 麦克风：macOS 隐私 → 麦克风 → 终端/Python；
- 辅助功能：继续用于按键注入；
- 不需要新的系统级权限。

## 10. 测试与验收

### 10.1 自动化

- `tests/test_dialog_schema.py`：JSON 解析、非法 task_type、非法 branch、候选绑定失效降级、`addressed=false`、单回复 + ack 抑制、动作族匹配、`resume_order`。
- `tests/test_preferences.py`：角色/认领/派活/指令的权重命中；分支级认领覆盖整条链（含未来候选、`FillCooker`、`Prefetch`）；`FetchToPlate` 被 chop 族覆盖；类型/分支级命令在未来候选上生效；没有脏盘子时「去洗碗」存成 pending，盘子出现后生效；TTL（12/30 秒）；证据续期回到 `base_ttl`（不是统一 240）；承诺被认领时立即清空。
- `tests/test_rescue_gate.py`：`cushion = solo_slack`；`0 <= cushion < margin` 时抢关键路径；`cushion < 0` 时不抢；非关键路径不抢；L0/L1/L2 不受认领约束；`RescueCooked`/`ReturnPot`/`RecoverLoosePot` 不受认领约束；冷却期不来回切；fallthrough 逐层向下。
- `tests/test_p2_intents.py`：「去洗碗」在高层未清时保持正向权重、不越过层；「先别煮饭」硬压制 8 秒；「先别煮」后又说「你去煮」能解除压制；「这单不要了」硬排除；`resume_order` 恢复。
- `tests/test_dialog_policy.py`：四个 LLM 入口；`new_order` 合并（开局第一批 + 1 秒窗口）；`rescue`/`help` 事件投递；主动开口 4 秒间隔（`speech` 不受限）；同意图不重复；紧急线走模板；`assign_expired` 不调 LLM；TTS 播放期间 VAD 暂停。
- `tests/test_dialog_integration.py`：真实帧 fixture + 脚本化 transcript，验证「玩家认领送餐 → P2 选低层」「玩家派低层活但高层未认领 → P2 仍选高层」「认领米饭 → 米链全让、黄瓜链不受影响」「没说脏盘子 → pending，盘子出现后去洗」「ack rejected 时抑制承诺」「截止闸抢单并说 overridden」「L2 刚熟不被认领挡住」。
- `tests/test_audio_vad.py`：设备枚举、分帧、RMS、VAD 暂停、TTS 播放期间不触发。
- 真实 API 冒烟测试放在 env flag 后面，不进 CI。

所有测试用 fake ASR/TTS/LLM/audio，不发网络请求、不占麦克风。

### 10.2 验收标准

1. 说「你先别煮饭，去把盘子洗了，我来切」，2.5 秒内 P2 有语音回应；若场上存在未认领的送餐候选，P2 先送餐并说明理由；否则去洗盘；P2 不再碰切菜（含同 item 的 FetchToBoard/FetchToPlate）。
2. 说「我来煮饭」，P2 立刻让出整条米链（取米、下锅、端饭上盘），黄瓜链不受影响；正在执行米链时也能停。
3. 说「我来送」，P2 下一拍放弃送餐候选；玩家 12 秒内没送但正在朝盘子/出餐口走，认领续期；完全没动，P2 接手。
4. 「我来煮饭」且订单进入最后窗口（`0 <= cushion < margin`）时 P2 抢回，并说「来不及了，我先煮」；`cushion < 0` 时不抢。
5. 说「去洗碗」时没有脏盘子：P2 回「现在没盘子，等有了我洗」，命令保留；30 秒内出现脏盘子 → P2 去洗；一直没出现 → 失效。
6. 新订单（含开局第一批）在 3 秒内有一次分工提案；1 秒内的多张新单只调一次 LLM；同一意图不重复；主动开口间隔 ≥4 秒。
7. 着火事件到 TTS 首包 <800 ms；P2 的实际动作与播报一致。
8. 无自听回声（TTS 播放期间 VAD 暂停）；对话层不造成主循环单拍 >100 ms；端到端 P95 <2.5 s。
9. 断网时游戏继续跑，紧急模板仍可用，进程不崩。
10. 现有测试全部通过；新增测试覆盖 §10.1。

## 11. 非目标 / 反目标

非目标：

- 不做吐槽、甩锅、闲聊、情感陪伴；
- 不做声音克隆、不做多语言；
- 不做唤醒词、不做按住说话（调试模式除外）；
- 不做菜谱级整单分工（「你做黄瓜寿司，我做鱼寿司」）；
- 不做 TTS 播放期间打断（v1 半双工；第二版再支持 barge-in）；
- 不让 LLM 进入 20 Hz 控制循环，不让它直接发按键；
- 不替代现有规则规划器和收益模型；
- 不支持线上联机语音。

反目标（出现即视为失败）：

- 一直说话、重复、抢话；
- 说一套做一套（说去洗盘，实际去煮饭）；
- 把自言自语当成命令，频繁误接；
- 玩家认领了却被忽略，或认领永远不过期；
- 网络抖动导致游戏卡顿/停摆；
- 为了对话把现有决策质量拖垮。

## 12. 我的取舍（可改）

以下是对话里没定、我按常识先定的，评审时可以直接推翻：

1. **语音默认开启**：有环境变量就开，没有就告警并退回无语音模式。
2. **默认音色**：年轻自然女声，语速 1.05，名字「小厨」。
3. **权重**：角色 ±1.2；派活 -4；明确认领 -8；玩家指令 +4；都用真实帧校准。
4. **TTL**：玩家认领绑候选 12 秒、类型/分支级 30 秒；玩家给 P2 的指令 30 秒；P2 派活 8–20 秒；角色活到关卡结束；`abandon_order` 60 秒。
5. **接手前提醒**：`assign_expired` 只走低优模板，玩家明显空着才说「那我切了啊」；不调 LLM。
6. **VAD**：先用 RMS，不引 WebRTC VAD；TTS 播放期间暂停。
7. **紧急线**：火/快糊/快超时走模板，不赌 LLM 延迟；非紧急才让 LLM 措辞。
8. **对话日志**：单独 `voice-*.jsonl`，记录 transcript、LLM 原始输出、命令、ack、权重、各段延迟；已 gitignore。
9. **认领粒度**：branch 用生料名；「我来送」默认认领整个 `DeliverPlate` 类型；带「这单」才绑 `candidate_key`。
10. **截止闸**：只对 `claim` / `assign` 生效，不对 `role` 生效；L0/L1/L2 和救援类不受约束；`cushion = solo_slack`，`margin = max(5 秒, min(0.3 × D, 10 秒))`。
11. **动作族**：手工维护 §4.5 的小表，不做通用推断；救援类不参与认领。
12. **播报顺序**：先命令、后 ack、再播一句；ack 只做校验/抑制，不选多套回复；`rejected` 时抑制原话。
13. **紧急线命名**：不发明锅号/订单号，用菜名和事实指代。
14. **P1 证据**：手持 + 站位/移动都算，从第一版就做；只用于续期，不做可靠度衰减。
15. **明确否决**：`abandon_order` / `cancel_p2` 是硬规则，不参与权重；`resume_order` 和同族新命令可以解除。
16. **第一版范围**：按 §4.9，不做菜谱级整单分工、交接、暂停、条件通知、超出上下文的查询、屏幕方位指代、TTS 期间打断。
17. **LLM 触发**：只有 `speech` / `new_order` / `rescue` / `help` 四个入口；`assign_expired` 和紧急线走模板。
18. **单回复**：LLM 只写一句；拿不准时用条件句。ack 的 `pending` / `deferred` 不要求它预写变体。
19. **音频**：v1 用 Mac 内置麦克风 + 扬声器，接受半双工和游戏声干扰；第二版再上软件 AEC / 会议全向麦。

## 13. 参考

- `docs/notes/multiplayer_comm_research_20261018.md`：玩家实际交流内容。
- PUBG Ally（KRAFTON + NVIDIA，2026）：System 1 行为树 + System 2 语言模型；有界工具接口；语音与动作同步。<https://arxiv.org/abs/2609.29837>
- DPT-Agent（上海交大，2025）：双过程理论，快系统实时反应 + 慢系统策略推理，Overcooked 环境。<https://arxiv.org/abs/2502.11882>
- HRT-ML（USC，2024）：Coordinator 低频战略 + Manager 高频指令；提示过多/不准会拖累表现。<https://arxiv.org/abs/2412.06808>
- Collab-Overcooked（2025）：LLM 多智能体在 Overcooked 里的自然语言协作评测。<https://arxiv.org/abs/2502.20073>
- 火山引擎流式 ASR：<https://www.volcengine.com/docs/6561/1354869>
- 火山引擎流式 TTS / 豆包语音：<https://www.volcengine.com/docs/DoubaoVoice/ProductOverview-7>
- 火山方舟流式输出：<https://www.volcengine.com/docs/82379/2123275>

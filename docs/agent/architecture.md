# 技术栈、目录结构与核心架构

本文是 `AGENTS.md` 的详细补充，面向需要了解实现细节的 AI 编码代理。`ai/` 内的注释大量引用「spec §N」，所指是 `docs/superpowers/specs/2026-10-04-ai-behavior-architecture-design.md`。

## 1. 技术栈与运行架构

- **语言/运行时**：Python 3.9.6（macOS 系统 Python）。无打包、无构建步骤，`ai/` 是纯库，通过 `pytest.ini` 的 `pythonpath = .` 直接 import。
- **依赖**：运行时除标准库外无第三方依赖（决策与执行都是纯 Python + 本地 socket）。开发依赖仅 `pytest==7.4.4`（见 `requirements-dev.txt`）。语音对话为可选链路，额外依赖见 `requirements-voice.txt`（`sounddevice` / `numpy` / `websockets` / `requests`），只在启用语音时延迟加载。没有 `pyproject.toml` / `setup.py` / `package.json`。
- **数据链路**：C# BepInEx 插件（`plugin/Oc2AIPlugin.cs`）5 Hz 推 NDJSON → `ai/state/world.py` 的 `StateClient` 按 `\n` 分帧 → `World` 解析快照。
- **控制链路**：叶子原语经 `ctx["exec"]` 发命令（`ai/exec/commands.py` 的 move/tap/hold/throw/halt；另有 ping 仅用于启动时确认通道活着）→ `ExecClient`（`ai/exec/client.py`）写 27412 → C# 插件逐帧驱动 InControl 虚拟手柄控制 P2（无需 macOS「辅助功能」权限）。离线/测试用 `NullExec`（同文件，命令即时回 ok 不连 27412）。
- **频率约定（关键，容易混淆）**：插件推送 **5 Hz**；Python 主循环 **20 Hz**（`TICK_INTERVAL = 0.05`）。代码里「拍」（tick）统一指 **Python 循环拍 = 50 ms**，所有按拍计的阈值都按 20 Hz 理解。实现上主循环用 `StateClient.read_latest()` **非阻塞读**、中间 4 拍复用最新帧重 tick——不能用阻塞的 `read()`，否则会被 5 Hz 推帧拖成 5 Hz（曾踩坑）。

## 2. 目录结构

`ai/` 是纯库（无 `__main__`），唯一入口是根级 `run_agent.py`。其余顶层目录：`plugin/`（C# BepInEx 插件 = 数据源）、`tests/`、`tools/`（开发工具，非运行时）、`docs/`、`.superpowers/`（SDD brief/report/review）。

`ai/` 各模块职责（`agent.py` 是根级入口，其余按职责分到子目录）：

| 子目录 | 模块 |
|---|---|
| 根级 | `agent.py` 装配（收帧→对账→推断→卡住观测→规划→执行→日志）· `primitives/` 原语 · `tasks/` task（对外只认 `from ai import tasks as T`） |
| `core/` 主循环 | `arbiter.py` 卡住检测（供 trace 观测） · `planner.py` 候选→可行性→协作过滤→打分→承诺惯性（最大文件）· `planner_layers.py` 分层过滤 · `executor.py` task 树生命周期 |
| `state/` 状态 | `blackboard.py` 唯一可变状态容器 · `bb_factory.py` 其构造 · `world.py` StateClient 分帧 + World 只读视图 · `reconcile.py` 每拍认领对账 · `p1model.py` P1 事实（holding）+ 站位软代价 |
| `query/` 查询 | `predicates.py` 纯谓词（无副作用、不依赖 World 类）· `recipes.py` RecipeBook 配方语义 · `recipe_graph.py` 配方 DAG 编译 + 候选发射 · `plate_tokens.py` 盘/锅内容 token 解析 · `stations.py` 站点检索 · `placement.py` 落点约束 |
| `space/` 空间 | `navigation.py` Dijkstra 距离场 · `occupancy.py` 可走性 + 实体去重 · `geometry.py` 网格↔世界坐标 · `seams.py`/`seam_clock.py` 平台接缝门洞与开关时间表 · `shuttle.py` 渡运台面（隔围栏够的移动台面：格外站位 + 可交互窗口） · `channels.py` 跨区通道（渡运台面 + 两边都够得着的静态共享台面） · `portals.py` 传送门（触发格对寻路不可走 + 过门跳边 + move 过门段；寻路参数统一走 `portals.nav_args`） · `lanes.py` 半格走廊（通道夹在两排格中心之间时的寻路跳边 + move 拐点） |
| `revenue/` 收益 | `economy.py` 小费/连击 · `eta.py` 订单 ETA/slack/必死 · `cook_supply.py` 熟料供需（见下） |
| `output/` 输出 | `trace.py` 决策日志 · `state_dump.py` 快照压成可序列化 dict |
| `exec/` 执行/回放 | `client.py` ExecClient/NullExec · `commands.py` 命令 schema · `bt.py` 行为树引擎（有记忆的 Sequence/Selector + RepeatUntil/Guard）· `record.py`/`replay.py` 录帧回放 |
| `dialog/` 对话（可选） | `schema.py` 偏好/命令/`VoiceConfig`/LLM JSON 校验 · `policy.py` 主动开口/限频/紧急模板 · `worker.py` 后台 daemon：PTT/VAD→ASR→LLM→TTS · `audio.py` 采集/播放/RMS VAD · `ptt.py` 全局按键 PTT（Quartz event tap）· `asr.py`/`tts.py`/`llm.py` 云客户端 · `env.py` `.env` 加载 |

## 3. 核心架构

三层 + 黑板（spec §3）：

```
World 快照 → 认领对账 → 更新 P1Model → 卡住观测
  → Planner 选 task（火情走 L_FIRE 顶层）→ Executor tick 成按键 → 输出 trace
```

- **Arbiter**（`arbiter.py`）：只保留卡住检测 `_is_stuck`（维护 `bb.scratch.same_count` 供 trace 观测），不再决定模式。火情由 Planner 的 `L_FIRE` 层（`Extinguish` 候选）统一处理：候选生成门 `_fire_needs_ai`（有 onFire 灶 + 灭火器可得 + P1 未在处理），`Extinguish.NO_ABANDON` 让它免疫拉黑，承诺惯性里显式放行火情抢占。实际脱困靠 `HoldUntil` 位置自愈 + Agent 连续失败计数写 `scratch.abandoned` + 执行层 `motion.stalled`。
- **Planner**（`planner.py`）：纯函数步进器。`select_task(world, bb, order, book)` 走：
  生成候选（枚举所有订单，Plan 4 多订单模式）→ 可行性过滤（`claims_available` 前置 +
  `_is_abandoned` 拉黑检查 + 各 task 的 `feasible()`）→ **分层过滤（取最高层，见
  `planner_layers.py`）** → 打分（该层白名单内的项）→ 承诺惯性（防抖）。
  **无状态每拍重算**，执行顺序从可行性涌现，不建 DAG、不拓扑排序。层是派生值：
  高层动作不可达时该候选自然从 feasible 消失、层自己掉下来。
- **Executor**（`executor.py`）：把 task 换成一棵小的 `bt` 行为树，每拍 tick 产出按键。task 身份（四元组，见 `tasks-primitives.md`）不变就复用同一棵树（进度自然保留），变了就 halt 旧树重建；每棵树包一层 `Timeout` 做最后兜底，默认 `MAX_TASK_TICKS=300` 拍，task 可用类属性 `TIMEOUT_TICKS` 声明自己的上限（灭火是两趟差事，用 `FIREFIGHT_TIMEOUT_TICKS=800`）。
- **Blackboard**（`blackboard.py`）：唯一可变状态容器，取代旧 Agent 上散落的约 20 个跨拍字段。写权受控（spec §4.5）：Planner 写 `claims`/`commitment`，P1Model 写 `p1_model`，Executor 只写 `commitment.steps_done/retries` 与 `scratch`（**不可写 claims**）。
- **World**（`world.py`）：一帧快照的**只读**视图，构造时只做解析 + 派生数据装配；可走性与寻路分别委托 `occupancy` 与 `navigation`。
- **Trace**（`trace.py`）：每拍写一条 JSONL 记录（模式/候选/打分/选中 task/执行状态/按键），落盘仓库根 `runs/trace-<时间戳>.jsonl`（已 gitignore），用于复盘「为什么选这个动作」。另有 `revenue` 摘要（分数/连击/剩余时间/订单 + 送达/连击归零/订单过期三类事件），省去手工 diff 全局 state。

### 3.1 收益模型与产能

这三个模块回答「为什么会 miss、怎么多赚分」。常数**全部来自真实 trace 回归**，不是经验值——改之前先看模块注释里的实测表：

- **`economy.py`** —— 收益函数。小费 = 新鲜度档位（8/5/3，按 `remaining/lifetime` 落在 >2/3、1/3~2/3、<1/3）× 连击系数（combo < 3 恒为 1，之后 `combo-1`）；`base` 固定 60/单，与早晚无关。**超时不扣分**（`EXPIRY_POINT_PENALTY = 0`，社区攻略说的 −10 coin 实测不存在），代价只在连击归零 + 沉没工时。由此定出优先级：**不丢单 >> 按序送 >> 送得新鲜**（新鲜度最多差 5 分，相对 base 60 仅 8%）。52 次送达逐条断言。
- **`eta.py`** —— 订单 ETA / slack / 必死判定。单位耗时取实测中位（见模块注释的表，两局「拍/格」6.2 与 5.9 吻合，已含移动损耗故不再乘裕度）。两个关键建模：① **烹饪是关键路径不是求和**（煮饭 12 秒里人自由，取 max）；② **多单共享一双手**，按最早截止排队后累计手上工时，且**判死的单不占队列容量**（否则判死会沿队列雪崩）。校准基准：一份寿司 ≈ 35~40 秒手上工时 → 130s 寿命最多托住 3 单。
- **`cook_supply.py`** —— 熟料供需。`demand` 只数需求、`supply` 数「在煮 + 已熟未端 + 手持 + 已装盘」，**已装盘的那份只能在一侧扣减**（两边都扣会让缺口被重复减去，这个 bug 单测抓不到、只有真实帧能暴露）。planner 侧再用 `_fill_plate_factor` 按「已匹配到盘的订单数」给填灶缺口封顶，避免为当前上不了盘的订单把灶占满。

**产能（为什么需要 `FillCooker`）**：`FetchToCooker` 绑订单，只能由 `recipe_graph.emit` 按单发射，一份熟料在一单里只发一个下锅候选，`_match_cookers_to_orders` 又是严格 1:1 —— 结构上「5 单要米」就不可能变成「3 口灶在烧」。实测 trace-20261014-120406 的 1700~3270 窗口 **59% 的拍里三口灶一个都没在煮**，而三单全在这窗口过期。灶空转的机会成本是 12 秒不可压缩的时钟，所以用新 task 换结构，而不是调权重。

**combo 的真实机制**（57 次送达假设检验，两条规律各自无反例）：
1. 送出一盘 dish D，游戏把它记给「同 dish 存活单里 `remaining` 最小」的那个（55/0）。**这不是我们能选的。**
2. combo 递增 ⟺ 被记账的那单是「`id` 数值最小」者（57/0；对比假设「remaining 最小」只有 43/14 —— 两者分歧是因为订单过期后 id 会被复用）。

**推论：AI 的杠杆是「选哪种菜」，不是「选哪一单」。** `DeliverPlate(order#X)` 里的 X 对 combo 毫无影响，所以送餐的紧迫度/新鲜度/完成度统一按 `_credited_order_id` 求值。又因为实机「料齐的盘」几乎从不同时有两张（本局 0 盘 4279 拍、1 盘 298 拍），送餐那一刻没有分叉，combo 偏好必须传导到**备菜**才有用（`COMBO_PREP_DISCOUNT`）。

### 3.2 对话层线程边界（spec §6.2）

- 主循环固定 **20 Hz**，仍是唯一碰 `World` / `Blackboard` 的地方；每 10 拍（**2 Hz**）把压缩后的上下文 `publish_context()` 给 worker，新快照覆盖旧快照。
- `DialogWorker` 跑独立 **daemon 线程**（内部可用 asyncio），只消费 context / 事件、产出 `DialogCommand` + ack；**不读 World、不写 Blackboard**。
- 输入切句支持两种模式：默认 RMS VAD（自适应噪声底）；设置 `OC2_VOICE_PTT_KEY` 后改用全局按键 PTT（`ai/dialog/ptt.py` 的 Quartz listen-only event tap），按住录音、松开发送，一次按住只触发一轮对话。PTT 监听失败会记录 `worker_error where=ptt` 并退回 VAD。
- 命令回到主循环做权威校验后才写 `bb.preferences` / 硬否决；worker 等到 ack 再决定播不播 LLM 的回复（`rejected` 静默）。
- 主循环用非阻塞队列与服务衔接，语音网络抖动或播放阻塞不会把 20 Hz 拖慢；关闭顺序是 Agent → worker → 游戏。
- 语音整个链路可缺席：缺 key（无 `--voice`）时 `voice=None`，主循环没有 worker，行为与纯按键版一致。

### 3.3 攻略与分工

- `ai/dialog/cookbook.py` 读取 `docs/guides`：先按「内部关卡名」精确匹配，主线再按 `{主题}_{世界}_{关}` 回退到 `story/{世界}-{关}.md`。攻略里的人称已统一为「你（AI）/ 玩家（人类）」，运行时不再转换。
- 开局把「关卡机制 / 打法与分工 / 陷阱」摘要注入 system prompt（稳定前缀，随关卡只变一次）；模型需要细节时调用 `read_cookbook(section)`。
- 分工的唯一写入入口是 `set_division`：`action` 枚举按本关站点动态生成（切/下锅/拌/装盘，送餐/洗盘不进分工），可选 `item` 做物料级细分，`owner` 为 `you/player/both`。主循环校验后写入 `bb.division`，编译成 planner 的 `Preference`（`you` direct +1、`player` claim -6、`both` 不加权）。
- 动态复核：每 1 秒比较一次站点拓扑签名；签名不变就不调 LLM。站点移动/出现/消失连续 2 秒确认才发 `division_review`，两次 review 至少隔 8 秒。转盘、梯子这类纯路径变化不触发——3-3 的左半场在单网格寻路之外，用路径可达性改分工会把「能装盘」误报成「够不着」。review 回合的 `speak` 先缓冲，只有 `set_division` 真的改了分工才播放。
- 3-2 这类「两人都要切不同物料」的关：模型应按 `item` 分别写 `chop Meat→玩家`、`chop Lettuce/Cheese→你`；分不清就整个动作不写，避免把某一侧切菜能力误压掉。

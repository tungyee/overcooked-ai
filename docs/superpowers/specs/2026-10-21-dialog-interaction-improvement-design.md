# 语音对话交互改进设计（2026-10-21，定稿）

## 1. 意图与范围

收集到的用户吐槽 + 后续讨论，最终收敛为 **6 个问题**。共同主题是：对话层能听懂、
但「听懂」之后没有可靠地传导到行为，且 AI 的陈述与真实世界脱节、互动太机械。
本文是定稿方案，覆盖全部 6 个问题的根因（含 `file:line`）与修法，供 Plan 落地。

## 2. 问题清单与根因

| # | 用户视角 | 根因（代码证据） | 修法 |
|---|---|---|---|
| 1 | 「让他干啥，经常干其她的」 | `do` 只是同层 +4 软加分（`duties.py:45`、`planner.py:1836`），被紧急单 urgency 5.0 压过（`planner.py:581`），也被更高层结构性压过（`planner_layers.py:72-83`） | 新增 `now`（马上去）硬指令 |
| 2 | 「切黄瓜当成有空去切」 | 同上：`do` 语义就是「优先」，从来没有「现在做」这一档 | 同上 |
| 3 | 「我说黄瓜我来，AI 还是去切」（我完全来得及） | `leave_to_p1` 的 -8 在 `_select_layer` 被整层跳过（`planner.py:1529-1566`），但**截止闸** `_rescue_gate_open` 在 `0 <= cushion < margin`（margin 5~10s）时把它抢回（`planner.py:1413-1430`）；而 `cushion = solo_slack`（`eta.py:119`）把 P1 贡献当 0，天生悲观 → 你正在做它也抢 | **收闸**（收紧 margin） |
| 4 | 「这关没黄瓜说有黄瓜」 | 换关重置只看关卡名（`agent.py:418-419`），同图重开 `scene_epoch` 变了却不触发 worker 历史重置 | 换关改用 `(session_id, scene_epoch, level)` 判变 |
| 5 | 「脏盘子只有一个」是编的 | 脏盘数从没进 LLM 上下文（`_plates_text` 只数空盘/装料盘，`agent.py:180-197`） | 上下文补脏盘数 |
| 6 | 「一句进一句出，没互动」 | 主动开口只有 `new_order`；无定时送单播报；prompt 无 turn-taking 引导 | 定时送单 check-in + prompt 引导 |

**附带（并入 #1 同一主题）**：`stop`（「先别 X」）在 planner 已完整实现且有测试，
但 LLM 侧不可达——`duties.py:34` `MODES = ("do", "leave_to_p1")` 不含 `stop`。
「听指挥」的反面（别做 X）现在是断的。

## 3. 目标 / 反目标

**目标**：用户说「马上去 X」当拍接手（除火情/糊锅/救熟锅）；「先别 X」真正生效；
「我来 X」在 P1 有把握时不被抢回；换关不串味；AI 陈述有真实数据支撑；对话有来有回。

**反目标（做到即失败）**：用户指令压过灭火/糊锅；`do` 变强到丢掉「有空优先」；
截止闸收太死导致 P1 放鸽子时单子必死；AI 变话痨。

**非目标**：不动任何 `W_*` / 分层阈值 / `COMMITMENT_MARGIN`；不做 LLM 回复通用事实
校验；不做 barge-in；不做 P1 进度推断（本轮明确不做）。

## 4. 设计（7 个工作流）

### A. 命令语义：新增 `now`（马上去）

- `duties.py:34` `MODES = ("do", "leave_to_p1", "now", "stop")`。
- `duties.preferences()` 把 `now` 编译成 `Preference(kind="now", owner="P2")`。

**A.1 `now` / `do` 语义与 prompt 映射**（`llm.py:188-203`）：
- `do`＝「有空优先」（你负责 / 归你 / 你来管 / 你包了），仍是 +4 软加分；
- `now`＝「马上去」（去X / 马上X / 先X / 现在X / 帮我X）；
- `leave_to_p1`＝「我来X / X交给我」；`stop`＝「先别X / 别做X」。
- 由 LLM 在 tool call 里选 mode，prompt 给映射 + 正反例；**拿不准默认 `now`**
  （把「去X」误判成 `do` 软处理的代价，远大于把「负责X」误判成 `now` 多做一点）。
  不做正则兜底。

**A.2 硬指令闸**：在 `planner.plan()` 可行性过滤之后、`layer_filter` 之前
  （`planner.py:1157-1166` 与 `:1174` 之间）加 `_select_imperative(feasible, world, bb)`：
  - 找出命中未过期 `now` 的可行候选；
  - **让位给 L0/L1/L2**（火 / 快糊 / 刚熟救锅）：feasible 里只要有这些层候选就不走闸；
  - 否则直接选命中候选里 `feasible` 顺序的第一个（链的下一步），跳过层选择、
    打分、承诺惯性；decision record 标 `imperative` 供 trace 复盘。
- 每拍重算，指令落地当拍即切；任务做完/不可行后 `now` 不再命中，自然失效。

**A.3 `now` 必须能覆盖软闸（以洗盘为例）**：`WashPlate.feasible()` 把「该不该
  现在洗」当成了硬 feasibility（`plate.py:246-247`：有干净空盘或台上装配盘就
  `return False`），所以干净盘还有剩时 WashPlate 进不了 feasible，`now` 也选不到。
  修法是拆开「能不能洗」与「该不该现在洗」：
  - `feasible()` 只保留物理约束：`dirty_plate_exists` + 续洗态 + `done`；
  - 「该不该洗」交给 layer：干净盘稀缺度已有 `_plate_pressure`（`planner.py:1836-1845`），
    但 `placed_plates`（`base.py:246-264`）不在这条曲线里，拆分时必须一并纳入 layer
    判断，否则「台上装配盘 + 0 干净盘」会把 WashPlate 顶进 L4、压过 FetchToPlate，
    召回「装配进行中却去洗盘」的实机 bug（`plate.py:241-244` 注释）；
  - 拆完后 `now` 洗盘 = 跳过 layer 直接选物理可行的 WashPlate，干净盘有剩也能洗。

### B. 接通 `stop`（「先别 X」）

- `duties.py` 的 `MODES`、`tool_schemas` mode enum（`duties.py:456-457`）、prompt
  （`llm.py:188-203`）各补 `stop` 与「先别 X / 别做 X → stop」映射。
- planner 侧 `_is_stopped`（`planner.py:380-401`）与 `_clear_claimed_commitment`
  （`planner.py:1601-1603`）已就绪，不改。

### C. `leave_to_p1` 收闸

- 只改 `_rescue_margin_ticks`（`planner.py:1400-1410`）的余量窗口，从
  `max(100, min(0.3*d, 200))`（5~10s）收窄到 `max(50, min(0.15*d, 100))`（2.5~5s）。
- `_rescue_gate_open` 的 `0 <= cushion < margin` 条件与 rescue cooldown 不变。
- **诚实标注**：收闸是缓解不是根治——`solo_slack` 仍看不见 P1，任何 margin 都会在
  P1 其实做得完时偶尔抢回；彻底解需要 P1 进度信号（本轮不做，留待后续）。
- 落点校准走 `tests/test_rescue_gate.py` + `tools/replay_urgency.py` 重放，确认
  「P1 有把握时不抢、真放鸽子时还抢得回来」的平衡点。

### D. 换关重置：改用场景身份

- `agent.py:404-432` `_maybe_reset_dialog_level` 的判变从 `world.level` 字符串改成
  场景身份 `(session_id, scene_epoch, level)`（与 `_observe_scene` `agent.py:860`
  同口径），存 `self._dialog_scene_key`。
- 场景身份变化（含同图重开）走完整重置：`ledger.clear()`、`bb.preferences` 清空、
  `bb.dialog = DialogState()`、`_seen_orders = {}`、`_drain_dialog_queues()`、
  `dialog_worker.reset_state()`。
- `world.level is None`（菜单/加载帧）仍提前返回不重置。

### E. 事实落地：上下文系统性规整

在既有上下文字段（`agent.py:559-612` `_build_dialog_context`）基础上，补一批纯读、
每拍现算、且能给定时 check-in 提供话料的字段：

| 字段 | 数据源 | 话料 |
|---|---|---|
| 脏盘数 | washing 站 `plate_count` + `items` 里 DirtyPlateStack（口径对齐 `ai/tasks/plate.py:68-115`） | 「脏盘还有 N 个」 |
| 砧板状态 | `workstations` 里 chopping 板内容 | 「砧板上黄瓜切一半了」 |
| 空灶数 | `world.cookers` 空灶计数 | 「还有 N 口灶空着」 |
| 连击/分数 | `world.raw.score` + trace revenue 事件（送达/连击归零/过期） | 「刚送出一单」「连击断了」 |
| 快到期单 | `orders` 里 remaining 低于阈值的单高亮 | 「#3 只剩 15 秒」 |

- 脏盘数：新增 `_dirty_plate_count(world)`，`_plates_text`（`agent.py:180-197`）
  末尾追加「脏盘 N 个」。
- 明确不加：P1 位置 / P1 正在做什么（`p1model.py:9-11` 短程意图推断已删、插件不发
  对应字段）；P1 侧只有 `holding` 可用。
- prompt（`llm.py:183`）补「脏盘/空灶等数量以【世界状态】为准」。

### F. 互动性

- **F.1 定时送单 check-in（随机排期，替代 rescue 播报）**：
  - 新事件 `checkin`，`_EVENT_TEXT["checkin"]`：按【世界状态】用一两句大白话报
    当前送单局面（剩几单、快不快、有没有要留意的），也可以轻松闲聊。
  - worker 新增 `_next_checkin_at`（monotonic）：每次回合结束（`_run_llm` 末尾
    `stream.wait()` 之后）掷 `time.monotonic() + random.uniform(CHECKIN_MIN_S,
    CHECKIN_MAX_S)`（起步 35~75s，模块常量，实机试完再决定是否升环境变量）；
    `reset_state` 里归 `None`。
  - 主循环里（`vad.paused` 那一跳之后、读音频帧之前）加 `_maybe_checkin()`：当
    `now >= _next_checkin_at` 且不在录音/播放、关卡内有订单时，发一个 `checkin`
    回合；`tool_choice="none"` 只说话、不改账本。
  - 判定抽成纯谓词 `policy.should_checkin(next_checkin_at, now, has_orders,
    in_level)`，跟 `test_dialog_policy.py` 同风格加单测。
  - 随机排期让节奏不机械；任何一次真的开口都会重新掷点，不会「玩家一闭嘴它
    立刻接话」。
- **F.2 prompt 引导**（`llm.py:180-186`）：加「可以主动追问没听清的、提议分工、
  简短报告进度」，保持「一两句、30 字以内」。
- **不做 `rescue` 播报**：收闸后截止抢回已很少，且播报「我来接手」正好戳中
  「你说你来做、AI 又抢」的痛处；抢回保持静默即可（`pending_event` 留着不动，
  不接线、不发声）。

### G. 其它 UX（体验打磨）

- **G.1 卡住 = bug 上报，不做独立播报**：
  - 开发者侧：trace 补一条结构化 `stuck` 事件，记 `task` / `reason`（motion stall /
    observation timeout / infeasible streak）/ `pos`（gx,gz）/ `duration_ticks` /
    `retries`，让 BT bug 可 grep 定位（现有 stall 信息散在 `motion.py` / arbiter，
    缺一个一眼能定位的入口）。
  - LLM 侧：`_build_dialog_context`（`agent.py:603-605`）把卡住信号填进 `p2["stuck"]`
    ——现在 `llm.py:255-256` 读了、但从没人填（死代码）。信号取自 arbiter 同格卡住
    （`arbiter.py:39,52`）或 motion 的 `stalled`（`motion.py:102-105`）。
  - **不新增 stuck 语音触发器**：卡住不 actionable、频繁会烦；轮到随机 check-in 时
    自然带一句即可（随机排期 + 限频已有兜底）。
- **G.2 漏听主动提示**：ASR 返回空/失败时，回一句「没听清，再说一遍」而不是静默
  抑制（worker `_handle_transcript` / `_run_llm` 的兜底路径）。
- **G.3 `now` 允许承诺**：放开 `llm.py:184`「不要承诺『马上』『这就去』」——`now`
  指令当拍就会接手，可以说「这就去」；`do` 仍说「有空就做」。让 AI 说的和做的一致。
- **G.4 语速接线**：把 `OC2_VOICE_RATE`（0.5–2.0）接到 TTS rate 参数（现「仅保留、
  未接到」，见 AGENTS.md），可调语速压「啰嗦感」。

## 5. 约束与测试口径

- 全程不动 `W_*` / 分层阈值 / `COMMITMENT_MARGIN` / 穿插阈值（见
  `docs/agent/status-backlog.md` §4）。
- `now` 的闸是**新增控制流**，不是调权重；`stop` 是接通已有逻辑；收闸只动一个
  margin 常数。
- 会被动到的现有断言：`tests/test_duties.py:313-317`（mode enum 只有 do/leave_to_p1）
  要加 now/stop；换关重置补「同图重开也清 LLM 历史」测试（现有 `test_agent_dialog.py:313`
  只覆盖换名路径）；洗盘拆分要补「干净盘有剩时 WashPlate 物理可行、`now` 能跳过 layer
  选它」与「台上装配盘 + 0 干净盘时仍不洗」两条回归（现有洗盘/供盘测试在
  `tests/test_plate_supply_guard.py` 一带）。
- 实现走 TDD：每个工作流先补失败测试再改实现，最后 `python3 -m pytest tests/` 全量。

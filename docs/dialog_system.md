# 语音对话系统

本文说明 P2 AI 陪玩的语音对话链路：怎么装、怎么配、怎么开、出问题怎么查。设计细节见 `docs/superpowers/specs/2026-10-18-voice-dialog-interaction-design.md`（下文 `spec §N` 均指该文档）。

## 1. 概览

语音链路把「人类 P1 说话」变成「AI 听懂并调整分工，再用一句话回应」：

```text
麦克风 → PTT 按住录音 / RMS VAD 切句 → 火山流式 ASR
      → 方舟 LLM 第一段（function calling：update_duties）
      → 主循环落分工账本、回填工具结果
      → 方舟 LLM 第二段（按真实结果说最终那句话）
      → LLM text 按句流式 → 火山流式 TTS → 扬声器播放
```

第一段 / 第二段的 text 都按句喂给 TTS 流式播放；第一段带工具调用时也不丢弃、直接播（换更快体感，接受「先出口、随后第二段再确认」的少量重复）。

- 代码在 `ai/dialog/`：`audio.py`（采集/播放/VAD）· `ptt.py`（全局按键 PTT）· `asr.py` · `tts.py` · `llm.py` · `policy.py`（主动开口/限频/新单合并）· `worker.py`（后台线程）· `duties.py`（分工账本/物料目录/工具 schema）· `schema.py`（偏好/作用域匹配/`VoiceConfig`）· `env.py`（`.env` 加载）。
- 主循环仍是 **20 Hz** 的唯一决策者；`DialogWorker` 是独立 daemon 线程，只通过上下文快照（2 Hz）、事件、工具批次、ack 四个队列与主循环通信，**不读 World、不写 Blackboard**。
- 语音是可选能力：没装依赖或没配 key 时，加上 `--no-voice`（或什么都不加）就退回纯按键模式，决策行为不变。

## 2. 先决条件

- **macOS + 麦克风 + 扬声器**。v1 只用系统默认输入/输出设备。
- 终端需要 **麦克风权限**（系统设置 → 隐私与安全性 → 麦克风），以及原有的**辅助功能**权限（按键注入）。
- 安装语音依赖（未启用语音时不需要）：

  ```bash
  pip3 install -r requirements-voice.txt   # sounddevice / numpy / websockets / requests
  ```

- **先做独立冒烟（不启动游戏）**：拿到 key 后按顺序跑 `tools/voice_smoke.py`，把设备、火山和方舟的问题分开定位。脚本默认读取仓库根 `.env`（已有环境变量优先），不会打印密钥，音频落在系统临时目录 `oc2-voice-smoke/`：

  ```bash
  python3 tools/voice_smoke.py doctor             # 依赖 / 环境变量 / 音频设备
  python3 tools/voice_smoke.py tone               # 扬声器（不需要 key）
  python3 tools/voice_smoke.py record --seconds 3 # 麦克风电平（不需要 key）
  python3 tools/voice_smoke.py tts                # TTS → 扬声器
  python3 tools/voice_smoke.py asr                # TTS → ASR（不用麦克风）
  python3 tools/voice_smoke.py asr --source mic   # 麦克风 → VAD → ASR（不测 PTT）
  python3 tools/voice_smoke.py llm                # 方舟一轮真实 JSON
  python3 tools/voice_smoke.py full               # 麦克风 → ASR → LLM → TTS
  python3 tools/voice_smoke.py all --with-mic     # 能自动跑的都跑一遍
  ```

  退出码：0 通过（`all` 模式下缺 key 的步骤记为跳过）；1 有真实请求失败；2 单模式缺配置/依赖。

- **历史 trace 回放 LLM**：`python3 tools/replay_dialog_llm.py --trace trace-<时间戳>.jsonl` 从 trace 的 `state` 快照重建 World，按局面选帧，走线上同一条路径（worker 两段式 LLM → 主循环落账本 → planner 重选），逐轮打印回复、工具调用、账本和 P2 的实际选择，并按期望检查（闲聊不调工具、一句两处分工都落、改口用 edit、叫停/恢复、截止抢回的话术与节点一致等）。`--dry-run` 只选帧打印 prompt；`--show-prompt` 每轮打印完整消息；`--scenario NAME` 只跑指定场景。

- **PTT（推荐）**：设置 `OC2_VOICE_PTT_KEY=f8` 后，按住 F8 录音、松开发送，完全绕过 VAD 的起止判断。一次按住 = 一轮对话：录音会带约 300 ms pre-roll 和 200 ms 尾音，ASR 返回的多个 definite 片段会先合并再送 LLM，避免一句话被拆成多次回复。
  - macOS 默认把 F8 当媒体键：需要 **Fn+F8**，或在「系统设置 → 键盘 → 键盘快捷键 → 功能键」里启用「使用 F1、F2 等键作为标准功能键」。
  - 当前只监听键盘事件。手柄需要通过 Steam Input 映射成 F8；如果游戏不是由 Steam 启动（本项目用 `run_game.sh` 直接启动），Steam 的**游戏布局**可能不生效，需要试 **Desktop Layout**，或另行实现原生手柄监听。
  - 监听建立失败（缺「辅助功能/输入监控」权限）时，worker 会记录 `worker_error where=ptt` 并自动退回 VAD。
- **半双工限制**：TTS 播放期间 VAD 暂停、PTT 按键事件被忽略，播完只 flush 播放窗口内的采集块（TTS 前已排队的语音保留），避免把自己的声音送进 ASR。因此 AI 说话时你插话会被忽略，等它说完再说。主动回合（新单/截止抢回）在 worker 里是串行的：LLM 请求在飞时新的人声要等这一回合结束才处理；v1 不做抢占（第二版再支持 barge-in）。游戏音效和扬声器外放也会干扰识别，建议戴耳机或把游戏音量调低。
- **默认值验证状态**：TTS 2.0（`seed-tts-2.0` + `zh_female_vv_uranus_bigtts`）、ASR 2.0（`volc.seedasr.sauc.duration`）、方舟 plan 端点（`/api/plan/v3`）与键盘 PTT（Fn+F8）均已用真实 key/设备验证；手柄经 Steam Input 直接触发 F8 尚未验证。普通方舟 key 仍用 `/api/v3`；首次换账号请按 §6 排障确认。`tests/test_live_voice.py` 在 `OC2_LIVE_VOICE=1` 时做真实冒烟，CI/常规测试不会执行。
- 还需要游戏侧正常：插件把状态推到 `127.0.0.1:27411`（见 `AGENTS.md`）。语音不改这条链路。

## 3. 环境变量

所有配置只从环境变量读，密钥不写进仓库。`.env.example` 列了全部变量名和占位符：

```bash
cp .env.example .env
# 编辑 .env，填入真实 key
python3 run_agent.py --voice --voice-debug
```

`run_agent.py` 启动时会自动读取仓库根 `.env`（已有环境变量优先）。需要指定其它文件用 `--env-file PATH`；不希望自动加载用 `--no-env-file`。也可以继续手动 `set -a; . ./.env; set +a`。

| 变量 | 必需 | 默认值 |
|---|---|---|
| `VOLC_ASR_API_KEY` | 是 | — |
| `VOLC_ASR_RESOURCE_ID` | 否 | `volc.seedasr.sauc.duration`（2.0 小时版；1.0 是 `volc.bigasr.sauc.duration`，需单独开通） |
| `VOLC_TTS_API_KEY` | 是 | — |
| `VOLC_TTS_VOICE` | 否 | `zh_female_vv_uranus_bigtts`（Vivi，2.0 音色；1.0 音色与 `seed-tts-2.0` 不匹配） |
| `VOLC_TTS_RESOURCE_ID` | 否 | `seed-tts-2.0` |
| `ARK_API_KEY` | 是 | — |
| `ARK_MODEL` | 是 | —（方舟推理接入点 ID 或模型名；推荐 `doubao-seed-2-1-pro-260915`（lite 理解分工指令不稳；plan 端点上 pro 整轮 1.5–5 s，turbo `doubao-seed-2-1-turbo-260628` 介于两者之间）。客户端固定传 `thinking.type=disabled`，带「深度思考」标签的模型也不会思考） |
| `ARK_BASE_URL` | 否 | `https://ark.cn-beijing.volces.com/api/v3`；方舟「兼容 OpenAI 接口协议工具」/ plan 套餐 key 用 `https://ark.cn-beijing.volces.com/api/plan/v3` |
| `OC2_VOICE` | 否 | 不设；`0` 强制关，其他值表示希望开（缺 key 时仍告警退回） |
| `OC2_VOICE_RATE` | 否 | `1.05`（允许 0.5–2.0，超出收敛；**当前仅保留、未接到 TTS 参数**） |
| `OC2_VOICE_VOLUME` | 否 | `0.7`（允许 0.3–1.2，超出收敛） |
| `OC2_VOICE_VAD_DB` | 否 | `9.0`（VAD 开口阈值，底噪之上的 dB，范围 2–20；越小越敏感） |
| `OC2_VOICE_PTT_KEY` | 否 | 不设（VAD 自动切句）；设为 `f8` 等则按住录音、松开发送。macOS 直接按 F8 可能是媒体键，用 Fn+F8 或启用标准功能键；手柄可用 Steam Input 映射到该键 |
| `OC2_LLM_TIMEOUT` | 否 | `10.0`（方舟请求超时秒数，范围 1–60；plan 端点容易超过 3s） |
| `OC2_VOICE_LOG` | 否 | 不落盘（没有时间戳默认文件名；建议 `voice*.jsonl`，已被 gitignore） |

控制台入口：ASR <https://console.volcengine.com/speech/service/16>，TTS <https://console.volcengine.com/speech/service/8>，方舟 <https://console.volcengine.com/ark>。

`OC2_VOICE_RATE` / `OC2_VOICE_VOLUME` 的非法或非有限值（`nan`/`inf`）会告警并回退默认值；有限但超范围的值会收敛到边界。

## 4. 对话与分工账本

- **一轮对话**：P1 的话作为 user 消息；世界状态（订单、灶、盘、P1/P2 在做什么、规划器接下来可做的、账本）作为本轮的 system 消息，不进历史。有工具调用时走两段式：第一段调用工具 → 主循环落地 → 第二段按真实结果说话（被拒的会如实说明）；闲聊/提问只请求一次。
- **历史**：本关完整对话（user / assistant / tool 结果）留在 `DialogState.history`，最多 60 条；换关卡清空，暂停不清。
- **账本**（`ai/dialog/duties.py`）：每条只描述 P2 自己——`do`（优先做，+4）、`leave_to_p1`（P1 在做，P2 避开，-8，长期有效，截止闸仍可抢回）、`stop`（先别做，硬压制，30 秒后解除）。新条目覆盖与它重叠的旧条目；改口用 `edit` 保留 id。账本是 `bb.preferences` 的唯一来源。
- **做什么 = 物料或共享动作**：工具参数 `target` 只能是本关的**物料**（各道菜配方 DAG 根下的直接成分，如 `Cucumber`、`BoiledSushiRice`，拼写取自帧）或**共享动作**（task 类声明 `SHARED` 的洗盘 / 摆盘 / 送餐，`STATION` 指定的站点本关没有时不出现）。物料代表这种料的整条链：取料、切、下锅、盛盘、装盘，以及同 item 的预取 / 填灶，中间产物（如意面链里切的番茄）也算在内。一种物料只归一个人；「切菜我来」就把每种要切的物料各认领一次，新菜出现时再商量。灭火等救援动作不开放认领。
- **目录渲染**：system 消息里给出「菜 = 物料组合」，每种物料带加工方式标签（要切 / 要煮 / 要拌 / 直接装盘）和中文步骤链（「取料上砧板 → 切 → 装盘」），再列共享动作、本关没有站点而做不了的动作（如没有洗碗池时的洗盘）、不开放认领的救援动作。标签与步骤名来自 task 类的 `PROCESS` / `LABEL`，prompt 里不写死任何 task 类型或关卡物料；新增 task 类型必须写 `DESC` 和 `LABEL`（`tests/test_duties.py` 会检查）。
- **做不到的请求**：目录里没有的物料或动作、做不了的动作，LLM 不调工具、直接说明并提一下本关有什么；跟游戏无关的请求轻松接一句。工具 `target` 的 enum 只含本关真实存在的值，主循环再校验一次。
- **覆盖**：同一 target 只有一条约定，再 add 就是改写；`edit` 保留 id。不同物料共用中间产物时新条目优先（旧条目 `exclude` 掉重叠的 item）。
- **账本影响**：世界状态里每条约定后面带「此刻」：它挡住了 P2 哪些可做的活、挡了多久、P1 手上拿着什么。挡住超过 10 秒时提示 LLM 在回话时顺带问一句要不要调整（只提议，P1 同意后才改账本）。
- **兑现不了的认领会如实回报**：认领带救锅步骤的物料（饭链里的 `TakeCookedToPlate`，planner L1/L2 免疫认领负分）时，工具结果带 `warning`，第二段回复据此转告玩家。不绑订单的认领（洗盘、填灶、预取）在截止闸里按最紧的活单计算 cushion。
- **事件**：新菜出现（`new_order`）由 LLM 按新菜的物料组成直接落账本并告诉 P1 分工，P1 之后可以改口；截止闸抢回（`rescue`）由 LLM 按被抢的具体步骤说明接手了哪一步。没有固定模板播报：火情 / 快糊由 planner 直接处理、不出声。

## 5. 启动与 CLI

```bash
python3 run_agent.py --voice --voice-debug        # 显式开语音 + 调试输出
python3 run_agent.py                              # 有 key 自动开，缺 key 告警后退回无语音
python3 run_agent.py --no-voice                   # 强制关闭，忽略环境变量
python3 run_agent.py --voice-log voice.jsonl      # 指定对话日志
python3 run_agent.py --dry-run --no-voice         # 只看决策、不注入按键、不碰音频
```

| Flag | 行为 |
|---|---|
| `--voice` | 显式启用；优先于 `OC2_VOICE=0`。任一必需 key 缺失时打印缺失项并 **退出码 2**（不会去连游戏） |
| `--no-voice` | 恒关闭，优先于 `--voice` 和 `OC2_VOICE` |
| `--voice-debug` | 把 `ai.dialog` logger 开到 DEBUG 并输出到 stderr（ASR 文本、两段 LLM 回复与工具调用、工具批次/ack、播报、worker 错误；不含分段计时） |
| `--voice-log PATH` | 写逐行 JSON 对话日志；优先于 `OC2_VOICE_LOG`。**没有时间戳默认文件名**：不传且环境变量为空时不落盘 |

注意 `--dry-run` 只关闭按键注入，**不关闭语音**。离线只想跑决策时用 `--dry-run --no-voice`。

启动成功时 stderr 会打印一行 `[run] 语音已启用：...`；缺 key 的告警和 `--voice` 的配置错误也在 stderr。

### 对话日志格式

`--voice-log PATH`（或 `OC2_VOICE_LOG`）写 **NDJSON：一行一个 JSON 对象**，追加模式，父目录不存在会自动创建。日志文件名建议用 `voice*.jsonl`（例如 `voice.jsonl`），`.gitignore` 已按该模式排除。每行都有：

- `ts`：UTC ISO-8601 时间戳（毫秒精度）；
- `kind`：事件类型；
- 其余为事件字段。

会记录的事件：

| `kind` | 字段 |
|---|---|
| `start` / `stop` | `debug`、`log_path` |
| `level_start` | `level`（Agent 第一次看到关卡 / 换关时写；`since_level_ms` 的分母） |
| `asr` | `text`、`tick`、`source`（`ptt` / `vad` / `direct`）、`audio_ms`、`asr_ms`、`end_to_asr_ms` |
| `llm_reply` | `event`、`stage`（`first` / `final`）、`content`、`tool_calls`、`request_ms`、`first_token_ms`、`elapsed_ms`、`end_to_first_token_ms`、`end_to_done_ms` |
| `duty_batch` | `event`、`batch_id`、`calls`（工具名 + 参数） |
| `duty_ack` | `batch_id`、`results`（逐条落地结果；超时为 null）、`ack_ms` |
| `ack` | `command_id`、`outcome` |
| `ack_aggregate` | `event`、`outcome`、`command_ids` |
| `speak` | `text`、`from_end_ms`（仅兜底/非流式路径；正常流式走下面的 `tts_first_chunk`） |
| `tts_first_chunk` | `text`、`tts_first_chunk_ms`、`end_to_first_chunk_ms` |
| `first_audio` | `text`、`tts_to_audio_ms`、`end_to_first_audio_ms` |
| `turn_latency` | `event`、`source`、`audio_ms`、`asr_ms`、`end_to_asr_ms`、`llm_first_token_ms`、`llm_done_ms`、`tool_ack_ms`、`tts_first_chunk_ms`、`first_audio_ms`、`playback_done_ms`、`since_level_ms`、`outcome` |
| `worker_error` | `where`、`error`，以及可用的 `event` |

计时口径：

- 一轮的起点是 `turn_latency.started`：PTT 是**按键松开**（之后还有 200 ms 尾音才送 ASR）；VAD 是**判定句尾**（已含约 400 ms 静音）；主动事件是 worker 收到事件（`new_order` 从第一张单到达算起，含 1 秒合并窗口）。
- `*_ms` 不带前缀的字段都相对这一起点；`request_ms` / `first_token_ms` / `elapsed_ms` / `tts_first_chunk_ms` / `tts_to_audio_ms` 是相对各段自己的起点。
- `llm_first_token_ms` 是起点到**第一次看到 content/tool_calls delta**；`llm_done_ms` 是到最终那段文字就绪（两段式包含工具落地和第二段）。
- `tts_first_chunk_ms` 是起点到第一块 PCM；`first_audio_ms` 是起点到输出回调第一次真正写入采样，更接近耳朵听到的时间。
- `since_level_ms` 只对主动事件（尤其开局 `new_order`）有意义，表示从 Agent 首次看到关卡到本轮开始隔了多久。

不记录密钥、完整 prompt 或原始音频。日志写入失败只降级为 debug，不影响对话链路；日志只由 worker / 生命周期线程写。`--voice-debug` 仍只打印文本级事件，分段计时看 `turn_latency`。

## 6. 排障

### VAD 不触发 / 频繁误触发

- 先看 `--voice-debug` 或对话日志里有没有 `asr` 事件：完全没有说明 VAD 没切出句子。
- VAD 是 RMS + 自适应噪声底（`ai/dialog/audio.py` 的 `VAD`，默认 `threshold_db=9.0`、`noise_floor_db=-40.0`、50 ms 帧）。环境噪声大时自动抬升噪声底；持续稳态噪声（风扇、空调）可能压住人声。
- 如果说话太轻：靠近麦克风、提高系统输入音量；如果回声/键盘声频繁触发：降低输入增益或换指向性麦克风。
- 阈值可用 `OC2_VOICE_VAD_DB` 调（默认 9）：不触发时先试 4–6；误触发/游戏声频繁打断时调回 8–12。
- 更底层的阈值改动在 `VAD(threshold_db=...)`，改完跑 `python3 -m pytest tests/test_audio_vad.py -q`。
- 环境噪声大、说话偏轻时优先用 PTT（见下），不要继续硬调 VAD。

### PTT 没反应 / 重复回复

- 日志先看 `PTT 已启用：按键 ...`；没有这行说明 `OC2_VOICE_PTT_KEY` 没生效或 `PushToTalk.start()` 失败（会同时出现 `worker_error where=ptt`，并退回 VAD）。
- 按下时应有 `PTT 事件: down`，松开有 `up`，随后是 `处理一段人声`。只有 `up` 没有 `down`，或完全没有事件，说明按键事件没到进程：
  - macOS 笔记本默认 F8 是媒体键，直接按不会产生标准 F8 的 keycode；用 **Fn+F8**，或在系统设置里启用「使用 F1、F2 等键作为标准功能键」。
  - 手柄通过 Steam Input 映射时，游戏若不是由 Steam 启动（本项目用 `run_game.sh`），Steam 的游戏布局可能不生效；试 `Steam → 设置 → 控制器 → 桌面布局`，或改用键盘/原生手柄监听。
  - 全局监听需要「辅助功能」权限；listen-only event tap 在部分系统上还需要「输入监控」权限，授权后重启进程。
- 一次按住只应回复一次。若同一句话被拆成多次回复，看 `ASR 合并文本`：PTT 模式已把多个 definite 片段合并；VAD 模式仍按片段处理，建议切 PTT。

### 自己的 TTS 被当成用户说话（回声）

- v1 依赖半双工：播放期间暂停 VAD、忽略 PTT 事件，播完 flush 缓冲。若仍出现自问自答，检查是否走了 `--voice-debug`（调试模式不静音，但 VAD 仍暂停）。
- 外放音量过大、房间混响重时，尾音可能在恢复后仍被采集；PTT 的 pre-roll 在播放期间会清空，但刚播完就按住仍可能录到尾音。戴耳机最有效；其次降低 `OC2_VOICE_VOLUME` 或在播完后稍等再按。
- 日志里看到 ASR 文本与自己刚说的话高度相似，优先按回声处理，不要急着调 LLM prompt。

### 延迟高（目标 P95 < 2.5 s）

预算拆解：VAD 尾段 400 ms + ASR 300 ms + LLM 首 token/生成 1.2 s + TTS 首包 300 ms + 播放。PTT 模式没有 VAD 起止等待，但 ASR/LLM/TTS 的耗时不变；实测方舟 plan 端点常要 2–3.2 s，TTS 首包 0.5–3 s，端到端延迟主要受这两段影响。

现在每轮结束会在 `voice-*.jsonl` 写一条 `turn_latency`，直接看这些字段：

- `end_to_asr_ms`：句尾 → ASR final；`asr_ms` 只含 ASR 流本身。ASR 已是流式（PTT 按下即送），`end_to_asr_ms` 应明显小于旧的整段重发。
- `llm_first_token_ms` / `llm_done_ms`：句尾（或事件到达）→ 首 token / 最终文字就绪；两段式时 `tool_ack_ms` 单独列出。
- `tts_first_chunk_ms` / `first_audio_ms`：句尾 → TTS 首包 / 扬声器首帧；TTS 已预连接（`WarmTTSClient` 复用长连接），且 LLM text 按句流式喂给 TTS（`llm_done_ms` 不再等于「开始合成」的起点，首句一完就开始）。
- `since_level_ms`：仅主动事件，开局分工讨论慢时先看它——大说明在等游戏报出新单，不是 ASR/LLM/TTS 慢。

`--voice-debug` 仍只打印文本级事件；精确分段看 `turn_latency`。

- ASR 报 `requested resource not granted`：账号没开通当前 `VOLC_ASR_RESOURCE_ID`。2.0 小时版用 `volc.seedasr.sauc.duration`（默认），1.0 用 `volc.bigasr.sauc.duration`；去 [ASR 控制台 service/16](https://console.volcengine.com/speech/service/16) 开通对应资源。
- ASR 报 `Invalid X-Api-Key`：`VOLC_ASR_API_KEY` 填成了别的服务的 key（例如方舟的 `ark-` key）；换成语音服务控制台的 36 位 API key。
- ASR 慢：确认 16 kHz、200 ms 分包，网络到火山服务是否正常。
- 方舟报 `401 AuthenticationError`：普通方舟 key 用 `ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3`；方舟「兼容 OpenAI 接口协议工具」/ plan 套餐 key 必须用 `https://ark.cn-beijing.volces.com/api/plan/v3`，两者混用会 401。
- LLM 报 `Read timed out`：`OC2_LLM_TIMEOUT` 默认 10s；方舟 plan 端点负载高时仍可能超时，可调到 15–20s，或换更快的模型/端点。
- LLM 慢：客户端已固定 `thinking.type=disabled`；仍慢时换更快的模型（如 `doubao-seed-2-1-lite-260915` / `doubao-seed-2-1-turbo-260628`），并确认 `ARK_MODEL` 不是深度思考专用的 `-thinking` 模型。
- TTS 报 `55000000 resource ID is mismatched with speaker related resource`：音色和资源版本不匹配。`seed-tts-2.0` 只能用 2.0 音色（默认 `zh_female_vv_uranus_bigtts`，也可用 `zh_female_qinqienv_uranus_bigtts` 等）；`zh_female_qingxin` 是 1.0 音色，需要 1.0 资源。
- TTS 慢：确认 `VOLC_TTS_RESOURCE_ID` 与账号开通的版本一致；首包慢时考虑换音色/版本。
- 整体卡顿：检查主循环是否仍保持 20 Hz；语音在网络线程里，正常情况下不应拖慢决策。

### 没有音频设备 / 打开麦克风失败

- `python3 -m sounddevice` 列出设备，确认系统有可用的输入和输出设备。
- macOS 首次运行需要在「系统设置 → 隐私与安全性 → 麦克风」里勾选终端；改完要重启终端。
- v1 使用系统默认设备，不提供设备选择参数。默认设备选错时，在 macOS「声音」设置里切换。
- 默认输入是 AirPods 时，CoreAudio 可能报 `-10863`，或打开输入流后一直收不到回调（表现为 VAD/PTT 完全不动）。把输入切到 MacBook 麦克风，或先用键盘/手柄 PTT 验证；输出可以继续用 AirPods。
- 报错 `PortAudio` / `sounddevice` 相关：确认 `pip3 install -r requirements-voice.txt` 成功，且 Python 与 sounddevice 架构一致（系统 Python 即可）。

### 连不上游戏但语音正常

语音与游戏状态链路互相独立。连不上 `127.0.0.1:27411` 时 Agent 仍会退出（退出码 1）；先用 `run_game.sh` 起游戏并进入关卡。

# 语音对话系统

安装依赖、填写 `.env` 和授予系统权限见平台搭建文档。 [macOS 搭建](../setup.md#首次配置) · [Windows 搭建](../setup-windows.md#首次配置)

## 对话链路

```text
按住说话键 → 麦克风音频流 → 火山流式 ASR → 合并识别结果
          → 方舟或 MiMo → 工具调用及结果 → 火山流式 TTS → 扬声器
```

- 游戏内语音输入走按键说话（PTT），默认 F8。录音带约 300 ms 的按键前音频，松开后再录约 200 ms 尾音。ASR 的多个最终分段合并成一条玩家消息。VAD 自动切句已停用，`OC2_VOICE_VAD_DB` 不影响游戏内录音。
- 按下 PTT 可以打断正在播放的 TTS。播放结束或取消时，worker 清理播放窗口内采集的音频，减少把 AI 台词录进下一轮。PTT 打断针对播报，当前一轮的 LLM 请求和主循环中的命令仍按各自的流程处理。
- 主循环以 20 Hz 决策，独占 `World` 和 `Blackboard`。`DialogWorker` 在独立线程处理语音与模型调用，通过 2 Hz 上下文快照、事件队列和工具批次及应答队列与主循环通信。 [对话 worker](../../ai/dialog/worker.py) · [按键监听](../../ai/dialog/ptt.py)

## 工具与分工

| 工具 | 行为 |
| --- | --- |
| `execute_actions(actions)` | 提交玩家点名要求的临时动作。按数组顺序处理，单个动作也用数组。结果逐条返回 `ok`、状态和可用的原因。 |
| `cancel_action(action?)` | 撤销指定动作。省略参数时撤销当前命令和队列，并短暂停手。 |
| `set_division(assignments)` | 设置自主选活时的长期分工。按动作分配，可选 `item` 细分物料，`owner` 为 `you/player/both`。送餐、洗盘和装盘由就近的一方处理。 |
| `get_world_state()` | 按需查询最新的订单、灶、盘等状态。 |
| `read_cookbook(section)` | 读取本关攻略的一节。有攻略时才提供。 |
| `speak(text)` | 将模型的中文台词交给 TTS 播放。 |

`execute_actions` 的动作标识为 `chop / cook / mix / plate / deliver / wash / fetch`，目标用关卡目录里的英文名。命令描述要达成的目标，planner 会补齐取料等前置步骤。队列最多容纳三条待执行命令，满时新指令替换队尾待执行项。当前命令等待资源时，后续可行的命令可以先跑。灭火和救锅可暂时接管执行，处理完再恢复命令。 [工具定义](../../ai/dialog/tools.py) · [命令队列](../../ai/dialog/command_queue.py)

一次对话可以包含多次模型请求。worker 把工具结果以 `role=tool` 回给模型。同一次回复里既有动作又有 `speak` 时，先等主循环确认动作。若动作被拒绝，暂缓播放确认台词，让模型根据实际结果回复。部分闲聊与播报事件可用纯文本兜底。动态分工复核只在分工发生变化后播放缓冲的台词。 [提示词](../../ai/dialog/prompts/) · [分工逻辑](../../ai/dialog/division.py)

## 启动选项和配置

平时用所在平台的启动器。单独运行 `run_agent.py` 时，可传以下参数。运行前让游戏加载插件，并在主菜单等虚拟手柄加入。仅重启 Agent 时，手柄若仍已接入，可以留在关卡内。

| 参数 | 行为 |
| --- | --- |
| `--voice` | 显式开启语音，优先于 `OC2_VOICE=0`。缺必填配置时退出码为 2。 |
| `--no-voice` | 关闭语音，优先于 `--voice`。两个参数均省略时，按环境配置决定是否开启。 |
| `--env-file PATH` / `--no-env-file` | 指定配置文件或关闭自动加载。默认自动读仓库根目录的 `.env`，已设置的环境变量优先。 |
| `--voice-debug` | 输出识别文本、模型回复、工具和播报等调试信息。macOS 启动器在语音模式下默认开启。 |
| `--voice-log PATH` | 指定逐行 JSON 日志文件，优先于 `OC2_VOICE_LOG`。默认与本次 trace 共用时间戳，写到 `runs/voice-<时间戳>.jsonl`。 |
| `--dry-run` | 只连接游戏状态端口，使用 `NullExec` 查看决策。如果还想关闭音频和模型调用，同时传 `--no-voice`。 |

`LLM_PROVIDER` 显式指定 `ark` 或 `mimo`。留空时，有 `MIMO_API_KEY` 就选 MiMo，否则选方舟。按键、音量和模型超时可用 `OC2_VOICE_PTT_KEY`、`OC2_VOICE_VOLUME` 和 `OC2_LLM_TIMEOUT` 调整。`OC2_VOICE_RATE` 当前未接入 TTS。凭据、服务地址及音色等可选项见模板。 [配置模板](../../.env.example) · [启动参数实现](../../run_agent.py)

## 看对话日志

日志为 NDJSON，每行包含 UTC 时间戳 `ts`、事件类型 `kind` 和该事件的字段。日志会记录识别文本、模型回复和工具参数，分享日志前可检查这些字段。

| 事件 | 看什么 |
| --- | --- |
| `ptt_cb` / `ptt` | 前者记录按键回调及是否有正在处理的播报，后者记录 worker 消费的按下或松开事件及录音状态。 |
| `barge_in` | 有活动的 TTS 流时按下 PTT，worker 请求停止播报。 |
| `asr` | 合并后的文本、音频时长和识别耗时。 |
| `turn_start` / `turn_stall` / `worker_dead` | 分别表示开始模型回合、某阶段久未推进、worker 已退出。用 `stage` 和 `event` 对照前后的记录。 |
| `llm_reply` | 一次模型请求的文本、工具调用和耗时。`stage` 如 `turn0`、`turn1`。 |
| `duty_batch` / `duty_ack` | worker 提交的工具批次和主循环返回的结果，通过 `batch_id` 对齐。 |
| `tts_first_chunk` / `first_audio` | 第一包合成音频和输出回调首次写入采样。 |
| `worker_error` | 用 `where` 区分按键、音频采集、ASR、LLM 和 TTS 等来源。 |
| `turn_latency` | 一轮的耗时汇总。部分阶段没有发生时，对应字段可能为空。 |

语音回合以松开 PTT 为计时起点，主动回合以 worker 开始处理事件为起点。

- `end_to_asr_ms` 从松键算到识别完成。`asr_ms` 从 ASR 启动算起，包含按住说话时已经开始的流式识别。
- `llm_first_token_ms` 和 `llm_done_ms` 表示从回合起点到模型首个输出、模型回合处理结束。逐次请求的耗时看 `llm_reply.elapsed_ms`，工具等待看 `tool_ack_ms`。
- `tts_first_chunk_ms`、`first_audio_ms` 和 `playback_done_ms` 分别表示从回合起点到合成首包、首次输出采样和播放结束。
- `since_level_ms` 表示从 Agent 首次看到关卡到本轮开始的间隔，可用于定位开局分工播报的等待时间。

## 排障

### 按键或录音没反应

1. 确认启动输出里有 `[run] 语音已启用`。如果有 `worker_error where=ptt`，按平台搭建文档检查权限与快捷键。
2. 按住说一句再松开，查看 `ptt` 是否收到了 `down/up`。macOS 上 F8 可能是媒体键，可试 Fn+F8。通过 Steam Input 映射时，macOS 启动器直接启动游戏，游戏布局可能未生效，可以先用键盘确认。
3. 已有 `down/up` 时，检查 `asr` 和 `worker_error`。用虚拟环境的 Python 运行 `python -m sounddevice` 可列出设备，确认系统默认输入和输出正确。

### 识别正常但听不到回复

- 查看 `llm_reply`、`duty_ack` 和 `worker_error`。模型可能在等工具结果，也可能没有提交台词。
- 若有 `tts_first_chunk` 却没有 `first_audio`，检查默认输出设备。若 ASR 录进了刚播放的台词，可以降低扬声器音量或使用耳机。
- TTS 提示音色与资源不匹配时，检查 `VOLC_TTS_VOICE` 和 `VOLC_TTS_RESOURCE_ID`。ASR 提示资源未授权时，检查语音服务账号的授权及 `VOLC_ASR_RESOURCE_ID`。方舟返回 401 时，核对 `ARK_API_KEY` 和 `ARK_BASE_URL` 是否对应同一类凭据。 [配置模板](../../.env.example)

### 隔离设备、服务和游戏问题

在仓库根目录用虚拟环境的 Python 运行。macOS 的解释器是 `.venv/bin/python`，Windows PowerShell 的是 `.\.venv\Scripts\python.exe`。下面以 macOS 为例。

```bash
.venv/bin/python tools/voice_smoke.py doctor
.venv/bin/python tools/voice_smoke.py tone
.venv/bin/python tools/voice_smoke.py record --seconds 3
.venv/bin/python tools/voice_smoke.py tts
.venv/bin/python tools/voice_smoke.py asr
.venv/bin/python tools/voice_smoke.py llm
```

`doctor` 检查依赖、配置和音频设备。`tone` 测播放，`record` 测麦克风。`tts`、`asr`、`llm` 和 `all` 会调用真实服务，其中默认的 `asr` 用合成语音测试识别。需要用真实麦克风测识别时，可运行 `asr --source mic --seconds 3`。这些测试独立于游戏，不经过游戏内的 PTT。

若语音服务正常，但 Agent 连不上状态端口 27411 或控制端口 27412，检查游戏是否通过相应平台的启动器加载了插件。 [游戏连接排障](../../README_CN.md#常见问题)

### 用历史对局复查模型行为

```bash
.venv/bin/python tools/replay_dialog_llm.py --trace 'runs/trace-<时间戳>.jsonl' --dry-run
```

回放工具从历史 trace 选取关卡状态，用当前工具协议复查对话场景。把示例路径替换为实际 trace 文件。带 `--dry-run` 只查看输入，去掉后调用模型。可加 `--scenario` 选场景，或用 `--show-prompt` 查看完整提示词。 [历史语音设计](../superpowers/specs/2026-10-18-voice-dialog-interaction-design.md)

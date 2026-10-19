# 变更记录（changelog）

本文件记录最近的修复/改动，按日期倒序。`AGENTS.md` 只保留稳定参考与入口，不再堆砌长记录；每次做完改动在这里追加一条。

## 语音对话收敛与 prompt 精简（2026-10-20）

在「语音延迟」基础上进一步收敛对话层与 prompt：

- 移除账本 `stop` 模式（`update_duties` 只留 do / leave_to_p1），以及 `impact`/「挡了 10 秒提议抢活」机制。
- 移除 rescue 语音播报（planner 抢单动作保留，只是不再开口解释）。
- 上下文不再给 LLM 传 P2 手持（`p2.held`）；保留 P1 手持和 P2 当前动作。
- prompt：`账本` 全换成 `分工/约定`；`new_order` 只补新物料、不动已约定物料；加均衡规则（不抢 P1 已认领的、不过度推让、可提议分担但不反复劝）；去掉 `edit` 提示和 `规划器` 内部词；补「target 用英文枚举、说话用中文」的边界。
- 工具 schema 的 `mode` 只留 do / leave_to_p1。

全量 1581 passed / 90 skipped；未改 DLL。

## 语音延迟：分段打点 + ASR 流式 + TTS 预连（2026-10-19）

`voice-*.jsonl` 新增 `level_start`、`asr` 的 `source`/`asr_ms`/`end_to_asr_ms`、`llm_reply` 的 `first_token_ms`/`request_ms`/`end_to_first_token_ms`、`duty_ack.ack_ms`、`tts_first_chunk`、`first_audio` 和每轮 `turn_latency`（含 `since_level_ms`）。轮次起点：PTT=松键、VAD=判定句尾、`new_order`=第一张单到达（含 1 秒合并窗）。LLM 客户端首 token 计时含建连到首个 content/tool_calls delta；TTS 首帧由 `AudioPlayer.first_output_at` 在输出回调里记。

- **ASR 流式**：PTT 按下 / VAD `start` 即建 `_ASRSession` 持续送 PCM，松键/句尾只补尾音等 final，不再整段重发；`ASRClient` 支持阻塞队列输入（executor 等待，不卡事件循环），recv 超时放宽到 15s。
- **TTS 预连**：`WarmTTSClient`（`TTSClient` 子类）后台持有长连接，`warmup()` 建好 WebSocket/StartConnection，每句 `synthesize` 只做 StartSession/TaskRequest/FinishSession、复用连接；worker `start()` 预热、`stop()` 关闭。实机测首包中位 0.70s → 0.43s（省约 0.27s）。
- **LLM→TTS 流式**：`LLMClient.chat(on_text=...)` 边收 content delta 边回调；worker 的 `StreamingTTS` 按句号/问号/感叹号/分号/换行切句，逐句送 TTS 并流式播放。第一段、第二段的 text 都喂进去按序播（第一段带工具调用也不丢弃、直接播，换更快体感，接受少量「先出口再确认」）；没 text 不发声。半双工由 `StreamingTTS` 在首块音频时暂停 VAD、播完 flush 恢复。

全量 1590 passed / 90 skipped；未改 DLL、未实机整轮验证。

## 配方自带 chop、去箱子依赖（2026-10-19）

「要不要切」以前只在箱子(DispenserCrate)上读 WorkableItem，箱子成了唯一数据源；Dynamic 关卡(1-6)的鱼/米/海苔只走传送带、没有箱子，而且开局后才动态刷出，于是鱼被当成不用切的料，DAG 只剩 FetchToPlate(SushiFish)。现在插件 BuildChopIndex 从三个来源读食材 prefab 的 WorkableItem（PickupItemSpawner.m_itemPrefab / IngredientContainer.m_contents / CarryableItem·TeleportableItem），导出 recipes 时把 chop/chop_stages 写进叶子节点；RecipeBook.chop_target/chop_stages 优先读配方字段、箱子只做旧插件兜底，_build_branches 同步。chop 索引用单调缓存 _chopIndexCache，每帧合并新扫到的食材、出现新键就让 recipes 重建（Dynamic 关卡食材开局后刷出，一次性 BuildStatic 会漏）。实机验证 1-6：鱼出现后 sushifish 由 null 变 ChoppedSushiFish。C# 插件与 Python 都改；协议加字段（recipes 每条多 chop/chop_stages），无破坏性变更。测试 1580 passed / 90 skipped。备份 .runtime/source-chop-from-prefab-20261019-230905.zip（已验证可用快照，含 recipes.py / Oc2AIPlugin.cs / test_recipes.py）。

## 1-4 台面盘失败循环修复（2026-10-19）

报告 `docs/notes/sushi14-stall-2026-10-19.md`。新实机轨迹 trace-20261019-173236 的 1-4 段 1576 拍有 704 failure：台面盘无网格字段被 Face 误作地面物跳过；Align 排除唯一站位后，Planner 不读 bad_pairs，NO_ABANDON 盛饭每拍继续选同盘。resolve 现在为同位置台面上的物品添加 interaction_surface/grid/gx/gz（不改原物品身份/世界位置），Face/Align/Tap 共享该元数据；reachability.can_interact 读取 bad_pairs，选择别的盘或救锅。新增 12 项真实帧/端到端回归，全量 1316 passed / 85 skipped。旧 AI 已停止，修改后只做离线重放，没有新实控；Python 修改，无 DLL 更新。备份 .runtime/source-before-sushi14-174154.zip。离线重放日志在 .runtime/sushi14-offline-agent-replay.jsonl，不能当实机出餐证据。

## 目标附近卡住修订（2026-10-19，当前增量）

见 `docs/notes/target-approach-recovery-2026-10-19.md` 与对应 plans。最新 1-2 轨迹有多格出餐口朝向追错锚点、对齐后未重新转向、正确句柄仍等绝对静止和计时归零后继续规划。修订路径回溯共用 edge_blocked、近目标折返精确控制/滑出纠偏、Align 两个新观测无进展且句柄正确的受保护交互、复合交互最多两次换位与新帧门槛、失位重走 Approach、EnsureHandsFree 刷新移动站点。bad_pairs 仅排除交互落点，不全局封路；按键后 Confirm 不被位姿/目标消失打断，手变但条件未满足不可重按。新增 interaction_recovery.py、trace 交互阶段和地图上下文。Python 修改，未改 DLL；本轮未实机再跑，不得声称通过实机通关。修改前 ai 备份 .runtime/source-before-target-approach-20261019-172026.zip。

## 本地修复记录（2026-10-19，优先于历史说明）

后续 Windows 集成及 150 秒 1-5 实测已完成，报告 `docs/notes/windows-live-validation-2026-10-19.md`。本目录 .venv、start/stop_windows 启动器可用；新版插件 0.19.0 / protocol 4 已部署，旧 DLL 在本目录 .runtime/backups/20261019-165850-395。Windows 禁用 dash 事件、不用右 Alt，方向键/右 Shift/右 Ctrl。本轮 AI 已到时退出：任务终态成功 11、失败 19；全队新增出餐 4，其中最后 1 份有 P2 持盘面对出餐口→空手与分数增加证据，不能全归因 AI。仍有导航/交互重试与插件约 47ms 的采样开销，不得声称稳定通关或性能问题已解决。实测后追加 P.holds_plate，按 held_by 修复同模型盘误归属；持盘详情加入 trace，trace 路径修回仓库根；追加修复仅离线回归，尚未再次实控。真实持盘 fixture 放 tests/fixtures/ownership，不能放根 fixtures（根的旧 parity 测试固定 1102 帧与单网格假设）。

基线 b7a1f82，分支 fix/bt-stall-recovery。复现与修改见 `docs/notes/bt-stall-recovery-2026-10-19.md`，计划见 `docs/plans/2026-10-19-bt-stall-recovery.md`。新增 core/recovery.py 的操作失败历史与动作冷却，space/reachability.py 的当前帧可达性与复用；修复原地挡火、认领残留、切关遗留和局部重算丢平台限制。无任务 trace 发布 waiting 原因。保留 NO_ABANDON 与旧显式 abandoned 资源表，不能混淆 owner="P2" 和厨师名="Player 2"。

本轮在独立目录修复最新上游，未覆盖旧 Windows 本地项目或游戏 DLL；未启动游戏控制。上游仍为 macOS 输入与新版 terrain/grids 协议，实机集成须另行核对。验证结果见修复报告。

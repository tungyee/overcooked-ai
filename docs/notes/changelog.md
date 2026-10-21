# 变更记录（changelog）

本文件记录最近的修复/改动，按日期倒序。`AGENTS.md` 只保留稳定参考与入口，不再堆砌长记录；每次做完改动在这里追加一条。

## now 落实即降级成 do + 硬指令闸去抖：修 1-6 逐拍振荡拿不到米（2026-10-21）

根因：`trace-20261021-141944` 的 1-6 段里，账本有「BoiledSushiRice / Cucumber = now（马上去）」的长期约定（`source=""`，非用户语音，是 LLM 开局分工）。`now` 偏好每拍命中 `_select_imperative` 硬指令闸，绕过承诺惯性直接换选同一作用域里的不同候选——`FetchToCooker`（绑订单，recipe_graph 发射）与 `FillCooker`（订单无关，_gen_misc 发射）每拍交替当选（两者的 fetch_to 认领 order_id 不同、互不压制），commitment 每拍被重置（trace `commit.ticks` 4522/4523/4524…逐拍变）、执行树每拍重建。上一笔「拦截等料」的取料目标锁死在 build() 闭包里，树一重建闭包就作废，于是角色继续追着移动的米跑，全程 0 帧拿到米、灶从没开煮。

**now 本该是一次性的**：prompt 语义里 now=「这就去」、do=「有空就做」，但代码只有 stop 有 30s TTL，now/do 的 `expires_tick=0` 且无消费点，于是 now 永久触发硬指令闸。修法分两层：

1. **账本层（主修）**：`ai/dialog/duties.py` 新增 `Ledger.consume_now(task)`——task 命中某条 now 的作用域，就把那条 now 降级成 do，返回是否有改动；`ai/agent.py` 在 task **执行成功**（`status == "success"`）后调用它并重编 `bb.preferences`。失败不降级，now 的紧迫性保留到下次重试——避免「认下活还没做成就把 now 消费掉」。
2. **planner 层（钉住，不返回 None）**：`ai/core/planner.py` 的 `_select_imperative` 在当前承诺已命中 now 作用域时，**把当前承诺的候选钉住返回**（不再返回 None 交普通打分）。因为普通打分会把送餐等更高分任务选上来，now 任务每拍被顶掉、永远做不成（`trace-20261021-145504` 的 1-6 段：FetchToBoard(Cucumber)↔DeliverPlate 逐拍交替、角色在 (0,-3)↔(1,-3) 来回卡）。当前承诺已不可行（不在 feasible）时才交回正常流程。

纯 Python，无 DLL 变更。

验证：`tests/test_duties.py` 新增 3 项（命中的 now→do / 未命中的保留 / do 与 None 不改），`tests/test_now_command.py` 新增 3 项（承诺命中→钉住返回 / 承诺不可行→让位 / 不命中→照常抢选）。全量 pytest 后台跑中。**未实机重跑**（需重启 agent）。

## 传送带取料改「拦截等料」：修 1-6 拿不到米饭（2026-10-21）

根因：`trace-20261021-131529` 的 1-6 段，米（`SushiRice`）只走台面高度传送带 `ConveyorStation`。`FetchToCooker`/`FillCooker` 的取料目标 `ingredient_source` 每拍把料解析成它**当前所在**的传送带段，段随料跨段移动而变（`on_station` 从 `ConveyorStation (6)→(5)→(4)`），角色追着移动的米从一段追到下一段、永远追不上。实机全程 P2 手持 `SushiRice` 帧数 0、灶 `pot_contents` 非空帧数 0——米从没进锅、从没煮熟。

修法：`FetchToCooker.build()` 的单料取料把目标**锁死在第一次解析出的站点**，并给 `InteractWith` 加 `wait_pred`/`wait_timeout`（到位后在 tap 前插入 `WaitUntil`）——对 `t="conveyor"` 的段，用新谓词 `loose_on_station`（`ai/tasks/base.py`，复用 `_station_for_item` 的 `on_station` 名称匹配 + 位置兜底）等料经过该段再 tap；非传送带目标不传 `wait_pred`，行为不变。纯 Python，无 DLL 变更。

验证：`tests/test_primitives.py` 新增 1 项（wait_pred 成立前不按键、成立后 tap），`tests/test_fill_cooker.py` 新增 3 项（loose_on_station 匹配/无料、FetchToCooker pickup 带 `_wait`）。全量 pytest 1639 passed / 90 skipped。**未实机重跑**（需重启 agent）。

残留：复盘时另见角色停在距目标格心 0.48（`ARRIVE_EPS=0.5` 内）时 move 命令仍不判到达、0.8s 后 stuck 失败——这是取料卡死的次级因素，属插件 move 到达判定的遗留问题，本次未处理（需 Unity debug 日志定位）。

## StashHeld 放宽：手持需切生料无空砧板/切好形态已可用时兜底放下（2026-10-21）

根因：手持一份需切的生料（Lettuce/Cucumber）时，若没有可行任务能消化它，`StashHeld` 又因 `item_wanted_by_orders(held) > 0` 拒绝放下，角色持料死锁（`no_feasible_task` / `recovery_cooldown`）。三次实机复现各有触发：① Prefetch 捡料后被 DeliverPlate 抢占（trace-20261021-123000）；② P1 抢先占了最后一块空砧板（trace-20261021-125032 t≈5336）；③ 场上已有切好形态（ChoppedX），recipe_graph 判该料生产链 done、不再发 FetchToBoard/Chop 候选（trace-20261021-125032 t≈8559）。③ 里 `recipe_graph._chopped_available`（「已切好」）与 `item_wanted_by_orders`（「订单还写着料名」）口径不一致——一边说不用再切、一边说还要，两条出路同时堵死。

修法：`ai/tasks/cleanup.py` 的 `StashHeld.feasible` 对 `needs_chopping` 的生料，满足任一条件即允许 stash：① 没有空砧板可放（`_has_free_chopping_board`）；② 切好形态已可用（`_chopped_form_available`，与 `recipe_graph._chopped_available` 同源）。纯 Python，无 DLL 变更。

验证：`tests/test_tasks.py` 新增 3 项（无空砧板→stash / 有空砧板→不 stash / 切好形态可用→stash）；全量 pytest 1635 passed / 90 skipped。用两份 trace 重建 World 复核：t=8559（手持生 Cucumber）与 t=684（手持生 Lettuce）的 `StashHeld.feasible` 均从 False 翻转为 True。未实机重跑（需重启 agent）。

## 写死 1-6 出餐口北侧墙后窄缝为不可走，修两处卡死（2026-10-21）

根因：出餐口 `workstation_plate_station` 的 `grid=None`，碰撞 footprint 没进 occupancy；且碰撞盒北缘（maxz≈3.6）伸进北邻那排（gz=3），角色半径过不去。于是 walkable 把 gz=3 墙后窄缝标成可走，寻路北上钻死路——（1）出餐口墙后卡死；（2）番茄在西侧被误判可达，反复选 FetchToBoard(Tomato) 失败、拉黑 19 个资源进 recovery_cooldown（trace-20261021-115327）。

修法：`ai/state/world.py` 对 level 前缀 `1_6_Dynamic` 的出餐口，把其北邻一排（gz+1，约 2 格宽）标进 occupancy 不可走。纯 Python，无 DLL 变更。

验证：全量 pytest 1632 passed / 90 skipped。未实机重跑（需重启 agent）。

## 插件上报 ConveyorStation 段，修传送带上的料拿不到（2026-10-21）

根因：1-6 的米/鱼/海苔只走 `ConveyorStation`（台面高度传送带），但插件只把地板级 `Travelator` 报进 `belts`，`ConveyorStation` 既不在 `stations` 也不在 `belts`。于是传送带上的料被当成「只有 x/z、无 gx/gz」的裸散落物，`Approach` 每拍把目标格设成料的**当前**格——料每帧在动，角色永远追着旧位置跑（trace-20261021-115327 的 1-6 段，全程拿不起米）。

修法：`AppendStations` 新增一段把 `ConveyorStation` 报进 `stations[]`，`t="conveyor"`、带 `gx/gz`（`GridIndexOn` 反解）与方向速度 `vx/vz`（`m_conveyanceDirectionXZ` × `m_conveySpeed`）。Python 侧复用现有 `ingredient_source` → `_station_for_item` 把料按 `on_station` 名称解析到静态段，`Approach` 目标从「每帧漂的料 x/z」变成「固定段格」；`_station_for_item` 另加「名字对不上就按世界坐标就近匹配 conveyor 段」的兜底。

验证：`./plugin/build.sh` 编译通过（仅既有 CS0169 警告）；`tests/test_tasks.py` 新增 2 项（名称解析 + 位置兜底）；全量 1631 passed / 90 skipped。**未实机重跑**——需 `cp plugin/build/Oc2AIPlugin.dll "$G/BepInEx/plugins/"` 并重启游戏。

## 插件 move 到达判定去掉 waypoint 门槛，修番茄/生菜取不起来（2026-10-21）

根因：`MoveCommand.Tick` 的到达分支要求先推进最后一个 waypoint（`_idx >= _path.Count`），推进又要求 `dist <= WAYPOINT_EPS`（0.15）。角色刹车 + 惯性会停在离 goal 0.15~0.5 的位置，_idx 永远到不了 _path.Count，到达分支永远不触发，角色僵死在交互位旁、拾取键永远不按（trace-20261021-103748 番茄 / trace-20261021-110601 生菜）。

修法：到达判定改为直接对 `_goal` 判距离——`dist <= ARRIVE_EPS`（0.5）即到达，`dist <= INTERACT_EPS`（1.0）且游戏句柄命中即到达，不再依赖 waypoint 推进；`WAYPOINT_EPS` 只用于中间走点推进。纯 C# 插件变更。

验证：`./plugin/build.sh` 编译通过（仅既有 CS0169 警告）。未实机重跑——需 `cp plugin/build/Oc2AIPlugin.dll "$G/BepInEx/plugins/"` 并重启游戏。

## 传送带散落物拾取：目标格做提前量（2026-10-21，已回退）

根因：传送带上的料（如 `Balloon_1_5_2P` 的 `SushiRice`，挂在 `ConveyorStation` 上）只报 `x/z`、无 `gx/gz`，`Approach` 把目标格设成料的**当前**格。料每帧在动，角色追着旧位置跑。

**已回退**：本修用「线性提前量」（速度 × 距离 × 每步拍数）预测未来格，在**循环传送带**上不成立——提前量超过一圈长度会把角色带到错误位置，且速度按物品名聚合，同名多段（米/鱼各在传送带不同段、方向不同）会取错方向（实机：角色在鱼/米传送带之间乱走、方向搞反）。纯 Python 改动全部回退（`_lead_moving_target` / `_update_item_motion` / `bb.item_velocities`），全量 1629 passed / 90 skipped。

正确方向：需要插件把 `ConveyorStation`（台面高度传送带）的段位置 + 方向 + 速度上报，角色走到静态传送带段旁**拦截**（等料经过），而不是追移动目标。

## _merge_station_coords 补拷 grid / t，修砧板目标空网格（2026-10-21）

根因：`find_ingredient` 对「切好的料在砧板上」返回 `kind="board"`，`_merge_station_coords` 只把 stations[] 同名条目的 `gx/gz/x/z` 合进 workstations 条目，漏了 `grid` 和 `t`。于是目标 `resource_id` 产出 `('', name)` 空网格键，认领与 `bad_pairs` 按 `(grid, name)` 比对时错位；`_avoid_occupied` 也因缺 `t` 把砧板当成散落物、跳过交互位避让（trace 里 `interaction.target` 首项为空即是此）。

修法：合并时补拷 `grid`、`t`。纯 Python，无 DLL 变更。

验证：全量 pytest 1631 passed / 90 skipped。

## 出餐口交互位只取厨房内侧，排除墙后死路（2026-10-21）

根因：出餐口（`plate_station`，嵌在外墙里的墙）的交互位按占地格四邻并集算（`interact_cells_multi`），把墙后那一面也算进去了。`1_6_Dynamic_Lvl_01_2P` 里墙后是一条贴墙死路（gz=3 窄缝），游戏 placement 句柄只从厨房内侧（南侧 gz=1）报 `workstation_plate_station`，墙后从不报。导航选「最近交互位」时把角色往墙后引，角色沿左列爬到左上角 (-5,3)，再往右被柜台顶部（z=3.0）卡住，move 命令 no_progress → `abandon_actions` 200 拍冷却 → 空转 → 重试，死循环（trace-20261021-102928）。

修法：新增 `navigation.serving_hatch_interact_cells`——按「沿墙法向连续可走深度」区分前/后（深度大的是厨房侧，小的是贴墙死路），水平墙比南北、垂直墙比东西，两侧等深或 1×1 占地退回全部四邻；`move.py` 的 `Approach`（本地与委托后端）经 `_station_interact_cells` 对出餐口走这条新路。纯 Python，无 DLL 变更。

验证：`tests/test_navigation.py` 新增 4 项（水平/垂直/两侧开放/单格回退）；真实帧回放确认出餐口交互位由「含 (-2,3)/(-1,3)」收敛为「仅 (-2,1)/(-1,1)」。全量 1625 passed / 90 skipped。

已知残留：本修是**预防**——修后角色不会再被引向墙后。已卡在墙后缝隙的角色（当前那局）恢复路径仍会走 gz=3 窄缝，可能因柜台顶部清障（角色 z≈3.08 < 柜台顶 3.0 + 半径）被挡，需要重启关卡或另做窄缝寻路/恢复修正，本轮未处理。

## 沥水架上的盘拾取修复：surface_hint 纳入 plate_return（2026-10-21）

`_surface_hint`（`ai/primitives/base.py`）之前只给 `counter/chopping/cooking/cooker` 四类站点做「同位置借用交互坐标」。沥水架 `DryingPart` / 回盘台 `workstation_plate_return` 是功能站（`t=plate_return`），不进 `BuildStationMap`，其上的盘 `grid=null` → `handle_desc` 回退到主网格（GridManager）算句柄坐标。1-4 沥水架在主网格、回退恰好正确；1-5 沥水架在非主网格（Balloon Basket），回退后盘算成主网格 gx=8 而游戏句柄报篮子本地 gx=0 → 拾取 `handle mismatch`（实机 1-5 拿不起沥水架上的盘）。

修法：station 类型白名单加 `plate_return`，盘借到 DryingPart 的 `interaction_grid/interaction_gx/gz`，`handle_desc` 在正确网格里算出坐标。纯 Python，无 DLL 变更。

验证：`tests/test_sushi14_stall.py` 新增 `test_surface_hint_borrows_plate_return_coordinates_for_drying_rack_plate`；全量 1610 passed / 90 skipped。用 1-5 真实帧回放确认 `handle_desc` 对沥水架盘产出 `gx=0,gz=2`，与游戏句柄 `pickup_gx=0,pickup_gz=2` 一致。未实机重跑。

## 插件网格原点上报修正：盘/散料拾取句柄坐标匹配（2026-10-21）

插件 `AppendGrids` 之前对每个 QuadGridManager 直接报 `transform.position` 当原点，但主网格的 transform.position 不是 `m_gridIndex` 使用的原点（Sushi_1_4 transform=12 而真实原点 14.4，差 2 格，即 `map.grid.origin.x`）。Python 侧导航早已用 `GridMapping` 从站点反解正确原点（`ai/state/world.py:196-198` 注释点名了这个坑），但 `handle_desc` 走 `target_grid_callbacks` → `GridSet`，读的正是插件的 ox=12。

后果：盘/散料这类只有 x/z、没有 gx/gz 的目标，`handle_desc` 用错误原点把世界坐标算错网格（盘在 (15.6,-6) 算出 gx=3，游戏句柄报 gx=1），插件 `HandleMatches` 的坐标分支对不上；拾取句柄又指向柜台而非盘，名匹配也对不上 → tap 永远 `handle mismatch`，拾取键从没按下去（`held_by` 恒 null，Confirm 超时 FAILURE）。站点（灶/料箱）自带 gx/gz 不走这条路，所以 `FillCooker`/`FetchToCooker` 正常，只有 `TakeCookedToPlate` 的取盘步卡死。

修法：`AppendGrids` 现在先 `CalibrateGrid()`，主网格（`_mainGrid.name` 命中的 QuadGridManager）报校准出的 `_originX/_originZ`，非主网格（气球篮）仍报 transform.position，与既有 `GridIndexOn` 的「主网格用校准原点、非主用 transform」判定一致。

验证：`plugin/build.sh` 编译通过；`tests/test_exec_primitives.py`、`test_geometry.py`、`test_exec_commands.py` 38 passed / 1 skipped。未实机重跑（需重新部署 DLL 并重启游戏），不得声称已通过实机验证。

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

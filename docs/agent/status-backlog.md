# 已知限制、当前状态与待办

## 盘匹配优先给「能立刻装熟料」的单（2026-10-31，未实机验证）

2-2（Balloon_2_3）4 局 trace（20261030-214824/224658/230700/233253）共 9 次：意面盘/空盘绑给缺肉、肉锅没开的非队首 MeatOnly（缺料数少，排序永远赢），Marinara 的熟鱼/熟虾没盘只能 `RescueCooked`。`_match_plates_to_orders` 改为两轮贪心：第一轮只配「订单要、盘还缺、且有已熟未糊源」的边，每配一条扣一份熟料供给（`cooked_sources` 计数，一口锅只让一块盘提前）；第二轮按原序（队首 → 缺料数 → 堆顶空盘 → remaining）配其余。队首能立刻用时仍是队首赢；钉住的配对先处理并占供给。fixture `balloon23-20261030-224658-plate-to-cooked`。未做：盘离锅距离 / 来不及就先救锅的 ETA 判断。

## 盘堆顶层盘直接参与装配（2026-10-31，未实机验证）

游戏里取盘站/沥碗架顶层盘能直接倒料、直接端去盛锅，原先因 `plates[]` 没有层信息，堆里的空盘一律不匹配，必须先 `PlaceCleanPlate` 挪到台面。插件 0.23.4 每块盘新增 `stack`（盘堆名，不在堆里为 null）/`stack_clean`/`stack_top`：遍历 `ServerCleanPlateStack`/`ServerDirtyPlateStack` 的 `m_stack`（`ServerStack.m_stackItems` + `InspectTopOfStack()`）；盘上报上限 8→32。Python：`P.plate_stack_top`/`P.plate_buried_in_stack`；`_match_plates_to_orders` 收干净堆顶盘（同等缺料时台面盘优先），中间层盘不当散落盘匹配；`_emit_plate` 不再绑中间层（pickup 只拿顶层）。配到堆顶盘的单不再发 `PlaceCleanPlate`，直接 `TakeCookedToPlate`/`FetchToPlate`。旧帧无字段时行为不变。

待实机确认：`m_stackItems` 元素确为盘 GameObject、端走顶层后下一块的 `stack_top` 当帧更新；沥碗架洗好的盘也是 `ServerCleanPlateStack`。已知限制：堆顶盘装了半份料后压住下面的干净盘，别的单要等它离堆。

## 已完成的 task 不再接着跑（2026-10-30）

`trace-20261030-214824` t=1743~1795（Balloon_2_3）：P1 先把面下进 cooker_01，P2 的 `FillCooker.done()` 已为真。`agent._tick_once` ⑨.5 判「旧树后置条件已成立」把 planner 选的 DeliverPlate 压回 FillCooker，但 `Executor.tick` 不看 `done()`，树只认自己的 expect，照样走去面箱又拿一份面（52 拍），之后拿着多余的面去送餐失败。修：⑨.5 命中时设 `settle_active`，⑫ 直接 `halt_and_discard` 并按 success 走 ⑬（释放认领、清承诺），不再 tick 旧树；这一拍不开新树，下一拍 planner 重选。测试 `tests/test_agent.py::test_done_active_task_settles_instead_of_ticking`。

## 落点去掉「不贴灶台」「不贴拼装台」（2026-10-30）

`placement` 原先把灶台、出餐口四邻和已就位盘的格 + 四邻都禁掉。只有放**到灶台上**才会接着加热，灶旁台面不会，且落点候选只取 counter / 地板、不含灶台本身；放在盘旁边也不挡往盘里装菜。现只禁出餐口四邻，加「已占用格」（盘/锅/料/砧板自己那一格，`_occupied_station_cells`，地板兜底 `find_drop_cell` 也用它）。`trace-20261030-214824` t=1164 救锅：右篮近处台面全被这两条四邻禁区挡住，锅被搬到 FoodBasket (12)；重算后选右篮 (2)（灶旁 2 步）。

## 接缝可走性诊断（2026-10-30，诊断中，未改决策）

`trace-20261029-234145` t=6199~6230：篮子接缝 gx=4 列缺口 1.7m、P1 来回走得过，但两格中心离墙都 <`CHEF_CLEARANCE`(0.35)，`make_walkable_from_terrain` 判不可走，P2 候选全空、拿着盘子原地不动。反编译确认游戏没有给玩家用的可走标志（`GridNavSpace.m_nodeMap` 只在 Start 按占位算一次、只给老鼠用）。

- 插件新增 `walk_probe` 帧字段（`plugin/Oc2WalkProbe.cs`，BepInEx 配置 `Diagnostics.WalkProbe` 默认开，**只在有会动附属网格的关卡出值**，其余为 null）：每个厨师 ±2 格、每格 3×3 子点，向下 Raycast 找地板 + 厨师碰撞体 OverlapCapsule；带身体尺寸、原地自检、阻挡体明细（类型/层/旋转/AABB/是否在 terrain 层）。做法参考 cortico-world-overcooked2 `Nav.Walkable/BodyBlocker`。`state_dump` 透传到 trace 的 `state.map.walk_probe`。
- 离线对照：`python3 tools/walk_probe_report.py trace-*.jsonl`（汇总 FN_center / FN_offset / FP 与阻挡体），`--at <t>` 打印某拍格子图。
- 实机结论（`trace-20261030-001857`）：厨师 CapsuleCollider 半径 0.4；接缝列格中心判定与物理一致（AABB 推算可信），误判全是 **FN_offset**——门洞开在两排格中心之间（t=1486 缝宽 1.26m，可站 z∈[-11.36,-11.0]，格中心 -12.0/-10.8 都在墙里）。

## 平台接缝门洞（2026-10-30）

- `ai/space/seams.py`：Ground 盒贴边 + 接缝上有薄墙 = 接缝；墙在墙带（接缝线 ±half）上的投影取补集、按 `GATE_RADIUS=0.45` 收窄得门洞。接缝列格是**门洞格**：只许沿穿缝方向进出，门洞与该行有交集才通。`seams.nav_predicates(world)` 给寻路用的 walkable/edge_blocked（`reachability.field_for`、agent 兜底、`tree_viz`）；**站位/交互位仍用 `world.walkable`**，门洞格不会被选成落脚点。`trace_back` 现在带上 edge_blocked（含原有薄墙判定）。
- move 命令新增可选 `gates` 字段（`seams.path_points`）：路径中间的门洞格换成「缝口入点 + 出点」两个路点，沿缝取门洞中线。插件 `Oc2Gate.cs` 每帧在墙带上 OverlapBox 现算中线（篮子 0.55m/s，Python 5Hz 的中线到人走到时已过期），入/出点按跨缝坐标判到达；门洞没开且还没进缝时在缝口外停下等（`GATE_WAIT=4s`，超时 `why="gate closed"`），已进缝则沿用上次中线。旧插件忽略 `gates`，按静态中线走。
- 回放 `trace-20261029-234145` t=6200~6220：x=13.2 门洞判通，P2 可达格 +20；t≥6225 缝 <1.0m 判关（P1 半径 0.4 硬挤过去，0.45 留余量）。
- **门洞时间表**（`ai/space/seam_clock.py`，`bb.seam_clock`）：篮子随 Animator 固定周期动，门洞开关也是周期的（Balloon_2_3 一圈 27s；x=13.2 开 9s / 关 18s，x=19.2 开 24s）。Agent 每拍 `_observe_seams` 记开→关/关→开沿，周期 = 相邻开门沿间隔（两段一致取均值；一段先暂用；只见一次开门时用插件 `grids[].anim` 的循环长度），开门时长 = 最近一次完整开→关；新开门沿偏离预测 >1s 丢掉重学。防脏：开/关不足 1s 当抖动；观测断档 >2s 只重定初值、不连沿（trace-234145 t=2007~6221 断档曾被连成 210s 的开门）；周期限 5~120s。时钟用插件 `level_time`（`Time.timeSinceLevelLoad`），旧插件退回本地单调时钟，暂停中不记。离线核对 trace-001857：74s 起时间表可用，108 个预测 0 错。
- **按到达时刻等门的寻路**：`navigation.distance_field(wait_cost=...)` 从普通格踏进门洞格时按 `now + 代价×SEC_PER_STEP(0.25s)` 查时间表，门关着就加等门步数（门开着但走到时已关也等下一圈），返回的 `Field` 带 `waits`（每格一路等门步数）；`trace_back` 自动用建场时的 `wait_cost`。时间表已知时，此刻关着、但开过的门洞格也算可走（中线取上次开门时这一格的中线）。没时间表时与之前一致（只认此刻开着的）。
- **等门让位**（`planner._select_layer` + `_gate_waits`）：要在门口等 >`GATE_DEFER_S`(3s) 的候选先让出本拍——同层有不用等的只在它们里挑，整层都要等就往下层找不用等的先做；等待缩到 3s 内自然回来（掐开门时间出发）。不让位：L0~L2（火/快糊/刚熟）、当前承诺的 task、P2 手里有东西。整张表都要等时照旧选（去门口等）。trace 顶层 `gate_waits` 记每拍候选等门秒数。
- **执行**：`move.gates[].max_wait` = 预测等待 + 3s（缺省 4s），move 超时同步加长；插件门关着时把找缝窗口放宽一倍（开门时缝不一定在预测中线上）。
- **实机修正（trace-20261030-123850，P2 拿意面停在 x=13.2 缝口 ~13s）**：Python 与 walk_probe 都判门洞通（mid=-11.98，缝口一排全空），插件 `Gate.Refresh` 用碰撞体 **AABB 投影**判关——网格碰撞体 / 与地板重合的整块假墙的 AABB 盖住了实际开着的门洞。改为按厨师胶囊**真实形状逐点测**（沿缝 0.1m 一点、跨缝 5 条线，与 walk_probe 同一套过滤，20Hz 节流）。兜底：GoToCell 超时 2.5s 比插件等门 4s 短，`gate closed` 永远回不来 → move 超时一律加上插件等门上限；`gate closed` 回传门洞格，Python 记 `bad_cells`（`reason=gate_closed`，只绕格不拉黑资源），不再在缝口反复重发。日志 `move gate wait ... blocker=` / `move gate closed after Xs blocker=` 给出挡路碰撞体。
- **实机修正（trace-20261030-125344，预测有了但没用上）**：时间表学会了（t≈1600 已有 `gate_waits` 18.8s、插件收到 `max_wait=20.25`），但 t=1665~1830 P2 空站 8s、跨缝 task 全 `unreachable`。根因：门关着时篮子已挪开，门洞格对面那格此刻悬空，可达场止于门洞格。修：时间表按格记「这一轮开门首帧两侧地板的沿缝下沿」（`Seam.anchors` / `clock.cell_anchors`），寻路给关着的预测门洞格加**跳边**到「开门时对面那格现在的位置」（篮子刚体平移），原相邻步作废（`seams.nav_jumps`，`distance_field(jumps=)` + `Field.via` 回溯）；插件路点按发令时的篮子地板锚定，开门时正好随篮子移到缝口。另：相位对不上（掉帧 / 漏看几帧）不再整表清空，离上次开门仍接近整数圈就沿用周期与开门时长、按这次重新对相位（旧实现要再等一整圈）。离线重放：P2 在固定地面时砧板不可达 25/36 帧 → 0/36。trace 顶层新增 `seam_clock`（每道门 known/T/D/open/next_open/next_close），`state.level_time`。
- **实机修正（trace-20261030-192949，送餐到出餐口附近卡住、盘被放下）**：手持物挂在厨师刚体下、碰撞体照开（`ServerPhysicalAttachment.Attach` 只换父节点），伸在身前先顶到台面。燃烧器旁中途路点 (12,-10.8)：身体离燃烧器还有 0.5m，盘子已顶住，P2 停在 z=-11.43（开局拿意面在同处卡在 -11.31），0.8s 后 stuck → no_progress 拉黑送餐 → 备菜腾手放盘。Python 可走判定与 walk_probe 都只按身体胶囊，看不到这一点。修：插件 MoveCommand 停滞 0.3s 时用 `Rigidbody.SweepTest`（整个刚体，含手持物）试探朝下一个路点走，没挡就跳过当前中途点（门洞入/出点、终点不跳），日志 `[EXEC] move skip waypoint`。不对单个 task 豁免拉黑。
- **实机修正（trace-20261030-213754 t=1752~1780，等门后掉头）**：参照系混用。P2 和入点前一个路点都在篮子上（相对位置一直不变，P2 还差 0.4m 没到它），等门范围却按缝口量（缝口不随篮子动）。P2 等门时被篮子带出范围，门没开就结束等待，转去够那个路点，盘子顶到篮子上的箱子 stuck，送餐失败 → 备菜放盘。修：进入缝口等待范围（离入点 ≤ GATE_HOLD+WAYPOINT_EPS）就把入点前一个路点直接算到达、以入点为目标；被带出范围会走回缝口继续等。
- **未修（同一 trace t=708）**：开局第一圈 x=13.2 门还没见过开→关，时间表 `known=false`；端盘途中门关上，出餐口判 `unreachable_destination`，送餐被否决、备菜放盘。时间表要至少看完一圈才能预测，开局这一段无解。可选方向：按关卡缓存学到的周期/开门时长，下局开局直接用。
- **待实机验证**：跳点是否按预期触发（`move skip waypoint`）；插件中线跟随与缝口等待（日志 `[EXEC] move gate wait` / `gate(x=13.2 ...)`）、窄缝时 0.45 是否过保守、`SEC_PER_STEP` 与实测走速、`grids[].anim` 是否 loop（不是单一循环态时只靠沿学，开局要多等一圈）、让位后能否按时回来（承诺惯性 2.0 分差可能拖住）。

## 单份投掷 / 精准投进锅（2026-10-29，插件 0.23.2 / 协议 7）

- **ThrowBatch 一次只投一份**（`relay.MAX_BATCH_UNITS = 1`）：缺口封顶到 1，树去掉 `RepeatUntil`、投完一份就交还 Planner，`batch_bonus` 随之为 0。分割地图（Rapids_2_1）照常投给对面；**连通地图远投关闭**（`throw_supply.ENABLE_FAR_THROW = False`）：单份没有摊销，料还落在锅旁地上要再捡。批量（锅位封顶、打分）以后再议。
- **FillCooker / FetchToCooker 末段精准投锅**（`primitives.ThrowIntoCooker`）：反编译确认锅接料靠触发器（`ServerAttachmentCatchingProxy` → `ServerIngredientCatcher.CanHandleCatch`：锅挂在灶上、能收这种料就收），飞行中穿过锅即可，不必落进锅；台面（`ServerAttachStation`）只接**非飞行**物，不会半路截走，但别的灶/锅会。命中关键是朝向：唯一成功样本 `trace-20261029-213506` t=558 出手距 4.7m、朝向偏 4°，此前落空的远投偏 ~13°（插件门槛 0.97≈14°）。现在出手距 3~5m、路径只过可走地板且无别的厨师、`aim_dot=0.996`（≈5°，5m 横偏 0.44m）、`strict`（超时没对准就不投）。投偏/没对准写 20s 冷却，期间这口灶走过去放；投偏的料先从地上捡回再放。
- 插件 `ThrowCommand` 新增可选字段 `aim_dot` / `strict` / `face_timeout_ms`，对准要连续 2 帧满足；缺省行为不变（旧 Python 不受影响）。
- **待实机标定**：出手距离带 3~5m（近距离料可能还在上升段越过锅）、0.996 门槛下小推杆转身能否稳定对准（`aim timeout` 多就放宽到 0.993）、冷却时长。

## async_resource 改为「进灶才给」（2026-10-29）

`trace-20261029-213506/220619`（Balloon_2_3）：意面相关 task 占约 1/3 拍，肉/鱼/虾排不上。`_is_async_task` 按类型名只认 FetchToCooker/Chop：意面取料即下锅第一步吃 +2，肉/鱼/虾第一步 FetchToBoard 吃不到；而切配本身并不异步（反编译 `ServerWorkstation.UpdateSynchronising`：有人切才推进），寿司鱼这类不进灶的 Chop 却照吃。改为 `planner._feeds_cooker`：FetchToCooker 恒给；FetchToBoard/Chop 看生料是否该单某道熟料的成分；无配方书退回旧判据。

离线回放（每 10 帧抽一帧无状态 plan）：
- Balloon_2_3（213506）：ThrowBatch(Pasta) 29→6、FillCooker(BoiledPasta) 29→20、ThrowBatch(BurritoMeat) 16→46、FetchToBoard(BurritoMeat) 0→5；（220619）FetchToBoard(Prawn) 28→37、FetchToBoard(UncookedFish) 8→12、ThrowBatch(Pasta) 21→15。
- Mine_2_5（235333）无变化；Sushi_1_2（233327）仅 ±1~4 帧（Chop(SushiPrawn) 少 1）。
- **Balloon_1_5（112633）需留意**：ThrowBatch(PastaTomato) 3→64、FillCooker(BoiledPasta) 14→0。灶空时 FillCooker（eta −1.5 + cooker_idle 3.73 = 2.23）以 0.02 输给番茄远投（eta −1.0 + async 2.0 + combo 0.25 + batch 1.0 = 2.25），14 帧是这个差距。根因是 FillCooker 与 ThrowBatch 两套加分口径（cooker_idle vs async+batch）本就不可比，此前靠 FetchToBoard 没有 async 掩盖；要不要让「灶空着」压过番茄备料，属于改权重，未动。
- **batch_bonus 改为几何递减**（`planner._batch_bonus`）：第 k 份加 `W_BATCH × BATCH_DECAY^(k−2)`，N=2/3/4/6 → 0.5/0.75/0.875/0.97，上限 1.0（原线性 0.5×(N−1)，220619 里出现过 1.5）。理由：N 随订单数涨，后几份服务的单更从容、锅位不够要等、批次常被打断。回放后 Balloon_1_5 的 FillCooker(BoiledPasta) 回到 14 帧（ThrowBatch(PastaTomato) 64→50）；Balloon_2_3 两局基本不变。
- ThrowBatch(BurritoMeat) 变多会放大 split 模式「拿起→放下」循环（见 220619 t=685~942）→ 已修，见下条。
- **split 模式拿着料的保护**（`relay.ThrowBatch`）：`_carrying_this_batch` 原只认 far。split 下篮子漂移让「P1 能到砧板」逐帧翻转，t=706/905 刚拿起肉就判不可行、树按「本批结束」SUCCESS、下一拍 StashHeld 放下，再拿起（ThrowBatch+StashHeld 约 8% 拍）。现在：拿着本批料且承诺是它时按探测口径（far/split）继续；这一帧暂无出手位也算可行，由单轮里新增的 `WaitUntil(有出手位, LAUNCH_WAIT_TICKS=60)` 兜底，等满失败 → 承诺清空 → 保护解除 → StashHeld。t=706/905 两帧当场就能出手（`throw_ok_here` 为真），落点按 P1 可达格判。fixture `balloon23-20261029-220619.ndjson.gz`。需实机验证。

## 食材配方节点导出（2026-10-29，插件 0.23.0 / 协议 7）

`trace-20261029-213506`（Balloon_2_3）：P2 从不碰鱼，3 张海鲜单的鱼全靠 P1。反编译确认游戏下锅/装盘按物品 prefab 上 `IngredientPropertiesComponent` / `CookableIngredient` 的 `m_ingredientOrderNode` 判（`IngredientToContainerBehaviour` → `AssembledNodeTransfer.CanCombineWithContents`），不看物品名。鱼箱出 `Fish`、切成 `ChoppedFish`，下平底锅后是 `ing:UncookedFish`（`trace-20261029-130632` t=263），而配方 `PanFriedFish` 要 `UncookedFish`——Python 按物品名找不到来源，鱼相关 task 全 infeasible。肉/虾/意面的物品名恰好等于节点名，没暴露。

- 插件新增：`items[].node`、`chefs[].held_node`（物品自身配方节点）；箱子 `node` / `chop_node`（出料与切好产物的节点）；`recipes[].chop_node`；切配索引同时按食材自身节点名登记。
- 实机确认（`trace-20261029-215035`）：`ChoppedFish→UncookedFish`、`ChoppedPrawn→Prawn`、`Pasta→Pasta`；鱼/虾/肉箱 `chop_node` 分别是 `UncookedFish`/`Prawn`/`BurritoMeat`。**要切的生料（Fish/Prawn/BurritoMeat）本身没有配方节点**（`node=null`，只挂 WorkableItem），只有切好产物和免切料（Pasta）有。
- **0.23.1 生料改名**（参考 `feat/throw-transport` 的 `BuildLeafRewrite`/`MapRawName`，该分支 0.20 在 `trace-20261027-233047` Mine_2_5 实机跑通切鱼煎鱼；只移植这一块，未合分支）：生料自身无配方节点、切好产物节点名 ≠ 生料名 ≠ 切好名时（鱼），登记 `RawNames`（`Fish → UncookedFish`），并把配方叶子 `UncookedFish` 补上 `chop=ChoppedFish`。改名作用于箱子 `ing`、`held`、砧板 `item`、`items[].n`、`interactions` 句柄名，以及 exec 侧 `ChefFrame.NameOf`（句柄/手持名），快照与句柄同源，Python 零改动。虾/肉/意面不受影响。`node`/`held_node` 保持游戏真值（生鱼为 null）。离线：改名后的 t=93 帧 `FetchToBoard(UncookedFish)` 可行（`test_renamed_fish_crate_makes_fish_choppable`）。**需实机验证**：散落生鱼的拾取、砧板上生鱼的切配。

## 篮子网格原点 / 飞行料取料（2026-10-29，插件 0.22.1 / 协议 6）

- **篮子台面拿不起盘**（`trace-20261029-204717` t=425~498，Balloon_2_3）：插件 `grids[]` 对非主网格报 `QuadGridManager.transform.position`，和主网格一样有父级偏移（RightBasket 报 (0,1.2)，按站点 `m_gridIndex` 反推是 (25.2,-9.6)；FoodBasket 报 z=-12.9，实为 -12.0）。盘/散料只有 x/z，Python 按错原点换算本地格 (24,-10)，句柄是 (3,-1)，`HandleMatches` 永远不过，拾取一次都没按。0.22.1 起非主网格原点按该网格下一条 Static/DynamicGridLocation 现算（`GridOrigin`），`GridIndexOn`（篮子上的洗碗池/出餐口）同源。**0.22.0 及更早的帧里非主网格 `ox/oz` 不可信。** 需实机验证。
- **追空中的料**（`trace-20261029-202903` t=231~312）：`find_ingredient` / `loose_count` 跳过 `flying`；FillCooker 取料只锁传送带段，散落料每拍重新解析，不锁 item 快照。

## 2-3 煮锅/平底锅/摆盘修复（2026-10-29，插件 0.22.0 / 协议 6）

`trace-20261029-130632`（Balloon_2_3）：意面投过去不下锅、熟锅不装盘、备好的菜不送。反编译 Assembly-CSharp 后的根因：

- **插件截断灶条目**：`AppendCookers` 按类型名子串同时命中 Server/ClientCookingStation，又有 `i >= 8` 上限；灶 >4 口时后几口只剩客户端条（ClientCookingHandler 无 `IsCooked()`），`cooked`/`onFire` 恒 null（Balloon_2_3 两口煮锅、Mine_2_5 后两口灶）。现按 `ServerCookingStation` 类型（含子类）筛、去掉上限。Python `cooker_has_pot` 改看 `cookware`/`cook_type`，旧帧也能判。
- **平底锅不算锅具**：`held_is_cookware` 原样子串比配方 cook 名，`fryingpan` 匹配不上 `utensil_frying_pan_01`。插件新增 `items[].cookware` / `chefs[].held_cookware`（`ServerCookableContainer` 组件，同参考项目），Python 优先读字段，旧帧用 `cookware_matches` 归一化；新增 `P.item_is_cookware`。
- **篮子台面不算台面**：`_counter_cells` / `_match_plates_to_orders` / `planner._placed_plates` 用站点本地 `gx/gz`，篮子上的盘永远不算就位，摆盘目标格全在 P2 够不着的厨房。改为主网格投影（`placement._station_cell`），拼装格先按 P2 可达性排。
- **装盘取错源**：散熟锅 token 按 `cooked_of_raw` 展开到所有炊具（平底锅虾也报成煮虾）；`_inst_take_cooked` 把匹配到本单的源一律当成每道熟料的源（鱼锅被当意面源）。现在散锅只留自身炊具的 token，装盘前校验源确实装着这道熟料。
- **连击跳单出口**：`_breaks_combo` 在队首剩余 ≤ `COMBO_SKIP_HEAD_REMAINING`(8s) 且没有料齐盘时放行。注意：这局的长时间不送，实际是队首 Marinara 等 P2 做（上面几条修了），队首 t=1996 由 P1 送出；之后 MeatOnly 盘不送是出餐口只在篮子靠岸时可达（`unreachable_destination`），属正常等待。
- **远投连续性**：远投按料源（散料/料箱）到消费站的距离判，手上那份不算；承诺中的 ThrowBatch 拿着料走近也继续投完。消费站附近半径 2.6→3.6m。

未修：P2 端着熟锅、场上没有可用盘时，RescueCooked 只把锅放台面，不会直接装盘。需要实机验证煮锅 `cooked` 字段与 `cookware` 字段（`./restart.sh` 重新部署插件）。

## 投掷传递 / 批量备料（2026-10-29，分支 feat/throw-bt）

按 `trace-20261028-234917`（Rapids_2_1，木筏↔厨房不连通，P2 切好料后 110 个候选全 unreachable 干等到底）：

- 主网格判定改为插件 0.21 的 `grids[].main`（挂 `GridNavSpace`，同参考项目 cortico-world），旧帧按名字 `GridManager`，再退众数并排除 `grids[].moving`。旧逻辑在 Rapids_2_1、Balloon_2_3 都会把附属网格当主网格。
- 新 task `ThrowBatch`（见 `tasks-primitives.md`）、原语 `GoToCell`、插件 `ThrowCommand` 朝向闭环、`items[].flying`。协议 4→5。
- **只做了离线验证**（fixture 回放 + 单测，1425 passed）。实机待验：射程/落点常量、`FrierBasket` 能否接住投来的料、木筏上 `GoToCell` 到位后出手点偏差。
- 连通关卡远距投掷默认开（`ENABLE_FAR_THROW`）：离线回放里 1-4 有拍选中「把米批量投向灶」、1-5 有拍选中投意面。`W_BATCH=0.5` 与 `THROW_MIN_DIST=9.0` 都是经验值，实机效果不好先关开关再调。
- 已知限制：木筏格投影到主网格，偏移非整格时靠砧板那排被判不可走（`docs/map/Rapids_2_1.md`）。
- 首次实机（`trace-20261029-122831`）两处停顿已修：① 出手格被木筏带走，`GoToCell` 追着过期格心耗到 move 超时（t=351~798）→ 先判「当下位置能否投」、段超时 2.5s 后重算格重试、分割地图落点容忍放宽到 4.5m；② 厨房/木筏同名台面（`countertop_01_standard_old (4)/(5)/(6)/(11)`），木筏上的料被解析到厨房台面，FetchToBoard 连败拉黑（t=1273~1469、1794~1990）→ `_station_for_item` 同名时按离物品最近选（插件 `item.grid` 同样按名反查，不可信）。
- 第二次实机（`trace-20261029-125348`）：P2 接住第二个土豆后拿着干站 7.5s。`recipe_graph` 的 Chop done 是「场上有一份切好的」，两单要薯条时第二单不再发取料上板。现在上游仍按存在性放行，另按「切好份数 ≥ 排在前面的等料单数」（`chop_waiters`）给缺的那单补发切菜链；`StashHeld` 同口径。

## 本地修复补充（2026-10-19）

1-4 台面盘长时间停止修复见 `../notes/sushi14-stall-2026-10-19.md`：Face 不再误跳过缺网格但实际在台面的盘/散物，借同位置容器的交互元数据且保留物品资源身份；规划读取 bad_pairs，避免唯一站位被排除后每拍重新选同盘失败。12 项新增真实帧回归，全量 1316 passed / 85 skipped；本轮未重新实控。

目标附近反馈修订见 `../notes/target-approach-recovery-2026-10-19.md`：多格站点朝向、对齐后重转向、无进展受保护尝试、局部两次换位/新帧门槛、移动站点刷新、归零守卫与薄墙回溯约束已修改。坏交互站位仍可当通道。新增 50 项反馈回归，尚未再做实机控制；不要把旧轨迹当修改后效果。

Windows 最新运行见 `../notes/windows-live-validation-2026-10-19.md`：已部署 0.19.0 / protocol 4 并完成 150 秒双人 1-5；11 个成功终态、19 个失败终态，全队新增 4 份出餐，仍有局部移动和交互失败。实测后修复同名盘模型导致的误归属，按 held_by 判断；最终 1254 passed / 85 skipped，追加修正尚未再次实控。插件采样中位 46.59ms，性能问题仍在。旧 Windows 项目和新运行目录不能混用协议。

基线 b7a1f82 的交替失败无法累计、裸资源退避误伤、原地守卫屏蔽火情、取料来源缺可达性过滤、局部重算丢平台掩码、旧认领与切关状态残留已在本地修复。新增 25 例，完整回归 1234 passed / 85 skipped。细节及尚未实机验证的边界见 `../notes/bt-stall-recovery-2026-10-19.md`；下文旧状态属于历史记录，不能再把修复项当作仍未修改。

本文是 `AGENTS.md` 的详细补充，记录技术债务、近期修复与后续待办。**改动细节不在此维护**——问 `git log` 更准，这里只记对下一步工作有约束力的东西。

## 1. 已知限制 / 技术债务

（多为 spec §10 的未来工作，或文档已记录的妥协）

- **寻路坐标系仍是单一主网格，但实际已能跨到篮子**：`navigation.cross_grid_bridges`
  算法仍未接入（dead code），`World.walkable`/`to_grid` 用的是主网格 GridManager
  的 origin 投影。但 `make_walkable_from_terrain` 按**世界坐标**的 terrain AABB
  判可走，两个网格的地板都在 AABB 里，所以角色实际能走到篮子上（篮子停靠在
  可达位置时）。网格边缘的薄墙由 `edge_blocked` 拦边补上。**真正没建模的是
  篮子沿 z 水平漂移**——它上面的料箱/洗碗池/盘回收随篮子漂，时可达、时不可达，
  这个动态性没有进入 planner（见 §2 本轮「仍未解决」）。
- **P1 名称硬编码**：`"Player 1"` / `"Player 2"`（`predicates.P1_NAME/P2_NAME`）。真实帧 `chefs[].slot` 全为 null，只能按 name 区分（已核实 2204 条 chef 条目）。
- **熟料 token 不透明（仅旧帧）**：`RecipeBook.want_tokens` 对需烹饪的料同时给 rich token（`cooked:{raw}:{cookware}`）和旧帧 opaque `COOKED_PLATE_TOKEN`。新帧 `detail` 已通过 `plate_tokens` 输出 rich token，`plate_has`/`plate_lacks`/`missing` 据此能区分同一盘多份不同熟料；旧帧（无 detail）仍退化为不透明 token，多熟料关卡在旧帧下无法区分。
- **打分权重部分未标定**：收益相关的几项（`W_COOKER_IDLE` / `W_COMBO_*` / `W_FRESHNESS` / slack 曲线）已按真实 trace 定标，其余 `W_*` 与穿插阈值（`INTERLEAVE_THRESHOLD/PENALTY`）仍是经验初值。改之前读 §4「改权重前必读」。
- 对话协作层（`Intents.assignments`、split mode 的显式分单 `order_assignment`）原为空壳预留，已连同 P1 短程意图推断一起作为死代码删除（`2026-10-18` 清理，见 `ai/state/p1model.py` 顶部说明）。
- 「16 个 task」的说法已过时（现为 19），以代码的 `TASK_TYPES` 为准。

## 2. 当前状态

分支 `feat/voice-dialog-interaction-v2`（语音对话 worktree），全量测试 **1498 passed, 80 skipped**。

**语音对话（2026-10-19 实机验证）**：

- TTS 2.0（`seed-tts-2.0` + `zh_female_vv_uranus_bigtts`）、ASR 2.0（`volc.seedasr.sauc.duration`）、方舟 plan 端点（`/api/plan/v3`）均已用真实 key 跑通；游戏内 `ASR → LLM → 命令 ack → TTS` 完整链路验证通过。
- 输入切句推荐 **PTT**：`OC2_VOICE_PTT_KEY=f8`，按住录音、松开发送，一次按住只触发一轮对话。macOS 笔记本默认 F8 是媒体键，需要 Fn+F8 或启用「标准功能键」。
- **已知限制**：`run_game.sh` 直接启动游戏，Steam Input 的“游戏布局”不保证生效，手柄按钮映射到 F8 尚未在游戏内验证；需要时改用 Steam 桌面布局或实现原生手柄监听（未实现）。
- 方舟 plan key 必须配 `ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/plan/v3`；普通 key 用 `/api/v3`。`OC2_LLM_TIMEOUT` 默认 10s，3s 会频繁超时。
- 实机环境默认输入曾是 AirPods，PortAudio 报 `-10863` 且会阻塞；改用 MacBook 麦克风/扬声器后正常。v1 仍只用系统默认音频设备。

**2026-10-16 修的三个实机卡死**（都是「可行性算对了、动作发不出去」一类）：

1. **传送带被当成不可走**（`88b8ebc`）：`ConveyorStation` 自己占住它那格，
   运行时占用表把 18 格传送带全报成占用。旧 `make_walkable` 有「传送带 →
   可走」的放行，`make_walkable_from_terrain` 漏了。寿司关两排传送带是左右
   两半唯一通路，全封后 P2 从出生点只够得到 **6 格**（修后 61），所有 task
   的 `Approach` 第一拍 FAILURE、整局零按键。
2. **`cookers[].grid` 恒为 null**（`b30c2e5`，插件侧）：网格归属挂在
   `StaticGridLocation` 上，灶状态挂在 `ServerCookingStation` 上，同一
   GameObject 的不同组件。于是 `(grid,name)` 资源 id 一半可用：
   `cooker_state` 查 `cookers[]` 命中（null 与 "" 都归一化成 ""），
   `cooker_station` 查 `stations[]` 落空（"GridManager" != ""）。
   `FillCooker` 取到米却解析不出灶的位置 → 「拿起米又放下」、整局零烹饪。
3. **两个交互字段语义不同**（`e746d57`→`60593ea`）：`interactions.gx/gz`
   来自 `GetFacingGridOccupant`（按**朝向**打分挑正对着的格），
   `placement_gx/gz` 来自 `ScanForObject`（按**距离**取基准点最近物）。
   `Face` 读前者、`Tap` 读后者，所以 **Face SUCCESS 不蕴含 Tap 能按**。
   实机 P2 偏离格心 0.3 格时出餐口以 1.4% 之差抢走句柄，树首
   `EnsureHandsFree` 塌 → 重建 → 再塌，钉死 287 拍。修法是 `AlignToCell`
   往格心收紧（反馈，不预测）。反编译依据见
   `docs/superpowers/specs/2026-10-16-interact-handle-feedback-design.md`。

**三者都只做过离线验证**（真实帧重放 + 单测），实机效果待下一局。

**本轮（2026-10-17）气球关多网格 / 移动平台一轮修**（插件 + Python，离线验证为主）：

背景：主关变成 **Balloon_1_5_2P**，它有一个**会沿 z 水平漂移的篮子**（Balloon
Basket，不是上下电梯，高度 y 不变）。篮子上挂着料箱、洗碗池、盘回收等站点；
篮子漂到不同 z 位置时，这些站点从主厨房时可达、时不可达。这一轮所有卡死都源于
「把会漂的篮子当成静态地板」。

已修：

1. **洗碗池放不下脏盘**（插件侧）：功能站 `washing`/`plate_station` 之前
   `grid=None`、`gx/gz` 被报成主网格投影（错帧），`interact_ready` 拿它跟游戏
   句柄的篮子本地坐标比对恒不等 → 角色站到洗碗池边也不按放置键。修法：
   `AppendStations` 功能站循环改用 `GridManagerObj`（本组件→同对象其它组件→
   子物体逐级找网格管理器）+ `GridIndexOn`（按该网格 origin/cell 反解本地坐标）。
2. **薄墙不挡路**（`make_edge_blocker`）：网格边缘有 0.5~0.8 宽的凸起 collider，
   夹在两格之间、盖不住任何格中心，点判漏掉 → 角色朝墙冲。修法：`distance_field`
   新增 `edge_blocked(cur,nxt)` 参数，拦「薄墙夹在两格中心之间」的边、不拦格。
   教训：更早试的「区域相交」判格会过度拦截（站点 collider 溢出邻格），把上层
   地板整排封死，已回退，`tests/test_occupancy.py` 钉住「边缘溢出不拦格」。
3. **移动平台上停稳误判**：角色站在篮子上时世界 x/z 被平台带着漂，
   `MotionControl` 的 observe 停稳判据永不成立 → `observation_timeout`。修法：
   插件 `AppendChefs` 报 `chef.rel_x/rel_z`（世界坐标减脚下地面 collider 参考点），
   motion 只用相对坐标判 settle/no_progress（目标距离 error 仍用世界坐标）。

仍未解决（本轮末尾卡死的根因）：

- **篮子漂移 → 站点可达性动态变化，planner 没有「等篮子漂回来 / 登上去」概念**。
  两个连锁问题：① `FetchToBoard`/`FillCooker`/`FetchToCooker` 的 `feasible` 只查
  「料箱存在 + 灶空」，不查「料箱当前可达」，篮子漂到顶部时料箱不可达，planner
  仍反复选这些 task、每拍立即失败；② 失败拉黑 `_note_failure` 要「同一 task key
  连续失败 3 次」，而 planner 在两个 task 间交替，streak 每拍重置 → 永远拉黑不了
  → 无限打转。
- **`note_motion_failure` 单次 `no_progress`/`observation_timeout` 就按资源 id
  拉黑 10 秒**：`TakeCookedToPlate`（盛饭）失败会把 `RescueCooked`（端锅）一起
  拉黑——两者 `claims()` 共享同一资源 id、动作不同。于是熟锅没人端，烧糊起火。
  拉黑粒度应是 (资源, 动作)，不是裸资源。
  **2026-10-18 部分缓解**：`RescueCooked`/`TakeCookedToPlate` 已声明 `NO_ABANDON`
  （写侧 `_note_failure`/`note_motion_failure` 与读侧 `_is_abandoned` 都跳过），
  这两个停火任务不再被拉黑，「烧糊起火」这一症状已封死；但「裸资源 id」的粒度
  问题对其它共享资源的 task（FillCooker/ReturnPot/Discard 之间）仍在，待修。
- **`RescueCooked` 对「已熟且快烧糊」的锅 urgency=0**：锅 progress 远超
  cooking_time 时端锅没有加分，和同分的盛饭抢被抢走、盛饭又失败。

本轮（2026-10-15）把打分从扁平加权和改成**层间词典序 + 层内加权和**
（`ai/planner_layers.py`，设计见 `docs/superpowers/specs/2026-10-15-layered-scoring-design.md`）。
动机：17 项加权和有 O(n²) 对交互，单组测试结构上查不出任何一对——§4 记的那次
回退就是这个性质的后果。分层后跨类优先级由控制流隔离保证：有高层候选时低层候选
压根不进打分。

层序（数字小 = 层高）：L0 火情 / L1 不可逆损失（锅快糊）/ L2 救料（刚熟的锅）/
L3 兑现 / L4 解卡（无盘）/ **L5 空层** / L6 备菜（含填灶）/ L7 清理。判据是「有
倒计时的压过没倒计时的，倒计时短的压过长的」。

**L5 原装 FillCooker，已证明是设计错误**（`828a99f`）：空灶没有倒计时 —— 灶空着
不会变质、不会毁料、不会蔓延，「灶空转的机会成本」是**期望收益**而非流失的时钟。
期望收益之间该用数值比较，那正是 L6 的事。实机代价：填灶拿到 top_layer 的
1043 拍**全部**压掉了本该做的备菜（一次例外都没有），而其中 **77% 的拍缺口只有
1 份**（`cooker_idle` 仅 1.9 分，本该输给中段备菜的 1.5~4）—— L5 里只有填灶
自己，打分形同虚设，1.9 分无需跟任何东西比较就赢。1002 拍工时只换来 4 锅米。
**判层序时要问的是「它在抢救什么正在流失的东西」，不是「它多重要」。**

**承诺惯性一行未改**——分层管「挑哪个」，抢占仍由 `COMMITMENT_MARGIN` 与
`pressure >= 1.0 跳过惯性` 负责。

最近一轮按 `trace-20261014-204822`（2 送达、tips 6、combo 0、收盘挂 5 单）定位并修掉四个 bug，提交 `cf2fc64`→`d644d25`：

- **slack 一个量被当两件事用**（`cf2fc64` 之后的 `53d6a2b`，影响最大）。`eta.slack` 同时承担「优先级」和「可完成性」，而它只适合其中一个。队列累计下队尾单背着全队工时 → slack 最小 → urgency 最高，得出「排得越后越优先」：**94% 的可比候选组（3516/3745）倒挂**。同一个量还让判死退化成排位歧视——order#4 出现 19 拍后即判死，当时 remaining=129/130s（寿命剩 99%），三个鱼单一单未送。现在打分走 `solo_slack`（只看自己的关键路径）、判死走队列 slack 并加 `DOOM_FRESHNESS_FLOOR=0.75`。倒挂 94% → 0%。
- **`time_left == 0` 被当成合法时限**（`ecbbadf`）：插件未开局/已结束都报 0，终局闸在开局就把 20 个候选全判 endgame，白站 147 拍。
- **完成度加分是线性的**（`37a0edc`）：2/3 比 1/3 只多 0.33，正好被 urgency 的组间差 0.35 抵掉。实机 t=1353「差一样黄瓜就能送」的盘输给「往 1/3 的盘加第二样」，这类局面本局 2290 拍。改成 `ratio^2 * W * 3`。
- **总分手写加法**（`cf2fc64`）：16 项重抄一遍，漏掉求和那处的症状是「trace 里有这一项但它不影响选择」而单测全不报警。改 `sum(terms.values())`。

**本轮仍只有离线验证**（`tools/replay_urgency.py` 重放本局 400 帧），实机待下一局。

按 `trace-20261015-165358`（2 送达、tips 6、combo 0、收盘挂 5 单）又定位修掉三个，
提交 `7d63253`→`827b038`：

- **散锅的时间压力归零**（影响最大）。`RescueCooked` t=1657 把刚熟的米锅端到台面后，
  `_time_pressure` 只查 `world.cookers` 查不到它 → 0.0 → 掉 L7 清理层。那个
  `TakeCookedToPlate` 候选 **184 拍都是 `ok=True/layer=7/top_layer=6`**：可行、
  分数算出来了，但层比任何备菜低一级，词典序下结构上选不中。熟米躺到收盘
  **1979 拍 ≈ 99 秒**，同期 #4/#5/#7 三单都只缺这份米。`TakeCookedToPlate._source`
  早就认散锅，是压力函数这一侧漏了。离线重放：那 184 拍全部 L7 → L2。
- **下料超时上限不够**：九次 `FillCooker` 四次撞 200 拍（t=149 用 199 拍只差一拍）。
  一趟实测 ≈95 拍，两趟 190 吃掉 95% 预算；失败后重试只用 40/25 拍就成功 ——
  活儿没白干，只是记账判了失败、重建树重走，三次白费 600 拍 = 30 秒。
  `TIMEOUT_TICKS` 做成 property（按料数摊开 + 让内外两层永远相等，详见下）。
- **trace 两处可观测性失效**：`top_layer` 3637 行全 null（`plan()` 算了但 Agent
  没转抄）；候选的 `chosen` 记的是 planner 原始选择而非实际跑的树，于是
  t=2582~2718 看着像 WashPlate/FillCooker 2:1 振荡，其实一直在跑同一棵树
  （⑨.5 接管，`infeasible_streak` 被反复清零）。现在并列记 `chosen` 与 `exec.task`。

- **填灶无条件压过备菜**（`828a99f`）：见上面 L5 那段。修法是把 FillCooker 放回
  L6、`cooker_idle` 随之进 L6 白名单（少了它填灶层内净分只剩 `eta` 的 -1.5，
  会从「永远赢」翻到「永远输」）。离线重放 1015 拍：缺 2 份保留填灶 100%、
  缺 1 份保留 33%、缺 0 份全让位 —— 连续单调无硬门。`W_COOKER_IDLE=5.6` 那套
  标定（按「压过备菜合计 4.97」定、饱和点缺 3 份）自动重新生效，没加新常数。

**一条教训**：`Sushi_PlainPrawn` 的 `score` 是 **20** 不是 60（`orders[].score`
字段里写着），所以「虾单一样料、该优先做」是错的 —— 单位工时收益
虾 20/340 ≈ 0.059 < 黄瓜 60/710 ≈ 0.085。先读 `score` 再谈选菜。同理
`order_hand_ticks` 按灶数并行折扣也是错的：它累加的是**手上**工时，一双手本就串行。

## 3. 待办

- **糊锅没人倒**：`Discard.feasible` 要求灶还在烧，所以火灭后的糊锅只能被 `StashHeld` 搁台面，要清空得等同灶再起火，或单独加一个「糊锅进垃圾桶」task。
- **`find_ingredient()` 返回板上的料时缺坐标**：给的是 `workstations[]` 那条，**没有 gx/gz/x/z**，于是 `FetchToPlate`（来源在板上）的 `Approach` 拿不到目标格，立即 FAILURE 并每拍重试。修法：按名字从 `world.stations` 合并坐标。
- **~~移动空转 51%~~ 已不成立，真正的瓶颈是 observe（帧率）**：trace-20261014-204822 的 cruise 2379 拍里 stall ≥0.3s 只有 13 拍（1%），无 ≥30 拍长卡窗口。按 `tools/exec_cost.py` 归因，时间在 `observe` 阶段（占有 task 拍数的 25%，957 拍 / 132 段、中位 7 拍）：每个精确步都要「松键 + 等新鲜帧 + 等停稳」≈ 8 拍 = 0.4 秒，而插件 5 Hz（实测每 3.5 拍才一个新帧）决定了 observe 至少等 4 拍。**杠杆在减少精确步次数或提高插件推送频率，不是调移动参数。** `eta.py` 的 COST_* 常数经实测仍吻合（FetchToPlate 中位 134 vs 常数 125），不用重标。
- **灭火「站位朝向都对却喷不灭」待复核**：`interactions.interactable` 报的是 `m_interactable`（手持物时报手持物），**不是朝向证据**，故未纳入 `interact_ready`。下一局用补全后的按键 trace 再看。**可能与 2026-10-16 第 3 条同源** —— 灭火器也走 `ScanForObject` 的 pickup 句柄。
- **`align_epsilon = cell/4` 只有一个实机样本**（t=9523 偏离 0.30/0.17，格心处全朝向可用）。下一局按 trace 的 `motion.aligning` 看介入频次与成功率：频繁介入却常常「收紧到位仍失败」说明阈值太松（该更早介入）或根因另有其它。
- **收紧动作本身耗时**：一次短按 + 观察 ≈ 8 拍（observe 至少 4 拍，插件 5 Hz，见上面 observe 那条）。预期远小于「树塌 + 重建」的代价（实机 287 拍彻底卡死），但没测过。
- **`AlignToCell` 只验证了 placement 一侧**：pickup 侧理论同构（`m_iHandlePickup` 与 `m_iHandlePlacement` 来自同一次 `ScanForObject` 扫描的同一个 GameObject），但没有实机样本。
- **`m_bGridSelection` 是关卡配置**（`GameUtils.GetLevelConfig().m_gridSelection`）。为 false 时 `GetFacingGridOccupant` 那条分支不走，通用 `gx/gz` 的语义会变，2026-10-16 第 3 条的前提随之失效。本关为 true（两个字段不一致即证明 B 分支生效），换关卡需复验。
- **全部改动只做过离线验证**（真实帧重放只回答「同样世界状态下会选什么」，不模拟物理执行），实机效果待下一局。
- **判死的新鲜度闸是个下限、不是完整方案**：`DOOM_FRESHNESS_FLOOR` 只保证新单不被排位歧视，但「5 单并发而一双手只够 2~3 单」这个结构性超额仍然存在。更对的做法可能是显式限制并发投入的单数（按 ETA 容量取前 N 单），而不是靠判死被动收口。
- **分层废掉了「提前端盘」的预取信号（已知代价）**：`_time_pressure` 在
  ratio 0.25~0.5 给 0→1.0 的渐变，本意是让 `TakeCookedToPlate` 提前去端盘站位
  （注释记着「等熟了才去拿盘会来不及，实机烧糊」）。分层后这整段落 L7、严格弱于
  任何 L6 备菜候选，到 pressure 1.0 才跳 L2——渐变对层选择零影响。这是词典序的
  固有代价：它表达不了「比备菜略高一点」。`tests/test_layered_selection.py::
  test_prewarm_ramp_lands_in_cleanup_known_tradeoff` 显式钉住了这个行为。
  **真实帧能重放时优先验这个窗口**；若确认烧糊，修法是改 spec §4.2 把
  `TakeCookedToPlate` 纳入 L6，不是调权重。
- **task 的超时上限：内外两层必须相等**。Executor 外层读
  `getattr(task, "TIMEOUT_TICKS", MAX_TASK_TICKS)`，而各 task 的 `build()`
  里往往自己又包一层 `PR.Timeout(..., TASK_TIMEOUT_TICKS)`。**两者不等时以更紧的
  为准**，于是「只加类属性放宽」会被内层静默吃掉——`FillCooker` 那四次撞线就是
  这么来的。`FetchToCooker` 现在把 `TIMEOUT_TICKS` 做成 property 返回与内层同一个
  表达式，钉在 `tests/test_fill_cooker_timeout.py`。**其余 task 还没统一**
  （`assembly.py` / `chop.py` / `cleanup.py` / `mix.py` / `relay.py` / `plate.py`
  都还是裸 `TASK_TIMEOUT_TICKS`，没声明类属性，所以内外一致、暂时无害），
  将来给它们放宽时记得两处一起改。
- **灶空转仍是 62.8%**（本局 2282/3636 拍三口灶一个都没在煮）。上面两条修完
  应该缓解（散熟锅被端走后 `cooked_shortfall` 才跟实际一致 —— 1658~2500 那 842 拍里
  `FillCooker` 有 735 拍报 `infeasible`，正是因为那口没人端的熟锅被算成了供给），
  但**没有离线办法验证**：重放只回答「同样世界状态下会选什么」，而这里的因果是
  「改选之后世界状态会不同」。下一局实机看这个比例。
- **填灶的终局误填还没修**：`_is_endgame_waste` 对 FillCooker 按
  `task_cost_ticks("FillCooker") + 60 = 180 拍 = 9 秒` 估预算，而它的产出要兑现
  必须走完**下料 120 + 煮熟 240 + 端盘盛 125 + 送 50 = 535 拍 ≈ 27 秒**，差 3 倍。
  所以 `time_left = 11` 秒时填灶不被否决，而那锅米 12 秒后才熟、结构上兑现不了。
  口径冲突是真实的：`COST_*` 表记的是**手上工时**（煮饭时人自由，见
  `architecture.md`「烹饪是关键路径不是求和」），对 ETA 队列是对的，对「值不值得开」
  是错的。修法是在 `eta.py` 加一个与 `task_cost_ticks` 并列的「兑现链耗时」函数并
  注释清楚两者不可混用。**优先级不高**：L6 竞争修完后缺口小时填灶自然让位，终局
  误填可能自己消失 —— 先重放看还剩多少再决定。
- **判死反复开合**：#4 首次判死在 `remaining/lifetime = 0.72`（floor 是 0.75，
  差一点没拦住），还剩 94 秒；整局开合 10 次，#4/#5 各 63%/64% 的存活拍处于判死
  状态。TTL 200 拍的迟滞没压住。与上一条的「结构性超额」是同一件事，别单独调 floor。

## 4. 改权重前必读

**跨层权重不会再互相抵消（分层后），但层内仍会。** 原先的实例：urgency 从
`W_URGENCY/remaining`（中段恒 ~0.1）改成读 slack 后，另一组验证过的七个「灶空转
该填灶」的拍全部回退，测试一个都没报警。那组回退现在由 L5 > L6 结构性排除
（`tests/test_layered_selection.py::test_fill_cooker_outranks_prep_structurally`
钉住）。动层**内**的 `W_*` 时，下面这套流程仍然适用。

动 `W_*` 之后：① 跑 `tests/test_fill_cooker_priority.py` 与 `tests/test_plate_supply_guard.py`（两处量级卡口）；② 用 `tools/replay_urgency.py <trace>` 查 urgency 是否仍单调（倒挂须为 0%）；③ 重放一局真实 trace 比对选择分布。

**打分口径**：urgency 必须读 `ETA.solo_slack_by_order`（独立 slack），**不要**读 `slack_by_order`（队列累计，判死口径）。两者对 remaining 的方向是反的，混用会让「排得越后越优先」，而单组测试查不出来——`tests/test_eta.py::test_queue_slack_is_the_one_that_inverts` 把这个差别钉住了。当前量级关系：**填灶与备菜在 L6 层内直接比**（`828a99f` 把 FillCooker 从 L5 挪回来），
`W_COOKER_IDLE=5.6` 按缺口线性给到饱和（缺 3 份满额），实测缺 3 份时合计压过备菜的
~4.97、缺 1 份时只有 1.9 分让位 —— 这一对是**今天唯一还靠数值定胜负的跨类关系**，
动它必须重放（见上面第 ③ 步）。端刚熟的锅（L2）与 0 盘洗盘（L4）已由层序隔离，
不再需要数值余量。

**警惕"自欺的修复"**：combo 第一版按 `task.order_id == head` 加分，离线复验看着像修好了（改选了 order#3），但盘里装的菜没变、游戏照样记给同 dish 里 `remaining` 最小的单 —— 实机行为与旧版一字不差，只是 trace 上的 order_id 变好看了。验证改动时要问「实机会不会不一样」，不是「trace 看起来有没有变」。

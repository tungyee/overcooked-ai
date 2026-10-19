# 已知限制、当前状态与待办

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
- 文档 `docs/notes/plan4_delivery_summary.md` 提到的「16 个 task」已过时（现为 19），以代码的 `TASK_TYPES` 为准。

## 2. 当前状态

分支 `feat/bt-opt`，全量测试 **1184 passed, 77 skipped**。

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

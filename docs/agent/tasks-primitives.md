# task 契约、执行原语与资源 id

本文是 `AGENTS.md` 的详细补充，聚焦 task / 原语 / 认领的完整约定。

## 1. task 的契约与身份

`ai/tasks/base.py` 定义 `Task` 基类。**task 实例 = 四元身份 `(TYPE, order_id, bound_resources, bound_item)`**，四项全同才是同一个 task；Task 是不可变值对象（`__slots__` + 禁 `__setattr__`），可作 dict 键/set 元素。每个 task 实现四个契约：

- `claims()` → 需要的 `(resource_id, action)` 认领键
- `feasible(ctx)` → 「此刻我可不可做」（按绑定参数求值）
- `done(ctx)` → 完成判定（即下游 task 的 `feasible`，上下游咬合）
- `build()` → 构建行为树（由 Executor 持有，不进黑板）

`ai/tasks/__init__.py` 是对外唯一入口：一律 `from ai import tasks as T`，**不要直接 import 子模块**。子模块按 spec 的「单文件 ≤ 400 行」硬指标分组（plate / cook / fire / chop / assembly / cleanup / mix / relay）。`T.TASK_TYPES` 登记全部 19 个类型，靠 `__init__.py` 的 `import *` 触发 `@_register` 才能完整；`tests/test_tasks.py::test_task_types_registry_covers_all_subclasses` 用计数钉住完整性。

主线 17 个 task 类型（另两个见表后）：

| 模块 | task |
|---|---|
| `chop.py` | `FetchToBoard`、`Chop` |
| `cook.py` | `FetchToCooker`、`TakeCookedToPlate`、`ReturnPot`、`RescueCooked` |
| `fire.py` | `Extinguish` |
| `plate.py` | `PlaceCleanPlate`、`WashPlate`、`ResetPlate` |
| `assembly.py` | `FetchToPlate`、`DeliverPlate` |
| `cleanup.py` | `Discard`、`RecoverLoosePot`、`StashHeld`、`Prefetch` |
| `mix.py` | `Mix` |
| `relay.py` | `PlaceToRelay` |

另有第 18 号 `FillCooker`（`cook.py`，继承 `FetchToCooker`）：**订单无关**的填灶，
`order_id` 恒为 None，谓词是纯世界谓词（空灶 + 熟料缺口 > 0）。它存在的理由是
结构性的，见 `architecture.md`「产能」一节。**它在 L6 与备菜同层按缺口竞争**，不在
独立的产能层——原因见 `status-backlog.md` 对 L5 的说明。

## 2. 执行原语（`ai/primitives/`）

树的叶子是带强制前后校验的原语（spec §7.2）。ctx 契约（`primitives/base.py`）为 `{w, chef, hands, bb, field, book, tick}`；target 优先传「可调用」以便每拍重算（树跨拍复用，构造时抓的 dict 第二拍就过期）。halt 只松键、不碰物品（优雅退出由抢占方树首的 `EnsureHandsFree` 负责）。

16 个原语（`primitives.__all__` 另含 `Primitive` 基类与 `resolve` 辅助）：

| 原语 | 作用（括号内是容易踩的坑） |
|---|---|
| `Approach` | 走到目标相邻交互位并落定。长距离巡航按住冲刺、距目标 ≤`DASH_BRAKE_CELLS`(3) 步松冲刺、最后一步短按停稳。（**只保证站位、不保证朝向**，定向交互须另配 `Face`；目标格就是脚下格时直接 SUCCESS） |
| `Face` | 转向 target（以 `interactions.gx/gz` 为准）。（`max_ticks` 只是兜底，不是「转好了」的判据） |
| `AlignToCell` | 句柄校验不符时朝格心收紧一步。游戏的 `placement_gx/gz` 来自 `ScanForObject`——**纯按距离**取基准点最近物，偏离格心时相邻目标会抢走句柄，于是「朝向对、放不下」。（**只在 `Face` 之后用**：`Approach` 的到位判据是位置不是朝向，放前面会与「两个灶之间横跳」那条教训撞车；**地板格不要用**，裸地板无 placement 信号会白收紧到 `max_ticks`。已居中仍不符时按 (资源,格) 拉黑并 FAILURE） |
| `Tap` | 单击一次，**边沿触发**（同一激活期内只按一次，否则 toggle 会反复拿起放下） |
| `Confirm` | 后置校验 `pred(ctx)->bool`，不成立先 RUNNING 等刷新。（`retries` 按**新快照**消耗，不是按调用次数） |
| `Settle` | 等 N 拍让状态刷新。（只等状态，不等物理稳定——后者已由 `Approach` 取代） |
| `WaitUntil` | **不按任何键**等到 `pred` 成立（端盘在锅前等饭熟）。（`timeout` 必须显式传） |
| `HoldUntil` | 按住 key 直到 `pred` 成立（切菜/喷灭火器/洗盘）。（传 `target` 则每拍校验 `interact_ready`，被传送带推离时松键 FAILURE 让 Sequence 重走 Approach，防空按卡死） |
| `TapWhileHolding` | 按住反向键的同时单击（传送带顶位专用，防被推走） |
| `EnsureHandsFree` | 手里有东西就放到就近合法落点，**幂等**。落点物料感知：空锅回灶、熟锅/糊锅不上灶；`home_cooker=False` 关掉归位（灭火端下来的锅只去台面/地上，否则会被当空灶放回去继续烧） |
| `InteractWith` | 复合 = Approach + (Face) + Tap + Confirm。（`expect` 必传，漏给即 TypeError） |
| `ThrowTo` | `/` 键投掷手持可投掷物，只校验离手。（落点不可控，米曾落到地图边界行够不到） |
| `Timeout` | task 级超时装饰，超时 halt 子树转 FAILURE 交还 Planner |
| `MarkBlocked` / `Nudge` / `AbandonTarget` | 三级脱困，**这三个节点本身仍预留未接线**（但 L1 的拉黑机制已有消费方：`AlignToCell` 写 `scratch.bad_pairs`、`Approach` 读它，是第一条真正接线的脱困路径）。实际脱困靠两处：`HoldUntil` 的位置自愈，以及 Agent 的连续失败计数（同一 task 连败 `ABANDON_FAIL_STREAK=3` → 写 `scratch.abandoned`，`filter_feasible` 跳过该候选，TTL 200 拍）。**灭火是例外**：`Extinguish` 声明 `NO_ABANDON`、`plan_firefighting` 传 `ignore_abandon=True`——拉黑按资源 id 记，别的 task 在同一口灶上反复失败会把着火的那口灶也写进去 |

移动控制用 `frame_seq/frame_received_at/now` 区分「新观测」与「循环拍」，短按与观察窗口按单调时钟计时（详见 `docs/notes/movement_control_fix_20261010.md`）。`motion.stall_seconds` 暴露巡航空转，见 `status-backlog.md`。

## 3. 资源 id 与认领粒度

资源 id 规则（spec §4.4）：
- 灶 / 板 / 洗碗池 → `(网格名, 站点实例名)`（spec 2026-10-15 地图重构后；
  同名站点可能分属不同网格）
- **拼装格 → 网格坐标 `(gx, gz)`（坐标即身份）**
- 盘 → 实例名 `n` 为主

认领是 **`(resource_id, action)` 动作级**粒度（不是按资源整锁）：P1 往 1 号锅放生饭只锁 `(cooker1, "fetch_to")`，`(cooker1, "take_cooked")` 仍开放——取熟饭是最有价值的接力。认领的 owner 常量是 `OWNER = "P2"`（AI 控制的是 P2）。

## 4. 代码风格

- **注释/文档用中文**，标识符用英文。注释强调「为什么」（尤其大量 `实机复现`/`实测 N 帧` 的踩坑记录与 spec § 引用），不要写解释「做了什么」的噪声注释。
- **类型注解用 `# type: (...) -> ...` 注释形式**（非 PEP 526 内联注解），全项目统一。
- 谓词一律**无副作用**：`predicates.py` 的函数只读入参 dict/List，不依赖 `World` 类（这样单测不必造真游戏帧）；`World` 是只读视图。
- 纯函数优先、controller 依赖注入：`Hands(ctrl)` 通过构造注入 controller，测试传记录调用的假 controller，不碰 Quartz。
- 键位沿袭 legacy 实机验证的 P2 分屏键位（`ai/output/controller.py` 的 `KEY` 表）：方向键移动、右 Shift 拾取、`/` 切菜/投掷/喷火，**右 Option（keycode 61）冲刺**。

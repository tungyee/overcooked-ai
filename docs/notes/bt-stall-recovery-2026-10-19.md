# 最新主线行为树卡住分析与修复

基线：[b7a1f82fe14cc9430045a964e02f396075273111](https://github.com/tungyee/overcooked-ai-dev/commit/b7a1f82fe14cc9430045a964e02f396075273111)，2026-10-19 的「多网格导航 + 句柄对齐 + 插件协议升级」。网页返回 404，但本机 Git 成功取得代码；以下针对实际克隆的最新主线，非原来的本地旧版。

修复目录 `D:/overcooked2_agent/overcooked-ai-dev-upstream`，分支 `fix/bt-stall-recovery`。使用 Code、brainstorming、writing-plans / executing-plans 的复现、实现和验证流程，计划见 `docs/plans/2026-10-19-bt-stall-recovery.md`。未启动游戏控制器、未调用模型、未更换游戏 DLL、未推送远程。

## 原因与修改

原版 **1209 passed / 85 skipped**，但缺少跨任务交替失败、原地工作期间起火、平台断开再连通和切关隔离的组合测试。这些是控制流与生命周期缺陷，不靠调整打分权重解决。

| 问题 | 原行为及后果 | 修复 |
| --- | --- | --- |
| 交替失败清零 | Agent 只记录上一个 task.key，A/B 交替或同动作换订单便清零，永远攒不到三次失败 | recovery.py 按 `(TYPE, resources, item)` 独立计数，排除订单号。200 拍窗口中三次失败触发退避；成功仅清当前操作的历史 |
| 冷却误伤其他动作 | 普通失败与移动失败把裸资源拉黑 200 拍，一次下料失败可能阻止同灶归锅、回收或清理 | 增加 `scratch.abandoned_actions[(resource, action)]`，读写一致。保留显式 AbandonTarget 的旧资源级表及 NO_ABANDON 紧急豁免 |
| 资源认领残留 | 失败或被抢占后，旧认领仍等对账/TTL 才释放，供需分析可能把它当成仍在加工 | 失败立即释放本任务认领；切换时替换 P2 当前认领集合，保留其他 owner 的认领 |
| 原地守卫锁模式 | 同一格达到阈值就返回旧模式，正常切菜/洗碗也触发；起火进不了灭火，火灭后也可能退不出 | 原地仅作诊断，继续评估降级和实时火情；移动恢复仍由原语/超时处理 |
| 原料存在但不可达 | ingredient_available 只查存在；隔在断开平台上的散落料会压住后面的可达箱子；不可达高层任务仍参加分层选择 | reachability.py 根据当前地形、薄墙边和平台限制建立距离场；跳过不可达来源继续寻找；分层前核对绑定资源、盘与必要目的地 |
| 局部重算开放禁入平台 | 主距离场禁止登上移动篮子，Approach 因坏站位局部重算时却漏带篮子掩码 | ctx.navigation_blocked 传递到局部重算，不能通过脱困重算开放禁入格 |
| 切关残留状态 | 旧 doomed_orders、冷却、平台位置和行为树留到新关 | level / 可选 session_id、scene_epoch 改变，或退出关卡再进入时，清理执行、输入、黑板、失败历史与收益日志基线；暂停不当成换关 |

## 动态地图与查询契约

- 保留上游多网格和交互句柄语义。路径优先使用目标世界 x/z 投影，不能直接把篮子 gx/gz 当全局格。
- 每个新 World 根据本帧拓扑重评估。距离场仅在同一快照内按起点、禁入格和软代价复用，不跨帧缓存平台连通性。
- 平台断开→连接→断开会重新判定可达性；临时不可达首先被过滤，不能反复执行失败后才识别。
- 已拿着正确原料时无需回来源，不因来源平台远离而放弃当前物料。
- 多格出餐口和洗碗放置保留现有占地规则；Discard 的逻辑资源 bin 按站点类型解析，不误报为缺少名叫 bin 的物体。
- 缺 Ground/level_bounds 的历史合成帧保留离线兼容，不在无界平面为每次查询无限建场，也不声称其路线已被证明。
- trace 新增 waiting.reason / veto_counts，区分等平台、等路线、恢复冷却、无订单及无可行任务。

## 测试与交付

新增 `tests/test_stall_recovery.py` 与 `tests/test_reachability.py`，共 **25 例**。第一批九例在修复前全部失败；来源不可达负例也复现了旧代码会接受断路原料。保留平台连通、已持物、旧帧兼容等正向用例，避免把所有任务直接否决。

旧 test_arbiter 的「原地优先保持 Normal」断言改为允许实时火情切换；旧恢复测试改读动作级冷却。没有跳过新增失败。

最终完整回归 **1234 passed / 85 skipped（31.21 秒）**；compileall、git diff --check 通过。85 个跳过是原基线已有，主要涉及缺少历史录帧及平台条件；新增 25 例全部实际执行。

```powershell
D:\overcooked2_agent\overcooked-ai-dev-main\overcooked-ai-dev-main\.venv\Scripts\python.exe -m pytest -q
D:\overcooked2_agent\overcooked-ai-dev-main\overcooked-ai-dev-main\.venv\Scripts\python.exe -m compileall -q ai tests
git diff --check
```

交付提供完整修复源码与针对基线提交的 Git 补丁；补丁在匹配版本先 `git apply --check`，再 `git apply`。包不含 .git、虚拟环境、凭证或完整游戏轨迹。最终补丁另在干净基线上验证可应用及定向回归。

## 现场与兼容性边界

原 Windows 项目 `D:/overcooked2_agent/overcooked-ai-dev-main/overcooked-ai-dev-main` 及游戏 DLL 保持原样。最新上游仍采用 macOS 输入后端和新版 terrain/grids 插件协议；本轮补丁不能直接当成旧 Windows 插件升级包。

没有实机出餐验证，不能宣称全关卡通关或不再卡住。真正物理断开的平台仍应等待路线/交接，不伪造地面。全局灭火任务、复杂多层地形、平台主动登乘与最新协议的 Windows 实机集成仍需单独验证。同关重开若完全没有 menu/session/scene 变化信号，也不能仅凭关卡名识别新会话。

# 最新修复版 Windows 实机验证

## 已完成的本机集成

基于最新主线 b7a1f82 及行为树修复，运行目录 `D:/overcooked2_agent/overcooked-ai-dev-upstream`；旧代码目录保留。复用旧版已验证的 Windows 扫描码/完整性级别实现，按新版目录结构迁入 ai/output，保留 macOS 输入后端。

- Windows 输入为方向键、右 Shift 拾取、右 Ctrl 工作；dash 不发送实际事件，避免右 Alt 干扰 1P。
- 输入后端与 Agent 双重检查前台；暂停、失焦、断流、缺少新协议或玩家时松键停止决策。只释放本进程按下的键。
- run_agent 支持 --seconds / --stop-file。启动器使用同目录互斥锁，按 Windows UIPI 完整性级别决定是否需要 UAC；后台窗口隐藏。
- 实际运行要求新 terrain/grids 协议，不能将旧的 0.18.2 数据当成兼容帧。
- 新本机插件版本 0.19.0 / protocol_version=4；保留上游 terrain/grids/occupancy/风区/句柄字段，补 session_id/scene_epoch，让同关重开也能触发状态清理。
- 默认关闭大规模 Discover，没客户端不扫描完整状态；网络写出移到客户端锁外、250ms 超时、只保留最新待发帧，避免旧队列和慢读锁住主线程。diagnostics 发布上一采样耗时与渲染帧时间。

Python 全量 **1249 passed / 85 skipped**，包含 25 项行为树恢复/可达性新增回归和 15 项 Windows ABI/权限/输入/限时守卫测试。引用本机 Unity 程序集编译成功（仅有旧的未使用字段警告）。

## 部署证据

游戏退出状态下已部署并重新启动；BepInEx 日志确认加载 OC2 AI Playmate 0.19.0，监听 127.0.0.1:27411。

部署 DLL SHA256：`BC2BEF2FF3562FDED0638B62640129EF5332321B638D8124083A723F9B7E74C8`。

旧 0.18.2 插件备份 `.runtime/backups/20261019-165850-395/Oc2AIPlugin.dll`；集成前源码备份 `.runtime/source-before-windows-live.zip`。

已收到新关卡帧，并于 17:03:24–17:05:54 完成 150 秒限时控制。菜单不推快照是上游的低负载设计，不等于插件没有连接。

## 150 秒实测结果

场景为 Balloon_1_5_2P，插件 0.19.0 / protocol 4，两名厨师在线。预检 80 个可达格，Pasta、PastaTomato 两个料箱均可达。实际启动器按游戏完整性级别提升运行；到时自动退出，无 Python 异常。

轨迹 `ai/trace-20261019-170324.jsonl` 共 2932 条；决策拍跨度 148.25 秒（启动就绪前约 1.75 秒）。149 秒左右的状态窗口中，关卡计时从 177 到 29 秒。1427 拍有实际动作键意图（排除 Windows 禁用的 dash）；没有按键的拍包含等熟、等待新观测与动作确认，不能直接等同卡死。

| 指标 | 观测结果 |
| --- | --- |
| AI 任务成功终态 | 11 次：下料 3、熟料装盘 5、端离熟锅 2、送餐 1 |
| AI 任务失败终态 | 19 次，其中带导航失败原因 8 次：5 observation_timeout、3 no_progress |
| 全队出餐 | 1 → 5，测试窗口新增 4 份 |
| 全队基础分 / 小费 | 40 / 8 → 200 / 44，合计 48 → 244 |
| 单次连续同目标最长区间 | 首锅取熟料约 19.2 秒，期间经历失败重试，后来完成 |

不能把全队新增 4 份都算成 AI 独立出餐。AI 的最后一次 DeliverPlate 有较强物理证据：t=2855/2860 P2 持盘且 placement 指向 workstation_plate_station，t=2868 P2 空手、全队 deliveries 4→5，P1 同期空手。其余任务数量为行为树终态，可能包含人类协作完成的后置条件。

结论：能实际取料、装盘、送餐，未出现整段测试持续不动；但有明显局部卡住、等待和重试，尚不能视作稳定通关版本。

### 已定位并追加修复

在 t=1154–1156，两名厨师都拿着 `equipment_plate_01`，DeliverPlate 连续立即失败。旧 Guard 只对比 P1 的 held 名称与目标盘模型名；多只盘共用同一模型名，误把 P2 自己的盘视作被 P1 拿走。

新增纯谓词 `P.holds_plate`，优先按绑定盘实例的 `held_by` 判断；只有旧帧缺字段才回退名称。送餐 Guard、送餐取盘前置以及端盘取熟料统一使用。`held_by=null` 明确表示未被持有，不回退名称。补真实采样 fixture 和并发持盘回归。

同时发现 trace 只落散盘，漏记 World.held_plates；已单列保存持盘实例、内容与归属，防止以后复盘缺证据。trace 路径也修回仓库根目录，旧轨迹仍保留原 ai/ 位置。

这些追加 Python 修改在本轮结束后完成；通过离线回归后下次启动即生效，**尚未再次实机控制，不能将本轮结果说成追加修复后的效果**。移动到灶前的 no_progress、短观察超时和洗盘失败仍待进一步实机定位。

最终全量回归 **1254 passed / 85 skipped（43.47 秒）**；其中 5 项新增持盘归属/真实采样/持盘 trace 回归，旧送餐抢盘测试仍通过。compileall、git diff --check 通过。原始多网格样本放专用 ownership 子目录，避免进入要求固定 1102 帧和单网格公式的历史 parity fixture 集合；该样本由专门回归显式读取，没有跳过本次新增验证。启动器锁已可重新取得，轨迹在 17:05:54 后不再增长，确认控制进程已结束；run/finally 负责松键。

### 性能观测

测试期间另做 10 秒只读采样，50 个有效样本：snapshot_ms 中位 46.59 ms、P95 48.88 ms、最大 54.03 ms。2266 个渲染帧中有 50 帧超过 33 ms，平均 4.42 ms、最大 58.31 ms。结果见 `.runtime/live019-performance.json`。

插件采样仍有明显周期性开销，不能声称掉帧已解决。此数据不是开关插件 A/B 对比；未验证全部尖峰都由采样导致。后续应给扫描/状态序列化/导航逐段计时，再缩减主线程反射与全量组件扫描。

原始预检、采样和汇总位于 `.runtime/live019-{preflight-level,during}.ndjson`、`.runtime/live019-{preflight-summary,result,performance}.json`。本轮没有调用模型 API。

## 本次实测步骤

先用 tools/check_windows.py 只读检查 plugin_version、terrain、grids、两名厨师、食材区与距离场；再通过以下启动器控制 P2 150 秒：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_windows.ps1 -Background -Seconds 150
```

停止使用 stop_windows.ps1。Windows 启动器会按需弹出 UAC；游戏离开前台后 AI 释放键，恢复前台后才继续决策。时限包含失焦时间，因此效果评估应另统计有效控制拍数。

测试只给 AI 的任务终态计数，出餐分数来自两人共同游戏，不自动全部归因 AI。若失败，保存真实帧与 trace 后定位，不用离线测试结果代替实机效果。

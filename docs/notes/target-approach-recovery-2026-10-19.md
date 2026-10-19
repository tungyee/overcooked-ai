# 目标附近寻路卡住：定位与修订

本轮修改 `D:/overcooked2_agent/overcooked-ai-dev-upstream` 的 Python 行为树与移动闭环，兼容已部署的 0.19.0 / protocol 4 插件，无 DLL 修改。保留原有未提交更改，修改前 ai 源码备份 `.runtime/source-before-target-approach-20261019-172026.zip`。

## 轨迹证据

分析最新 `trace-20261019-171224.jsonl`（Sushi_1_2_2P，4096 条）以及上一轮 `ai/trace-20261019-170324.jsonl`（Balloon_1_5_2P）。最新轨迹执行终态为 24 成功、11 失败；motion.observe 1477 拍，约占整份轨迹 36.1%，aligning 570 拍，约 13.9%。这些比例有交集，观察也包含必要的惯性等待，不能简单全部记作卡死。

| 问题 | 证据 / 原因 | 本轮修订 |
| --- | --- | --- |
| 出餐口旁转向错误 | 1-2 t=2777：P2 位于主格 (3,2)，出餐口真实占地 (2,3)、(3,3)；Face 追单一锚点 (2,3)，向左走而不是向眼前的上方转向 | 多格站点朝向取最近占地部分 |
| 对齐后朝向失效 | Face 后 AlignToCell 会横向移动，移动也改变朝向；旧流程对齐完直接 Tap，未复查朝向 | InteractWith 与 EnsureHandsFree 增加对齐后的 Face，再由 Tap 校验拾放句柄 |
| 正确句柄仍等绝对静止 | 1-2 t=1208–1228：observe 期间 P2 横向持续移动，仍在交互格、pickup 正指向 Seaweed 料箱，却 observation_timeout | 松键后至少等新观测，交互格和句柄均有效时可交给后续转向/对齐；滑出交互格则提前恢复接近，不等满 1 秒 |
| 贴墙无法到格心 | 大量站点边缘 x/z 被碰撞限制；强求格心会每次走到约 1.6 秒 no_progress 才尝试交互 | 至少尝试一次收紧；连续两个新观测无收紧且句柄有效时放行，仍依赖 Confirm 验证真实结果，不直接谎报任务成功 |
| 重建后回到同一失败位置 | Face、Align、Tap、Confirm 失败常整棵 task 重建；单次对齐以外的失败没有可靠的目标-站位记录 | 站点交互内部最多换位重试两次；等待下一新帧；最后失败站位也记录。只有该目标不能在此交互，其他任务和通行仍可使用该格 |
| 移动台面的落点过期 | EnsureHandsFree 锁定站点后保留旧 dict 世界坐标，目标随平台移动时未刷新 | 按 (grid,n) 从本帧站点表刷新；未按键前失位则重跑 Approach；按键后保持 Confirm，不因目标消失或被推走再按一次 toggle |
| 游戏已结束却仍规划 | 最新轨迹 t=3954、4021、4061 的 3 次 no_progress 全部 timer.left=0 | 有效有限计时器归零即 round_finished，松键、清执行树并暂停规划；未知/无时限计时不当成归零 |
| 薄墙约束在回溯时丢失 | distance_field 检查 edge_blocked，trace_back 却仅按代价等式找前驱；同代价邻居可能隔着墙 | 回溯与建场共用边约束。已用构造障碍图复现；本轮抽查的 1-2 帧未发现实际跨墙路径，不把它说成该轨迹已证实的主因 |

## 恢复的边界

- 局部重试需要真实 frame_seq。等待新帧期间释放移动，旧帧不得消耗换位预算或触发第二次拾放。
- 按键后只在后置确认超时、手持仍是原物、目标仍存在时换位；手持意外改变就交回 Planner。被拿走的物品短暂消失时继续等待确认，不重新接近或反复按 toggle。
- 多次失败最后返回 FAILURE，由既有操作退避和任务规划处理；不把局部恢复做成无限 RUNNING。
- 仍保留前台、断流、暂停、平台隔离、应急任务 NO_ABANDON、动作确认和 Windows 禁用右 Alt 的限制。
- trace 现在记录 interaction.phase、attempt、interaction_failure、interaction_recovery、alignment；state_dump 补 session/protocol、level_bounds、belts、wind_zones，后续可区分到位等待、转向、对齐、确认与换位。

## 验证

新增 `tests/test_target_approach_recovery.py`：50 项通过，其中 36 组在 5 Hz 观测 / 20 Hz 输入下组合不同初始距离、速度和惯性，验证到位后松键且不会继续滑出目标格。其余覆盖真实 1-2 出餐口/横向推动/归零帧、真实 1-5 冻结碰撞姿态的有限恢复、换位预算、旧帧防重复按键、按后等待、移动台面刷新、误持物与坏站位仍可通行。

真实轨迹精简样本在 `tests/fixtures/motion/{sushi12,balloon15}-docking-20261019.ndjson.gz`。这些样本从 trace.state 重建，缺少原轨迹未保存的风区等字段；不冒充完整原始插件帧。测试从专用 motion 子目录显式读取，不干扰旧 1102 帧的单网格 parity 集。

最终全量回归 **1304 passed / 85 skipped（31.39 秒）**；新增 50 项均执行通过，85 项历史跳过数量未增加。compileall 与 git diff --check 的退出码均为 0。没有用离线结果替代新的实机结论。

## 使用与局限

停止旧 AI 后从本目录重新运行即可使用 Python 修订；无需替换 DLL 或重启游戏：

```powershell
cd D:\overcooked2_agent\overcooked-ai-dev-upstream
.\start_windows.ps1 -Background -Seconds 150
```

停止命令 `./stop_windows.ps1`。本轮修改期间游戏未运行，没有重新注入键盘或新增实机出餐数据。离线物理模拟不能替代复杂地图上的实测，也不能据此保证各关稳定通关。插件约 47ms 的采样开销不在本轮修改范围内，周期性停顿仍须独立优化。

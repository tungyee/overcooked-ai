# 目标附近寻路与交互修订 Implementation Plan

**Goal:** 修复接近站点后观察空转、对齐被墙卡住、面向失效与失败重回同位，维持多网格/移动平台约束。

**Architecture:** 保留距离场和行为树。路径回溯与建场共享边约束；交互在本地做有上限的换位恢复，按键后的 Confirm 阶段不被目标消失或位置漂移打断。未按键时失去站位重走 Approach，对齐之后重新校验朝向。游戏计时已归零时停止控制。

**Tech Stack:** Python、pytest、5 Hz 游戏观测 / 20 Hz 控制；兼容本机 0.19.0 插件，不改 DLL。

对比方案：仅加超时会增加等待；替换整套寻路影响面过大。选择保留架构、补齐闭环反馈与恢复，并用真实帧和带物理模拟的回归验证。

## 任务与验证

1. 保存修改前源码与轨迹统计。提取 1-2 出餐口/归零、1-5 靠灶卡住样本至 tests/fixtures/motion，避免进入历史单网格 fixture 集。
2. tests/test_target_approach_recovery.py 先复现：薄墙路径回溯不能走被挡边、多格站点朝向、格心受阻且句柄正确应有限尝试、观察期间滑出目标应恢复、交互失位重寻路/失败换位/按后等待不重复 toggle、移动站点刷新、归零松键。
3. 修改 ai/space/navigation.py、ai/primitives/{move,motion,align,interact}.py。换位失败只记目标-站位，不全局封死通道；恢复最多两次，必须消费新观测，不能重复使用旧帧重按。持续失败返回 Planner，不无限运行。
4. 修改 ai/agent.py 的计时结束守卫，trace 提供执行阶段、失败/恢复原因；完善 state_dump 的地图上下文，便于后续复盘。
5. 针对回归通过后执行全量 pytest、compileall、diff --check；记录实际结果与局限。当前游戏未运行，离线验证不能声称实机性能提升或完成通关。Python 修改下次启动生效，无需替换 DLL。

用户已经明确要求分析并修改，直接在现有修复分支执行，不提交或覆盖已有未提交更改，不额外要求实施批准。

执行完成：50 项新增回归通过；最终全量 1304 passed / 85 skipped，compileall 与 diff --check 通过。实现与证据见 `docs/notes/target-approach-recovery-2026-10-19.md`。本轮无新实控，插件/DLL 未改变。

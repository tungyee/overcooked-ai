# BT Stall Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复最新 b7a1f82 中交替失败无限重试、资源冷却误伤、原地守卫挡住火情及动态来源不可达仍入选的问题。

**Architecture:** 保留现有多网格、分层规划与行为树。把失败计数/退避收敛到动作粒度的有界恢复模块；规划与执行共享当前帧的可达性查询，不能用原料存在替代可达。生命周期切换清除旧执行/恢复状态，原地观测仅作为诊断，不能屏蔽火情。

**Tech Stack:** Python 3.9+ 标准库、pytest 7.4.4、现有任务/BT/terrain 协议；本轮不修改或部署 Unity 插件，不注入按键。

---

使用 Code、brainstorming、writing-plans 流程。用户已授权分析与修复，直接按步骤执行，无需额外设计批准或新任务。原 Windows 本地目录保持原样；工作目录是单独克隆的 D:/overcooked2_agent/overcooked-ai-dev-upstream，分支 fix/bt-stall-recovery，基线 1209 passed / 85 skipped。不上推远程。

### Task 1: 可复现的恢复与火情失败用例

**Files:** Create tests/test_stall_recovery.py; Modify tests/test_arbiter.py。

先测试 A/B 交替失败、不同订单同动作、其他任务成功不抹掉失败历史、冷却到期、同资源不同动作及紧急任务豁免。原地切菜后起火应进入灭火，而不是保持 Normal；火灭后也必须退出 Firefighting。

Run: `D:/overcooked2_agent/overcooked-ai-dev-main/overcooked-ai-dev-main/.venv/Scripts/python.exe -m pytest tests/test_stall_recovery.py -q`。确认新增失败后再实现。

### Task 2: 动作恢复模块与生命周期

**Files:** Create ai/core/recovery.py; Modify ai/agent.py, ai/primitives/stuck.py, ai/core/planner.py。

每个 (TYPE, resources, item) 独立累计失败；成功仅清本操作，旧记录按 TTL 清理。冷却写 (resource, action)，读侧兼容旧的显式资源级 AbandonTarget。NO_ABANDON 保留。任务失败释放本任务认领，切关/重新进入关卡清掉旧树、黑板与恢复历史；暂停只释放输入并停止旧树，不当作换关。加对应回归。

### Task 3: 实时来源可达性过滤

**Files:** Create ai/space/reachability.py, tests/test_reachability.py; Modify ai/core/planner.py, ai/tasks/base.py, ai/query/recipes.py, ai/agent.py。

在有真实有界地形/边阻挡数据时构造并按当前帧复用距离场，目标采用世界位置投影，保留多格出餐/洗碗站契约。不可达高层目标不能压住可达备菜；原料查找应跳过不可达的板上/散落候选并继续找可达箱子。手中已有正确物料不需要回到来源，不能被来源断开否决。移动平台每帧重评估，不把临时不可达写成永久资源故障。缺历史拓扑时保留离线兼容，但 trace 不得声称已证实可达。

### Task 4: 模式守卫与诊断

**Files:** Modify ai/core/arbiter.py, ai/agent.py; Extend tests/test_agent_lifecycle.py, tests/test_stall_recovery.py。

原地状态仍记录，但继续评估降级和火情；不得早返回旧模式导致永久不灭火或火灭后不退出。无可行任务的 trace 区分冷却、移动平台和不可达资源，避免 idle 无解释。

### Task 5: 全量验证与交付

运行新增定向回归，再完整 pytest、compileall、git diff --check。检查修复对已有平台/对齐/分层/超时测试的影响，仅因明确契约变更更新旧断言。报告记录基线、复现与证据、修改位置、验证数量及实机限制，生成可移植 Git patch；不修改旧 Windows 安装、不启动游戏控制，不把离线回归当通关证明。

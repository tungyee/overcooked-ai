# -*- coding: utf-8 -*-
"""卡住检测（原 Arbiter 模式仲裁已下沉 planner）。

原 Arbiter 每拍决定 Normal / Firefighting 模式；火情现已由 planner 的
L_FIRE（Extinguish 候选）统一处理，不再有独立 Firefighting 模式。中断 task
连续失败降级（写 blacklisted_resources 的拉黑）是只写不读的死状态，一并删除。

这里只保留卡住检测 `_is_stuck`：维护 `bb.scratch.same_count` 计数器，供
`agent._stuck_info` 读走做 trace 卡住观测。真正的脱困在别处（HoldUntil 位置
自愈 + Agent 连续失败计数写 scratch.abandoned + 执行层 motion.stalled）。

Spec: §G.1 卡住观测
"""
from ai.query import predicates

# 连续多少拍在同一格算卡住。插件 5Hz（一帧 4 拍），3 拍 < 一帧，切菜/
# 洗碗/盛盘站定一帧就报 stuck（trace-20261111-001945 里 125 次误报）。
# 20 拍 = 1 秒；原地动作另由 _STATIONARY_TASK_TYPES 直接排除。
STUCK_SAME_CELL_THRESHOLD = 20

# 这些 task 的完成本来就要站定，不能用「同格」判卡住；真正走不动由执行层
# motion.stalled 兜底（trace._stuck_info 的第二分支）。
_STATIONARY_TASK_TYPES = frozenset({
    "Chop", "WashPlate", "FillCooker", "TakeCookedToPlate",
    "PlaceCleanPlate", "MergePlates", "ResetPlate",
})


def _is_stuck(world, bb):
    # type: (object, object) -> bool
    """检测 AI 是否卡住（连续多拍在同一格未推进）。

    有副作用：推进 `bb.scratch.same_count`（StuckScratch）。agent 每拍调一次
    维护这个计数，`agent._stuck_info` 读它写 trace；这里返回的布尔值只作
    观测用，不再决定模式。
    """
    from ai.state.blackboard import StuckScratch

    # 取 AI 厨师（Player 2）
    ai_chef = predicates.chef_by_name(world.chefs, "Player 2")
    if not ai_chef:
        return False

    # 当前格子
    current_cell = predicates.chef_cell(ai_chef, world.to_grid)
    if not current_cell:
        return False

    # 如果 scratch 不是 StuckScratch，初始化
    if not isinstance(bb.scratch, StuckScratch):
        bb.scratch = StuckScratch()
        bb.scratch.last_cell = current_cell
        bb.scratch.same_count = 1
        return False

    scratch = bb.scratch

    # 原地动作（切菜/洗碗/盛盘）本来就不动，不能算卡住；清零并刷新格，
    # 换到移动 task 时从新格重新计数。走不动由执行层 motion.stalled 兜底。
    active = getattr(getattr(bb, "commitment", None), "active_task", None)
    if getattr(active, "TYPE", None) in _STATIONARY_TASK_TYPES:
        scratch.last_cell = current_cell
        scratch.same_count = 0
        return False

    # 先检查当前是否已经卡住
    is_currently_stuck = scratch.same_count >= STUCK_SAME_CELL_THRESHOLD

    # 检查是否在同一格
    if scratch.last_cell == current_cell:
        # 如果还没达到阈值，继续累加
        if not is_currently_stuck:
            scratch.same_count += 1
    else:
        # 移动了，重置计数
        scratch.last_cell = current_cell
        scratch.same_count = 1
        is_currently_stuck = False

    # 达到阈值判定卡住
    return is_currently_stuck

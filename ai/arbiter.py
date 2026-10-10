# -*- coding: utf-8 -*-
"""模式仲裁层（Arbiter）。

职责：
  每拍第一个跑，决定把控制权交给哪个模式。不执行任何动作。

与 oc/ai/ 的差异：
  - 旧实现：模式切换逻辑混在 agent.py 和 _build_tree() 的闭包副作用里。
  - 新实现：独立的纯函数（world, bb）→ mode，三行模式表 + Stuck 跨模式前置守卫。

接口契约（Interface Contract）：
  Arbiter 读取但不写入以下字段，调用方（Executor）负责维护：
  - bb.mode: 调用方必须将返回的 mode 写入 bb.mode
  - commitment.interrupt_failure_count: Executor 在 task FAILURE 时递增
  - commitment.target_resource_id: Executor 在启动 task 时设置目标资源 ID

Spec: §5 Arbiter
"""
from typing import Optional, Tuple

from ai import predicates

# 常量
INTERRUPT_FAILURE_THRESHOLD = 3  # 中断 task 连续失败 N 次强制退回 Normal
BLACKLIST_TICKS = 100  # 拉黑目标的时长（拍数）
STUCK_SAME_CELL_THRESHOLD = 3  # 连续多少拍在同一格算卡住
HANDLING_DISTANCE = 2.0  # P1 在灶附近多少距离算"正在处理"


def select_mode(world, bb):
    # type: (object, object) -> Tuple[str, str]
    """选择当前拍的模式。

    返回: (mode, reason)
    reason 用于日志，例 "cooker#2 onFire" / "no fire"
    """
    # ① Stuck 前置守卫（跨模式生效，先于一切检查）
    if _is_stuck(world, bb):
        return _handle_stuck(world, bb)

    # ② 中断 task 连续失败降级
    if _should_degrade_from_failure(bb):
        return _handle_failure_degradation(bb)

    # ③ 按优先级表选模式
    # 优先级 1: Firefighting
    fire_reason = _should_enter_firefighting(world, bb)
    if fire_reason:
        return "Firefighting", fire_reason

    # 优先级 2: Assigned（对话层指派，本计划为空壳）
    if bb.intents and bb.intents.assignments:
        return "Assigned", "user assignment"

    # 优先级 3: Normal（默认）
    return "Normal", "default"


def _should_degrade_from_failure(bb):
    # type: (object) -> bool
    """检查是否应该因连续失败而降级。"""
    if bb.mode == "Normal":
        return False
    c = bb.commitment
    if not c:
        return False
    count = getattr(c, "interrupt_failure_count", 0)
    return count >= INTERRUPT_FAILURE_THRESHOLD


def _handle_failure_degradation(bb):
    # type: (object) -> Tuple[str, str]
    """处理连续失败降级：退回 Normal，拉黑目标。"""
    c = bb.commitment
    target = getattr(c, "target_resource_id", None)

    # 拉黑目标
    if target:
        bb.blacklisted_resources[target] = bb.tick + BLACKLIST_TICKS

    # 重置失败计数
    c.interrupt_failure_count = 0

    return "Normal", "interrupt task failure degradation"


def _is_stuck(world, bb):
    # type: (object, object) -> bool
    """检测 AI 是否卡住（连续多拍在同一格未推进）。"""
    from ai.blackboard import StuckScratch

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
        from ai.blackboard import StuckScratch
        bb.scratch = StuckScratch()
        bb.scratch.last_cell = current_cell
        bb.scratch.same_count = 1
        return False

    scratch = bb.scratch

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


def _handle_stuck(world, bb):
    # type: (object, object) -> Tuple[str, str]
    """处理卡住：保留原模式，由 Executor 执行脱困原语。"""
    from ai.blackboard import StuckScratch

    # 确保 scratch 是 StuckScratch
    if not isinstance(bb.scratch, StuckScratch):
        bb.scratch = StuckScratch()

    # 保留原模式
    current_mode = getattr(bb, "mode", "Normal")
    return current_mode, "stuck detected"


def _should_enter_firefighting(world, bb):
    # type: (object, object) -> Optional[str]
    """检查是否应进入 Firefighting。

    返回 None（不进）或进入原因字符串（如 "cooker#2 onFire"）。
    """
    # 枚举所有着火的灶
    on_fire_cookers = []
    for cooker in world.cookers:
        if cooker.get("onFire"):
            on_fire_cookers.append(cooker)

    if not on_fire_cookers:
        return None

    # 检查是否有可获得的灭火器
    available_ext = _has_available_extinguisher(world, bb)
    if not available_ext:
        return None

    # 检查是否有"未被处理"的着火灶
    p1_chef = predicates.chef_by_name(world.chefs, "Player 1")

    for cooker in on_fire_cookers:
        if _is_fire_unhandled(cooker, p1_chef, world):
            cooker_name = cooker.get("n", "unknown")
            return "cooker {} onFire".format(cooker_name)

    return None


def _has_available_extinguisher(world, bb):
    # type: (object, object) -> bool
    """检查是否有 AI 可获得的灭火器。

    可获得 = held 为 null 或 "Player 2"。
    特殊规则：P1 持唯一灭火器时不进 Firefighting（spec §5.3）。

    优先从 p1_model 读取 P1 持有状态（Plan 4 Task 2）。
    """
    total_count = 0
    available_count = 0
    p1_holds_count = 0

    # 先从 p1_model 检查 P1 是否持灭火器
    p1_holds_extinguisher = False
    if bb.p1_model and bb.p1_model.holding:
        if "extinguisher" in bb.p1_model.holding.lower():
            p1_holds_extinguisher = True

    for ext in world.extinguishers:
        total_count += 1
        held = ext.get("held")
        if held is None or held == "Player 2":
            available_count += 1
        elif held == "Player 1":
            p1_holds_count += 1

    # P1 持唯一灭火器 → 不进（优先使用 p1_model 判断）
    if p1_holds_extinguisher and total_count == 1:
        return False
    if total_count == 1 and p1_holds_count == 1:
        return False

    return available_count > 0


def _is_fire_unhandled(cooker, p1_chef, world):
    # type: (dict, object, object) -> bool
    """着火灶是否"未被处理"。

    未被处理 = P1 不在该灶旁 且 P1 不持该灶对应的灭火器。
    """
    if not p1_chef:
        return True

    # 检查 P1 是否在灶旁
    cooker_pos = (cooker.get("x"), cooker.get("z"))
    p1_pos = (p1_chef.get("x"), p1_chef.get("z"))

    if None not in cooker_pos and None not in p1_pos:
        dx = cooker_pos[0] - p1_pos[0]
        dz = cooker_pos[1] - p1_pos[1]
        distance = (dx * dx + dz * dz) ** 0.5
        if distance <= HANDLING_DISTANCE:
            return False

    # 检查 P1 是否持灭火器
    p1_held = predicates.held_name(p1_chef)
    if "extinguisher" in p1_held:
        return False

    return True


def on_mode_transition(old_mode, new_mode, bb):
    # type: (str, str, object) -> None
    """模式转移时的清理：清空 scratch。

    注：halt 树由 Executor 负责，此处只清 scratch。
    """
    if old_mode != new_mode:
        bb.scratch = None

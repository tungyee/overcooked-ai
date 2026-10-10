# -*- coding: utf-8 -*-
"""P1Model：人类意图推断。

职责：
  推断 P1 在做什么，目的是避开撞车、接管下游。

三层输出（spec §6.7）：
  1. 事实：holding（手持何物），置信度 1.0
  2. 短程意图：p1_active_task（推断的 task），置信度 0~1
  3. 显式告知：intents.assignments（对话层，本计划为空）

Spec: §6.7 P1Model
"""
from typing import Optional, Dict, Tuple

# 常量
CONFIDENCE_DECAY = 0.95  # 每拍无新证据时的衰减系数
CONFIDENCE_THRESHOLD = 0.3  # 低于此值清空推断
MOVING_CONFIDENCE = 0.7  # 手持 + 移动推断的置信度
INTERACTING_CONFIDENCE = 1.0  # 正在交互的置信度
P1_SOFT_COST = 3.0  # P1 周边格子的额外代价
P1_RADIUS = 2  # 曼哈顿距离


def update(world, bb):
    # type: (object, object) -> None
    """更新 P1 推断。

    每拍调用，更新 bb.p1_model。
    """
    p1_chef = _find_p1_chef(world)
    if not p1_chef:
        return

    # ① 事实层：holding
    bb.p1_model.holding = p1_chef.get("held")

    # ② 短程意图推断
    new_task, new_confidence = _infer_short_term_intent(p1_chef, world, bb)

    if new_confidence > 0:
        # 有新证据
        bb.p1_model.p1_active_task = new_task
        bb.p1_model.confidence = new_confidence
        bb.p1_model.last_evidence_tick = world.tick
    else:
        # 无新证据，衰减
        bb.p1_model.confidence *= CONFIDENCE_DECAY
        if bb.p1_model.confidence < CONFIDENCE_THRESHOLD:
            bb.p1_model.p1_active_task = None
            bb.p1_model.confidence = 0.0


def _find_p1_chef(world):
    # type: (object) -> Optional[Dict]
    """找到 P1 厨师。"""
    for chef in world.chefs:
        if chef.get("name") == "Player 1":
            return chef
    return None


def _infer_short_term_intent(p1_chef, world, bb):
    # type: (Dict, object, object) -> Tuple[Optional[str], float]
    """推断 P1 的短程意图。

    返回: (p1_active_task, confidence)
    """
    # ① 正在交互某站点 → confidence = 1.0
    interactions = p1_chef.get("interactions", {})
    if interactions:
        station_id = interactions.get("station")
        if station_id:
            # 推断正在交互该站点
            task = _infer_task_from_interaction(station_id, p1_chef, world)
            return task, INTERACTING_CONFIDENCE

    # ② 手持 + 移动中 → confidence = 0.7
    holding = p1_chef.get("held")
    if holding and _is_p1_moving(p1_chef, bb):
        # 推断正在移动
        task = _infer_task_from_holding(holding, p1_chef, world)
        return task, MOVING_CONFIDENCE

    # ③ 其他情况：不推断
    return None, 0.0


def _infer_task_from_interaction(station_id, p1_chef, world):
    # type: (str, Dict, object) -> Optional[str]
    """从交互站点推断 task。

    例如：P1 在板上 → 可能是 FetchToBoard 或 Chop
    简化：返回通用描述 "interacting_with_<station>"
    """
    return "interacting_with_{}".format(station_id)


def _is_p1_moving(p1_chef, bb):
    # type: (Dict, object) -> bool
    """判断 P1 是否在移动。

    通过比较当前位置与上一拍位置。
    """
    # 需要在 bb.p1_model 中记录上一拍位置
    current_pos = (p1_chef.get("x"), p1_chef.get("z"))

    # 读取上一拍位置
    last_pos = getattr(bb.p1_model, "last_position", None)

    # 更新位置
    bb.p1_model.last_position = current_pos

    # 首次调用，无法判断移动
    if last_pos is None:
        return False

    # 比较位置
    if current_pos[0] is None or current_pos[1] is None:
        return False
    if last_pos[0] is None or last_pos[1] is None:
        return False

    dx = abs(current_pos[0] - last_pos[0])
    dz = abs(current_pos[1] - last_pos[1])

    # 移动阈值：格子大小约 1.0，移动超过 0.1 算移动
    return dx > 0.1 or dz > 0.1


def _infer_task_from_holding(holding, p1_chef, world):
    # type: (str, Dict, object) -> Optional[str]
    """从手持物推断 task。

    例如：持 Cucumber → 可能要送去某处
    简化：返回通用描述 "moving_with_<item>"
    """
    return "moving_with_{}".format(holding)


def p1_soft_cost_map(world, p1_model):
    # type: (object, object) -> Dict[Tuple[int, int], float]
    """生成 P1 周边的软代价图。

    P1 周边格子（曼哈顿距离 ≤2）加通行代价。

    返回: {(gx, gz): extra_cost}
    """
    # 只有 P1 在手持物品时才产生软代价（表示 P1 正在执行任务）
    if not p1_model or not p1_model.holding:
        return {}

    p1_chef = _find_p1_chef(world)
    if not p1_chef:
        return {}

    p1_x = p1_chef.get("x")
    p1_z = p1_chef.get("z")

    if p1_x is None or p1_z is None:
        return {}

    p1_gx, p1_gz = world.mapping.to_grid(p1_x, p1_z)

    soft_cost = {}

    for dx in range(-P1_RADIUS, P1_RADIUS + 1):
        for dz in range(-P1_RADIUS, P1_RADIUS + 1):
            if abs(dx) + abs(dz) <= P1_RADIUS:
                neighbor = (p1_gx + dx, p1_gz + dz)
                soft_cost[neighbor] = P1_SOFT_COST

    return soft_cost

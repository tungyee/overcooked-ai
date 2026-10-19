# -*- coding: utf-8 -*-
"""P1Model：人类玩家（P1）的少量可用事实。

只保留实机真正可用的两层信息：
  1. 事实层：holding（P1 手持何物）。arbiter 用它判断「P1 拿灭火器」时不进
     Firefighting；soft cost 用它判断 P1 是否在干活、要不要避让。
  2. 站位软代价：p1_soft_cost_map 给 P1 周边格子加通行代价，让 P2 绕开 P1。

短程意图推断（p1_active_task / confidence）已删：插件从不发
`interactions.station`（只发 interactable/pickup/placement/grid 等），推断链
数据契约对不上；下游的 P1 冲突惩罚与 relay 加成在实机恒为 0，属死代码。
"""
from typing import Dict, Optional, Tuple

# 常量
P1_SOFT_COST = 3.0  # P1 周边格子的额外代价
P1_RADIUS = 2  # 曼哈顿距离


def update(world, bb):
    # type: (object, object) -> None
    """每拍把 P1 手持物写进黑板（事实层）。"""
    p1_chef = _find_p1_chef(world)
    if not p1_chef:
        return
    bb.p1_model.holding = p1_chef.get("held")


def _find_p1_chef(world):
    # type: (object) -> Optional[Dict]
    """找到 P1 厨师。"""
    for chef in world.chefs:
        if chef.get("name") == "Player 1":
            return chef
    return None


def p1_soft_cost_map(world, p1_model):
    # type: (object, object) -> Dict[Tuple[int, int], float]
    """生成 P1 周边的软代价图。

    P1 周边格子（曼哈顿距离 ≤2）加通行代价。只有 P1 在手持物品时才产生
    软代价（表示 P1 正在执行任务）。

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

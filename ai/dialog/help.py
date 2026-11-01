# -*- coding: utf-8 -*-
"""P1 求助触发：P1/P2 成本比较、订单 DAG 前沿扫描与候选排名（spec §5.1–§5.2）。

本模块由主循环（`agent._service_dialog`）调用，只读 `World` / `Blackboard`，
不写；产出的是「P1 做哪一步明显更划算」的判定，最终经 `help_request` 事件
交给对话 worker 措辞。与投掷机制的区别：这里只算「谁去更划算」，真正发令
（扔/取/切/煮/盛/装）仍是 P1（人类）或 P2 自己的规划，不由本模块控制。
"""
from typing import Any, Dict, List, Optional

from ai.space import reachability

# P1 比 P2 省多少步才值得开口求助（≈0.3 秒/步，8 步 ≈ 2.4 秒 ≈ 半张图）。
HELP_THRESHOLD_STEPS = 8


def cost_to(world, bb, name, source):
    # type: (Any, Any, str, Dict) -> Optional[int]
    """`name` 厨师走到 `source` 旁交互的最短步数；够不着返回 None。

    口径与 planner 的 `task_veto` 完全一致：`field_for(world, bb, name)` +
    `_interact_goals`，只把 chef 换成 `name`。source 是站点/物品 dict。
    """
    ctx = reachability.context(world, bb, name)
    field = ctx.get("field")
    if not field:
        return None
    goals = reachability._interact_goals(ctx, source)
    if goals is True:                 # 该厨师正拿着这件东西：0 步
        return 0
    vals = [field[g] for g in goals if g in field]
    return min(vals) if vals else None


def p1_saves_enough(p1_cost, p2_cost, threshold=HELP_THRESHOLD_STEPS):
    # type: (Optional[int], Optional[int], int) -> bool
    """P1 是否明显更划算（spec §5.1 统一阈值）。

    P2 够不着（None）→ 必喊（结构性，分割图）；P1 够不着（None）→ 不喊；
    否则 P2 步数 − P1 步数 ≥ threshold 才喊。
    """
    if p1_cost is None:
        return False
    if p2_cost is None:
        return True
    return p2_cost - p1_cost >= threshold

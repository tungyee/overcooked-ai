# -*- coding: utf-8 -*-
"""P1/P2 成本比较与订单 DAG 前沿扫描（spec §5.1–§5.2）。

只读 `World` / `Blackboard`，不写。`cost_to` 现在是分工校验和动态复核的
物理判据；`frontier_steps` / `p1_saves_enough` 保留给后续协作逻辑，不再
产生 help_request 账本条目。
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ai.dialog.schema import RESCUE_TYPES
from ai.query import predicates as P
from ai.query import recipe_graph
from ai.revenue import eta as ETA
from ai.space import reachability, seams

# 料源定位时故意不过滤 P2 可达性：p1/p2 谁够得着由 cost_to 另行比较。
_SOURCE_ANY = lambda st: True  # noqa: E731


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


def p1_saves_enough(p1_cost, p2_cost):
    # type: (Optional[int], Optional[int]) -> bool
    """P2 够不着、P1 够得着才求助（结构性，分割图）。

    经济性（P2 够得着但 P1 更省步数）不再作为求助触发条件：那类「你更划算」
    不是紧急事件，还容易对 P1 反复喊（trace-20261103-205951）。
    """
    return p1_cost is not None and p2_cost is None


@dataclass
class FrontierStep(object):
    """订单 DAG 前沿里、仍需做的一步及其料源/站与两侧成本。

    `source` 为 None 表示料源暂时定位不到（料被拿在手里），此时两侧成本
    也都是 None；该步仍算「需要做」，用于 help_request 剪枝而非排名。
    `order_id` 是发射该步的那张订单，用于按订单紧迫度（solo_slack）排名。
    """
    task_type: str
    item: Optional[str]
    source: Optional[Dict]
    p1_cost: Optional[int]
    p2_cost: Optional[int]
    order_id: Optional[str] = None


def _source_for(world, book, task, ctx):
    # type: (Any, Any, Any, Dict) -> Optional[Dict]
    """料源/站（不过滤 P2 可达性）：盛盘取熟料所在灶、下锅反查生料、其余按 item 名定位。"""
    from ai.query import recipes as R
    t = getattr(task, "TYPE", None)
    if t == "TakeCookedToPlate":
        fn = getattr(task, "_source", None)
        if callable(fn):
            s = fn(ctx)
            if s:
                return s.get("target")
        return None
    if t in ("FetchToCooker", "FillCooker"):
        fn = getattr(task, "_raws", None)
        raws = fn(ctx) if callable(fn) else ()
        for raw in raws:
            loc = R.find_ingredient(world, raw, exclude_plate=True,
                                    can_access=_SOURCE_ANY)
            if loc is not None and loc.get("st") is not None:
                return loc.get("st")
        return None
    item = getattr(task, "item", None)
    if not item:
        return None
    loc = R.find_ingredient(world, str(item), exclude_plate=True,
                            can_access=_SOURCE_ANY)
    if loc is None or loc.get("kind") == "held":
        return None
    return loc.get("st")


def _rescue_for_help(task, source):
    # type: (Any, Dict) -> bool
    """该步是否永不求助：救援类/灭火恒跳过；盛盘只在锅已着火时跳过。

    「快糊/已糊（burning）」的盛盘放行进候选——它正是「锅熟了但赶不及去救」
    的求助场景。真着火（onFire）归 P2 自己灭火/救锅，不麻烦 P1。
    """
    t = getattr(task, "TYPE", None)
    if t in RESCUE_TYPES or t == "Extinguish":
        return True
    if t == "TakeCookedToPlate":
        return bool((source or {}).get("onFire"))
    return False


def _burn_remaining_ticks(cooker):
    # type: (Optional[Dict]) -> Optional[float]
    """这口灶还有多少拍烧糊；没在煮（空灶/刚点火）或字段缺失返回 None。

    口径与 `ai/core/planner.py::_burn_eta_ticks` 一致：`cooking_progress` 单位≈秒，
    糊点 = 2 × `cooking_time`；`burning`/`onFire` 视为 0 拍。
    """
    if not cooker:
        return None
    if cooker.get("burning") or cooker.get("onFire"):
        return 0.0
    prog = cooker.get("cooking_progress")
    ctime = cooker.get("cooking_time")
    if prog is None or ctime is None:
        return None
    try:
        burn_at = 2.0 * float(ctime)
    except (TypeError, ValueError):
        return None
    return max(0.0, (burn_at - float(prog)) * ETA.TICKS_PER_SECOND)


def _burn_rescue_needed(step):
    # type: (FrontierStep) -> bool
    """盛盘（TakeCookedToPlate）的「赶不及救锅」：锅要糊、P2 救不了、P1 够得着。

    P2 够不着（p2_cost=None，分割图或动态图被切断）也算「赶不及」，只要 P1
    够得着就喊；P2 够得着但走过去的耗时超过剩余烧糊时间也算。P1 也够不着
    则求了没用，不喊。
    """
    if step.task_type != "TakeCookedToPlate" or step.source is None:
        return False
    if step.p1_cost is None:
        return False
    remaining = _burn_remaining_ticks(step.source)
    if remaining is None:
        return False
    if step.p2_cost is None:
        return True
    p2_ticks = step.p2_cost * seams.SEC_PER_STEP * ETA.TICKS_PER_SECOND
    return p2_ticks > remaining


def frontier_steps(world, bb, book, orders, plate_match, cooker_match):
    # type: (Any, Any, Any, List[Dict], Dict, Dict) -> List[FrontierStep]
    """订单 DAG 前沿里「上游就绪但仍未完成」的步骤 + 料源 + P1/P2 成本。

    复用 `recipe_graph.emit`（不做 P2 feasible 过滤）；`task.feasible` 在
    `field=None` 下求值——`can_interact` 对 None 场恒 True，于是
    `ingredient_available` 变成「料是否存在」而非「P2 够得着」，正好保留
    P2 够不着的料源（Rapids 土豆）。料源用 `_source_for`（同样不过滤可达性）。
    料源定位不到（料正被拿在手里）时 `source=None`、两侧成本记 None：步骤
    仍「需要做」，留在前沿供剪枝判定，只是不参与「谁更划算」排名。
    """
    ctx = {
        "w": world,
        "chef": P.chef_by_name(world.chefs, P.P2_NAME),
        "bb": bb,
        "tick": getattr(bb, "tick", 0) or 0,
        "book": book,
        "field": None,
    }
    out = []  # type: List[FrontierStep]
    seen = set()
    for order in (orders or getattr(world, "orders", None) or []):
        for task in recipe_graph.emit(world, bb, book, order,
                                      plate_match, cooker_match):
            if getattr(task, "SHARED", False):
                continue
            if not task.feasible(ctx):
                continue
            if task.done(ctx):
                continue
            item = getattr(task, "item", None)
            if not item:
                continue
            source = _source_for(world, book, task, ctx)
            if _rescue_for_help(task, source):
                continue
            key = (task.TYPE, str(item).lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(FrontierStep(
                task_type=task.TYPE,
                item=str(item).lower(),
                source=source,
                p1_cost=(cost_to(world, bb, P.P1_NAME, source)
                         if source is not None else None),
                p2_cost=(cost_to(world, bb, P.P2_NAME, source)
                         if source is not None else None),
                order_id=getattr(task, "order_id", None),
            ))
    return out

# -*- coding: utf-8 -*-
"""P1 求助触发：P1/P2 成本比较、订单 DAG 前沿扫描与候选排名（spec §5.1–§5.2）。

本模块由主循环（`agent._service_dialog`）调用，只读 `World` / `Blackboard`，
不写；产出的是「P1 做哪一步明显更划算」的判定，最终经 `help_request` 事件
交给对话 worker 措辞。与投掷机制的区别：这里只算「谁去更划算」，真正发令
（扔/取/切/煮/盛/装）仍是 P1（人类）或 P2 自己的规划，不由本模块控制。
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ai.dialog.schema import RESCUE_TYPES, scope_matches
from ai.query import predicates as P
from ai.query import recipe_graph
from ai.revenue import eta as ETA
from ai.space import reachability

# P1 比 P2 省多少步才值得开口求助（≈0.3 秒/步，8 步 ≈ 2.4 秒 ≈ 半张图）。
HELP_THRESHOLD_STEPS = 8

# 料源定位时故意不过滤 P2 可达性：p1/p2 谁更划算由 cost_to 另行比较。
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
    """该步是否永不求助：救援类恒跳过；盛盘在锅快糊/起火时跳过。"""
    t = getattr(task, "TYPE", None)
    if t in RESCUE_TYPES or t == "Extinguish":
        return True
    if t == "TakeCookedToPlate":
        return bool((source or {}).get("burning") or (source or {}).get("onFire"))
    return False


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


def _help_rank_key(step, slack_map=None):
    # type: (FrontierStep, Optional[Dict[Any, float]]) -> tuple
    """求助候选排序键：P2 够不着（blocking）最优先，其次按订单紧迫度
    （solo_slack 小 = 更急 = 先喊），再按 P1 优势步数，最后 (task_type, item)
    稳定兜底。slack 缺失的订单排最后（视为不紧）。"""
    slack_map = slack_map or {}
    blocking = 0 if step.p2_cost is None else 1
    adv = (step.p2_cost - step.p1_cost) if (step.p1_cost is not None
                                            and step.p2_cost is not None) else 0
    slack = slack_map.get(step.order_id)
    if slack is None:
        slack = float("inf")
    return (blocking, slack, -adv, step.task_type, step.item or "")


def top_help_request(world, bb, book, orders, plate_match, cooker_match, ledger):
    # type: (Any, Any, Any, List[Dict], Dict, Dict, Any) -> Optional[FrontierStep]
    """P1 更划算的前沿步骤里价值最高的一个；无则 None。

    排除已存在 help_request duty 的 (task_type, item)；排序：P2 够不着（blocking）
    优先，其次按所属订单的 solo_slack（越紧越先），再按 P1 优势（P2−P1）降序、
    (task_type, item) 稳定兜底。
    """
    steps = frontier_steps(world, bb, book, orders, plate_match, cooker_match)
    # 剪枝依据是「步骤仍需做」，不是「此刻 P1 更划算」：后者随厨师位置/
    # 手持状态每拍波动，用它剪会把求助 duty 反复删掉又重发（一直喊装盘）。
    needed = {(s.task_type, s.item) for s in steps}
    prune = getattr(ledger, "prune_help_requests", None)
    if callable(prune):
        prune(needed)
    requested_scopes = [getattr(d, "scope", None) for d in
                        (getattr(ledger, "duties", None) or ())
                        if getattr(d, "source", "") == "help_request"]
    tick = getattr(bb, "tick", 0) or 0

    def _already_requested(step):
        # 动作级 help duty 的 scope 是 {(type, None)}，节点级是 {(type, item)}；
        # 统一用 scope_matches 判覆盖，避免动作级 scope 被节点级精确匹配漏掉。
        task = type("T", (), {"TYPE": step.task_type, "item": step.item})()
        return any(scope_matches(scope, task) for scope in requested_scopes)

    candidates = [s for s in steps
                  if s.source is not None
                  and p1_saves_enough(s.p1_cost, s.p2_cost)
                  and not _already_requested(s)
                  and not ledger.help_request_recent(s.task_type, s.item, tick)]
    if not candidates:
        return None

    try:
        slack_map = ETA.solo_slack_by_order(world, book, plate_match,
                                            cooker_match, orders=orders)
    except Exception:                       # noqa: BLE001
        # 紧迫度只是增强信号；算不出（空匹配/异常）时退回「都不紧」的旧行为。
        slack_map = {}
    candidates.sort(key=lambda s: _help_rank_key(s, slack_map))
    return candidates[0]

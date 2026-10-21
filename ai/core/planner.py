"""Planner：单订单规划层候选生成（Plan 3 Task 3）。

为给定订单生成所有可能的下一步操作候选。
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from ai.dialog.schema import (NO_BRANCH_TYPES, RESCUE_TYPES,
                              preference_matches_task)
from ai.revenue import cook_supply as CS
from ai.revenue import economy as ECON
from ai.revenue import eta as ETA
from ai.query import predicates as P
from ai.query import recipe_graph
from ai.query import stations as S
from ai import tasks as T
from ai.state.blackboard import Blackboard, Commitment
from ai.query.plate_tokens import parse_plate_detail, parse_pot_contents
from ai.query.recipes import RecipeBook, find_ingredient
from ai.tasks.base import _grid_matches, _unpack_resource_id, cooked_sources
from ai.state.world import World
from ai.space import reachability

# Plan 3 固定估计步数（spec §7.3.4）
EST_FETCH, EST_CHOP, EST_COOK_OPS, EST_SERVE = 10, 20, 15, 5

# Python 主循环拍率（AGENTS.md §2：插件推 5 Hz，主循环 20 Hz，「拍」=50ms）。
# 秒 ↔ 拍换算只许走这个常量，别再散落 `* 20` 的魔数。
TICKS_PER_SECOND = 20


@dataclass
class Candidate:
    """候选：task + 前置条件 + 估计步数（spec §10.4）。"""
    task: Any
    preconditions: List[str]
    estimated_steps: int
    # 对话分支作用域（spec §4.5）：RecipeBook 归一后的生料名。放最后并给
    # 默认值，既有构造点不必改；共享/整单/清理动作没有分支。
    branch: Optional[str] = None


def branch_of_task(task, book):
    # type: (Any, Optional[RecipeBook]) -> Optional[str]
    """候选的分支名：由 RecipeBook 把熟料/切好的名字折回生料（spec §4.5）。

    共享/整单/清理动作（NO_BRANCH_TYPES）与没有 item 的 task 一律 None；
    没有配方视图时也无法归一，返回 None（branch 级偏好不会命中，但不会炸）。
    """
    if getattr(task, "TYPE", None) in NO_BRANCH_TYPES:
        return None
    item = getattr(task, "item", None)
    if not item or book is None:
        return None
    return book.ingredient_branch(item)


def _order_is_abandoned(bb, order_id):
    # type: (Blackboard, Any) -> bool
    """玩家说「别管这单」的硬排除期（spec §4.4）。

    `abandoned_orders` 是 order id → 失效拍号；到期的条目不再过滤（清理归
    主循环，这里只读）。值为 0 / 缺失都表示未放弃。
    """
    if order_id is None:
        return False
    table = getattr(bb, "abandoned_orders", None) or {}
    return table.get(order_id, 0) > getattr(bb, "tick", 0)


def _dialog_live_orders(world, bb):
    # type: (World, Blackboard) -> List[Dict]
    """未被放弃（`bb.abandoned_orders`）的订单。

    放弃是**订单级**的，不只是「不给它发候选」：盘/灶 1:1 匹配与打分 slack
    都必须只看这些单，否则被放弃的单会替活单占掉匹配或队列容量
    （两单同菜 + 一张盘时，活单会拿不到盘）。
    """
    return [o for o in world.orders if not _order_is_abandoned(bb, o.get("id"))]


def generate_candidates(world, bb, order, book, plate_match=None, cooker_match=None):
    # type: (World, Blackboard, Optional[Any], RecipeBook, Optional[Dict[Any, Any]], Optional[Dict[Any, Any]]) -> List[Candidate]
    """为所有订单生成候选任务（Plan 4 多订单模式）。

    order 参数保留用于向后兼容，但在多订单模式下被忽略。
    配方结构由 ai.recipe_graph 编译成任务依赖 DAG，候选从 DAG 的
    「依赖全 done、自身未 done」前沿发射（spec §6.0 的反向展开，递归化）。

    plate_match / cooker_match 是 plan() 每拍算好的 1:1 匹配，供 DAG 落地时
    绑定盘 / 熟料源；与订单无关的清理/回收/待命 task 由 _gen_misc 补上。
    """
    plate_match = plate_match or {}
    cooker_match = cooker_match or {}
    candidates = []  # type: List[Candidate]
    chef = P.chef_by_name(world.chefs, P.P2_NAME)
    hands_empty = P.hands_free(chef)

    # 被放弃的订单整单不发候选（spec §4.4：硬排除，不是打低分）。
    orders = _dialog_live_orders(world, bb)

    # 配方驱动的候选（放盘 + 备菜 + 上盘 + 出餐），逐订单从 DAG 发射
    for ord in orders:
        for task in recipe_graph.emit(world, bb, book, ord,
                                      plate_match, cooker_match):
            pre, est = _CAND_META.get(task.TYPE, ([], EST_FETCH))
            candidates.append(Candidate(task, list(pre), est,
                                        branch=branch_of_task(task, book)))

    # 与订单无关的清理 / 回收 / 待命 task（第 2/5/6/11/12/13/14/15 号）
    candidates.extend(_gen_misc(world, bb, book, chef, hands_empty))

    # 当前承诺的 task 必须留在候选里：手持状态是 ETA 惩罚而非硬否决
    # （spec §6.1 选 (b)），所以"拿着料去装盘"这类 task 会被上面的
    # hands_empty 门挡掉。不重新加入，正在执行的任务就会被 Planner 自己
    # 饿死（每拍候选为空 → select_task 返回 None → AI 停摆）。
    cur = bb.commitment.active_task if bb.commitment else None
    if (getattr(cur, "TYPE", None)
            and not _order_is_abandoned(bb, getattr(cur, "order_id", None))
            and not any(c.task == cur for c in candidates)):
        candidates.append(Candidate(cur, [], _estimate_task_steps(cur),
                                    branch=branch_of_task(cur, book)))

    return candidates


# 每个 task TYPE 的候选元数据：前置条件字符串 + 估计步数（供打分的手部/ETA）。
_CAND_META = {
    "PlaceCleanPlate": (["hands_empty"], EST_FETCH),
    "FetchToBoard": (["hands_empty"], EST_FETCH),
    "Chop": (["chopping_station_free"], EST_CHOP),
    "FetchToPlate": (["plate_ready"], EST_FETCH),
    "FetchToCooker": (["hands_empty"], EST_COOK_OPS),
    "TakeCookedToPlate": (["holding_plated_dish"], EST_COOK_OPS),
    "DeliverPlate": (["holding_plated_dish"], EST_SERVE),
    "Mix": (["hands_empty"], EST_COOK_OPS),
    "PlaceToRelay": (["hands_empty"], EST_FETCH),
}


def _plate_cell(world, plate):
    # type: (World, Dict) -> Optional[Tuple[int, int]]
    x, z = plate.get("x"), plate.get("z")
    if x is None or z is None:
        return None
    return world.to_grid(x, z)


def _cookers(world):
    # type: (World) -> List[Dict]
    """灶状态条目。cookers[] 是权威来源；stations 里
    t in ("cooker","cooking") 作兜底（部分单测只给 stations）。"""
    out = list(world.cookers)
    names = set(str(c.get("n")) for c in out if c.get("n"))
    for st in world.stations:
        if st.get("t") in ("cooker", "cooking"):
            n = st.get("n")
            if n and str(n) not in names:
                names.add(str(n))
                out.append(st)
    return out


def _loose_pots(world):
    # type: (World) -> List[Dict]
    out = []  # type: List[Dict]
    for it in world.items:
        if not P.held_is_cookware({"held": it.get("n")}, world.recipes):
            continue
        toks = parse_plate_detail(it.get("detail") or [])
        if any(t.startswith("cooked:") for t in toks):
            continue            # 熟锅留台面等盘，不该生成"端回灶台"候选
        # 与 cooked_sources 同款兜底：detail 只报原始料名（ing:SushiRice）、没有
        # cooked: token 时，cooked 字段才是"熟"的权威信号（实机：手持/散熟锅的
        # detail 是 ['ing:SushiRice']、cooked="Cooked"）。漏掉这条会把熟锅当散空锅，
        # 生成 RecoverLoosePot 与 TakeCookedToPlate 抢同一口锅 → 每拍横跳。
        if str(it.get("cooked") or "").lower() == "cooked":
            continue
        out.append(it)
    return out


def _has_bin(world):
    # type: (World) -> bool
    for st in world.stations:
        if st.get("t") == "bin":
            return True
    return False


def _has_dirty_plate(world, sink):
    # type: (World, Dict) -> bool
    """洗碗池是否有活可干：场上有 DirtyPlateStack，或池里已有待洗的盘。

    直接复用 `ai.tasks.plate.dirty_plate_exists`，两边不再各写一份口径；
    候选生成阶段先做这层过滤，让「没有脏盘」时根本不存在 WashPlate 候选
    （ack 才能是 pending）。
    """
    from ai.tasks.plate import dirty_plate_exists
    return dirty_plate_exists({"w": world}, (sink or {}).get("n"))


def _gen_misc(world, bb, book, chef, hands_empty):
    # type: (World, Blackboard, RecipeBook, Any, bool) -> List[Candidate]
    """与订单无关的 task：锅具回收 / 清理 / 待命（spec §6.1 第 2/5/6/11/12/13/14/15 号）。

    这些 task 的 feasible 谓词会再过滤一遍；这里只做资源枚举与手部门——
    需要先拿起东西的（Discard / RescueCooked / RecoverLoosePot / ResetPlate /
    WashPlate）只在手空时生成，否则其树首的 pickup 会把手里的东西换掉。
    """
    cands = []  # type: List[Candidate]

    # 第 2 号 WashPlate：有洗碗池且有脏盘才生成；feasible 再判物理可行性。
    # 只按洗碗池生成会把「没有脏盘」也当成一个当前候选，玩家说「去洗碗」
    # 时 ack 会误报 accepted（spec §4.7：目标不存在时应 pending，等盘出现）。
    if hands_empty:
        for st in world.stations:
            if (st.get("t") == "washing" and st.get("n")
                    and _has_dirty_plate(world, st)):
                cands.append(Candidate(T.WashPlate(resources=(st.get("n"),)),
                                       ["hands_empty"], EST_FETCH))

    for c in _cookers(world):
        if not c.get("n"):
            continue
        cid = P.resource_id(c)
        # 第 5 号 ReturnPot：手持空/生锅 → 放回空灶（熟锅走 TakeCookedToPlate）
        if (P.held_cookware_state(chef, P.held_items(world), world.recipes)
                in (P.COOKWARE_EMPTY, P.COOKWARE_RAW)
                and not P.cooker_has_pot(c)):
            cands.append(Candidate(T.ReturnPot(resources=(cid,)), [], EST_FETCH))
        if hands_empty:
            # 第 6 号 RescueCooked / 第 11 号 Discard：feasible 判熟/糊
            cands.append(Candidate(T.RescueCooked(resources=(cid,)),
                                   [], EST_COOK_OPS))
            if _has_bin(world):
                cands.append(Candidate(T.Discard(resources=(cid,)),
                                       [], EST_COOK_OPS))
            # 第 12 号 RecoverLoosePot：空灶 + 地上有锅（feasible 再判）
            if not P.cooker_has_pot(c):
                for pot in _loose_pots(world):
                    task = T.RecoverLoosePot(resources=(cid,),
                                             item=pot.get("n"))
                    cands.append(Candidate(task, [], EST_FETCH,
                                           branch=branch_of_task(task, book)))

    if hands_empty:
        # 第 14 号 ResetPlate：绑盘当前所在格为拼装格（feasible 判是否错料盘）
        for p in world.plates:
            pid = p.get("n")
            cell = _plate_cell(world, p) if pid else None
            if pid and cell is not None:
                cands.append(Candidate(T.ResetPlate(None, (pid, cell)),
                                       [], EST_FETCH))
        # 第 15 号 Prefetch：多单都要的料（feasible 再判）
        seen = set()
        for order in world.orders:
            if _order_is_abandoned(bb, order.get("id")):
                continue
            for ing in book.requirements(order):
                if ing in seen or book.needs_cooking(ing):
                    continue
                seen.add(ing)
                task = T.Prefetch(item=ing)
                cands.append(Candidate(task, [], EST_FETCH,
                                       branch=branch_of_task(task, book)))
    else:
        # 第 13 号 StashHeld：手里有无用物（feasible 再判）
        cands.append(Candidate(T.StashHeld(), [], EST_FETCH))

    # 1-3 中转交接候选：仅在配置了中转台的关卡生成（relay_cells 非空）。
    # 可投掷食材的批量取+投已并入通用 FetchToBoard（见 chop.py）；这里只剩
    # 传盘（PlaceToRelay）。feasible 再精确判（中转台有空位），这里只做粗枚举。
    # order_id 一律 None：relay 是协作动作，不绑定特定订单（同 Prefetch）。
    relay_cells = S.relay_cells_for(world)
    if relay_cells:
        # 干净盘 → 放中转台（传盘子；feasible 判盘干净 + 中转台有空位）
        for p in world.plates:
            pid = p.get("n")
            if pid and P.plate_count(p) == 0:
                task = T.PlaceToRelay(item=pid)
                cands.append(Candidate(task, [], EST_FETCH,
                                       branch=branch_of_task(task, book)))

    # 第 18 号 FillCooker：订单无关的填灶（第 1 组 1.1）。
    # 按单发射的 FetchToCooker 受「同单只发一个 + 灶↔单 1:1」限制，无法把
    # 空灶填满——实测 59% 的拍里三口灶一个都没在煮，三单因此过期。这里按
    # 「空灶 × 需烹饪的料」粗枚举，缺口判定留给 feasible（读 cook_supply）。
    for c in CS.idle_cookers(world):
        if not c.get("n"):
            continue
        cid = P.resource_id(c)
        ck = P.cooker_cookware(c)
        for item in CS.cooked_items_wanted(world, book):
            raws = book.raws_of(item)
            if not raws:
                continue
            want = (book.cookware_of(item) or "").lower()
            # 炊具类型匹配（煮意面配炖锅、煎西红柿配煎锅）；灶上炊具未知时不过滤。
            if want and ck and not P.cookware_matches(ck, want):
                continue
            task = T.FillCooker(None, (cid, tuple(raws)), item)
            cands.append(Candidate(task, ["hands_empty"], EST_COOK_OPS,
                                   branch=branch_of_task(task, book)))

    # 散锅回收：空灶 + 可捡的散锅（空/生）→ 捡锅上灶再下料（第 1 组 1.5）。
    for c, pot_name, item, raws in CS.recoverable_cookers(world, book):
        cid = P.resource_id(c)
        task = T.FillCooker(None, (cid, tuple(raws), pot_name), item)
        cands.append(Candidate(task, ["hands_empty"], EST_COOK_OPS,
                               branch=branch_of_task(task, book)))

    return cands


def _time_left_ticks(world):
    # type: (World) -> Optional[float]
    """本局还剩多少拍。拿不到计时器信息时返回 None（= 不知道，别据此否决）。

    `left <= 0` 也算「不知道」：插件在**未开局**和**已结束**两种状态下都报 0，
    而 0 是个合法数字，旧实现的 `left is not None` 放它过去了 —— 于是终局闸
    在开局那几拍就把全部候选判了 endgame。实机 trace-20261014-204822 开局
    t=8~155 共 147 拍（7 秒）站着不动，20 个候选全 endgame，正好够填一口灶。
    真正剩 0 秒时本局已经定局，不否决也不会多做无效工。
    """
    timer = (getattr(world, "raw", None) or {}).get("timer") or {}
    for side in ("server", "client"):
        part = timer.get(side) or {}
        left = part.get("left")
        if left is None:
            continue
        try:
            ticks = float(left) * ETA.TICKS_PER_SECOND
        except (TypeError, ValueError):
            return None
        return ticks if ticks > 0 else None
    return None


# 终局否决：开一个 task 至少要留出它自己的耗时 + 一次送餐的时间，否则产出
# 来不及兑现。实测一次 DeliverPlate 约 45~55 拍，取 60 拍留余量。
ENDGAME_DELIVER_RESERVE = 60


def _is_endgame_waste(task, cand, time_left):
    # type: (Any, Candidate, Optional[float]) -> bool
    """时限内变不成送达的活（第 4 组 4.1）。

    实机 t>4300（剩不到 15 秒）仍在跑 Prefetch(Seaweed) 并连续 failure，
    45 拍纯浪费。送餐例外：料齐的盘在最后几秒也要送，那是 60 分 base。
    """
    if time_left is None:
        return False            # 不知道剩多少时间 → 不否决（宁可白干也别罢工）
    if getattr(task, "TYPE", None) == "DeliverPlate":
        return False
    # 用 ETA 的实测耗时表，**不是** candidate.estimated_steps——后者是按 TYPE
    # 猜的常数（EST_FETCH=10 拍=0.5 秒），与实测差一个数量级，拿它判终局等于
    # 永不触发（这正是旧 _deadline_forecast 整局只触发 33 拍的同一个坑）。
    need = ETA.task_cost_ticks(getattr(task, "TYPE", None)) \
        + ENDGAME_DELIVER_RESERVE
    return time_left < need


def _targets_moving_platform(task, ctx):
    # type: (Any, Dict) -> bool
    """任务认领的资源是否在「正在移动的非主网格平台」（气球篮）上。

    方案 B：篮子移动时，把目标在篮子上的 task 判为不可行，而不是选出来再
    执行失败（选到 → 必失败 → 重选的死循环）。粒度是 task.claims() 的
    资源 id——WashPlate 的 sink、FetchToCooker 的灶/料箱等都带 (grid, name)，
    只要 grid 是非主网格且篮子正在动就否决。共享判据见 P.on_moving_platform。
    """
    for resource_id, _action in task.claims():
        if isinstance(resource_id, tuple) and len(resource_id) == 2:
            if P.on_moving_platform(resource_id[0], ctx):
                return True
    return False


def _is_stopped(task, bb):
    # type: (Any, Blackboard) -> bool
    """task 是否落在账本 `stop`（「先别做」）的硬压制里（spec §4.6）。

    stop 条目编译成 `kind="stop"` 的偏好，按作用域命中、到期即失效。

    **救援动作不受压制**：玩家说「先别煮饭」不等于「饭要糊了也别管」；
    `TakeCookedToPlate` / `RescueCooked` / `ReturnPot` / `RecoverLoosePot` /
    `Extinguish` 同理（spec §4.5 末段）。
    """
    if _is_rescue_action(task):
        return False
    tick = getattr(bb, "tick", 0) or 0
    for pref in getattr(bb, "preferences", None) or ():
        if getattr(pref, "kind", None) != "stop":
            continue
        expires = getattr(pref, "expires_tick", 0) or 0
        if expires and tick >= expires:
            continue
        if preference_matches_task(pref, task):
            return True
    return False


def _breaks_combo(task, world):
    # type: (Any, World) -> bool
    """送餐是否会把 combo 打断：送非队首菜（队首存在）。

    队首 = _combo_head_id（id 最小）；credited = _credited_order_id（游戏把
    一盘菜记给同 dish 存活单里 remaining 最小者）。credited != 队首时 combo 断。

    刻意**不做跳单 escape**：多人协作下 solo_slack 是 P2 单人手口径、不准，
    用它当「队首没救就放开跳单」的信号会误放行。先严格保顺序；队首做不出来
    时的取舍，留到有更准的协作口径再处理。
    """
    if getattr(task, "TYPE", None) != "DeliverPlate":
        return False
    head = _combo_head_id(world.orders)
    if head is None:
        return False
    dish = _dish_of_order(world.orders, getattr(task, "order_id", None))
    credited = _credited_order_id(world.orders, dish)
    return credited is not None and credited != head


def _feasibility_of(cand, ctx, bb, ignore_abandon=False, time_left=None):
    # type: (Candidate, Dict, Blackboard, bool, Optional[float]) -> Tuple[bool, str]
    """判断单个候选是否可行，返回 (ok, 否决原因)。

    否决原因供 trace 排查：claimed（资源被他人认领）/ abandoned（L3 拉黑）
    / p2_suppressed（账本 stop：玩家让先别做）/ infeasible（task.feasible()
    为假）。ok=True 时原因为空串。

    ignore_abandon=True 给火情用：拉黑是按资源 id 记的，别的 task 在同一
    口灶上反复失败也会把着火的那口灶写进 scratch.abandoned（比如 Normal
    模式里 ReturnPot 卡在灶边），但火不能不管。
    """
    task = cand.task
    if not T.claims_available(task, ctx):
        return False, "claimed"
    # 方案 B（篮子移动时否决目标在篮子上的候选 task）已禁用。
    # if _targets_moving_platform(task, ctx):
    #     return False, "platform_moving"
    if _is_stopped(task, bb):
        return False, "p2_suppressed"
    if not ignore_abandon and _is_abandoned(task, bb):
        return False, "abandoned"
    if _breaks_combo(task, ctx["w"]):
        return False, "combo_break"
    # 终局否决暂时禁用（用户反馈：临近结束角色停摆，看起来像卡了）。
    # 需要恢复时取消下面两行注释即可。
    # if _is_endgame_waste(task, cand, time_left):
    #     return False, "endgame"
    if not task.feasible(ctx):
        return False, "infeasible"
    route_veto = reachability.task_veto(task, ctx)
    if route_veto:
        return False, route_veto
    return True, ""


def filter_feasible(world, bb, candidates, book):
    # type: (World, Blackboard, List[Candidate], RecipeBook) -> List[Candidate]
    """可行性过滤（spec §6.2）。"""
    feasible = []  # type: List[Candidate]
    ctx = _make_context(world, bb, book)
    for cand in candidates:
        ok, _ = _feasibility_of(cand, ctx, bb)
        if ok:
            feasible.append(cand)
    return feasible


def _is_abandoned(task, bb):
    # type: (Any, Blackboard) -> bool
    """L3 兜底：task 的认领资源是否处于 abandoned 拉黑期（spec §7.2）。

    AbandonTarget（或 agent 的连续失败兜底）写 scratch.abandoned[res_id] =
    失效拍号，这里按 TTL 判断：未过期 → 跳过该候选，让 planner 落到第二
    候选，而不是每拍重选同一个反复失败的 task（实机复现：切菜被传送带
    反复推走时 flap）。
    """
    # NO_ABANDON 的任务（端锅/盛饭/灭火）读侧也不受拉黑约束：拉黑期比
    # 烧糊倒计时还长，一旦被否决就等于放弃救锅 → 必着火。
    if getattr(task, "NO_ABANDON", False):
        return False
    scratch = getattr(bb, "scratch", None)
    abandoned = getattr(scratch, "abandoned", None)
    actions = getattr(scratch, "abandoned_actions", None) or {}
    abandoned = abandoned or {}
    tick = getattr(bb, "tick", 0)
    for res_id, action in task.claims():
        if abandoned.get(res_id, 0) > tick or actions.get((res_id, action), 0) > tick:
            return True
    return False


def _make_context(world, bb, book=None):
    # type: (World, Blackboard, Optional[RecipeBook]) -> Any
    """构造 task 可行性判定所需的上下文。

    与执行层 ctx 对齐（primitives/base.py 的契约）：w / chef / bb / tick /
    book。task 的 feasible/done 谓词里会读 ctx["chef"]，缺了会 KeyError。
    """
    ctx = {
        "w": world,
        "chef": P.chef_by_name(world.chefs, P.P2_NAME),
        "bb": bb,
        "tick": bb.tick,
    }
    if book is not None:
        ctx["book"] = book
    ctx['field'] = reachability.field_for(world, bb)
    return ctx


# ==================== 打分与选择（Plan 3 Task 5）====================

# 权重常量（Plan 3 最小集）
W_URGENCY = 5.0
# 时间压力权重：锅熟了/快糊了要越早去端。pressure 曲线 0~2（0=生、1=刚熟、
# 2=快糊），权重越高，熟料/快糊锅越能压过备菜、送餐等普通候选。
W_PRESSURE = 5.0
# 洗盘压力权重：干净空盘越少越该洗（同 pressure 口径：0=充足、1=只剩一块、
# 2=用光）。无盘时拼装被卡死，需压过备菜与填灶；只剩一块时提前补盘但不盖过
# 送餐。
#
# 5.0 → 3.5（Ruling 22）：5.0 时 0 盘档吃满 10.0 分，压过 progress=23/24 的
# 快糊锅（9.58）—— 那是用「盘没洗」换「锅烧糊」，不划算。
#
# 这个教训本来已经学过一次：姐妹项 W_NO_PLATE_PLACED 的定标注释（见下）里
# 记着「W=5.0 时摆盘吃满 8.5 分，连快糊的 RescueCooked(8.08) 都压得过去」，
# 结论是把它定在 3.0。但那次只改了 W_NO_PLATE_PLACED，**本权重留在 5.0、
# 从未受过同样检验** —— 两者都乘 _plate_pressure 的 0 盘档(2.0)，产出同一个
# 过高分数。tests/test_score_ordering.py::test_hot_pot_outranks_wash 第一次
# 运行就抓到了它。
#
# 取 3.5 而非 3.0：3.5 时 0 盘档得 7.0（压过备菜/填灶 5.6、让位快糊锅、且
# 明确高于刚熟的锅 5.0），1 盘档得 3.5（提前补盘但不盖过送餐 3.0+）。
# 4.0 时 0 盘档 8.0 与快糊锅只差 0.08，是噪声级的胜负，不算钉住。
#
# 3.5 → 2.5（最终复审的 must-fix）：3.5 只验算了 **0 盘档**（W×2=7.0 对快糊锅
# 9.58），**没检查 1 盘档对它的同层兄弟** W_NO_PLATE_PLACED=3.0 —— 1 盘时洗盘
# 3.5 仍压过摆盘 3.0，而 spec §7 的 L4 层内序是「0 盘 > 无盘就位 > 1 盘」。
# 顺序反了，且当时这一档占 L4 可达域的一半（_plate_pressure > 0 仅当
# clean <= 1，那时 WashPlate.feasible 允许 clean < 2）。如今 L4 层谓词要求
# clean==0 且无装配盘（spec §A.3 把「要不要洗」从 feasible 拆进层），
# 1 盘洗盘进不了 L4，但下面的权重区间仍保留。
#
# 讽刺的是这正是本权重上一次改动（5.0 → 3.5）诊断出的那个「同一教训只修一半」
# 的病：那次改完 W_NO_PLATE_PLACED 却没回头看本权重，这次改完本权重的 0 盘档
# 又没回头看 1 盘档。
#
# 取值要满足 `W×2 > W_NO_PLATE_PLACED > W`，可行区间 [2.0, 3.0)，取中点 2.5
# （0 盘档 5.0、1 盘档 2.5，两侧各留 0.5 余量）。
#
# **旧约束「0 盘洗盘不能压过刚熟的锅（W_PRESSURE*1.0=5.0）」已不再靠数值**
# ——分层后 L2 救料 > L4 解卡，结构性保证；所以 0 盘档 5.0 与刚熟锅并列不成
# 问题。「不能压过快糊的锅」同理（L1 > L4）。若 W_NO_PLATE_PLACED 变动，
# 上面那个区间要重算。
W_PLATE_PRESSURE = 2.5
W_ETA = 0.1

# Plan 4 Task 4 权重（spec §6.8）
W_ASYNC_RESOURCE = 2.0      # 异步资源优先（FetchToCooker > FetchToPlate）
W_COMPLETION_RATIO = 1.0     # 完成度（盘里食材越接近完成优先级越高）
W_INGREDIENT_REUSE = 0.5     # 食材复用（一次取料满足多个订单）
# Combo 队首加分 / 跳单惩罚（第 3 组 3.3/3.4）。
#
# 量级依据（ai/economy.py 的实测收益函数）：新鲜度最多差 5 分（3→8，相对
# base 60 只有 8%），而 combo 到 3 就系数翻倍、到 5 是 4 倍。所以优先级是
# **不丢单 >> 按序送 >> 送得新鲜**，combo 项必须显著高于新鲜度项，否则两者
# 在同一量级上互相抵消（队首不新鲜、后面某单很新鲜时该听谁的）。
#
# 上限控制在 2.0：它叠加在 W_DELIVER_BONUS(3.0) 之上，合计 5.0 已与端刚熟的
# 锅（W_PRESSURE*1.0）持平——再高就会为了保连击而让锅烧糊。
W_COMBO_HEAD = 2.0
# 跳单惩罚：队首仍可救时，送后面的单会把连击打断。取与加分同量级，形成
# 「送队首 +2.0 / 送别人 -2.0」的 4.0 分差，足以压过备菜但不盖过救锅。
W_COMBO_BREAK = 2.0
# 新鲜度加分（满档与最低档之差）。实测只值 5 分小费，故权重取小。
W_FRESHNESS = 0.6

# 备菜吃 combo 偏好时的折扣。备菜离"兑现连击"还隔着好几步，不该和送餐等价，
# 更不该盖过救锅（W_PRESSURE）/ 填灶（W_COOKER_IDLE）。
#
# 取 0.25（±0.5 分）而不是 0.5：0.5 时备菜拿到 +1.0，`FetchToPlate` 会以
# 2.98 压过 `FillCooker` 的 2.50，把第 1 组在真实帧上验证过的「灶空转时先填灶」
# 直接顶翻（tests/test_fill_cooker_priority 抓到）。combo 在备菜阶段只需要能
# 在**两个订单之间**分先后，不需要改变「备菜 vs 填灶」这种跨类优先级。
COMBO_PREP_DISCOUNT = 0.25
# 哪些备菜 task 吃 combo 偏好：都是「为某一单推进一步」的类型。
# 不含 FillCooker（订单无关）与清理类。
_COMBO_PREP_TYPES = ("FetchToPlate", "FetchToBoard", "Chop",
                     "FetchToCooker", "TakeCookedToPlate", "PlaceCleanPlate")
W_DEADLINE_LOOKAHEAD = 10.0  # 截止前瞻（会导致烧糊/超时的 task 惩罚）

# 填灶权重（第 1 组 1.4）：灶空转的机会成本是 12 秒不可压缩的时钟，补不回来。
# 实测 trace-20261014-120406 的 1700~3270 窗口三口灶 59% 空转、缺口恒为 3~5，
# 而同期 planner 一直在摆海苔/切鱼——order#3/4/5 三单全在这窗口过期。
#
# 量级：必须压过备菜的**实际合计**，否则还是选不上；但不能压过端刚熟的锅
# （W_PRESSURE*1.0=5.0，锅会烧糊、盘不会）。
#
# 4.0 → 4.8 的原因：第 3 组把 urgency 从 `W_URGENCY/remaining`（中段恒 ~0.1）
# 改成读 slack 的分段线性后，备菜候选在中段能拿到 3~4 分 urgency，合计
# 2.7~3.3 分，把 4.0 的填灶顶了下去——第 1 组在真实帧上验证过的七个"灶空转
# 该填灶"的拍全部回退（t=1800/2000/2100/3000/3100/3200/3400）。这是两组权重
# 叠加的典型冲突，靠单组的测试发现不了，只有把真实帧重放一遍才看得见。
# 4.8 仍严格低于 5.0，端熟锅的优先级不变。
#
# 4.8 → 5.6（Ruling 12）：原先「必须压过备菜的实际合计 2.7~3.3」这个标定依据
# 被一处重复计分污染了——当时 urgency 被 deadline_lookahead 以同形曲线反向抵消
# （见 _deadline_forecast 的 ② 已移除那段），备菜候选的净分被压到 -4 左右，
# 4.0/4.8 不战而胜。删掉重复计分后，备菜的真实合计是 **4.97**
# （urgency 3.72 + ingredient_reuse 1.0 + combo_head 0.25，实测灶全空 3 单要米）。
#
# 5.6 的推导依赖 **eta ≈ -1.5**：5.6 - 1.5 = 4.1 > 3.47，留 0.6 余量。
# spec §10 列着「两套耗时常数统一」（EST_* 10/20/15/5 vs eta.COST_* 125/30/120/50），
# 那件事会改 eta 的量级 —— 届时这个 5.6 必须重算。
#
# 注意 5.6 > W_PRESSURE*1.0 = 5.0，**字面上**违反了「不能压过端刚熟的锅」这条
# 约束。但 cooker_idle 要扣 eta（净 4.1），而端熟锅的 pressure 分不扣同量 eta，
# 所以净值仍是端熟锅优先。下面那条测试钉的是**净值**关系，不是常量大小。
W_COOKER_IDLE = 5.6
# 缺口饱和点：缺 3 份以上就算"满额紧急"，再多不额外加分——否则开局 5 单
# 全要米时填灶会盖过送餐和灭火。
COOKER_SHORTFALL_SATURATION = 3

# 送餐优先（先上得分更高）：料齐的盘应尽快送出去，压过继续备菜的候选。
# 取值要压过 W_ASYNC_RESOURCE(2.0)+completion_ratio(1.0)，但低于快糊锅
# 的 pressure 分（W_PRESSURE*2.0=10.0），否则会耽误端快糊的锅（实机烧糊）。
# 注：刚熟的锅 pressure 分 = W_PRESSURE*1.0=5.0，略高于送餐——熟料优先
# 于送餐（锅会烧糊，盘不会）。若实机发现送餐被压过头，可下调 W_PRESSURE。
# 由 4.0 降到 3.0：4.0 会把 DeliverPlate 抬到比当前 task 高 2.0 以上、频繁
# 打破承诺惯性（实机：备菜/传盘做到一半被送餐抢走）。3.0 仍高于备菜（2.0
# async + 1.0 completion），但分差落在 COMMITMENT_MARGIN(2.0) 内，不轻易改选。
#
# **分层后本权重已不参与打分**（Ruling 3 / spec §7.1）：deliver_bonus 不在任何
# 层的白名单里——L3 兑现层的全部候选都是 DeliverPlate，给每个加同一个常数不改变
# 任何排序。常量保留是因为三个测试文件引用它算量级天花板，删它会连带改测试，
# 而「既有测试零改动」是分层正确性的核心证据。
W_DELIVER_BONUS = 3.0

# 摆盘压力权重：一块盘都没就位时 = 拼装链没有落点，和「0 干净盘」等价
# 地卡死整条装配。
#
# 为什么需要独立一项：`PlaceCleanPlate` 本身只能吃 urgency（订单相关），
# 而 urgency 在中段只有 3~4 分，稳定输给 FillCooker(5.6) / WashPlate(0 盘档
# 5.0、1 盘档 2.5)。
# 实机 trace-20261014-221306 后半局：晾干架上躺着唯一那块干净盘，
# `PlaceCleanPlate` 可行 1101 拍、选中 **0** 次，807 拍（24%）场上一块盘
# 都没就位——锅在煮、菜在切，而没有一只盘接得住，收盘挂 5 单。
#
# 量级 3.0（不是与 W_PLATE_PRESSURE 对称的 5.0）：摆盘要压过备菜与填灶，
# 但**不能压过快糊的锅**——锅会烧糊，盘只是在那儿等着。
#
# 定标过程（按本局 662 帧重放，`无盘就位却没去摆盘` 的拍数 / 救锅是否还救）：
#   W=2.0 → 53 拍不摆盘，救锅保住 14 拍
#   W=3.0 → 47 拍不摆盘，救锅保住 14 拍   ← 取这个
#   W=5.0 → 33 拍不摆盘，救锅 **全部 0 拍**（t=7295~7310 的熟锅没人管）
# 5.0 时摆盘吃满 8.5 分，连 progress=23/24（快糊）的 RescueCooked（8.08）
# 都压得过去——那是用「盘没摆」换「锅烧糊」，不划算。3.0 下刚熟的锅
# （pressure 4.17/6.00）让位摆盘，快糊的锅（8.08）仍优先救。
W_NO_PLATE_PLACED = 3.0

# 1-3 关卡 relay task 的固定加分。
#
# **约束机制已变（Ruling 44）**：原注释写「必须低于端熟锅 pressure（≈5.0）和
# 送餐（W_DELIVER_BONUS≈4.0），否则米熟了 P2 还在传盘、锅烧糊」——那个实机现象
# 是真的，但分层后这两条「必须低于」都不再由**数值余量**保证，而由**层序**保证：
#   - 送餐在 L3 兑现、传盘在 L6 备菜 → 送餐结构性优先（且 deliver_bonus 已不
#     参与打分，不在任何层白名单里）；
#   - 端熟锅在 L1/L2（pressure >= 1.0 即进救料层）、传盘在 L6 → 同理，且
#     pressure 不在 L6 的白名单里。
#
# 所以这个数现在只需在 **L6 层内**与备菜的其余项可比（urgency / completion_ratio
# / async_resource / ingredient_reuse / combo_* 等）。原注释的第二句「传盘是协作
# 支线，低于下锅（W_ASYNC_RESOURCE≈2.0），否则 P2 一直传盘、不下锅煮（实机复现）」
# **仍然有效**——两者同在 L6，仍靠数值分先后。
#
# 传盘（PlaceToRelay）是协作支线，低于下锅（FetchToCooker 的
# W_ASYNC_RESOURCE≈2.0），否则 P2 一直传盘、不下锅煮（实机复现）。
_RELAY_BONUS_1_3 = {
    "PlaceToRelay": 1.5,
}

# 手部前置的腾手代价（spec §6.1(b)）：task 需要空手但手里有东西时，按
# "约 EnsureHandsFree 的步数"折算成 ETA 代价。它不是"判不可行"，而是让
# 打分能比较"先放下手里的活去干它"与"先把手里的活收尾"。
HAND_CLEAR_STEPS = 10

# 拾取代价（spec §6.1(b)）：holding_cooked / holding_plated_dish 这类前置
# 的"手不对"不是要腾手，而是过去把目标物捡起来（锅/盘就在那儿），成本远
# 低于 EnsureHandsFree 的清手。用同 HAND_CLEAR_STEPS 会让 DeliverPlate /
# TakeCookedToPlate 被 10 步清手惩罚压着，做好的盘送不出去（实机复现）。
HAND_PICKUP_STEPS = 3

# 搬料惩罚：FetchToBoard 的来源若是"另一块板"，它其实是"搬"而非"取"，
# 零产出。按 ETA 步数折算成代价，让 Chop（有产出）优先。数值取远大于
# EST_CHOP，保证"搬"永远打不过"切"。
MOVE_PENALTY_STEPS = 50

# ==================== 承诺惯性（Plan 3 Task 6）====================

# 承诺惯性常量（spec §6.5）
COMMITMENT_MARGIN = 2.0         # 改选门槛（分数差）
PROGRESS_BONUS_PER_STEP = 1.0   # 每完成一步的进度加成
INFEASIBLE_THRESHOLD = 3        # 连续多少拍不可行才放弃

# Task 5: 承诺穿插常量
INTERLEAVE_THRESHOLD = 5        # 剩余步数阈值（低于此值不允许穿插）
INTERLEAVE_PENALTY = 3.0        # 穿插的额外分数门槛


# ==================== 每拍 1:1 匹配（盘↔订单 / 灶↔订单）====================

def _remaining_of(order):
    # type: (Dict) -> float
    r = order.get("remaining")
    return r if isinstance(r, (int, float)) else float("inf")


def _order_plate_id(plate_match, oid):
    # type: (Dict, Any) -> Optional[Any]
    for pid, o in plate_match.items():
        if o == oid:
            return pid
    return None


def _order_has_plate(plate_match, oid):
    # type: (Optional[Dict], Any) -> bool
    """订单是否已有盘就位（盘↔订单匹配里有它）。plate_match 为 None 表示
    无匹配信息（保守 True）；空 dict 表示匹配结果为空、订单没盘（False）。"""
    if plate_match is None:
        return True
    return _order_plate_id(plate_match, oid) is not None


def _plate_by_n(world, pid):
    # type: (World, Any) -> Optional[Dict]
    for p in P.all_plates(world):
        if p.get("n") == pid:
            return p
    return None


def _pinned_plate_order(world, bb, book):
    # type: (World, Blackboard, RecipeBook) -> Optional[Tuple[Any, Any]]
    """P2 当前承诺绑定的盘↔订单配对，钉死它不参与每拍贪心重排。

    只在「订单还活着 + 盘还在 + 盘内容仍与该单兼容」时钉死；任何一条破了
    （P1 送掉订单 / 拿走盘 / 倒错料）本拍自然解钉。钉是派生值，不写黑板、
    无生命周期负担。
    """
    commit = getattr(bb, "commitment", None)
    task = getattr(commit, "active_task", None) if commit else None
    if task is None:
        return None
    pid = getattr(task, "plate_id", None)
    oid = getattr(task, "order_id", None)
    if pid is None or oid is None:
        return None
    want = {o["id"]: book.order_want_tokens(o)
            for o in world.orders if o.get("id") is not None}
    wt = want.get(oid)
    if wt is None:
        return None                        # 订单已送掉/过期
    plate = None
    for p in P.all_plates(world):
        if p.get("n") == pid:
            plate = p
            break
    if plate is None:
        return None                        # 盘已消失
    if not (book.plate_tokens(plate) <= wt):
        return None                        # 盘内容与该单不再兼容
    return (pid, oid)


def _match_plates_to_orders(world, bb, book, orders=None):
    # type: (World, Blackboard, RecipeBook, Optional[List[Dict]]) -> Dict[Any, Any]
    """盘↔订单 1:1 匹配（每拍重算、不写黑板）。返回 {plate_id: order_id}。

    orders 显式传入时只用这些单（planner 传 `_dialog_live_orders`，避免被
    放弃的单占住盘）；缺省仍读 world.orders。

    带成本的贪心边选取：边 (盘, 单) 兼容当且仅当 盘token ⊆ 单需求token，
    成本 missing = |单需求token − 盘token|。空盘 missing=整道菜，自然排最后、
    只填没盘的订单。

    排序第一键是「是否队首」：队首（combo 队首，id 最小）优先拿盘，否则
    接近完成的盘会被配给需求更少的非队首单、队首反而没盘 → 跳单送餐、combo
    断。熟锅没盘可由 RescueCooked 端锅离火兜住，不必在这里优先。
    """
    raw_orders = orders if orders is not None else world.orders
    orders = [o for o in raw_orders if o.get("id") is not None]
    if not orders:
        return {}
    want = {o["id"]: book.order_want_tokens(o) for o in orders}
    head = _combo_head_id(orders)

    # 匹配「已就位」的盘（落在 counter 拼装格上）+ P2 手里正端着的盘
    # + 掉在地上且带料的半成品盘。仍排除：
    #   - P1 手里端着的盘：P2 没法往 P1 手里的盘倒料、也没法端走；
    #   - 沥碗架（plate_return+DryingPart）/ 盘堆里的**空**盘：空盘该走
    #     PlaceCleanPlate 端到拼装格。空盘不匹配的另一个原因是堆叠里只有顶层
    #     能被倒料，而 plates[] 没有高度/层信息，直接匹配空盘会误绑到中间层；
    #   - 洗碗池里还没洗的盘：脏盘不参与装配。
    # 地上的带料盘必须算进来：盘被碰掉/掉落后若不算，就成孤儿——没有任何
    # task 再 target 它（实机 trace-20261018-104804：BoiledPasta 盘掉地后
    # 躺到收盘没人碰）。
    counter_cells = set()  # type: Set[Tuple[int, int]]
    for st in world.stations:
        if st.get("t") != "counter":
            continue
        gx, gz = st.get("gx"), st.get("gz")
        if gx is not None and gz is not None:
            counter_cells.add((int(round(gx)), int(round(gz))))
    # P2 手里正端着的盘也计入匹配：端盘盛饭/送餐中盘会离开 counter 格，
    # 但「离开台面」不等于「丢了」。漏掉它会在 AI 端起盘的那一拍把订单
    # 重绑到另一块空盘 → task 身份变化 → 树重建 → 手里这块被放下（实机：
    # 端起盘溜达一圈又放下）。盘名在 chef.held 里，按名匹配。
    p2 = P.chef_by_name(world.chefs, P.P2_NAME)
    p2_held_plate = P.held_name(p2) if p2 is not None else ""
    p1 = P.chef_by_name(world.chefs, P.P1_NAME)
    p1_held_plate = P.held_name(p1) if p1 is not None else ""
    edges = []
    for p in P.all_plates(world):
        pid = p.get("n")
        if not pid:
            continue
        on_counter = _plate_cell(world, p) in counter_cells
        held_by_p2 = bool(p2_held_plate) and \
            p2_held_plate == str(p.get("name") or "").lower()
        held_by_p1 = bool(p1_held_plate) and \
            p1_held_plate == str(p.get("name") or "").lower()
        # 地上散盘（有料、非手持、不在台面）：被碰掉/掉落的半成品盘也要能
        # 继续加料/盛饭/送餐。空盘（count==0）不在这里匹配——照旧走
        # PlaceCleanPlate 端到拼装格，避免改变空盘的行为。
        loose_with_contents = (not on_counter and not held_by_p2
                               and not held_by_p1 and P.plate_count(p) > 0)
        if not (on_counter or held_by_p2 or loose_with_contents):
            continue
        tokens = book.plate_tokens(p)
        for o in orders:
            oid = o["id"]
            wt = want[oid]
            if tokens <= wt:
                is_not_head = 0 if oid == head else 1
                edges.append((is_not_head, len(wt - tokens), _remaining_of(o),
                              str(pid), str(oid), pid, oid))
    # 钉死手持盘：P2 当前承诺绑定的盘不参与贪心重排，保持它和订单的配对。
    # 否则 P1 往另一块盘里放一样料，就能让贪心 tiebreak 翻盘、把 P2 手里的盘
    # 改绑走（实机 trace-20261017-172221 t=663：换盘导致丢盘、熟饭烧糊）。
    pinned = _pinned_plate_order(world, bb, book)
    matched = {pinned[0]: pinned[1]} if pinned else {}  # type: Dict[Any, Any]
    used_plates = set(matched.keys())  # type: Set[Any]
    used_orders = set(matched.values())  # type: Set[Any]
    edges = [e for e in edges if e[5] not in used_plates]
    edges.sort(key=lambda e: (e[0], e[1], e[2], e[3], e[4]))
    for _is_head, _missing, _rem, _pid_s, _oid_s, pid, oid in edges:
        if pid in used_plates or oid in used_orders:
            continue
        matched[pid] = oid
        used_plates.add(pid)
        used_orders.add(oid)
    return matched


def _cooker_burn_danger(c):
    # type: (Dict) -> float
    """锅离烧糊还有多远：0=安全，越大越接近烧糊（1.0=快糊，1.5=已糊/着火）。

    只对灶上锅有意义——散锅已离火，不会再继续烧糊，调用方对散锅给 0。
    比例口径与 _time_pressure 一致：ratio = progress / (2*time)，0.5=刚熟。
    """
    if c.get("burning") or c.get("onFire"):
        return 1.5
    prog = c.get("cooking_progress")
    ctime = c.get("cooking_time")
    if prog is None or ctime is None:
        return 0.5 if c.get("cooked") else 0.0
    try:
        ratio = float(prog) / (2.0 * float(ctime))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.5 if c.get("cooked") else 0.0
    return max(0.0, min(ratio, 1.5))


def _burn_eta_ticks(c):
    # type: (Dict) -> Optional[float]
    """这口灶还有多少**拍**烧糊；没在煮则 None（无烧糊截止）。

    字段语义按真实帧实测（trace-20261014-011202）：

      - `cooking_time` 是**常量容量**（Sushi_1_4_2P 恒为 12），不是已耗时；
      - `cooking_progress` 是进度，单位≈秒（实测 1.07/秒）；
      - 熟点 = progress 达到 `cooking_time`，糊点 = `2*cooking_time`
        （实测曲线：progress 16.9 熟 → 24.0 burning+onFire）。

    口径与 `_cooker_burn_danger` 的 `2*ctime` 一致。
    旧 `_deadline_forecast` 把 `cooking_time` 当已耗时，算出的"距烧糊时间"
    恒为常数、与真实进度无关，于是那条前瞻从未按进度触发过——锅一路烧到
    着火，planner 全程没给惩罚。
    """
    if c is None:
        return None
    if c.get("burning") or c.get("onFire"):
        return 0.0
    prog = c.get("cooking_progress")
    ctime = c.get("cooking_time")
    if prog is None or ctime is None:
        return None
    try:
        prog = float(prog)
        burn_at = 2.0 * float(ctime)
    except (TypeError, ValueError):
        return None
    if burn_at <= 0 or prog <= 0:
        return None            # 没在煮：空灶 / 刚放料还没点火
    return max(0.0, (burn_at - prog) * TICKS_PER_SECOND)


def _match_cookers_to_orders(world, bb, book, plate_match, orders=None):
    # type: (World, Blackboard, RecipeBook, Dict[Any, Any], Optional[List[Dict]]) -> Dict[Any, Any]
    """熟料源↔订单 1:1 匹配（每拍重算）。返回 {source_id: order_id}。

    orders 显式传入时只用这些单（同 `_match_plates_to_orders`）；缺省仍读
    world.orders。

    源两类：
      - 灶上有料的锅（在煮/熟/糊/着火）：pot_contents 反查熟料名。在煮的
        也匹配，好让 _order_needs_cooked 的「该单已有锅」不重复下锅。
      - 台面/地上的散熟锅（cooked_sources kind=="loose"）：items[].detail
        精确给出熟料 token。

    匹配键统一用 rich token（cooked:{raw}:{cookware}）。贪心排序优先级：

      1. 熟且未糊（散锅恒为熟）+ 有盘就位的单（可立即端锅装盘）
      2. 其余按 (remaining asc, source_id asc, order.id asc)

    覆盖「生米在煮」和「米已熟」两段。
    """
    sources = []  # type: List[Dict]
    for c in world.cookers:
        raw = parse_pot_contents(c.get("pot_contents") or [])
        if not raw:
            continue
        if not (c.get("isCooking") or c.get("cooked")
                or c.get("burning") or c.get("onFire")):
            continue
        names = book.cooked_of_raw(raw[0])
        if not names:
            continue
        toks = set()  # type: Set[str]
        for n in names:
            t = book.rich_token(n)
            if t:
                toks.add(t)
        if not toks:
            continue
        ready = bool(c.get("cooked")) and not bool(c.get("burning"))
        sources.append({"id": P.resource_id(c), "tokens": toks, "ready": ready,
                        "danger": _cooker_burn_danger(c)})
    for src in cooked_sources(world, book):
        if src["kind"] != "loose":
            continue
        # 散锅已离火，不会再继续烧糊，危险度 0。
        sources.append({"id": src["id"], "tokens": src["tokens"], "ready": True,
                        "danger": 0.0})
    if not sources:
        return {}
    needy = {}  # type: Dict[str, List[Tuple[Any, Dict]]]
    raw_orders = orders if orders is not None else world.orders
    for o in raw_orders:
        oid = o.get("id")
        if oid is None:
            continue
        matched_pid = _order_plate_id(plate_match, oid)
        matched_plate = _plate_by_n(world, matched_pid) if matched_pid is not None else None
        for ing in book.requirements(o):
            if not book.needs_cooking(ing):
                continue
            if matched_plate is not None and book.plate_has(matched_plate, ing):
                continue
            token = book.rich_token(ing)
            if token:
                needy.setdefault(token, []).append((oid, o))
    edges = []
    for s in sources:
        sid = s["id"]
        # 熟且未糊的源 = 能立即端锅装盘；有盘就位的单 = 能立即接住。
        # 这两者应优先配对：否则贪心按源名分，会把「熟源」配给「没盘的单」、
        # 「生锅」配给「有盘的单」——熟饭两头堵死干烧糊（实机复现）。
        danger = s.get("danger", 0.0)
        for token in s["tokens"]:
            for oid, o in needy.get(token, []):
                has_plate = _order_plate_id(plate_match, oid) is not None
                prio = 0 if (s["ready"] and has_plate) else 1
                # 越接近烧糊的源越要先配给有盘的单：锅在灶上（danger 高）比
                # 散锅（danger 0）更紧急，否则散锅抢走有盘的单、灶上锅配给
                # 没盘的单，TakeCookedToPlate 发不出来，活活烧糊（实机复现）。
                edges.append((prio, -danger, _remaining_of(o), str(sid),
                              str(oid), sid, oid))
    edges.sort(key=lambda e: (e[0], e[1], e[2], e[3], e[4]))
    matched = {}  # type: Dict[Any, Any]
    used_sources = set()  # type: Set[Any]
    used_orders = set()  # type: Set[Any]
    for _prio, _danger, _rem, _sid_s, _oid_s, sid, oid in edges:
        if sid in used_sources or oid in used_orders:
            continue
        matched[sid] = oid
        used_sources.add(sid)
        used_orders.add(oid)
    return matched


def select_task(world, bb, order, book):
    # type: (World, Blackboard, Optional[Any], RecipeBook) -> Optional[Any]
    """Planner 主入口：生成候选、过滤可行性、打分选择（spec §6）。

    多订单模式（Plan 4）：order 参数被忽略，候选生成枚举所有订单。

    返回选中的 Task，若无可行候选则返回 None。
    需要候选/打分明细（trace）时用 plan()。
    """
    return plan(world, bb, order, book)[0]


def plan(world, bb, order, book):
    # type: (World, Blackboard, Optional[Any], RecipeBook) -> Tuple[Optional[Any], Dict]
    """select_task 的完整版：额外返回候选/打分明细（供 trace 记录）。

    返回 (chosen_task, decision_record)。decision_record 形如：
      {"candidates": [(task, score, terms, ok, veto, chosen, layer, in_top_layer), ...],
       "top_layer": int | None,
       "layers": {str(task.key): layer},
       "branches": {str(task.key): 生料名 | None}}
    与 trace._format_candidates 的入参格式一致（spec §8.3 决策可观测性）。
    尾部两项由分层添加：layer 是该候选所在层，in_top_layer 标记它是否进了打分
    （spec §6）。`top_layer` 是**实际选中**的层：本层全被认领且截止闸没开时
    会逐层向下掉（spec §4.6）。闸被打开且抢到的候选还会写 `rescue_override`
    （订单 id）。plan_firefighting 不分层，仍发 6 元组。

    candidates 含**全部**生成候选：被可行性过滤掉的（ok=False）也保留，
    并用 veto 标注被否决原因，方便复盘「为什么没选拿熟饭」。
    """
    # ⓪ 玩家认领/派活命中当前承诺 → 立刻放弃承诺（spec §4.6）。
    # 必须在生成候选之前：否则本拍的候选仍会围绕旧承诺打分。
    _clear_claimed_commitment(bb)
    # ⓪.1 抢回保护期只保护「还在执行的那个 task」：承诺已清（做完/失败/
    # 被换掉）或保护期已过就释放，别把一条旧 hold 带进下一轮。
    _release_stale_rescue_hold(bb)

    # ⓪.5 放弃的订单是整单硬排除：匹配、打分 slack 都只算活单，否则它会
    # 替活单占掉 1:1 盘/灶匹配或队列容量。
    live_orders = _dialog_live_orders(world, bb)

    # ① 每拍 1:1 匹配（盘↔订单、灶↔订单，只算不写黑板）
    plate_match = _match_plates_to_orders(world, bb, book, orders=live_orders)
    cooker_match = _match_cookers_to_orders(world, bb, book, plate_match,
                                            orders=live_orders)
    # 独立 slack：打分 urgency、截止闸 cushion 两处共用。
    slack_map = ETA.solo_slack_by_order(world, book, plate_match,
                                        cooker_match, orders=live_orders)

    # ② 生成候选（多订单模式：枚举所有订单）
    candidates = generate_candidates(world, bb, order, book,
                                     plate_match, cooker_match)
    if not candidates:
        _finalize_rescue_hold(bb, None)
        record = _record([], plate_match, cooker_match)
        record["branches"] = {}
        return None, record

    # ③ 可行性过滤（同时记下每个候选的否决原因）
    ctx = _make_context(world, bb, book)
    time_left = _time_left_ticks(world)
    feasible = []  # type: List[Candidate]
    ok_map = {}    # type: Dict[Any, Tuple[bool, str]]
    for cand in candidates:
        ok, veto = _feasibility_of(cand, ctx, bb, time_left=time_left)
        ok_map[cand.task.key] = (ok, veto)
        if ok:
            feasible.append(cand)

    # ③.5「马上去」硬指令闸（Task 2）：命中未过期的 now 偏好直接选中，跳过
    # 层选择 / 打分 / 承诺惯性。L0-L2 救援在闸内让位，这里只在无火情时抢选。
    imperative = _select_imperative(feasible, world, bb)
    if imperative is not None:
        chosen_task = imperative.task
        # 仍用 layer_filter 算 layer_map 供 trace（闸跳过了层选择，但层号是
        # 复盘候选可观测性的基础）；打分项记 0、terms 空，chosen 只标闸选中的。
        from ai.core.planner_layers import layer_filter
        _ignored_top, _ignored_in_layer, layer_map = layer_filter(feasible, world)
        record_cands = []
        for cand in candidates:
            key = cand.task.key
            ok, veto = ok_map[key]
            record_cands.append((cand.task, 0.0, {}, ok, veto,
                                 key == chosen_task.key,
                                 layer_map.get(key), False))
        record = _record(record_cands, plate_match, cooker_match)
        record["top_layer"] = None
        record["layers"] = {str(k): v for k, v in layer_map.items()}
        record["branches"] = {str(c.task.key): c.branch for c in candidates}
        record["imperative"] = chosen_task.key
        _finalize_rescue_hold(bb, chosen_task)
        return chosen_task, record

    chosen_task = None  # type: Optional[Any]
    score_map = {}      # type: Dict[Any, Tuple[float, Dict]]
    top_layer = None    # type: Optional[int]
    layer_map = {}      # type: Dict[Any, int]
    in_layer = []       # type: List[Candidate]
    rescue_order = None  # type: Optional[Any]
    if feasible:
        # ③.5 分层（spec §6）。**层判定不变**，只是选择集合从「最高层的
        # 全部候选」换成「第一个还有未认领候选的层」——本层全被 claim/
        # assign 认领时逐层向下掉（spec §4.6 fallthrough）。
        #
        # 层是**派生值**——每拍从 feasible 重算。所以「高层动作不可达」
        # （盘被 P1 拿走 / 锅被端走）自然表现为该候选从 feasible 消失、
        # top_layer 自己掉下来，不需要降层动作或失效检测（spec §8.1）。
        from ai.core.planner_layers import layer_filter
        _ignored_top, _ignored_in_layer, layer_map = layer_filter(feasible, world)

        # ④ 打分选择
        # 打分读**独立** slack，不读队列累计 slack：后者队尾单背着全队工时 →
        # slack 最小 → urgency 最高，得出「排得越后越优先」。实机
        # trace-20261014-204822 有 94% 的可比候选组（3516/3745）urgency 与
        # 到期顺序反向。slack_map 已在候选过滤前算好（选层用它当截止闸 cushion，
        # 送餐 combo 闸用它当 escape）。
        top_layer, in_layer = _select_layer(
            feasible, world, layer_map, bb, slack_map, bb.tick)

        # 闸只对「本来会被认领负分压掉」的候选生效。记下它们对应的订单，
        # 选中后写冷却与对话事件（让 Agent 能转告玩家「这单要来不及了」）。
        gate_orders = {}  # type: Dict[Any, Any]
        for cand in in_layer:
            if _claim_penalty(cand, bb, top_layer) >= 0:
                continue
            cushion = _candidate_cushion(cand, world, slack_map)
            if _rescue_gate_open(cand, cushion, bb, bb.tick):
                # 不绑订单的 housekeeping 用最紧的那张单代言（上下文里报菜名）
                oid = _effective_order_id(world, cand.task)
                if oid is None:
                    oid = _tightest_order(slack_map)
                if oid is not None:
                    gate_orders[cand.task.key] = oid

        scored = _score_tasks([c.task for c in in_layer], in_layer, world, bb,
                              book, plate_match, slack_map, cooker_match,
                              top_layer)
        if scored:
            # ⑤ 承诺惯性（Plan 3 Task 6）。**分层不改这里一行**：分层管
            # 「挑哪个」，抢占仍由 COMMITMENT_MARGIN 与 pressure>=1.0 跳过
            # 惯性负责（spec §8）。当前承诺若落在低层，它不在 scored 里 →
            # 走 infeasible_streak 的 3 拍宽限再改选，白捡一个防抖。
            chosen_task = _apply_commitment_inertia(scored, world, bb)
            score_map = {task.key: (score, terms)
                         for task, score, terms in scored}
            rescue_order = gate_orders.get(getattr(chosen_task, "key", None))
            if rescue_order is not None:
                bb.rescue_cooldown_until = bb.tick + 100
                bb.dialog.pending_event = ("rescue", rescue_order)
                # 闸只开一瞬。把抢回的 task 钉住 200 拍（= task 超时），
                # 让它在冷却期内继续参与选择、且不被同一条 claim 清掉。
                # 只钉这一个 task：别的单仍受全局冷却约束。
                bb.dialog.rescue_task_key = chosen_task.key
                bb.dialog.rescue_hold_until = bb.tick + 200

    chosen_key = getattr(chosen_task, "key", None)
    scored_keys = set(c.task.key for c in in_layer)
    record_cands = []
    for cand in candidates:
        key = cand.task.key
        ok, veto = ok_map[key]
        score, terms = score_map.get(key, (0.0, {}))
        # layer / in_top_layer 让 trace 能区分「这个候选被否决了」与「它
        # 可行但层不够高（或被认领且闸没开）」——分层最大的可观测性收益
        # （spec §6）。
        layer = layer_map.get(key)
        record_cands.append((cand.task, score, terms, ok, veto,
                             key == chosen_key, layer,
                             key in scored_keys))

    record = _record(record_cands, plate_match, cooker_match)
    record["top_layer"] = top_layer
    record["layers"] = {str(k): v for k, v in layer_map.items()}
    record["branches"] = {str(c.task.key): c.branch for c in candidates}
    if rescue_order is not None:
        record["rescue_override"] = rescue_order
    # 本拍没再选抢回的那个 task（做完 / 不可行 / 被更高层抢走）→ 保护期结束。
    _finalize_rescue_hold(bb, chosen_task)
    return chosen_task, record


def plan_firefighting(world, bb, book):
    # type: (World, Blackboard, RecipeBook) -> Tuple[Optional[Any], Dict]
    """Firefighting 模式专用规划：生成一个全局 Extinguish 候选。

    返回 (chosen_task, decision_record)，格式与 plan() 一致。

    实机复现：拍 596-727 Firefighting 触发但 P2 完全 idle，原因是 Agent
    主循环在非 Normal 模式下跳过了 Planner 调用 → candidates=0 → 无任务可选。
    旧实现按灶逐个生成 Extinguish 候选、每拍选列表里第一个着火的灶，既没有
    承诺惯性、又会在火复燃时反复换灶（换灶＝重建树＝丢灭火器，实机复现）。
    现在灭火是一个全局 task：树内部循环处理所有着火灶，这里只发一个候选。
    """
    ctx = _make_context(world, bb, book)
    cands = []  # type: List[Candidate]

    # 有任一着火/烧焦的灶 → 发一个全局 Extinguish（树内部循环处理所有灶）
    if any(c.get("onFire") or c.get("burning") for c in world.cookers):
        cands.append(Candidate(T.Extinguish(), [], EST_COOK_OPS))

    if not cands:
        # 无灶着火（Arbiter 误判 or 火已被 P1 扑灭）→ 返回空
        return None, {"candidates": []}

    # 可行性过滤
    feasible_cands = []  # type: List[Candidate]
    ok_map = {}          # type: Dict[Any, Tuple[bool, str]]
    for cand in cands:
        # 火情不受 L3 拉黑约束：着火的那口灶必须一直可攻击（拉黑按资源
        # id 记，别的 task 在同一口灶上反复失败也会把它写进去）。
        ok, veto = _feasibility_of(cand, ctx, bb, ignore_abandon=True)
        ok_map[cand.task.key] = (ok, veto)
        if ok:
            feasible_cands.append(cand)

    if not feasible_cands:
        # 候选不可行（灭火器被 P1 拿走 / 无法到达）
        record_cands = [(c.task, 0.0, {}, False, ok_map[c.task.key][1], False)
                        for c in cands]
        return None, {"candidates": record_cands, "plate_match": {}, "cooker_match": {}}

    chosen = feasible_cands[0].task
    chosen_key = chosen.key

    # 组装 decision_record（格式同 plan()，方便 trace 记录）
    record_cands = []
    for cand in cands:
        key = cand.task.key
        ok, veto = ok_map[key]
        # Firefighting 不打分，统一给 0 分
        # 火情路径**不分层**（单候选，且 L_FIRE 的白名单为空、分层等于恒等变换），
        # 所以仍发 6 元组。trace 渲染出的 `chosen=True, in_top_layer=False` 不是
        # 矛盾：in_top_layer 只对分层路径有意义（spec §6）。
        record_cands.append((cand.task, 0.0, {}, ok, veto, key == chosen_key))

    return chosen, {"candidates": record_cands, "plate_match": {}, "cooker_match": {}}


def _record(candidates, plate_match, cooker_match):
    # type: (List, Dict, Dict) -> Dict
    """组装 decision_record：候选明细 + 每拍匹配关系（盘↔订单、灶↔订单）。

    匹配关系的 key/value 统一转 str，保证 json.dumps 可序列化
    （订单 id / 站点名在真实帧里是 str，但测试会混入 int）。
    """
    return {
        "candidates": candidates,
        "plate_match": {str(k): str(v) for k, v in (plate_match or {}).items()},
        "cooker_match": {str(k): str(v) for k, v in (cooker_match or {}).items()},
    }


def _score_tasks(tasks, candidates, world, bb, book=None, plate_match=None,
                 slack_map=None, cooker_match=None, layer=None):
    # type: (List[Any], List[Candidate], World, Blackboard, Optional[Any], Optional[Dict[Any, Any]], Optional[Dict[Any, float]], Optional[Dict[Any, Any]], Optional[int]) -> List[Tuple[Any, float, Dict]]
    """对可行任务打分（spec §6.4）。

    返回: [(task, score, terms), ...]
    terms 是各项分数的拆解，用于决策日志和审计。

    layer 非 None 时，分数只计该层白名单内的项（spec §7）；terms 仍全量，
    便于 trace 复盘「这一项算出来是多少、只是本层不用它」。
    """
    scored = []
    for task, cand in zip(tasks, candidates):
        score, terms = _score_candidate(world, bb, cand, book, plate_match,
                                        slack_map, cooker_match, layer)
        scored.append((task, score, terms))
    return scored


def _matching_preferences(candidate, bb, layer):
    # type: (Candidate, Blackboard, Optional[int]) -> List[Tuple[Any, float]]
    """命中候选、且未被免疫规则跳过的 (preference, weight)（spec §4.5）。

    - 过期的 preference 跳过（非零 `expires_tick` 且 `bb.tick >= expires_tick`，
      与 `prune_expired_preferences` 同界：到期那一拍即失效）；
    - 匹配走 schema.preference_matches_task 的优先级规则；
    - 负分在 L0/L1/L2 与救援类上整体跳过，正分照常生效。

    纯读：不在打分路径里删 preference（清理归 Task 12 / Agent）。
    """
    task = candidate.task
    tick = getattr(bb, "tick", 0) or 0
    immune = layer in (0, 1, 2) or task.TYPE in RESCUE_TYPES
    out = []  # type: List[Tuple[Any, float]]
    for pref in getattr(bb, "preferences", None) or ():
        if getattr(pref, "kind", None) == "now":
            continue            # now 是 trace-only 权重，唯一消费点是 _select_imperative
        expires = getattr(pref, "expires_tick", 0) or 0
        if expires and tick >= expires:
            continue
        if not preference_matches_task(pref, task):
            continue
        weight = float(getattr(pref, "weight", 0.0) or 0.0)
        if weight < 0 and immune:
            continue
        out.append((pref, weight))
    return out


def _preference_score(candidate, bb, layer):
    # type: (Candidate, Blackboard, Optional[int]) -> float
    """偏好权重项：命中候选的 role/claim/assign/direct 之和（spec §4.5）。"""
    return sum(weight for _pref, weight
               in _matching_preferences(candidate, bb, layer))


def _claim_penalty(candidate, bb, layer):
    # type: (Candidate, Blackboard, Optional[int]) -> float
    """候选身上的认领负分（spec §4.6 截止闸用）。

    只汇总负的 claim：direct 的「让 P2 做」不是认领冲突，不应影响「本层
    是否全被认领」的判断。
    """
    return sum(weight for pref, weight
               in _matching_preferences(candidate, bb, layer)
               if getattr(pref, "kind", None) == "claim" and weight < 0)


def _select_imperative(feasible, world, bb):
    # type: (List[Candidate], World, Blackboard) -> Optional[Candidate]
    """「马上去」硬指令闸（Task 2）：命中未过期的 now 偏好直接选中候选。

    找未过期的 now 偏好；没有则返回 None。任一 feasible 候选落在
    L0/L1/L2（火 / 快糊 / 刚熟救锅）→ 让位返回 None，交给正常分层路径；
    否则返回第一个命中 now scope 的 feasible 候选（feasible 顺序即链的
    下一步）。

    这是 now 的**唯一**消费点：`_matching_preferences` 已跳过 kind=="now"，
    weight=WEIGHT_NOW 从不进普通打分（trace-only 不变量保持干净）。
    """
    tick = getattr(bb, "tick", 0) or 0
    now_prefs = [p for p in (getattr(bb, "preferences", None) or ())
                 if getattr(p, "kind", None) == "now"
                 and (not getattr(p, "expires_tick", 0)
                      or tick < getattr(p, "expires_tick", 0))]
    if not now_prefs:
        return None
    # 「马上去」已经认下：把它**钉住**，别让普通打分（送餐等更高分任务）抢走。
    # 否则 now 任务每拍被更高分候选顶掉、永远做不成——trace-20261021-145504 的
    # 1-6 段正是 FetchToBoard(Cucumber)↔DeliverPlate 逐拍交替、角色在 (0,-3)↔(1,-3)
    # 来回卡。当前承诺已不可行（不在 feasible）才交回正常流程。
    commitment = getattr(bb, "commitment", None)
    current = getattr(commitment, "active_task", None) if commitment else None
    if current is not None and any(preference_matches_task(p, current)
                                   for p in now_prefs):
        for cand in feasible:
            if cand.task == current:
                return cand
        return None
    from ai.core.planner_layers import layer_of_task
    for cand in feasible:
        if layer_of_task(cand.task, world) in (0, 1, 2):
            return None
    for cand in feasible:
        for pref in now_prefs:
            if preference_matches_task(pref, cand.task):
                return cand
    return None


# ---------- 截止闸与层内 fallthrough（spec §4.6）----------

def _rescue_margin_ticks(task_type):
    # type: (Any) -> int
    """截止闸的余量（拍）：2.5 秒起步、长链封顶 5 秒（spec §4.6）。

    `D` 是「做完这个动作、再送到出餐口」的耗时。DeliverPlate 本身就是
    送餐，不再加送餐段；其它动作都要留出 `COST_DELIVER`。

    收窄到 2.5~5 秒是「P1 有把握时不抢、真放鸽子还抢得回来」的折中：
    窗口太宽会抢 P1 正在做的活，太窄则 P1 真放鸽子时来不及补救。
    """
    d = ETA.task_cost_ticks(task_type)
    if task_type != "DeliverPlate":
        d += ETA.COST_DELIVER
    return int(max(50, min(int(0.15 * d), 100)))


def _rescue_gate_open(candidate, cushion, bb, tick=None):
    # type: (Candidate, Optional[float], Blackboard, Optional[int]) -> bool
    """截止闸（spec §4.6）：还等得起就尊重认领，只有最后窗口才允许抢。

    `cushion` 是 `ETA.solo_slack_by_order` 的**独立 slack**（剩余拍 − 该单
    完整 solo ETA），不是剩余时间。不要再减一次 D——ETA 已经扣过了，重复
    扣会让闸永远不会开。

    `cushion < 0` 是必死单，不为它抢；`cushion` 为 None（算不出 / 订单
    不在 slack_map 里）同样关闸，宁可不抢也不要用错的量级。
    """
    if cushion is None:
        return False
    now = (getattr(bb, "tick", 0) or 0) if tick is None else tick
    if now <= getattr(bb, "rescue_cooldown_until", 0):
        return False
    margin = _rescue_margin_ticks(getattr(candidate.task, "TYPE", None))
    return 0 <= cushion < margin


def _effective_order_id(world, task):
    # type: (World, Any) -> Any
    """候选求值用的订单 id（spec §10，见 `_credited_order_id`）。

    送餐记给游戏实际会记账的那一单，不是 task 绑的那单；其它 task 用
    自己绑的 order_id。打分与截止闸必须共用这一条，否则闸会对着一个
    游戏根本不会结算的订单开。
    """
    order_id = getattr(task, "order_id", None)
    if getattr(task, "TYPE", None) == "DeliverPlate" and order_id is not None:
        credited = _credited_order_id(
            world.orders, _dish_of_order(world.orders, order_id))
        if credited is not None:
            return credited
    return order_id


def _candidate_cushion(candidate, world, slack_map):
    # type: (Candidate, World, Optional[Dict[Any, float]]) -> Optional[float]
    """候选对应订单的独立 slack（拍）；订单不在表里返回 None。

    不绑订单的候选（洗盘 / 填灶 / 预取等 housekeeping）取**最紧的活单**
    slack：它们服务于所有订单，玩家认领后迟迟不做时，截止闸要能按最急
    的那张单把活抢回来，否则「我来洗碗」之后盘子耗尽、全部订单一起饿死。
    slack < 0 的单不参与，与按单的口径一致。
    """
    oid = _effective_order_id(world, candidate.task)
    if oid is not None:
        return (slack_map or {}).get(oid)
    live = [v for v in (slack_map or {}).values()
            if v is not None and v >= 0]
    return min(live) if live else None


def _tightest_order(slack_map):
    # type: (Optional[Dict[Any, float]]) -> Any
    """slack 最小（且 slack >= 0）的订单 id；没有返回 None。"""
    live = [(v, oid) for oid, v in (slack_map or {}).items()
            if v is not None and v >= 0]
    return min(live, key=lambda r: r[0])[1] if live else None


def _rescue_hold_live(bb, task, tick=None):
    # type: (Blackboard, Any, Optional[int]) -> bool
    """这个 task 是否处在截止闸抢回后的保护期内（spec §4.6）。

    闸只在最后窗口开一瞬，冷却期内同一 task 的 claim 仍然有效。若没有
    保护期，下一拍 `_select_layer` 会把它排除、`_clear_claimed_commitment`
    又会按同一条 claim 清掉 active_task —— "抢回"只维持一拍。
    """
    if task is None:
        return False
    dialog = getattr(bb, "dialog", None)
    if dialog is None:
        return False
    key = getattr(dialog, "rescue_task_key", None)
    if key is None or getattr(task, "key", None) != key:
        return False
    now = (getattr(bb, "tick", 0) or 0) if tick is None else tick
    return now < (getattr(dialog, "rescue_hold_until", 0) or 0)


def _clear_rescue_hold(bb):
    # type: (Blackboard) -> None
    """释放抢回保护期。"""
    dialog = getattr(bb, "dialog", None)
    if dialog is None:
        return
    dialog.rescue_task_key = None
    dialog.rescue_hold_until = 0


def _release_stale_rescue_hold(bb):
    # type: (Blackboard) -> None
    """起拍清理：抢回的 task 已不是当前承诺（做完 / 失败 / 被换掉）或
    保护期已过 → 释放，让选择回到正常认领规则。"""
    dialog = getattr(bb, "dialog", None)
    if dialog is None or getattr(dialog, "rescue_task_key", None) is None:
        return
    commitment = getattr(bb, "commitment", None)
    active = getattr(commitment, "active_task", None) if commitment else None
    if not _rescue_hold_live(bb, active):
        _clear_rescue_hold(bb)


def _finalize_rescue_hold(bb, chosen_task):
    # type: (Blackboard, Optional[Any]) -> None
    """选完 task 之后：本拍没再选它 → 保护期结束（任务已完成或被换掉）。"""
    dialog = getattr(bb, "dialog", None)
    if dialog is None or getattr(dialog, "rescue_task_key", None) is None:
        return
    if chosen_task is None \
            or getattr(chosen_task, "key", None) != dialog.rescue_task_key:
        _clear_rescue_hold(bb)


def _select_layer(feasible, world, layer_map, bb, slack_map, tick):
    # type: (List[Candidate], World, Dict[Any, int], Blackboard, Optional[Dict[Any, float]], int) -> Tuple[Optional[int], List[Candidate]]
    """按层向下找第一个还有「未被认领」候选的层（spec §4.6）。

    层内 eligibility：
      - `_claim_penalty >= 0`（没有被认领/派活负分）；
      - 或者截止闸开着（本层全被认领、但有单进入最后窗口）；
      - 或者是闸抢回后仍处在保护期内的那个 task（闸/冷却已关，但它还没
        做完——否则抢回只维持一拍）。

    **逐层向下**，不是只掉一层：本层全部候选都被认领且没有单要丢时，
    继续看下一层，直到找到可用层或没有可行候选。

    返回 `(选中的层, 该层可用候选)`；全层都不可用返回 `(None, [])`。
    候选顺序保持 `feasible` 的原顺序——下游同分时靠稳定排序决定胜者。
    """
    by_layer = {}  # type: Dict[int, List[Candidate]]
    for cand in feasible:
        layer = layer_map.get(cand.task.key)
        if layer is None:
            continue
        by_layer.setdefault(layer, []).append(cand)

    for layer in sorted(by_layer):
        eligible = []  # type: List[Candidate]
        for cand in by_layer[layer]:
            if _claim_penalty(cand, bb, layer) >= 0:
                eligible.append(cand)
                continue
            if _rescue_hold_live(bb, cand.task, tick):
                eligible.append(cand)
                continue
            cushion = _candidate_cushion(cand, world, slack_map)
            if _rescue_gate_open(cand, cushion, bb, tick):
                eligible.append(cand)
        if eligible:
            return layer, eligible
    return None, []


def _clear_claimed_commitment(bb):
    # type: (Blackboard) -> None
    """硬意图命中当前承诺 → 立刻放弃当前 task（spec §4.6）。

    三种来源都算硬意图，不走 3 拍承诺惯性宽限：
      1. 玩家的 `claim` 负分命中（认领到了就停手）；
      2. 当前 task 落在账本 `stop` 的作用域里；
      3. 当前 task 属于被放弃的订单（`abandoned_orders`）。

    只清 `active_task` 与 `infeasible_streak`，**不替换 Commitment 对象**：
    Executor / Arbiter 持有它的引用并继续写 steps_done / retries，换成新对象
    会让那些写入落到孤儿上。已到期的 preference / 排除表与正分（direct
    的正向鼓励）不触发放弃。

    **L0–L2 救援动作免疫**：`RESCUE_TYPES`、`Extinguish`、
    `TakeCookedToPlate` 一律不清——玩家说「先别煮饭」不等于「饭要糊了也别
    管」（spec §4.5 末段）。

    **截止闸抢回保护期**：`bb.dialog` 里记着被闸抢回的 task key 时，它在
    保护期内同样不清——否则冷却期一开、同一条 claim 又命中，抢回的 task
    下一拍就没了（spec §4.6）。
    """
    commitment = getattr(bb, "commitment", None)
    active = getattr(commitment, "active_task", None) if commitment else None
    if active is None:
        return
    # 截止闸抢回的 task 在保护期内不吃 claim：闸只开一瞬、冷却期内
    # 同一条 claim 仍然有效，放开就会被清掉，"抢回"退化成装饰。
    if _rescue_hold_live(bb, active):
        return
    if _is_rescue_action(active):
        return
    if _is_stopped(active, bb) \
            or _order_is_abandoned(bb, getattr(active, "order_id", None)):
        commitment.active_task = None
        commitment.infeasible_streak = 0
        return
    tick = getattr(bb, "tick", 0) or 0
    for pref in getattr(bb, "preferences", None) or ():
        if getattr(pref, "kind", None) != "claim":
            continue
        if float(getattr(pref, "weight", 0.0) or 0.0) >= 0:
            continue
        expires = getattr(pref, "expires_tick", 0) or 0
        if expires and tick >= expires:
            continue
        if preference_matches_task(pref, active):
            commitment.active_task = None
            commitment.infeasible_streak = 0
            return


def _is_rescue_action(task):
    # type: (Any) -> bool
    """是否属于 L0–L2 的救援动作（不吃 claim/assign，也不被硬压制清承诺）。

    除 `RESCUE_TYPES` 外还要带上 `Extinguish`（L0 灭火）与
    `TakeCookedToPlate`（L1/L2 端熟料/救锅）——它们没有收进
    `RESCUE_TYPES`，但 spec §4.6 同样豁免。
    """
    task_type = getattr(task, "TYPE", None)
    return task_type in RESCUE_TYPES or task_type in (
        "Extinguish", "TakeCookedToPlate")


def _score_candidate(world, bb, candidate, book=None, plate_match=None,
                     slack_map=None, cooker_match=None, layer=None):
    # type: (World, Blackboard, Candidate, Optional[Any], Optional[Dict[Any, Any]], Optional[Dict[Any, float]], Optional[Dict[Any, Any]], Optional[int]) -> Tuple[float, Dict]
    """对单个候选打分（spec §6.4）。

    Plan 3 最小权重集：订单紧迫、时间压力、ETA
    Plan 4 新增：P1 冲突惩罚 + 6 项新权重（Task 4）
    """
    task = candidate.task
    terms = {}

    # 送餐的「有效订单」是游戏会记账的那一单，不是 task 绑的那个（实测 55/0：
    # 游戏挑同 dish 存活单里 remaining 最小者）。紧迫度 / 新鲜度 / 完成度都该
    # 按它求值——否则同一盘菜绑给不同 order_id 会算出不同分数，而实机行为完全
    # 一样，纯属自欺。见 _credited_order_id / _effective_order_id。
    effective_order_id = _effective_order_id(world, task)

    # ① 订单紧迫：读 slack（remaining - ETA）而非裸 remaining（第 3 组 3.1）。
    # 旧曲线 W_URGENCY/remaining 在 60s 处只有 0.08、100s 处 0.05，中段分不出
    # 先后，只有最后 1~2s 才爆到 5.0——整局 deadline 近乎不参与决策。
    # 只对「dish 会记到队首」的候选生效：队首快过期才催它，不催非队首——
    # 否则会去备一个暂时送不出去（combo 闸挡着）的单。
    # slack 为 None（算不出 / 订单无关）时退回 0：宁可不给紧迫分，也不要用
    # 一个错的量级去挤别的项。
    urgency_score = 0.0
    order = None
    slack = None   # type: Optional[float]
    if effective_order_id:
        head = _combo_head_id(world.orders)
        dish = _dish_of_order(world.orders, task.order_id)
        credited = _credited_order_id(world.orders, dish)
        if head is not None and credited == head:
            order = _find_order_by_id(world, effective_order_id)
            if order:
                slack = (slack_map or {}).get(effective_order_id)
                if slack is None:
                    slack = _fallback_slack(order)
                urgency_score = _urgency_from_slack(slack)
    terms["urgency"] = urgency_score

    # ② 时间压力
    pressure = _time_pressure(task, world)
    pressure_score = W_PRESSURE * pressure
    terms["pressure"] = pressure_score

    # ③ ETA（暂用 estimated_steps 的负数）
    eta = candidate.estimated_steps
    eta_score = -W_ETA * eta
    terms["eta"] = eta_score

    # ④ 异步资源优先（Plan 4 Task 4）
    # 仅当订单已有盘就位时才给下锅/切菜加分：没盘时它们的输出无处可去，
    # 提前备料会抢走放盘（实机：洗好碗后 P2 一直煮饭/切菜、不去端盘）。
    #
    # 这条曾经把「灶空转」钉死（没盘 → 不煮 → 米不够 → 单过期），但现在
    # 不再是问题：填灶走订单无关的 FillCooker（第 1 组 1.1），没有 order_id、
    # 不经过这一项，而是吃 cooker_idle 加分。所以这里保持收紧语义不动——
    # 它修的是另一个实机 bug，放宽会让那个 bug 复发。
    if _is_async_task(task) and order is not None \
            and _order_has_plate(plate_match, task.order_id):
        terms["async_resource"] = W_ASYNC_RESOURCE
    else:
        terms["async_resource"] = 0.0

    # ⑤ 完成度（Plan 4 Task 4）。曲线是凸的，见 _completion_bonus。
    # 按 effective_order_id 求值：送餐时它是 credited order，与 task 绑的
    # order_id 可能不同（见本函数开头的注释）。
    completion_ratio = _order_completion_ratio(task, world, bb, book,
                                               plate_match, effective_order_id)
    terms["completion_ratio"] = _completion_bonus(completion_ratio)

    # ⑥ 食材复用（Plan 4 Task 4）
    reuse_count = _ingredient_reuse_count(task, world, bb, plate_match, book)
    if reuse_count >= 2:
        terms["ingredient_reuse"] = W_INGREDIENT_REUSE * (reuse_count - 1)
    else:
        terms["ingredient_reuse"] = 0.0

    # ⑨ Combo：按屏幕队首依次送才累加，跳单或过期归零（第 3 组 3.3/3.4）。
    # 旧实现判的是「连续送同类菜」（比较 order["name"]），与游戏机制无关。
    # 关键：combo 由「送的是哪种**菜**」决定，不由 task 绑的 order_id 决定。
    # 游戏把这一盘记给「同 dish 存活单里 remaining 最小」的那个（实测 55/0），
    # 我们只能选菜。见 _credited_order_id 的注释。
    # 送餐与**备菜**都吃这个信号。只给送餐是不够的：实机 4577 拍里「料齐的
    # 盘」分布是 0 盘 4279 拍、1 盘 298 拍——从没有两盘同时料齐，送餐那一刻
    # 根本没有分叉，加分永远碰不到。真正的分叉在上游「现在该给哪一单备菜」，
    # 所以备菜也给（打折，见 COMBO_PREP_DISCOUNT）。
    combo_head = 0.0
    combo_break = 0.0
    is_deliver = (task.TYPE == "DeliverPlate")
    if task.order_id is not None and (is_deliver or task.TYPE in _COMBO_PREP_TYPES):
        head = _combo_head_id(world.orders)
        dish = _dish_of_order(world.orders, task.order_id)
        credited = _credited_order_id(world.orders, dish)
        mult = ECON.combo_multiplier(_current_combo(world) + 1)
        scale = 1.0 if is_deliver else COMBO_PREP_DISCOUNT
        if head is not None and credited is not None and credited == head:
            # 这种菜会记在队首头上 → 连击能接上。
            # 连击越高、保住它越值钱（combo 2→3 系数翻倍，远超一次备菜）
            combo_head = W_COMBO_HEAD * min(1.0, mult / 2.0) * scale
        elif head is not None:
            # 这种菜会记给别人、把连击打断 → 罚。
            combo_break = -W_COMBO_BREAK * scale
    terms["combo_head"] = combo_head
    terms["combo_break"] = combo_break

    # ⑨.5 新鲜度：送得越早小费档位越高（8/5/3）。权重刻意取小——实测只值
    # 5 分（相对 base 60 仅 8%），单独追新鲜度不值得牺牲任何东西。读「距掉档
    # 还有几秒」而非线性 remaining：档位是台阶，离边界远时早送晚送没区别。
    freshness = 0.0
    if task.TYPE == "DeliverPlate" and order is not None:
        tier = ECON.tip_tier(order.get("remaining"), order.get("lifetime"))
        span = float(ECON.TIP_FRESH - ECON.TIP_LATE) or 1.0
        freshness = W_FRESHNESS * (tier - ECON.TIP_LATE) / span
    terms["freshness"] = freshness

    # ⑩ 截止前瞻（Plan 4 Task 4；口径改为 slack，见第 2 组 2.4）。
    # **必须乘 W_DEADLINE_LOOKAHEAD**：_deadline_forecast 返回 0~1 的系数，
    # 不乘权重的话这一项的实际量级是 1.0 而非 10.0，比 W_COOKER_IDLE=5.6
    # （Ruling 12 重标后的值，原文的 4.8 已过期）小一个数量级——等于这条
    # 前瞻从未参与决策（spec §10）。
    deadline_penalty = _deadline_forecast(task, candidate, world, bb, slack,
                                          cooker_match)
    terms["deadline_lookahead"] = -W_DEADLINE_LOOKAHEAD * deadline_penalty

    # ⑪ 手部前置的腾手代价 + 收尾加分（spec §6.1(b)）：按候选 preconditions
    # 精确判，而非笼统的 HANDS。手持目标物（holding_cooked / holding_plated_dish）
    # 时任务已在「收尾」，给正向加分，别让异步任务（FetchToCooker / Chop）抢走。
    hand_penalty = 0.0
    hand_bonus = 0.0
    chef = P.chef_by_name(world.chefs, P.P2_NAME)
    pre = candidate.preconditions or []
    if chef is not None:
        need_clear = False
        need_pickup = False
        holding_ok = False
        if "hands_empty" in pre:
            need_clear = not P.hands_free(chef)
        elif "holding_cooked" in pre:
            holding_ok = (P.held_cookware_state(
                chef, P.held_items(world), world.recipes) == P.COOKWARE_COOKED)
            need_pickup = not holding_ok
        elif "holding_plated_dish" in pre:
            holding_ok = P.held_is_plate(chef)
            need_pickup = not holding_ok
        elif getattr(task, "HANDS", "any") == "empty":
            need_clear = not P.hands_free(chef)
        if need_clear:
            hand_penalty = W_ETA * HAND_CLEAR_STEPS
        elif need_pickup:
            hand_penalty = W_ETA * HAND_PICKUP_STEPS
        elif holding_ok:
            hand_bonus = W_ASYNC_RESOURCE
    terms["hands"] = hand_bonus - hand_penalty

    # ⑫ 搬料惩罚：FetchToBoard 的来源若是"另一块板"，它是"搬"而非"取"，
    # 零产出——让 Chop 这类有产出的动作优先。
    move_penalty = 0.0
    if task.TYPE == "FetchToBoard":
        loc = find_ingredient(world, task.item)
        if loc is not None and loc.get("kind") == "board":
            move_penalty = W_ETA * MOVE_PENALTY_STEPS
    terms["move_penalty"] = -move_penalty

    # ⑬ 送餐优先（先上得分更高）：料齐的盘尽快送出去，别被继续备菜压住。
    # DeliverPlate.feasible 已经保证"盘料齐"，所以这里是硬加分。
    if task.TYPE == "DeliverPlate":
        terms["deliver_bonus"] = W_DELIVER_BONUS
    else:
        terms["deliver_bonus"] = 0.0

    # ⑭ 洗盘压力：干净空盘越少越该洗（提前补盘，别等到用光拼装被卡死）。
    if task.TYPE == "WashPlate":
        terms["plate_pressure"] = W_PLATE_PRESSURE * _plate_pressure(world)
    else:
        terms["plate_pressure"] = 0.0

    # ⑮ 1-3 关卡 relay 加分：让 P2 倾向投掷/中转（支线），但不盖过端锅/送餐。
    if world.level == "Sushi_1_3_2P" and task.TYPE in _RELAY_BONUS_1_3:
        terms["relay_1_3_bonus"] = _RELAY_BONUS_1_3[task.TYPE]
    else:
        terms["relay_1_3_bonus"] = 0.0

    # ⑯ 填灶（第 1 组 1.4）：按熟料缺口加分。只给 FillCooker——按单发射的
    # FetchToCooker 已有 async_resource 加分，不重复给。
    if task.TYPE == "FillCooker":
        shortfall = CS.cooked_shortfall(world, book, task.item) \
            if book is not None else 0
        terms["cooker_idle"] = _cooker_idle_bonus(shortfall)
    else:
        terms["cooker_idle"] = 0.0

    # ⑰ 摆盘压力：一块盘都没就位 = 拼装链无落点，和「0 干净盘」等价地卡死。
    # 判据用 plate_match 而不是「盘的数量」：盘在晾干架/盘堆里一样倒不进料，
    # 只有进了匹配表（落在 counter 拼装格或 P2 手上）的盘才算就位。
    # plate_match 为 None 是「没有匹配信息」而不是「没盘」（同
    # `_order_has_plate` 的约定），那时不给加分——宁可少一项，也不要凭
    # 缺失的信息把摆盘抬到 5 分。
    if task.TYPE == "PlaceCleanPlate" and plate_match is not None \
            and not plate_match:
        terms["no_plate_placed"] = W_NO_PLATE_PLACED
    else:
        terms["no_plate_placed"] = 0.0

    # ⑱ 对话偏好（spec §4.5）：role/claim/assign/direct 的权重之和。
    # 进 terms 后无论 layer 是否启用都由白名单决定是否参与本层打分；
    # layer=None（扁平老路）时照常全量求和。
    terms["pref"] = _preference_score(candidate, bb, layer)

    # 总分 = terms 的和。**不要**改回手写加法：旧版把 16 项重抄一遍、还混用
    # 局部变量与 terms[...]，加一项要改两处；漏掉求和那处的症状是「trace 里
    # 有这一项但它不影响选择」，而单测断言的是 terms、一个都不会报警。
    # 推论：凡进 terms 的都必须是已带符号的最终贡献（惩罚项存负数）。
    #
    # layer 非 None 时只算该层白名单内的项（spec §7）。terms 仍然全量返回
    # ——trace 要能看见「这一项算出来是多少、只是本层不用它」。layer 为
    # None（默认）时与分层前完全一致：全项求和。
    if layer is not None:
        from ai.core.planner_layers import filter_terms
        score, _kept = filter_terms(terms, layer)
        return score, terms
    return sum(terms.values()), terms


# slack 的分段线性区间（拍）：余量大于 SLACK_CALM 不贡献紧迫分，
# 小于等于 0 吃满分，中间线性。90 秒 ≈ 2.5 份寿司的工时，是「还能从容安排」
# 与「必须盯着它」的分界；实测一份寿司约 35~40 秒手上工时（见 ai/eta.py）。
SLACK_CALM_TICKS = 90 * 20


def _fallback_slack(order):
    # type: (Dict) -> float
    """没有 slack_map（旧调用点 / 单测直接调打分）时的退化估计。

    用「remaining 减去一份菜的典型工时」近似，而不是把 remaining 当 slack：
    后者会让所有单看起来都很从容。典型工时取 ETA 模块实测的一份寿司
    （约 37 秒手上工时）。
    """
    try:
        remaining = float(order.get("remaining"))
    except (TypeError, ValueError):
        return float(SLACK_CALM_TICKS)
    return remaining * ETA.TICKS_PER_SECOND - TYPICAL_DISH_TICKS


# 一份菜的典型手上工时（拍）。实测两个成功送达单是 781 / 697 拍（39s / 35s），
# 取 740 拍≈37 秒。只用于没有 slack_map 的退化路径。
TYPICAL_DISH_TICKS = 740


def _urgency_from_slack(slack_ticks):
    # type: (float) -> float
    """按 slack 给紧迫分（第 3 组 3.1）。

    旧曲线 `W_URGENCY / max(remaining,1)` 形态是错的：remaining=60s 只有
    0.08、100s 只有 0.05，分不出先后；只有跌到 1~2s 才冲到 2.5~5.0。于是整
    局 deadline 对决策的影响≈0，直到最后 20 拍突然把人拽过去——实机 t=2755
    为只剩 0.9s 的 order#3 放黄瓜（必死），而 #4/#5 全程饿死。

    新曲线读 slack 而非裸 remaining：slack 小才叫紧迫；remaining 小但工序也
    少的单其实不急。分段线性保证中段（40~90s）也有可比的梯度。

    这条曲线曾被 `_doom_penalty` 复用一份——两者**同向**（同一公式、同一
    形状，只差一个 1/5 的系数），导致同一事实计两次分（见 `_deadline_forecast`
    已移除的 ②）：一份接成加分项（urgency），另一份接成惩罚项（deadline
    penalty），曲线本身没有反向，是两处**用法**的符号相反，净效果才等于
    「紧迫度越高总分越低」。那个函数已删除——若将来要给「订单将到期」加独立
    惩罚，必须设计**不同形状**的曲线，不要再复用本函数。
    """
    if slack_ticks >= SLACK_CALM_TICKS:
        return 0.0
    if slack_ticks <= 0:
        return W_URGENCY
    return W_URGENCY * (1.0 - float(slack_ticks) / SLACK_CALM_TICKS)


def _cooker_idle_bonus(shortfall):
    # type: (int) -> float
    """填灶加分：按熟料缺口线性抬升，到 COOKER_SHORTFALL_SATURATION 封顶。

    缺 1 份和缺 5 份不该同分（前者可以等，后者是产能崩了）；但也不能无限
    加分，否则开局 5 单全要米时会盖过送餐与灭火。
    """
    if shortfall <= 0:
        return 0.0
    ratio = min(1.0, float(shortfall) / COOKER_SHORTFALL_SATURATION)
    return W_COOKER_IDLE * ratio


def _plate_pressure(world):
    # type: (Any) -> float
    """干净空盘稀缺度：0=充足，越大越该洗。0 块=2.0（拼装被卡死）、1 块=1.0、
    ≥2 块=0.0（不洗）。"""
    clean = P.clean_plate_count(world.plates)
    if clean <= 0:
        return 2.0
    if clean == 1:
        return 1.0
    return 0.0


def _placed_plates(world):
    # type: (Any) -> bool
    """是否有盘落在 counter 拼装格上（world 级；spec §A.3）。

    与 ai/tasks/base.placed_plates(ctx) 同口径，但按 world 重写：拼装格由
    stations 里 t=="counter" 且带 gx/gz 的站点给出，盘用 to_grid 反解网格
    坐标后比对。只认 world.plates（非手持）：还在盘堆/晾干架/手上的盘不算
    「已就位」，不能往上倒料。
    """
    counters = set()
    for st in world.stations:
        if st.get("t") != "counter":
            continue
        gx, gz = st.get("gx"), st.get("gz")
        if gx is not None and gz is not None:
            counters.add((gx, gz))
    for p in world.plates:
        x, z = p.get("x"), p.get("z")
        if x is None or z is None:
            continue
        if world.to_grid(x, z) in counters:
            return True
    return False


def _cooker_lookup(world, cid):
    # type: (World, Any) -> Optional[Dict]
    """按资源 id 在 world.cookers 里查灶（spec §5.2：cid 可能是
    (grid,name) 元组，也可能是裸字符串——与 `_unpack_resource_id` 同一套
    解包规则，不另造一套）。散锅（台面/地上）不在 world.cookers 里，查
    不到属预期，调用方按 None 退回散锅压力。"""
    grid, name = _unpack_resource_id(cid)
    for c in world.cookers:
        if not P.station_name_matches(str(c.get("n") or ""), name):
            continue
        if not _grid_matches(c.get("grid"), grid):
            continue
        return c
    return None


def _loose_pot_pressure(world, source_id):
    # type: (Any, str) -> float
    """散锅（台面/地上的锅）的时间压力。灶上查不到时走这条退路。

    散锅不在灶上、不走烹饪进度，帧里没有 cooking_progress，所以这里只能给
    二元压力——与 `_time_pressure` 里「无连续进度（旧帧/空锅）→ 退回二进制」
    那条退路同口径：
      糊了   → 2.0（L1，还要灭火，比刚熟的更急）
      熟了   → 1.0（L2，马上去盛）
      生/空  → 0.0

    判据与 `TakeCookedToPlate._source` 完全一致（rich token 或 cooked 字段，
    两条任一成立），刻意不另起一套：那边认得源、这边算不出压力，正是实机
    那 1979 拍的成因。
    """
    for it in (getattr(world, "items", None) or ()):
        if str(it.get("n")) != source_id:
            continue
        if P.cooker_burning(it) or str(it.get("cooked") or "").lower() in (
                "burned", "burnt", "burning"):
            return 2.0
        toks = parse_plate_detail(it.get("detail") or [])
        if any(str(t).startswith("cooked:") for t in toks):
            return 1.0
        if str(it.get("cooked") or "").lower() == "cooked":
            return 1.0
        return 0.0
    return 0.0


def _time_pressure(task, world):
    # type: (Any, World) -> float
    """时间压力曲线（spec §6.4）：锅里的东西熟了/糊了，去取它 / 救它的分数要高。

    spec §5.2 的判据是"cooked → 取出装盘，**分数随'快糊了'上升**"；
    §6.4 把它列为"时间压力曲线"，且明确是"初版只启用 订单紧迫 / 时间压力 /
    ETA 三项"之一。

    旧帧插件只报 cooked/burning/onFire，做不出连续曲线；现在插件推
    cooking_progress / cooking_time（同一单位 float），据此做连续曲线：
      生 progress < time、熟 time <= progress < 2*time、糊 progress > 2*time
    ratio = progress / (2*time)：0.5=刚熟、1.0=快糊。压力从刚熟 1.0 线性升到
    快糊 2.0，让「快糊了」能跨过承诺惯性门槛、第一时间去端锅；煮到一半
    （ratio 0.25）起又加一段 0→1.0 的「快熟」渐变，让端盘盛饭能提前去准备。
    """
    task_type = getattr(task, "TYPE", "")
    # 只有"取熟料 / 救熟料 / 清糊锅"这类灶状态相关任务才吃时间压力
    if task_type not in ("TakeCookedToPlate", "RescueCooked", "Discard"):
        return 0.0
    cid = getattr(task, "cooker_id", None)
    if cid is None:
        return 0.0
    c = _cooker_lookup(world, cid)
    if c is None:
        # 锅被端下灶（RescueCooked 的产物）后仍然能盛料——`TakeCookedToPlate.
        # _source` 早就认散锅，是这里漏了，于是压力归零、候选掉到 L7 清理层。
        # 实机 trace-20261015-165358：t=1657 救下来的那口熟米锅躺到收盘
        # 1979 拍（99 秒）没人碰，候选 184 拍都是 ok=True/layer=7/top_layer=6
        # ——可行、分数算出来了，但层比任何备菜低一级，词典序下结构上选不中。
        # 同期 #4/#5/#7 三单都只缺这份米，#4/#5 过期。
        _, cid_name = _unpack_resource_id(cid)
        return _loose_pot_pressure(world, cid_name)

    prog = c.get("cooking_progress")
    ctime = c.get("cooking_time")
    if prog is None or ctime is None:
        # 无连续进度（旧帧/空锅）→ 退回二进制
        return 1.0 if (P.cooker_cooked(c) or P.cooker_burning(c)) else 0.0
    try:
        prog = float(prog)
        ctime = float(ctime)
    except (TypeError, ValueError):
        return 1.0 if (P.cooker_cooked(c) or P.cooker_burning(c)) else 0.0
    if ctime <= 0:
        return 1.0 if (P.cooker_cooked(c) or P.cooker_burning(c)) else 0.0

    ratio = prog / (2.0 * ctime)   # 0=生, 0.4=刚熟(策略), 1.0=快糊
    if ratio >= 1.0:
        return 2.0                 # 已糊/快糊，满压
    if ratio >= 0.4:
        return 1.0 + (ratio - 0.4) / 0.6   # 刚熟→快糊，压力 1.0→2.0
    # 快熟阶段（0.15=煮约三成 → 0.4=刚熟）：压力 0→1.0 渐变，让 TakeCookedToPlate
    # 提前去端盘、站到锅前等，而不是等熟了才动（端盘盛饭前置了取盘动作，
    # 等熟了才去拿盘会来不及，实机烧糊）。「刚熟」策略点从 0.5（实际煮熟）再
    # 提前到 0.4，让 AI 在真的煮熟前约 1 秒就进入救料层、更早去就位。
    if ratio >= 0.15:
        return (ratio - 0.15) / 0.25   # 0→1.0 覆盖 ratio 0.15→0.4
    return 0.0                     # 刚下锅，不急


def _find_order_by_id(world, order_id):
    # type: (World, Any) -> Optional[Any]
    """从 world.orders 中查找订单。"""
    for order in world.orders:
        if order.get("id") == order_id:
            return order
    return None


# ==================== 承诺惯性实现（Plan 3 Task 6）====================

def _apply_commitment_inertia(scored, world, bb):
    # type: (List[Tuple[Any, float, Any]], World, Blackboard) -> Optional[Any]
    """承诺惯性：除非完成/失败/连续不可行/出现显著更高分者，否则不改选（spec §6.5）。

    两条规则：
    1. 进度加成：已完成的步数越多，改选门槛越高
    2. 不可行迟滞：连续 N 拍不可行才允许切换

    Task 5 新增：承诺穿插（Commitment Interleaving）
    3. 穿插条件：当前 task RUNNING、剩余步数 > INTERLEAVE_THRESHOLD、新 task 分数显著更高
    4. 穿插时暂停当前 task 到 bb.paused_task，完成后恢复

    返回: 选中的 task（可能是当前承诺或新选择）
    """
    if not scored:
        return None

    # 按分数降序排序
    scored_sorted = sorted(scored, key=lambda x: x[1], reverse=True)
    top_task, top_score, top_terms = scored_sorted[0]

    # ① 优先恢复 paused_task（Task 5）
    if bb.paused_task:
        # 查找 paused_task 是否在可行候选中
        paused_task_id = bb.paused_task.get("task_id")
        for task, score, terms in scored:
            if task.key == paused_task_id:
                # 恢复暂停的任务，并保留暂停前的进度。bb.commitment 可能是 None：
                # 穿插的那个 task 刚在 agent.py ⑬ 成功/失败，commitment 被清成了
                # None（而非测试里模拟的 active_task=None），直接 .steps_done 会
                # AttributeError。这里先把 commitment 恢复成指向被暂停 task 的有效
                # 对象，再写回进度。
                if bb.commitment is None:
                    bb.commitment = Commitment()
                bb.commitment.active_task = task
                bb.commitment.steps_done = bb.paused_task.get("steps_done", 0)
                bb.paused_task = None  # 清空 paused_task
                return task

        # 如果 paused_task 不可行了，清空并继续正常流程
        bb.paused_task = None

    # ② 检查是否有当前承诺
    if not bb.commitment or not bb.commitment.active_task:
        # 无承诺，选最高分
        return top_task

    current_task = bb.commitment.active_task

    # 熟/快糊/已糊的锅要第一时间去端/救，跳过承诺惯性。光靠把 pressure 加进
    # 分数不够：pressure 把 TakeCookedToPlate 拉到 ~1-2，但当前 FetchToCooker /
    # FetchToPlate 也会被 async_resource / ingredient_reuse / completion_ratio
    # 抬到 ~0.5-1.4，分差 < COMMITMENT_MARGIN，于是锅都熟了还赖在当前 task 上
    # 不去端盘（实机复现：米熟了 P2 还在给下一单下锅，不去盛饭）。
    # pressure >= 1.0 即「已熟」（ratio 0.5），正是该立刻去盛饭的点。
    # 只有「当前任务本身不是在处理热锅」时才用这条紧急切换去救锅。若已经
    # 在端/救一锅熟饭（current 的 pressure >= 1.0），就别因为另一个同类 task
    # （换盘 / 换锅）把当前执行 yank 掉——丢了手里的盘，原来那锅也可能烧糊。
    if (_time_pressure(top_task, world) >= 1.0
            and not _tasks_equal(top_task, current_task)
            and _time_pressure(current_task, world) < 1.0):
        return top_task

    # 在候选中查找当前承诺的 task
    current_entry = None
    for task, score, terms in scored:
        if _tasks_equal(task, current_task):
            current_entry = (task, score, terms)
            break

    # ③ 当前 task 已不在可行候选中 → 检查不可行连续性
    if current_entry is None:
        bb.commitment.infeasible_streak += 1
        if bb.commitment.infeasible_streak < INFEASIBLE_THRESHOLD:
            # 未达阈值，保持承诺（即使不可行，仍返回当前 task）
            return current_task
        else:
            # 连续不可行超过阈值，放弃承诺，选最高分
            bb.commitment.infeasible_streak = 0
            return top_task

    # ④ 当前 task 仍可行 → 重置不可行计数
    bb.commitment.infeasible_streak = 0
    current_task_obj, current_score, current_terms = current_entry

    # ⑤ Task 5: 检查是否允许穿插
    est_steps = _estimate_task_steps(current_task_obj)
    remaining_steps = est_steps - bb.commitment.steps_done

    # 穿插条件判断
    if (remaining_steps > INTERLEAVE_THRESHOLD and
        _task_is_interruptible(current_task_obj) and
        top_score - current_score > (COMMITMENT_MARGIN + INTERLEAVE_PENALTY)):

        # 穿插：保存当前 task 到 paused_task
        bb.paused_task = {
            "task_id": current_task_obj.key,
            "steps_done": bb.commitment.steps_done
        }
        return top_task  # 执行新任务

    # ⑤.5 不可打断的 task 一律保持承诺（Discard = 倒掉还在烧的锅）。
    # 这一条原先只写在上面的"穿插"分支里，而下面的**常规改选**分支没查——
    # 于是分差够大时 Discard 照样会被普通备菜抢走，不可打断形同虚设。
    # 以前分差很少够大（urgency 旧曲线在中段恒 ~0.1），所以没暴露；
    # urgency 改读 slack 后中段能到 4+，这个洞立刻就会被踩到
    # （tests/test_planner_multi.py::test_no_interleave_firefighting）。
    if not _task_is_interruptible(current_task_obj):
        return current_task_obj

    # ⑥ 计算进度加成（常规承诺惯性）
    progress_ratio = bb.commitment.steps_done / max(est_steps, 1.0)
    progress_bonus = progress_ratio * PROGRESS_BONUS_PER_STEP
    threshold = COMMITMENT_MARGIN + progress_bonus

    # ⑦ 若 top 分数未显著高于 current，保持承诺
    if top_score - current_score < threshold:
        return current_task_obj

    # 否则改选最高分
    return top_task


def _tasks_equal(task1, task2):
    # type: (Any, Any) -> bool
    """判断两个 task 是否相同（基于四元身份）。"""
    if task1 is None or task2 is None:
        return task1 == task2
    # Task 基类已经实现了 __eq__ 基于 key（四元身份）
    return task1 == task2


def _estimate_task_steps(task):
    # type: (Any) -> int
    """估计 task 的步数（使用 Plan 3 固定估计）。"""
    task_type = getattr(task, "TYPE", None)

    if not task_type:
        return 10  # 默认值

    # 根据任务类型返回固定估计步数
    if "Chop" in task_type:
        return EST_CHOP
    elif "Cook" in task_type or "TakeCooked" in task_type or "FetchToCooker" in task_type:
        return EST_COOK_OPS
    elif "Deliver" in task_type or "Serve" in task_type:
        return EST_SERVE
    else:
        return EST_FETCH


def _task_is_interruptible(task):
    # type: (Any) -> bool
    """检查任务是否可被穿插打断（Task 5）。

    某些任务不允许被打断：
    - Discard: 灭火/倒垃圾必须立即完成
    - 其他时效性强的清理任务

    返回: True 表示可打断，False 表示不可打断
    """
    task_type = getattr(task, "TYPE", None)
    if not task_type:
        return True  # 默认可打断

    # 不可打断的任务类型
    non_interruptible = ["Discard"]  # 灭火/倒垃圾

    for pattern in non_interruptible:
        if pattern in task_type:
            return False

    return True  # 默认可打断


# ==================== Plan 4 Task 4: 新增打分辅助函数 ====================

def _is_async_task(task):
    # type: (Any) -> bool
    """检测是否为异步资源任务（spec §6.8 异步资源优先）。

    异步任务：下锅（FetchToCooker）、切菜（Chop）等启动后可自动运行的任务。
    同步任务：取盘（PlaceCleanPlate）、送餐（DeliverPlate）等需要人等待的任务。
    """
    task_type = getattr(task, "TYPE", "")
    # FetchToCooker: 下锅后煮饭自动进行（20秒）
    # Chop: 切菜后自动完成
    async_types = ["FetchToCooker", "Chop"]
    return any(t in task_type for t in async_types)


def _completion_bonus(ratio):
    # type: (float) -> float
    """完成度加分。**凸**曲线：最后一样料远比第一样值钱。

    为什么不能线性：线性下 2/3 比 1/3 只多 W_COMPLETION_RATIO*0.33 ≈ 0.33，
    而同批候选里 urgency 的组间差就有 0.35，正好把它抵掉。实机
    trace-20261014-204822 t=1353：order#1 的盘已有米+海苔（2/3，差一样黄瓜就
    能送）却以 2.24 输给「往 1/3 的盘加第二样」的 2.69 —— 于是五张盘各做一半、
    全部烂尾，本局收盘挂着 5 单、只送出 2 单。

    凸性的依据是收益结构本身：盘不齐一分不得，齐了就是 60 分 base。离齐
    一步之遥的那一手是**兑现**，中间那些手只是**投资**，不该等价。

    取平方（ratio^2 * W * 3）：0 / 1/3 / 2/3 → 0 / 0.33 / 1.33，最后一步的
    增益（1.0）是第一步（0.33）的 3 倍，且 2/3 与 1/3 的差距 1.0 显著超过
    urgency 的典型组间差。系数 3 让满档仍落在 W_COMPLETION_RATIO*3 = 3.0，
    与 W_DELIVER_BONUS 同量级 —— 盘齐了就该去送，而不是继续加料。
    """
    if ratio <= 0:
        return 0.0
    return W_COMPLETION_RATIO * 3.0 * float(ratio) * float(ratio)


def _order_completion_ratio(task, world, bb, book=None, plate_match=None,
                            order_id=None):
    # type: (Any, World, Blackboard, Optional[Any], Optional[Dict[Any, Any]], Optional[Any]) -> float
    """计算订单完成度（spec §6.8 完成度）：盘里已就位的食材占总需求的比例。

    order_id 显式传入时按它求值，缺省用 task.order_id。**送餐必须传
    credited order**：游戏把一盘菜记给「同 dish 存活单里 remaining 最小」的
    那个（实测 55/0），与 task 绑谁无关，所以同一盘菜绑给不同 order_id 必须
    算出相同完成度——否则只是 trace 上的 order_id 变好看了（spec §10）。

    两处曾经让这个权重**在实机上恒为 0**（只剩单测里好看）：

      1. 盘内容读的是 `plate["item"]`——帧里的字段叫 `contents`（"item" 只
         存在于早期单测的夹具里，那个夹具把盘写成 stations 的一项）；
      2. 判定用字面比较。盘里记的是**装盘态词汇**（熟料在 detail 里是
         rich token、旧帧是不透明 token，见 RecipeBook.plate_tokens），
         字面比较对熟料永远不成立。故有 `book` 时必须走 `book.plate_has`。

    book 缺省时按 world 现造一本（单测的调用点不传 book；生产路径由
    select_task 一路带下来，不会重复造）。
    """
    oid = order_id if order_id is not None else task.order_id
    if not oid:
        return 0.0

    order = _find_order_by_id(world, oid)
    if not order:
        return 0.0

    composition = order.get("composition", [])
    if not composition:
        return 0.0

    # 查找该订单对应的盘子
    plate = _find_plate_for_order(oid, world, plate_match)
    if not plate:
        return 0.0

    if book is None:
        from ai.query.recipes import RecipeBook
        book = RecipeBook(world.recipes, world.stations)

    filled_count = 0
    for ingredient in composition:
        if book.plate_has(plate, ingredient):
            filled_count += 1

    return float(filled_count) / len(composition)


def _ingredient_reuse_count(task, world, bb, plate_match=None, book=None):
    # type: (Any, World, Blackboard, Optional[Dict[Any, Any]], Optional[Any]) -> int
    """统计食材复用次数（spec §6.8 食材复用）。

    返回：该食材**还能**同时满足多少个订单的需求（已经摆好的不算）。

    这里踩过与 `_order_completion_ratio` 完全相同的两个坑，修法也相同：

      1. 盘内容读的是 `plate["item"]`——帧里的字段叫 `contents`。于是
         "盘已有该料"永不成立，计数恒等于「所有要这料的订单数」；
      2. 字面比较对熟料不成立（盘里记的是装盘态词汇），故有 `book` 时
         必须走 `book.plate_has`。

    实机后果（trace-20261014-011202）：Seaweed 的 reuse 恒为 4，拿满
    `W_INGREDIENT_REUSE*(4-1)=1.5~2.0` 分，压过 FetchToCooker 的异步
    2.0 分（后者还要扣更高的 ETA）。于是 P2 挨个盘摆海苔、锅一直空着，
    多个订单都要米饭却只下了第一锅。
    """
    task_type = getattr(task, "TYPE", "")

    # 只对取料类任务计算复用
    if "FetchToBoard" not in task_type and "FetchToPlate" not in task_type:
        return 0

    # 提取任务要取的食材
    ingredient = getattr(task, "item", None)
    if not ingredient:
        return 0

    if book is None:
        from ai.query.recipes import RecipeBook
        book = RecipeBook(world.recipes, world.stations)

    # 统计有多少个订单还缺这个食材
    count = 0
    for order in world.orders:
        composition = order.get("composition", [])
        if ingredient not in composition:
            continue
        plate = _find_plate_for_order(order.get("id"), world, plate_match)
        if plate is None:
            # 没有盘也算需要：料取回来总有盘可放
            count += 1
            continue
        # 只有盘子还缺这个食材时才计数（装盘态词汇，不做字面比较）
        if not book.plate_has(plate, ingredient):
            count += 1

    return count


def _current_combo(world):
    # type: (World) -> int
    """当前连击数。残帧/单测缺 score 时按 0（最保守）。"""
    for src in (getattr(world, "score", None),
                (getattr(world, "raw", None) or {}).get("score")):
        if isinstance(src, dict) and src.get("combo") is not None:
            try:
                return int(src["combo"])
            except (TypeError, ValueError):
                return 0
    return 0


def _order_sort_key(order):
    # type: (Dict) -> Tuple[int, Any]
    """订单 id 的数值序（非数字 id 排在后面，按字符串序）。"""
    oid = order.get("id")
    try:
        return (0, int(oid))
    except (TypeError, ValueError):
        return (1, str(oid))


def _combo_head_id(orders):
    # type: (Optional[List[Dict]]) -> Any
    """屏幕队首订单的 id：**id 数值最小**者（UI 槽位序）。

    判据不是 remaining 最小。两个假设在全部 9 局 57 次送达上对比过：
        A) remaining 最小（存活最久）→ 43 一致 / 14 不一致
        B) id 数值最小（UI 槽位序）  → 57 一致 /  0 不一致
    B 是唯一无反例的解释。两者会分歧，是因为订单过期后 id 会被复用到新单上，
    此时"id 小"的那单反而 remaining 很大——真实帧 t=3000 的存活单是
    #3 rem=117.9、#4 rem=127.9、#5 rem=7.9，队首是 #3 而不是 #5。
    """
    live = [o for o in (orders or ()) if o.get("id") is not None]
    if not live:
        return None
    return min(live, key=_order_sort_key).get("id")


def _credited_order_id(orders, dish):
    # type: (Optional[List[Dict]], Any) -> Any
    """送出一盘 `dish` 时，游戏会把它记给哪一单。

    **这是实测事实，不是我们能选的**（55/0 无反例）：游戏挑「同 dish 的存活
    单里 remaining 最小」的那个记账，与 AI 把 task 绑给哪个 order_id 无关。

    这条结论改变了 combo 该怎么建模。我们唯一的杠杆是**选择送哪种菜**，而
    不是"选择送哪一单"：
        combo 递增 <=> _credited_order_id(orders, dish) == _combo_head_id(orders)
    上式在 57 次送达上 57/0 一致。

    所以 `DeliverPlate(order#X)` 里的 X 对 combo 毫无影响——按 order_id 给
    队首加分是错的（曾经那样写过：离线复验 t=3226 会"改选 DeliverPlate
    (order#3)"，但盘里装的是 Cucumber，游戏照样记给同 dish 里 remaining 最
    小的单，结果与旧版一字不差，只是 trace 上的 order_id 变了，自欺欺人）。
    """
    same = [o for o in (orders or ())
            if o.get("id") is not None and o.get("dish") == dish]
    if not same:
        return None
    return min(same, key=lambda o: _remaining_of(o)).get("id")


def _dish_of_order(orders, order_id):
    # type: (Optional[List[Dict]], Any) -> Any
    for o in (orders or ()):
        if o.get("id") == order_id:
            return o.get("dish")
    return None


def _deadline_forecast(task, candidate, world, bb, slack_ticks=None,
                       cooker_match=None):
    # type: (Any, Candidate, World, Blackboard, Optional[float], Optional[Dict[Any, Any]]) -> float
    """截止前瞻（spec §6.8）。返回惩罚系数（0.0 = 安全，1.0 = 会错过）。

    两个截止时刻：

    ① **锅烧糊**。口径走 `_burn_eta_ticks`（读 cooking_progress，而不是把
       常量 cooking_time 当已耗时）。这条仍按单个 task 的估计比较——它要
       回答的是"这一趟来不来得及在糊之前把锅端走"，确实是单 task 粒度。

    ② **订单到期** —— 已移除（Ruling 10）。它曾 `return _doom_penalty(slack_ticks)`，
       而 `_doom_penalty` 与 `_urgency_from_slack` 读同一条 slack 曲线、形状相同
       符号相反：接上 W_DEADLINE_LOOKAHEAD=10 后 `-10d` 永远压过 `+5u`，净效果
       = -urgency，紧迫度越高总分越低（把 53d6a2b 解掉的 94% 倒挂反转回去）。
       同一事实不该计两次分：软梯度由 urgency 独家承担（硬否决曾由
       `_is_doomed_investment` 承担，现已随判死逻辑一并移除）。
       若将来确实需要独立于 urgency 的到期惩罚，必须设计一条**不同形状**的曲线
       （例如只在 slack < 20s 时陡降），而不是复用 `_doom_penalty`。
    """
    est_ticks = candidate.estimated_steps

    if task.order_id:
        cooker = _find_cooker_for_order(task.order_id, world, bb, cooker_match)
        if cooker is not None and not cooker.get("burning"):
            eta = _burn_eta_ticks(cooker)
            if eta is not None and est_ticks > eta:
                return 1.0  # 这一趟赶不上，锅会糊

    return 0.0  # 安全


def _find_plate_for_order(order_id, world, plate_match):
    # type: (Any, World, Optional[Dict[Any, Any]]) -> Optional[Any]
    """查找订单匹配到的盘子（读每拍 1:1 匹配结果，不再读持久 bb.plate_order）。

    查不到就返回 None：不知道这单的盘是哪个时，完成度就应该是未知（0），
    不该拿别人的盘冒充。
    """
    if not plate_match or order_id is None:
        return None
    pid = _order_plate_id(plate_match, order_id)
    return _plate_by_n(world, pid) if pid is not None else None


def _find_cooker_for_order(order_id, world, bb, cooker_match=None):
    # type: (Any, World, Blackboard, Optional[Dict[Any, Any]]) -> Optional[Any]
    """查找订单对应的锅。

    cooker_match 是 plan() 每拍算好的「熟料源↔订单」1:1 匹配，**优先读它**。

    为什么不能只读 bb.cooker_assignments：那个属性在 Blackboard 上根本不存在
    （`hasattr(Blackboard(), "cooker_assignments")` 为 False），全项目无人写入
    —— 只有两个测试自己造。于是本函数在生产路径上恒返回 None，_deadline_forecast
    恒返回 0.0，那条前瞻从未参与过决策。A1 给它接上权重时并没发现这一层，是评审
    用 hasattr 查出来的。
    """
    if cooker_match:
        # cooker_match 形状是 {source_id: order_id}（反向），按 value 查 key。
        # source_id 可能是 (grid,name) 元组（灶上源，§5.2）或裸字符串
        # （台面散锅，散锅没有 grid 概念，id 恒是 item 名）。
        for source_id, oid in cooker_match.items():
            if oid != order_id:
                continue
            if isinstance(source_id, tuple) and len(source_id) == 2:
                sid_grid, sid_name = source_id
            else:
                sid_grid, sid_name = None, source_id
            for cooker in world.cookers:
                if cooker.get("n") != sid_name:
                    continue
                if not _grid_matches(cooker.get("grid"), sid_grid):
                    continue
                return cooker
            for station in world.stations:
                if station.get("n") != sid_name or station.get("t") != "cooker":
                    continue
                if not _grid_matches(station.get("grid"), sid_grid):
                    continue
                return station

    # 兜底：优先从 bb.cooker_assignments 查找
    if hasattr(bb, "cooker_assignments") and order_id in bb.cooker_assignments:
        cooker_id = bb.cooker_assignments[order_id]
        if isinstance(cooker_id, tuple) and len(cooker_id) > 0:
            cooker_id = cooker_id[0]

        # 在 world.cookers 中查找
        for cooker in world.cookers:
            if cooker.get("n") == cooker_id:
                return cooker

        # 也在 world.stations 中查找（有些锅在 stations 列表中）
        for station in world.stations:
            if station.get("n") == cooker_id and station.get("t") == "cooker":
                return station

    return None

"""Planner：单订单规划层候选生成（Plan 3 Task 3）。

为给定订单生成所有可能的下一步操作候选。
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from ai import cook_supply as CS
from ai import economy as ECON
from ai import eta as ETA
from ai import predicates as P
from ai import recipe_graph
from ai import stations as S
from ai import tasks as T
from ai.blackboard import Blackboard, Commitment
from ai.plate_tokens import parse_plate_detail, parse_pot_contents
from ai.recipes import RecipeBook, find_ingredient
from ai.tasks.base import cooked_sources
from ai.world import World

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

    # 配方驱动的候选（放盘 + 备菜 + 上盘 + 出餐），逐订单从 DAG 发射
    for ord in world.orders:
        for task in recipe_graph.emit(world, bb, book, ord,
                                      plate_match, cooker_match):
            pre, est = _CAND_META.get(task.TYPE, ([], EST_FETCH))
            candidates.append(Candidate(task, list(pre), est))

    # 与订单无关的清理 / 回收 / 待命 task（第 2/5/6/11/12/13/14/15 号）
    candidates.extend(_gen_misc(world, bb, book, chef, hands_empty))

    # 当前承诺的 task 必须留在候选里：手持状态是 ETA 惩罚而非硬否决
    # （spec §6.1 选 (b)），所以"拿着料去装盘"这类 task 会被上面的
    # hands_empty 门挡掉。不重新加入，正在执行的任务就会被 Planner 自己
    # 饿死（每拍候选为空 → select_task 返回 None → AI 停摆）。
    cur = bb.commitment.active_task if bb.commitment else None
    if getattr(cur, "TYPE", None) and not any(c.task == cur for c in candidates):
        candidates.append(Candidate(cur, [], _estimate_task_steps(cur)))

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


def _gen_misc(world, bb, book, chef, hands_empty):
    # type: (World, Blackboard, RecipeBook, Any, bool) -> List[Candidate]
    """与订单无关的 task：锅具回收 / 清理 / 待命（spec §6.1 第 2/5/6/11/12/13/14/15 号）。

    这些 task 的 feasible 谓词会再过滤一遍；这里只做资源枚举与手部门——
    需要先拿起东西的（Discard / RescueCooked / RecoverLoosePot / ResetPlate /
    WashPlate）只在手空时生成，否则其树首的 pickup 会把手里的东西换掉。
    """
    cands = []  # type: List[Candidate]

    # 第 2 号 WashPlate：有洗碗池就生成，feasible 判"没干净盘且场上有脏盘"
    if hands_empty:
        for st in world.stations:
            if st.get("t") == "washing" and st.get("n"):
                cands.append(Candidate(T.WashPlate(resources=(st.get("n"),)),
                                       ["hands_empty"], EST_FETCH))

    for c in _cookers(world):
        cid = c.get("n")
        if not cid:
            continue
        # 第 5 号 ReturnPot：手持空/生锅 → 放回空灶（熟锅走 TakeCookedToPlate）
        if (P.held_cookware_state(chef, world.items, world.recipes)
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
                    cands.append(Candidate(
                        T.RecoverLoosePot(resources=(cid,), item=pot.get("n")),
                        [], EST_FETCH))

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
            for ing in book.requirements(order):
                if ing in seen or book.needs_cooking(ing):
                    continue
                seen.add(ing)
                cands.append(Candidate(T.Prefetch(item=ing), [], EST_FETCH))
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
                cands.append(Candidate(
                    T.PlaceToRelay(item=pid), [], EST_FETCH))

    # 第 18 号 FillCooker：订单无关的填灶（第 1 组 1.1）。
    # 按单发射的 FetchToCooker 受「同单只发一个 + 灶↔单 1:1」限制，无法把
    # 空灶填满——实测 59% 的拍里三口灶一个都没在煮，三单因此过期。这里按
    # 「空灶 × 需烹饪的料」粗枚举，缺口判定留给 feasible（读 cook_supply）。
    for c in CS.idle_cookers(world):
        cid = c.get("n")
        if not cid:
            continue
        for item in CS.cooked_items_wanted(world, book):
            raws = book.raws_of(item)
            if not raws:
                continue
            cands.append(Candidate(
                T.FillCooker(None, (cid, tuple(raws)), item),
                ["hands_empty"], EST_COOK_OPS))

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


def _feasibility_of(cand, ctx, bb, ignore_abandon=False, time_left=None):
    # type: (Candidate, Dict, Blackboard, bool, Optional[float]) -> Tuple[bool, str]
    """判断单个候选是否可行，返回 (ok, 否决原因)。

    否决原因供 trace 排查：claimed（资源被他人认领）/ abandoned（L3 拉黑）
    / infeasible（task.feasible() 为假）。ok=True 时原因为空串。

    ignore_abandon=True 给火情用：拉黑是按资源 id 记的，别的 task 在同一
    口灶上反复失败也会把着火的那口灶写进 scratch.abandoned（比如 Normal
    模式里 ReturnPot 卡在灶边），但火不能不管。
    """
    task = cand.task
    if not T.claims_available(task, ctx):
        return False, "claimed"
    if not ignore_abandon and _is_abandoned(task, bb):
        return False, "abandoned"
    if _is_doomed_investment(task, bb):
        return False, "doomed"
    if _is_endgame_waste(task, cand, time_left):
        return False, "endgame"
    if not task.feasible(ctx):
        return False, "infeasible"
    return True, ""


# 判死的迟滞期（拍）。slack 每拍重算会在 0 附近抖动，不加迟滞候选会 flap；
# 与 L3 的 ABANDON_TTL_TICKS 同量级。
DOOM_TTL_TICKS = 200

# 判死的**入口**迟滞（拍）：要连续这么多拍算出必死才真的写表。
# 原先入口无迟滞、出口 200 拍，这个不对称会让一次 slack 抖动把候选打翻：
# 实机 trace-20261014-195652 的 order#3 在 t=9098 解禁（TTL 到期）、t=9120
# 被选中开始走、t=9171 又判死 —— AI 在 50 拍的路上目标被抽掉，折返白走一个
# 来回，最后重新拿起脚下那份黄瓜片。20 拍 = 1 秒，比 slack 的抖动周期长，
# 又远短于一个 task 的典型时长（50~180 拍），不会让真必死的单多吃工时。
DOOM_CONFIRM_TICKS = 20


def _is_doomed_investment(task, bb):
    # type: (Any, Blackboard) -> bool
    """这个候选是否在往一个来不及做完的订单里**继续投入**（第 2 组 2.3）。

    必死单不该靠「分数低一点」来躲——它应该根本不在候选里。本局 order#3
    吞掉 1214 拍（全局 26% 注意力）仍然过期，而 #4/#5 全程饿死。

    两条例外：
      - 订单无关的 task（order_id 为 None：填灶/洗盘/灭火/清理）从不牵连；
      - **送餐例外**。盘已料齐只差送出去时，沉没成本已经付了，送出去还有
        60 分 base；连送餐都挡住会让料齐的盘烂在手里。否决只针对新投入。
      - **在飞例外**。当前承诺正在执行的那一个 task 不被抽走：它的路也走了
        一半、料也许已在手上，半途改选等于两头都白费（实机
        trace-20261014-195652 t=9120~9173 正是这样折返的）。planner 永不
        **选中**已判死单的 task，所以承诺只会建立在当时未判死的单上 ——
        豁免只覆盖这一个 task 的时长，它做完后同单的下一个 task 照样被否决。
    """
    oid = getattr(task, "order_id", None)
    if oid is None:
        return False
    if getattr(task, "TYPE", None) == "DeliverPlate":
        return False
    commitment = getattr(bb, "commitment", None)
    active = getattr(commitment, "active_task", None) if commitment else None
    if active is not None and _tasks_equal(active, task):
        return False
    doomed = getattr(bb, "doomed_orders", None)
    if not doomed:
        return False
    return doomed.get(oid, 0) > getattr(bb, "tick", 0)


def mark_doomed_orders(world, bb, book, plate_match=None, cooker_match=None):
    # type: (World, Blackboard, RecipeBook, Optional[Dict], Optional[Dict]) -> None
    """算一遍 slack，把来不及的订单写进 bb.doomed_orders（带 TTL）。

    每拍在 plan() 开头调用。只**新增/续期**判死，不主动撤销——撤销靠 TTL
    自然过期，这样 slack 抖动不会让候选集抖动。

    入口同样有迟滞：要连续 DOOM_CONFIRM_TICKS 拍算出必死才真的写表，中间
    任何一拍「有救」就把确认计数清零。
    """
    table = getattr(bb, "doomed_orders", None)
    if table is None:
        return
    tick = getattr(bb, "tick", 0)
    try:
        doomed = ETA.doomed_order_ids(world, book, plate_match, cooker_match)
    except Exception:                       # noqa: BLE001
        return                              # 残帧算不出来就不判死（保守）
    streak = getattr(bb, "doom_streak", None)
    if streak is None:
        streak = {}
        bb.doom_streak = streak
    confirmed = set(doomed)
    # 本拍没判死的，确认计数清零（已经写进 table 的仍由 TTL 兜着）
    for oid in [k for k in streak if k not in confirmed]:
        del streak[oid]
    for oid in doomed:
        streak[oid] = streak.get(oid, 0) + 1
        if streak[oid] < DOOM_CONFIRM_TICKS and oid not in table:
            continue                        # 还没攒够确认拍数
        table[oid] = tick + DOOM_TTL_TICKS
    # 顺手清掉过期条目，避免字典随局面无限增长
    for oid in [k for k, v in table.items() if v <= tick]:
        del table[oid]


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
    scratch = getattr(bb, "scratch", None)
    abandoned = getattr(scratch, "abandoned", None)
    if not abandoned:
        return False
    tick = getattr(bb, "tick", 0)
    for res_id, _action in task.claims():
        if abandoned.get(res_id, 0) > tick:
            return True
    return False


def _filter_collaboration(feasible, world, bb):
    # type: (List[Any], World, Blackboard) -> List[Any]
    """协作过滤：接力 / 分单模式（spec §6.3）。

    接力模式（默认）：对 p1_active_task 做分数惩罚（在打分阶段处理）。
    分单模式：只留 order_assignment == "P2" 的 task，归属为空则退回接力。

    返回: 过滤后的任务列表
    """
    collab_mode = bb.intents.collab_mode if bb.intents else "relay"

    if collab_mode == "split":
        return _filter_split_mode(feasible, world, bb)
    else:
        # 接力模式：所有任务保留，冲突检测在打分阶段处理
        return feasible


def _filter_split_mode(feasible, world, bb):
    # type: (List[Any], World, Blackboard) -> List[Any]
    """分单模式：只留 order_assignment == "P2" 的 task。

    归属为空 → 退回接力模式。
    """
    # 检查是否有任何订单归属 P2
    has_p2_assignment = any(
        bb.order_assignment.get(order.get("id")) == "P2"
        for order in world.orders
    )

    if not has_p2_assignment:
        # 归属为空，退回接力模式
        return feasible

    # 只留归属 P2 的 task
    filtered = []
    for task in feasible:
        order_id = getattr(task, "order_id", None)
        if order_id:
            assignment = bb.order_assignment.get(order_id)
            if assignment == "P2":
                filtered.append(task)
        else:
            # 无 order_id 的 task（清理类）保留
            filtered.append(task)

    return filtered


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
# 顺序反了，且这一档占 L4 可达域的一半（_plate_pressure > 0 仅当 clean <= 1，
# 而 WashPlate.feasible 要求 clean < PLATE_BUFFER=2）。
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

# Plan 4 协作过滤权重
W_P1_CONFLICT = 1.0  # P1 冲突惩罚权重

# Plan 4 Task 4: 新增 6 项权重（spec §6.8）
W_ASYNC_RESOURCE = 2.0      # 异步资源优先（FetchToCooker > FetchToPlate）
W_RELAY_BONUS = 1.5          # 接力加成（承接 P1 上游的下游 task）
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
    for p in world.plates:
        if p.get("n") == pid:
            return p
    return None


def _match_plates_to_orders(world, bb, book):
    # type: (World, Blackboard, RecipeBook) -> Dict[Any, Any]
    """盘↔订单 1:1 匹配（每拍重算、不写黑板）。返回 {plate_id: order_id}。

    带成本的贪心边选取：边 (盘, 单) 兼容当且仅当 盘token ⊆ 单需求token，
    成本 missing = |单需求token − 盘token|。空盘 missing=整道菜，自然排最后、
    只填没盘的订单。接近完成的盘优先配更紧的单（完成度感知）。

    排序第一键是「该单是否有熟料在产（锅在煮/熟）」：有熟料的单优先拿盘，
    否则空盘会先配给「需求更少」的虾单、米饭单没盘 → 熟锅无盘可接 → 烧糊
    （实机复现：锅 4 米熟了却没人端，因为空盘全被虾单抢走）。
    """
    orders = [o for o in world.orders if o.get("id") is not None]
    if not orders:
        return {}
    want = {o["id"]: book.order_want_tokens(o) for o in orders}

    # 有熟料在产的订单：锅在煮/熟/糊/着火，pot_contents 反查熟料 token。
    cooking_orders = set()  # type: Set[Any]
    for c in world.cookers:
        raw = parse_pot_contents(c.get("pot_contents") or [])
        if not raw:
            continue
        if not (c.get("isCooking") or c.get("cooked")
                or c.get("burning") or c.get("onFire")):
            continue
        names = book.cooked_of_raw(raw[0])
        for n in names:
            token = book.rich_token(n)
            if not token:
                continue
            for o in orders:
                if token in want[o["id"]]:
                    cooking_orders.add(o["id"])

    # 匹配「已就位」的盘（落在 counter 拼装格上）+ P2 手里正端着的盘。
    # P1 手里端着的盘、沥碗架（plate_return+DryingPart）里的盘、盘堆里的盘
    # 都不能往上倒料，也不该算作某张单的拼装盘——否则 TakeCookedToPlate 会
    # 去端那些盘、往沥碗架倒料（实机复现）。
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
    edges = []
    for p in world.plates:
        pid = p.get("n")
        if not pid:
            continue
        on_counter = _plate_cell(world, p) in counter_cells
        held_by_p2 = bool(p2_held_plate) and \
            p2_held_plate == str(p.get("name") or "").lower()
        if not (on_counter or held_by_p2):
            continue
        tokens = book.plate_tokens(p)
        for o in orders:
            oid = o["id"]
            wt = want[oid]
            if tokens <= wt:
                has_cooking = 0 if oid in cooking_orders else 1
                edges.append((has_cooking, len(wt - tokens), _remaining_of(o),
                              str(pid), str(oid), pid, oid))
    edges.sort(key=lambda e: (e[0], e[1], e[2], e[3], e[4]))
    matched = {}  # type: Dict[Any, Any]
    used_plates = set()  # type: Set[Any]
    used_orders = set()  # type: Set[Any]
    for has_cooking, _missing, _rem, _pid_s, _oid_s, pid, oid in edges:
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

    口径与 `_cooker_burn_danger` / `cook._about_to_burn` 的 `2*ctime` 一致。
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


def _match_cookers_to_orders(world, bb, book, plate_match):
    # type: (World, Blackboard, RecipeBook, Dict[Any, Any]) -> Dict[Any, Any]
    """熟料源↔订单 1:1 匹配（每拍重算）。返回 {source_id: order_id}。

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
        sources.append({"id": c.get("n"), "tokens": toks, "ready": ready,
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
    for o in world.orders:
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
       "layers": {str(task.key): layer}}
    与 trace._format_candidates 的入参格式一致（spec §8.3 决策可观测性）。
    尾部两项由分层添加：layer 是该候选所在层，in_top_layer 标记它是否进了打分
    （spec §6）。plan_firefighting 不分层，仍发 6 元组。

    candidates 含**全部**生成候选：被可行性过滤掉的（ok=False）也保留，
    并用 veto 标注被否决原因，方便复盘「为什么没选拿熟饭」。
    """
    # ① 每拍 1:1 匹配（盘↔订单、灶↔订单，只算不写黑板）
    plate_match = _match_plates_to_orders(world, bb, book)
    cooker_match = _match_cookers_to_orders(world, bb, book, plate_match)

    # ①.5 判死（第 2 组 2.3）：算 slack，把来不及的单写进黑板（带 TTL 迟滞）。
    # 必须在可行性过滤之前——否决必死单的新投入，把手让给能救的单。
    mark_doomed_orders(world, bb, book, plate_match, cooker_match)

    # ② 生成候选（多订单模式：枚举所有订单）
    candidates = generate_candidates(world, bb, order, book,
                                     plate_match, cooker_match)
    if not candidates:
        return None, _record([], plate_match, cooker_match)

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

    chosen_task = None  # type: Optional[Any]
    score_map = {}      # type: Dict[Any, Tuple[float, Dict]]
    top_layer = None    # type: Optional[int]
    layer_map = {}      # type: Dict[Any, int]
    if feasible:
        # ③.5 分层（spec §6）：只有最高层的候选进入打分。
        #
        # 层是**派生值**——每拍从 feasible 重算。所以「高层动作不可达」
        # （盘被 P1 拿走 / 锅被端走）自然表现为该候选从 feasible 消失、
        # top_layer 自己掉下来，不需要降层动作或失效检测（spec §8.1）。
        from ai.planner_layers import layer_filter
        top_layer, in_layer, layer_map = layer_filter(feasible, world)

        # ④ 打分选择
        # 打分读**独立** slack，不读队列累计 slack：后者是判死口径，队尾单
        # 背着全队工时 → slack 最小 → urgency 最高，得出「排得越后越优先」。
        # 实机 trace-20261014-204822 有 94% 的可比候选组（3516/3745）urgency
        # 与到期顺序反向；t=890 选了到期最晚、盘里还空的 order#3，放着盘里
        # 已有米饭的 order#1 不管。
        slack_map = ETA.solo_slack_by_order(world, book, plate_match,
                                            cooker_match)
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

    chosen_key = getattr(chosen_task, "key", None)
    record_cands = []
    for cand in candidates:
        key = cand.task.key
        ok, veto = ok_map[key]
        score, terms = score_map.get(key, (0.0, {}))
        # layer / in_top_layer 让 trace 能区分「这个候选被否决了」与「它
        # 可行但层不够高」——分层最大的可观测性收益（spec §6）。
        layer = layer_map.get(key)
        record_cands.append((cand.task, score, terms, ok, veto,
                             key == chosen_key, layer,
                             layer is not None and layer == top_layer))

    record = _record(record_cands, plate_match, cooker_match)
    record["top_layer"] = top_layer
    record["layers"] = {str(k): v for k, v in layer_map.items()}
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
    # 一样，纯属自欺。见 _credited_order_id。
    effective_order_id = task.order_id
    if task.TYPE == "DeliverPlate" and task.order_id is not None:
        credited = _credited_order_id(
            world.orders, _dish_of_order(world.orders, task.order_id))
        if credited is not None:
            effective_order_id = credited

    # ① 订单紧迫：读 slack（remaining - ETA）而非裸 remaining（第 3 组 3.1）。
    # 旧曲线 W_URGENCY/remaining 在 60s 处只有 0.08、100s 处 0.05，中段分不出
    # 先后，只有最后 1~2s 才爆到 5.0——整局 deadline 近乎不参与决策。
    # slack 为 None（算不出 / 订单无关）时退回 0：宁可不给紧迫分，也不要用
    # 一个错的量级去挤别的项。
    urgency_score = 0.0
    order = None
    slack = None   # type: Optional[float]
    if effective_order_id:
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

    # ④ P1 冲突惩罚（Plan 4 Task 3）
    p1_conflict_penalty = _p1_conflict_penalty(task, world, bb)
    if p1_conflict_penalty > 0:
        terms["p1_conflict"] = -p1_conflict_penalty
    else:
        terms["p1_conflict"] = 0.0

    # ⑤ 异步资源优先（Plan 4 Task 4）
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

    # ⑥ 接力加成（Plan 4 Task 4）
    if _is_relay_task(task, world, bb):
        terms["relay_bonus"] = W_RELAY_BONUS
    else:
        terms["relay_bonus"] = 0.0

    # ⑦ 完成度（Plan 4 Task 4）。曲线是凸的，见 _completion_bonus。
    # 按 effective_order_id 求值：送餐时它是 credited order，与 task 绑的
    # order_id 可能不同（见本函数开头的注释）。
    completion_ratio = _order_completion_ratio(task, world, bb, book,
                                               plate_match, effective_order_id)
    terms["completion_ratio"] = _completion_bonus(completion_ratio)

    # ⑧ 食材复用（Plan 4 Task 4）
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
        elif head is not None and not _is_doomed_order(head, bb):
            # 这种菜会记给别人、把连击打断，而队首仍有救 → 罚。
            # 队首已判死则不罚：连击注定要断，再罚只会让料齐的盘也送不出去。
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
                chef, world.items, world.recipes) == P.COOKWARE_COOKED)
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

    # 总分 = terms 的和。**不要**改回手写加法：旧版把 16 项重抄一遍、还混用
    # 局部变量与 terms[...]，加一项要改两处；漏掉求和那处的症状是「trace 里
    # 有这一项但它不影响选择」，而单测断言的是 terms、一个都不会报警。
    # 推论：凡进 terms 的都必须是已带符号的最终贡献（惩罚项存负数）。
    #
    # layer 非 None 时只算该层白名单内的项（spec §7）。terms 仍然全量返回
    # ——trace 要能看见「这一项算出来是多少、只是本层不用它」。layer 为
    # None（默认）时与分层前完全一致：全项求和。
    if layer is not None:
        from ai.planner_layers import filter_terms
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
    c = P.cooker_for(world.cookers, str(cid))
    if c is None:
        return 0.0

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

    ratio = prog / (2.0 * ctime)   # 0=生, 0.5=刚熟, 1.0=快糊
    if ratio >= 1.0:
        return 2.0                 # 已糊/快糊，满压
    if ratio >= 0.5:
        return 1.0 + (ratio - 0.5) * 2.0   # 刚熟→快糊，压力 1.0→2.0
    # 快熟阶段（0.25=煮一半 → 0.5=刚熟）：压力 0→1.0 渐变，让 TakeCookedToPlate
    # 提前去端盘、站到锅前等，而不是等熟了才动（端盘盛饭前置了取盘动作，
    # 等熟了才去拿盘会来不及，实机烧糊）。
    if ratio >= 0.25:
        return (ratio - 0.25) * 4.0
    return 0.0                     # 刚下锅，不急


def _p1_conflict_penalty(task, world, bb):
    # type: (Any, World, Blackboard) -> float
    """计算 P1 冲突惩罚（Plan 4 Task 3）。

    接力模式：P1 正在使用的资源，AI 的任务如果也要用则惩罚。
    惩罚大小 = W_P1_CONFLICT * confidence * 基础惩罚值(5.0)

    返回: 惩罚值（>=0）
    """
    # 只在接力模式下计算惩罚
    collab_mode = bb.intents.collab_mode if bb.intents else "relay"
    if collab_mode != "relay":
        return 0.0

    # 没有 P1 推断信息
    if not bb.p1_model or not bb.p1_model.p1_active_task:
        return 0.0

    # P1 置信度太低
    if bb.p1_model.confidence < 0.1:
        return 0.0

    # 检查任务资源是否与 P1 冲突
    if _task_conflicts_with_p1(task, bb.p1_model):
        base_penalty = 5.0
        penalty = W_P1_CONFLICT * bb.p1_model.confidence * base_penalty
        return penalty

    return 0.0


def _task_conflicts_with_p1(task, p1_model):
    # type: (Any, Any) -> bool
    """检查任务是否与 P1 推断的活动冲突。

    冲突检测基于资源匹配：
    - P1 正在交互 "interacting_with_X" → AI 的任务如果使用资源 X 则冲突
    - P1 正在移动 "moving_with_Y" → 暂不视为冲突（移动目标不确定）
    """
    p1_task = p1_model.p1_active_task
    if not p1_task:
        return False

    # 解析 P1 正在交互的资源
    # 格式: "interacting_with_<resource_id>"
    if p1_task.startswith("interacting_with_"):
        p1_resource = p1_task[len("interacting_with_"):]

        # 检查 AI 任务是否使用该资源
        task_resources = _extract_task_resources(task)
        return p1_resource in task_resources

    # 其他情况不视为冲突
    return False


def _extract_task_resources(task):
    # type: (Any) -> List[Any]
    """提取任务使用的资源 ID 列表：claims() 的资源 ∪ 四元身份里绑定的资源。

    为什么要并集而不是只读 claims()：claims 是**动作级**粒度（spec §6.3：
    P1 往 1 号锅放生饭只锁 (cooker1, "fetch_to")，取熟饭仍开放），而
    p1_conflict 要回答的是「P1 正在碰哪个**物理**资源」——口径本就不同。
    只读 claims 会让 FetchToPlate 的资源变成空集（它的 claims() 返回 ()，
    见 ai/tasks/assembly.py），于是 p1_conflict 对它恒为 0；而原先那版手写
    属性名表又漏掉拼装格坐标和 FillCooker 的 (cid, raws)。

    保留资源 id 的**原始类型**（坐标是 tuple）：_task_conflicts_with_p1 用
    `in` 直接比较，转字符串会让坐标永不匹配。
    """
    out = []    # type: List[Any]
    seen = set()

    def _is_cell(value):
        # type: (Any) -> bool
        """这个元组是不是「拼装格坐标」（spec §4.4 坐标即身份）。

        判据是**结构**（长度 2 的数值对），不是「元素是否全为 int」。后者把
        类型当语义用：`(3, 4.0)` 会被判成「不是坐标」而被摊平成两个裸标量，
        于是 `_task_conflicts_with_p1` 的 `in` 比较永不匹配。`to_grid` 当前
        返回 int 所以今天不触发，但没有任何东西强制这一点——这是埋着的雷
        （评审查出）。

        bool 要排除：`isinstance(True, int)` 为真，而 `(True, False)` 不是坐标。
        """
        return (len(value) == 2
                and all(isinstance(v, (int, float))
                        and not isinstance(v, bool) for v in value))

    def _add(value):
        # 嵌套元组（FillCooker 的 (cid, raws)）要摊平一层，坐标整体保留；
        # 不可哈希的跳过
        if isinstance(value, tuple) and value and not _is_cell(value):
            for v in value:
                _add(v)
            return
        try:
            if value in seen:
                return
            seen.add(value)
        except TypeError:
            return          # 不可哈希（list/dict）→ 不是资源 id
        if value is not None:
            out.append(value)

    claims = getattr(task, "claims", None)
    if callable(claims):
        try:
            for res_id, _action in claims():
                _add(res_id)
        except Exception:        # noqa: BLE001
            pass                 # 残缺 task（测试替身）不该让打分崩

    for res in (getattr(task, "_resources", None) or ()):
        _add(res)
    return out


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
    if (_time_pressure(top_task, world) >= 1.0
            and not _tasks_equal(top_task, current_task)):
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


def _is_relay_task(task, world, bb):
    # type: (Any, World, Blackboard) -> bool
    """检测是否为接力任务（spec §6.8 接力加成）。

    接力任务：承接 P1 上游工作的下游任务。
    例如：P1 下锅煮饭（FetchToCooker），AI 取熟饭（TakeCookedToPlate）。
    """
    # 检查是否有 P1 推断信息
    if not bb.p1_model or not bb.p1_model.p1_active_task:
        return False

    task_type = getattr(task, "TYPE", "")
    p1_task = bb.p1_model.p1_active_task

    # 场景1：P1 完成下锅（FetchToCooker），AI 取熟饭（TakeCookedToPlate）
    if "TakeCooked" in task_type and "fetch_to_cooker" in p1_task.lower():
        return True

    # 场景2：P1 切好菜（Chop），AI 去取切好的食材
    if "FetchToPlate" in task_type and "chop" in p1_task.lower():
        return True

    return False


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
        from ai.recipes import RecipeBook
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
        from ai.recipes import RecipeBook
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


def _is_doomed_order(order_id, bb):
    # type: (Any, Blackboard) -> bool
    """这一单是否已被判死（读第 2 组写进黑板的表）。"""
    doomed = getattr(bb, "doomed_orders", None) or {}
    return doomed.get(order_id, 0) > getattr(bb, "tick", 0)


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
       同一事实不该计两次分：硬否决在可行性层（`_is_doomed_investment`），软梯度
       由 urgency 独家承担。
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
        # cooker_match 形状是 {source_id: order_id}（反向），按 value 查 key
        for source_id, oid in cooker_match.items():
            if oid == order_id:
                for cooker in world.cookers:
                    if cooker.get("n") == source_id:
                        return cooker
                for station in world.stations:
                    if station.get("n") == source_id \
                            and station.get("t") == "cooker":
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

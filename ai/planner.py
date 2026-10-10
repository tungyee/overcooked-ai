"""Planner：单订单规划层候选生成（Plan 3 Task 3）。

为给定订单生成所有可能的下一步操作候选。
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from ai import predicates as P
from ai import recipe_graph
from ai import tasks as T
from ai.blackboard import Blackboard
from ai.plate_tokens import parse_plate_detail, parse_pot_contents
from ai.recipes import RecipeBook, find_ingredient
from ai.tasks.base import cooked_sources
from ai.world import World

# Plan 3 固定估计步数（spec §7.3.4）
EST_FETCH, EST_CHOP, EST_COOK_OPS, EST_SERVE = 10, 20, 15, 5


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
    "TakeCookedToPlate": (["holding_cooked"], EST_COOK_OPS),
    "DeliverPlate": (["holding_plated_dish"], EST_SERVE),
    "Mix": (["hands_empty"], EST_COOK_OPS),
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

    return cands


def _feasibility_of(cand, ctx, bb):
    # type: (Candidate, Dict, Blackboard) -> Tuple[bool, str]
    """判断单个候选是否可行，返回 (ok, 否决原因)。

    否决原因供 trace 排查：claimed（资源被他人认领）/ abandoned（L3 拉黑）
    / infeasible（task.feasible() 为假）。ok=True 时原因为空串。
    """
    task = cand.task
    if not T.claims_available(task, ctx):
        return False, "claimed"
    if _is_abandoned(task, bb):
        return False, "abandoned"
    if not task.feasible(ctx):
        return False, "infeasible"
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
W_PRESSURE = 3.0
W_ETA = 0.1

# Plan 4 协作过滤权重
W_P1_CONFLICT = 1.0  # P1 冲突惩罚权重

# Plan 4 Task 4: 新增 6 项权重（spec §6.8）
W_ASYNC_RESOURCE = 2.0      # 异步资源优先（FetchToCooker > FetchToPlate）
W_RELAY_BONUS = 1.5          # 接力加成（承接 P1 上游的下游 task）
W_COMPLETION_RATIO = 1.0     # 完成度（盘里食材越接近完成优先级越高）
W_INGREDIENT_REUSE = 0.5     # 食材复用（一次取料满足多个订单）
W_COMBO_CONTINUE = 1.0       # Combo 延续（连续送同类菜加分）
W_DEADLINE_LOOKAHEAD = 10.0  # 截止前瞻（会导致烧糊/超时的 task 惩罚）

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
    成本 missing = |单需求token − 盘token|；按 (missing asc, remaining asc,
    plate.n asc, order.id asc) 贪心取边。空盘 missing=整道菜，自然排最后、
    只填没盘的订单。接近完成的盘优先配更紧的单（完成度感知）。
    """
    orders = [o for o in world.orders if o.get("id") is not None]
    if not orders:
        return {}
    want = {o["id"]: book.order_want_tokens(o) for o in orders}
    # 只匹配「已就位」的盘（落在 counter 拼装格上）。P1 手里端着的盘、沥碗架
    # （plate_return+DryingPart）里的盘、盘堆里的盘都不能往上倒料，也不该算作
    # 某张单的拼装盘——否则 TakeCookedToPlate 会去端那些盘、往沥碗架倒料
    # （实机复现）。
    counter_cells = set()  # type: Set[Tuple[int, int]]
    for st in world.stations:
        if st.get("t") != "counter":
            continue
        gx, gz = st.get("gx"), st.get("gz")
        if gx is not None and gz is not None:
            counter_cells.add((int(round(gx)), int(round(gz))))
    edges = []
    for p in world.plates:
        pid = p.get("n")
        if not pid:
            continue
        if _plate_cell(world, p) not in counter_cells:
            continue
        tokens = book.plate_tokens(p)
        for o in orders:
            oid = o["id"]
            wt = want[oid]
            if tokens <= wt:
                edges.append((len(wt - tokens), _remaining_of(o),
                              str(pid), str(oid), pid, oid))
    edges.sort(key=lambda e: (e[0], e[1], e[2], e[3]))
    matched = {}  # type: Dict[Any, Any]
    used_plates = set()  # type: Set[Any]
    used_orders = set()  # type: Set[Any]
    for _missing, _rem, _pid_s, _oid_s, pid, oid in edges:
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
      {"candidates": [(task, score, terms, ok, veto, chosen), ...]}
    与 trace._format_candidates 的入参格式一致（spec §8.3 决策可观测性）。

    candidates 含**全部**生成候选：被可行性过滤掉的（ok=False）也保留，
    并用 veto 标注被否决原因，方便复盘「为什么没选拿熟饭」。
    """
    # ① 每拍 1:1 匹配（盘↔订单、灶↔订单，只算不写黑板）
    plate_match = _match_plates_to_orders(world, bb, book)
    cooker_match = _match_cookers_to_orders(world, bb, book, plate_match)

    # ② 生成候选（多订单模式：枚举所有订单）
    candidates = generate_candidates(world, bb, order, book,
                                     plate_match, cooker_match)
    if not candidates:
        return None, _record([], plate_match, cooker_match)

    # ③ 可行性过滤（同时记下每个候选的否决原因）
    ctx = _make_context(world, bb, book)
    feasible = []  # type: List[Candidate]
    ok_map = {}    # type: Dict[Any, Tuple[bool, str]]
    for cand in candidates:
        ok, veto = _feasibility_of(cand, ctx, bb)
        ok_map[cand.task.key] = (ok, veto)
        if ok:
            feasible.append(cand)

    chosen_task = None  # type: Optional[Any]
    score_map = {}      # type: Dict[Any, Tuple[float, Dict]]
    if feasible:
        # ④ 打分选择
        scored = _score_tasks([c.task for c in feasible], feasible, world, bb,
                              book, plate_match)
        if scored:
            # ⑤ 承诺惯性（Plan 3 Task 6）
            chosen_task = _apply_commitment_inertia(scored, world, bb)
            score_map = {task.key: (score, terms)
                         for task, score, terms in scored}

    chosen_key = getattr(chosen_task, "key", None)
    record_cands = []
    for cand in candidates:
        key = cand.task.key
        ok, veto = ok_map[key]
        score, terms = score_map.get(key, (0.0, {}))
        record_cands.append((cand.task, score, terms, ok, veto, key == chosen_key))

    return chosen_task, _record(record_cands, plate_match, cooker_match)


def plan_firefighting(world, bb, book):
    # type: (World, Blackboard, RecipeBook) -> Tuple[Optional[Any], Dict]
    """Firefighting 模式专用规划：只生成 Extinguish 候选，优先灭火。

    返回 (chosen_task, decision_record)，格式与 plan() 一致。

    实机复现：拍 596-727 Firefighting 触发但 P2 完全 idle，原因是 Agent
    主循环在非 Normal 模式下跳过了 Planner 调用 → candidates=0 → 无任务可选。
    本函数只针对着火/烧焦的灶生成 Extinguish（拿灭火器喷射）候选，优先
    灭最紧急的火。旧实现误用 RescueCooked（端锅离灶）——那既拿不起着火的
    锅，也灭不了火，只会让 Firefighting 空转。
    """
    ctx = _make_context(world, bb, book)
    cands = []  # type: List[Candidate]

    # 枚举所有着火或烧焦的灶，生成 Extinguish（灭火）候选
    for c in world.cookers:
        if c.get("onFire") or c.get("burning"):
            cid = c.get("n")
            if cid:
                cands.append(Candidate(T.Extinguish(resources=(cid,)),
                                      [], EST_COOK_OPS))

    if not cands:
        # 无灶着火（Arbiter 误判 or 火已被 P1 扑灭）→ 返回空
        return None, {"candidates": []}

    # 可行性过滤
    feasible_cands = []  # type: List[Candidate]
    ok_map = {}          # type: Dict[Any, Tuple[bool, str]]
    for cand in cands:
        ok, veto = _feasibility_of(cand, ctx, bb)
        ok_map[cand.task.key] = (ok, veto)
        if ok:
            feasible_cands.append(cand)

    if not feasible_cands:
        # 所有候选都不可行（灭火器被 P1 拿走 / 无法到达）
        record_cands = [(c.task, 0.0, {}, False, ok_map[c.task.key][1], False)
                        for c in cands]
        return None, {"candidates": record_cands, "plate_match": {}, "cooker_match": {}}

    # 优先选最紧急的 (onFire > burning)
    def urgency(cand):
        # type: (Candidate) -> int
        cooker = next((c for c in world.cookers
                      if c.get("n") == cand.task.cooker_id), None)
        if cooker and cooker.get("onFire"):
            return 0  # 着火最紧急
        return 1      # 只是烧焦，次优先

    feasible_cands.sort(key=urgency)
    chosen = feasible_cands[0].task
    chosen_key = chosen.key

    # 组装 decision_record（格式同 plan()，方便 trace 记录）
    record_cands = []
    for cand in cands:
        key = cand.task.key
        ok, veto = ok_map[key]
        # Firefighting 不打分，统一给 0 分
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


def _score_tasks(tasks, candidates, world, bb, book=None, plate_match=None):
    # type: (List[Any], List[Candidate], World, Blackboard, Optional[Any], Optional[Dict[Any, Any]]) -> List[Tuple[Any, float, Dict]]
    """对可行任务打分（spec §6.4）。

    返回: [(task, score, terms), ...]
    terms 是各项分数的拆解，用于决策日志和审计。
    """
    scored = []
    for task, cand in zip(tasks, candidates):
        score, terms = _score_candidate(world, bb, cand, book, plate_match)
        scored.append((task, score, terms))
    return scored


def _score_candidate(world, bb, candidate, book=None, plate_match=None):
    # type: (World, Blackboard, Candidate, Optional[Any], Optional[Dict[Any, Any]]) -> Tuple[float, Dict]
    """对单个候选打分（spec §6.4）。

    Plan 3 最小权重集：订单紧迫、时间压力、ETA
    Plan 4 新增：P1 冲突惩罚 + 6 项新权重（Task 4）
    """
    task = candidate.task
    terms = {}

    # ① 订单紧迫
    urgency_score = 0.0
    order = None
    if task.order_id:
        order = _find_order_by_id(world, task.order_id)
        if order:
            remaining = order.get("remaining", 1)
            urgency = 1.0 / max(remaining, 1)
            urgency_score = W_URGENCY * urgency
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
    # 仅当订单存在时才给予异步资源加成
    if _is_async_task(task) and order is not None:
        terms["async_resource"] = W_ASYNC_RESOURCE
    else:
        terms["async_resource"] = 0.0

    # ⑥ 接力加成（Plan 4 Task 4）
    if _is_relay_task(task, world, bb):
        terms["relay_bonus"] = W_RELAY_BONUS
    else:
        terms["relay_bonus"] = 0.0

    # ⑦ 完成度（Plan 4 Task 4）
    completion_ratio = _order_completion_ratio(task, world, bb, book, plate_match)
    terms["completion_ratio"] = W_COMPLETION_RATIO * completion_ratio

    # ⑧ 食材复用（Plan 4 Task 4）
    reuse_count = _ingredient_reuse_count(task, world, bb, plate_match)
    if reuse_count >= 2:
        terms["ingredient_reuse"] = W_INGREDIENT_REUSE * (reuse_count - 1)
    else:
        terms["ingredient_reuse"] = 0.0

    # ⑨ Combo 延续（Plan 4 Task 4）
    if _is_combo_continuation(task, world, bb):
        terms["combo_continue"] = W_COMBO_CONTINUE
    else:
        terms["combo_continue"] = 0.0

    # ⑩ 截止前瞻（Plan 4 Task 4）
    deadline_penalty = _deadline_forecast(task, candidate, world, bb)
    terms["deadline_lookahead"] = -deadline_penalty

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

    total_score = (urgency_score + pressure_score + eta_score - p1_conflict_penalty +
                   terms["async_resource"] + terms["relay_bonus"] +
                   terms["completion_ratio"] + terms["ingredient_reuse"] +
                   terms["combo_continue"] - deadline_penalty
                   + hand_bonus - hand_penalty - move_penalty)
    return total_score, terms


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
    快糊 2.0，让「快糊了」能跨过承诺惯性门槛、第一时间去端锅。
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
    if ratio < 0.5:
        return 0.0                 # 还没熟
    if ratio >= 1.0:
        return 2.0                 # 已糊/快糊，满压
    return 1.0 + (ratio - 0.5) * 2.0   # 刚熟→快糊，压力 1.0→2.0


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
    # type: (Any) -> List[str]
    """提取任务使用的资源 ID 列表。"""
    resources = []

    # 从任务的 resource_ids 或参数中提取
    # FetchToBoard(order_id, board_id, ingredient)
    # board_id 是元组，如 ("board1",)

    # 通用方法：检查 task 的属性
    for attr_name in ["board_id", "cooker_id", "plate_id", "station_id", "counter_id"]:
        if hasattr(task, attr_name):
            resource_tuple = getattr(task, attr_name)
            if resource_tuple:
                # 资源 ID 通常是元组的第一个元素
                if isinstance(resource_tuple, tuple) and len(resource_tuple) > 0:
                    resource_id = resource_tuple[0]
                    if isinstance(resource_id, str):
                        resources.append(resource_id)
                elif isinstance(resource_tuple, str):
                    resources.append(resource_tuple)

    return resources


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
                # 恢复暂停的任务
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

    # 快糊/已糊的锅要第一时间去端/救，跳过承诺惯性。光靠把 pressure 加进分数
    # 不够：pressure 把 TakeCookedToPlate 拉到 ~2，但当前 FetchToPlate 也会被
    # ingredient_reuse / completion_ratio 抬到 ~1.4，分差 < COMMITMENT_MARGIN，
    # 于是锅都熟到快糊了还赖在 FetchToPlate 上不去端（实机复现）。
    if (_time_pressure(top_task, world) >= 2.0
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


def _order_completion_ratio(task, world, bb, book=None, plate_match=None):
    # type: (Any, World, Blackboard, Optional[Any], Optional[Dict[Any, Any]]) -> float
    """计算订单完成度（spec §6.8 完成度）：盘里已就位的食材占总需求的比例。

    两处曾经让这个权重**在实机上恒为 0**（只剩单测里好看）：

      1. 盘内容读的是 `plate["item"]`——帧里的字段叫 `contents`（"item" 只
         存在于早期单测的夹具里，那个夹具把盘写成 stations 的一项）；
      2. 判定用字面比较。盘里记的是**装盘态词汇**（熟料在 detail 里是
         rich token、旧帧是不透明 token，见 RecipeBook.plate_tokens），
         字面比较对熟料永远不成立。故有 `book` 时必须走 `book.plate_has`。

    book 缺省时按 world 现造一本（单测的调用点不传 book；生产路径由
    select_task 一路带下来，不会重复造）。
    """
    if not task.order_id:
        return 0.0

    order = _find_order_by_id(world, task.order_id)
    if not order:
        return 0.0

    composition = order.get("composition", [])
    if not composition:
        return 0.0

    # 查找该订单对应的盘子
    plate = _find_plate_for_order(task.order_id, world, plate_match)
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


def _ingredient_reuse_count(task, world, bb, plate_match=None):
    # type: (Any, World, Blackboard, Optional[Dict[Any, Any]]) -> int
    """统计食材复用次数（spec §6.8 食材复用）。

    返回：该食材可以同时满足多少个订单的需求。
    """
    task_type = getattr(task, "TYPE", "")

    # 只对取料类任务计算复用
    if "FetchToBoard" not in task_type and "FetchToPlate" not in task_type:
        return 0

    # 提取任务要取的食材
    ingredient = getattr(task, "item", None)
    if not ingredient:
        return 0

    # 统计有多少个订单需要这个食材
    count = 0
    for order in world.orders:
        composition = order.get("composition", [])
        if ingredient in composition:
            # 检查该订单的盘子是否已有这个食材
            plate = _find_plate_for_order(order.get("id"), world, plate_match)
            if plate:
                plate_contents = plate.get("item", [])
                if isinstance(plate_contents, str):
                    plate_contents = [plate_contents]
                elif not isinstance(plate_contents, list):
                    plate_contents = []

                # 只有盘子还缺这个食材时才计数
                if ingredient not in plate_contents:
                    count += 1
            else:
                # 没有盘子也算需要
                count += 1

    return count


def _is_combo_continuation(task, world, bb):
    # type: (Any, World, Blackboard) -> bool
    """检测是否为 combo 延续（spec §6.8 combo 延续）。

    Combo：连续送出同类菜品可获得加成。
    """
    task_type = getattr(task, "TYPE", "")

    # 只对送餐任务计算 combo
    if "Deliver" not in task_type:
        return False

    # 检查是否有上次送出的菜品记录
    last_dish = getattr(bb, "last_delivered_dish", None)
    if not last_dish:
        return False

    # 检查当前任务的订单菜品
    if not task.order_id:
        return False

    order = _find_order_by_id(world, task.order_id)
    if not order:
        return False

    current_dish = order.get("name")

    # 菜品名称匹配则为 combo
    return current_dish == last_dish


def _deadline_forecast(task, candidate, world, bb):
    # type: (Any, Candidate, World, Blackboard) -> float
    """截止前瞻（spec §6.8 截止前瞻）。

    检查任务执行时间是否会错过已知截止时刻：
    1. 锅烧糊时刻（cooked → burning）
    2. 订单到期时刻（order.remaining）

    返回：惩罚系数（0.0 = 安全，1.0 = 会错过）
    """
    est_ticks = candidate.estimated_steps

    # ① 检查锅烧糊（该订单相关的锅）
    if task.order_id:
        cooker = _find_cooker_for_order(task.order_id, world, bb)
        if cooker and cooker.get("cooked") and not cooker.get("burning"):
            # cooking_time 通常是秒数
            # 游戏机制：米煮熟后（cooked=True），大约有 10 秒变成 burning
            # 如果 cooking_time=18 秒，意味着已经煮了 18 秒，早已超过了煮熟的时间
            # 假设煮熟需要 10 秒，那么 cooked 状态已经维持了 18-10=8 秒，只剩 2 秒就要烧糊
            cooking_time = cooker.get("cooking_time", 0)

            # 假设煮熟需要约 10 秒，cooked 状态能维持约 10 秒
            # cooking_time - 10 = 已经在 cooked 状态的时间
            # 10 - (cooking_time - 10) = 距离 burning 的时间
            COOK_TIME_SECONDS = 10  # 煮熟需要的时间
            BURN_GRACE_SECONDS = 10  # cooked 后到 burning 的时间

            time_in_cooked_state = max(cooking_time - COOK_TIME_SECONDS, 0)
            time_to_burn_seconds = max(BURN_GRACE_SECONDS - time_in_cooked_state, 0)
            time_to_burn_ticks = time_to_burn_seconds * 20  # 秒 → 拍

            if est_ticks > time_to_burn_ticks:
                return 1.0  # 会错过

    # ② 检查订单到期
    if task.order_id:
        order = _find_order_by_id(world, task.order_id)
        if order:
            remaining_seconds = order.get("remaining", 999)
            remaining_ticks = remaining_seconds * 20  # 秒 → 拍（20 拍/秒）

            # 如果任务时间超过剩余时间的 80%，认为有风险
            if est_ticks > remaining_ticks * 0.8:
                return 1.0  # 会错过

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


def _find_cooker_for_order(order_id, world, bb):
    # type: (Any, World, Blackboard) -> Optional[Any]
    """查找订单对应的锅。"""
    # 优先从 bb.cooker_assignments 查找
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

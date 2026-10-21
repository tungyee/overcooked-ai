# -*- coding: utf-8 -*-
"""订单 ETA 与 slack：这一单还能不能在 remaining 内变成送达（第 2 组）。

**为什么要新写一层**：旧 `planner._deadline_forecast` 整局只触发 33 拍
（trace-20261014-120406，4577 拍）。它拿 `candidate.estimated_steps` 当 ETA，
而那是一张按 TYPE 查的常数表（`EST_FETCH/CHOP/COOK_OPS/SERVE = 10/20/15/5`）
—— 10 拍 = 0.5 秒，对 130 秒的单寿命，`est > remaining*0.8` 几乎永不成立；
而且它只算**当前一个 task**，不是「这一单剩下的全部活」。于是 deadline 对
决策的影响≈0，直到最后 20 拍突然把人拽过去（t=2755 为只剩 0.9s 的 order#3
放黄瓜，必死），而那时已经救不了。

单位耗时全部来自实测（成功 task 的中位耗时，单位＝20Hz 的拍）：

  task                 120406 中位 / 最大    004742 中位
  FetchToPlate         136 / 224             112
  FetchToBoard         139 / 169             —
  FetchToCooker        157 / 158             75
  TakeCookedToPlate    123 / 137             131
  Chop                  30 / 31              —
  DeliverPlate          45 / 97              55
  WashPlate            152 / 152             —
  PlaceCleanPlate      175 / 175             —

两局的「拍/格」也吻合：6.2 与 5.9（总计 3412 拍/554 格、3911 拍/663 格）。
注意这个数字已经**含**实机的移动损耗（51% 的移动拍没产生位移），所以不要
再额外乘裕度——裕度已经在标定里了。

校准基准：两个成功送达单的 P2 独占工时是 781 拍（39s）与 697 拍（35s）。
**一份寿司 ≈ 35~40 秒手上工时** → 130s 寿命最多托住 3 单，而本局并发 5 单，
必然丢 2~3 个。这个量级本身就是「为什么会 miss」的答案。

两个关键建模决定：

1. **烹饪是关键路径，不是求和**。煮饭的 12 秒里人是自由的（可以去切鱼），
   所以 `总 ETA = max(手上工时, 锅上时钟)`。求和会把 ETA 夸大到人人必死。
2. **多单共享一双手 → 队列累计**。每单单独算都「来得及」，但 P2 只有一双
   手。本局 order#3 吞掉 1214 拍（全局 26% 注意力）仍然过期，就是因为没人
   算过「为它花的时间会让 #4/#5 饿死」。所以 `order_queue` 按最早截止排序后
   累加手上工时，第 N 单的完工时刻含前 N-1 单的占用。
"""
from typing import Any, Dict, List, Optional

from ai.query import predicates as P
from ai.query.plate_tokens import parse_pot_contents
from ai.tasks.base import _grid_matches

TICKS_PER_SECOND = 20

# 各工序实测耗时（拍）。取两局的中位数量级，不取最大值——按最坏情况估会
# 让所有单都判必死，失去区分力。
COST_FETCH_TO_PLATE = 125      # 取一样料放盘（120406 中位 136 / 004742 112）
COST_FETCH_TO_BOARD = 135      # 取生料上砧板
COST_FETCH_TO_COOKER = 120     # 取生料下锅（两局 157 / 75，取中间）
COST_TAKE_COOKED = 125         # 端盘到锅前盛熟料
COST_CHOP = 30                 # 切一样料（砧板前站定后）
COST_DELIVER = 50              # 端盘送出餐口
COST_PLACE_PLATE = 170         # 取干净盘放拼装位
COST_WASH_PLATE = 150          # 洗一个脏盘

# 煮熟所需进度（秒）缺字段时的兜底：本关 cooking_time 恒 12。
DEFAULT_COOK_SECONDS = 12.0


# 按 task TYPE 查实测耗时（拍）。供「这一趟来不来得及」这类单 task 判断用，
# 取代 planner 里那张按 TYPE 猜的常数表（EST_FETCH=10 拍=0.5 秒，与实测差一个
# 数量级，见模块注释的实测表）。
TASK_COST = {
    "FetchToPlate": COST_FETCH_TO_PLATE,
    "FetchToBoard": COST_FETCH_TO_BOARD,
    "FetchToCooker": COST_FETCH_TO_COOKER,
    "FillCooker": COST_FETCH_TO_COOKER,
    "TakeCookedToPlate": COST_TAKE_COOKED,
    "Chop": COST_CHOP,
    "DeliverPlate": COST_DELIVER,
    "PlaceCleanPlate": COST_PLACE_PLATE,
    "WashPlate": COST_WASH_PLATE,
    "Prefetch": COST_FETCH_TO_PLATE,
    "ResetPlate": COST_FETCH_TO_PLATE,
    "StashHeld": COST_CHOP,
    "ReturnPot": COST_FETCH_TO_COOKER,
    "RescueCooked": COST_TAKE_COOKED,
    "RecoverLoosePot": COST_FETCH_TO_COOKER,
    "Discard": COST_TAKE_COOKED,
    "PlaceToRelay": COST_FETCH_TO_PLATE,
    "Mix": COST_CHOP,
}

# 没登记的 TYPE（新 task / 灭火）按一次取料的代价兜底。
DEFAULT_TASK_COST = COST_FETCH_TO_PLATE


def task_cost_ticks(task_type):
    # type: (Any) -> int
    """这个 task 做一趟大约要多少拍（实测中位）。"""
    return TASK_COST.get(str(task_type), DEFAULT_TASK_COST)


class OrderETA(object):
    """一个订单的完工预测。`eta` 是**队列累计后**的完工拍数（含前面单的占用）。"""

    __slots__ = ("order_id", "remaining", "lifetime", "hand_ticks",
                 "solo_eta", "eta", "slack", "solo_slack", "rank")

    def __init__(self, order_id, remaining, lifetime, hand_ticks,
                 solo_eta, eta, slack, solo_slack=None, rank=0):
        # type: (Any, float, Any, int, int, int, float, Optional[float], int) -> None
        self.order_id = order_id
        self.remaining = remaining
        self.lifetime = lifetime
        self.hand_ticks = hand_ticks
        self.solo_eta = solo_eta
        self.eta = eta
        # 队列累计 slack：含前面各单的手上工时占用。只作诊断展示，不要拿它
        # 当优先级——队尾单背着全队工时，slack 必然最小，用它打分会得出
        # 「排得越后越紧急」的倒挂结论（见 solo_slack）。
        self.slack = slack
        # 独立 slack：只看这一单自己的关键路径，不含别单占用。打分的 urgency
        # 读它——同款菜下它对 remaining 单调，队首自然最紧迫。
        self.solo_slack = (remaining * TICKS_PER_SECOND - solo_eta
                           if solo_slack is None else solo_slack)
        self.rank = rank            # EDF 队列位次（0 = 队首）

    def __repr__(self):
        return ("OrderETA(#%s rem=%.1fs eta=%d slack=%.0f)"
                % (self.order_id, self.remaining, self.eta, self.slack))


def _plate_for(world, plate_match, order_id):
    # type: (Any, Optional[Dict], Any) -> Optional[Dict]
    for pid, oid in (plate_match or {}).items():
        if oid != order_id:
            continue
        for p in (getattr(world, "plates", None) or ()):
            if p.get("n") == pid:
                return p
    return None


def _cooker_for(world, cooker_match, order_id):
    # type: (Any, Optional[Dict], Any) -> Optional[Dict]
    """cid 可能是 (grid,name) 元组（灶上源，§5.2）或裸字符串（台面散锅，
    散锅不在 world.cookers 里，退化到名字比较也查不到，属预期）。"""
    for cid, oid in (cooker_match or {}).items():
        if oid != order_id:
            continue
        if isinstance(cid, tuple) and len(cid) == 2:
            grid, name = cid
        else:
            grid, name = None, cid
        for c in (getattr(world, "cookers", None) or ()):
            if c.get("n") != name:
                continue
            if not _grid_matches(c.get("grid"), grid):
                continue
            return c
    return None


def _cook_remaining_ticks(cooker):
    # type: (Optional[Dict]) -> int
    """这口锅还要煮多少拍才熟。没在煮返回整段时长。

    口径与 planner._burn_eta_ticks 一致：`cooking_time` 是常量容量（本关恒
    12）、`cooking_progress` 是进度（单位≈秒）。把 cooking_time 当已耗时是
    旧代码踩过的坑。
    """
    if cooker is None:
        return int(DEFAULT_COOK_SECONDS * TICKS_PER_SECOND)
    if P.cooker_cooked(cooker):
        return 0
    total = cooker.get("cooking_time") or DEFAULT_COOK_SECONDS
    prog = cooker.get("cooking_progress") or 0
    try:
        left = max(0.0, float(total) - float(prog))
    except (TypeError, ValueError):
        left = float(DEFAULT_COOK_SECONDS)
    return int(left * TICKS_PER_SECOND)


def order_hand_ticks(world, book, order, plate_match=None, cooker_match=None):
    # type: (Any, Any, Dict, Optional[Dict], Optional[Dict]) -> int
    """这一单还要占用 P2 多少拍「手上工时」（不含等锅的空档）。

    沉没进度要折掉：盘里已有的料不再计取料，锅里已在煮的米不再计下锅。
    否则估计值与「已经干了一半」的事实脱节，slack 会一路悲观到全员必死。
    """
    plate = _plate_for(world, plate_match, order.get("id"))
    cooker = _cooker_for(world, cooker_match, order.get("id"))
    total = 0

    # ① 盘：没有匹配到盘就得先取一个（甚至先洗）
    if plate is None:
        total += COST_PLACE_PLATE
        if P.clean_plate_count(getattr(world, "plates", None)) <= 0:
            total += COST_WASH_PLATE

    # ② 逐样成分
    for ing in book.requirements(order):
        if plate is not None and book.plate_has(plate, ing):
            continue                      # 已装盘：沉没进度
        if book.needs_cooking(ing):
            in_pot = bool(cooker is not None
                          and parse_pot_contents(cooker.get("pot_contents") or []))
            if not in_pot:
                total += COST_FETCH_TO_COOKER
            total += COST_TAKE_COOKED     # 端盘去盛，无论是否已在煮
            continue
        if book.needs_chopping(ing):
            total += COST_FETCH_TO_BOARD + COST_CHOP
        total += COST_FETCH_TO_PLATE

    # ③ 送餐
    total += COST_DELIVER
    return total


def order_cook_ticks(world, book, order, plate_match=None, cooker_match=None):
    # type: (Any, Any, Dict, Optional[Dict], Optional[Dict]) -> int
    """这一单还要等多少拍的「锅上时钟」（不可压缩，但人在此期间自由）。"""
    plate = _plate_for(world, plate_match, order.get("id"))
    cooker = _cooker_for(world, cooker_match, order.get("id"))
    worst = 0
    for ing in book.requirements(order):
        if not book.needs_cooking(ing):
            continue
        if plate is not None and book.plate_has(plate, ing):
            continue
        worst = max(worst, _cook_remaining_ticks(cooker))
    return worst


def order_eta_ticks(world, book, order, plate_match=None, cooker_match=None):
    # type: (Any, Any, Dict, Optional[Dict], Optional[Dict]) -> int
    """单独做这一单要多少拍（不含别的单的排队占用）。

    关键路径而非求和：煮饭那 12 秒人可以去干别的，取 max 不取和。
    """
    hands = order_hand_ticks(world, book, order, plate_match, cooker_match)
    cook = order_cook_ticks(world, book, order, plate_match, cooker_match)
    return max(hands, cook)


def _remaining_of(order):
    # type: (Dict) -> float
    r = order.get("remaining")
    try:
        return float(r)
    except (TypeError, ValueError):
        return 999.0


def order_queue(world, book, plate_match=None, cooker_match=None, orders=None):
    # type: (Any, Any, Optional[Dict], Optional[Dict], Optional[List]) -> List[OrderETA]
    """按最早截止（EDF）排队后，算每单的累计完工时刻与 slack。

    为什么必须排队：P2 只有一双手，每单单独算都「来得及」是幻觉。本局
    order#3 吞掉 1214 拍仍过期，#4/#5 全程饿死——没人算过并发代价。

    P1 的产能不计入：本局 P1 整局只移动 55 拍、几乎站桩，按两人算会系统性
    乐观。等 p1_model 能给出可信的活跃度，再按它折扣这里的累计工时。

    orders 显式传入时只排这些单（planner 传「未被放弃」的列表）；缺省仍读
    world.orders，保持既有调用点不变。
    """
    raw = orders if orders is not None else getattr(world, "orders", None)
    orders = list(raw or ())
    if not orders:
        return []
    orders.sort(key=lambda o: (_remaining_of(o), str(o.get("id"))))

    rows = []  # type: List[OrderETA]
    busy = 0   # 前面**仍要做**的单已占用的手上工时
    for rank, o in enumerate(orders):
        hands = order_hand_ticks(world, book, o, plate_match, cooker_match)
        solo = order_eta_ticks(world, book, o, plate_match, cooker_match)
        # 累计：这一单最早也要等前面的手上工时做完。锅上时钟可以与别人的
        # 手上工时重叠，所以只对 hands 累加，再与自己的关键路径取 max。
        eta = max(solo, busy + hands)
        remaining_ticks = _remaining_of(o) * TICKS_PER_SECOND
        slack = remaining_ticks - eta
        busy += hands
        rows.append(OrderETA(o.get("id"), _remaining_of(o), o.get("lifetime"),
                             hands, solo, eta, slack, rank=rank))
    return rows


def solo_slack_by_order(world, book, plate_match=None, cooker_match=None,
                        orders=None):
    # type: (Any, Any, Optional[Dict], Optional[Dict], Optional[List]) -> Dict[Any, float]
    """{order_id: 独立 slack（拍）}。打分的 urgency 口径。

    只看这一单自己的关键路径，不含别单占用 —— 于是同款菜下它对 remaining
    单调，队首最紧迫。「该先做谁」本来就该由 EDF 的排队结论回答，而队列
    累计 slack 恰好把那个结论**反向**排列。
    """
    return {r.order_id: r.solo_slack
            for r in order_queue(world, book, plate_match, cooker_match,
                                 orders=orders)}

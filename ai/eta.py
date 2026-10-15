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

from ai import predicates as P
from ai.plate_tokens import parse_pot_contents

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
                 "solo_eta", "eta", "slack", "doomed", "solo_slack", "rank")

    def __init__(self, order_id, remaining, lifetime, hand_ticks,
                 solo_eta, eta, slack, doomed, solo_slack=None, rank=0):
        # type: (Any, float, Any, int, int, int, float, bool, Optional[float], int) -> None
        self.order_id = order_id
        self.remaining = remaining
        self.lifetime = lifetime
        self.hand_ticks = hand_ticks
        self.solo_eta = solo_eta
        self.eta = eta
        # 队列累计 slack：含前面各单的手上工时占用。**只用于判死**，不要拿它
        # 当优先级——队尾单背着全队工时，slack 必然最小，用它打分会得出
        # 「排得越后越紧急」的倒挂结论（见 solo_slack）。
        self.slack = slack
        self.doomed = doomed
        # 独立 slack：只看这一单自己的关键路径，不含别单占用。打分的 urgency
        # 读它——同款菜下它对 remaining 单调，队首自然最紧迫。
        self.solo_slack = (remaining * TICKS_PER_SECOND - solo_eta
                           if solo_slack is None else solo_slack)
        self.rank = rank            # EDF 队列位次（0 = 队首）

    def __repr__(self):
        return ("OrderETA(#%s rem=%.1fs eta=%d slack=%.0f%s)"
                % (self.order_id, self.remaining, self.eta, self.slack,
                   " DOOMED" if self.doomed else ""))


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
    for cid, oid in (cooker_match or {}).items():
        if oid != order_id:
            continue
        for c in (getattr(world, "cookers", None) or ()):
            if c.get("n") == cid:
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


# 判死的新鲜度下限：`remaining/lifetime` 高于这个比例的单一律不判死。
#
# 为什么必须有这条闸：队列累计下，只要同时有 4 单以上，队尾单**定义上**
# slack < 0 —— 判死就退化成了「排位歧视」而非可完成性判断。实机
# trace-20261014-204822：order#4 在 t=530 出现、t=549 判死，当时
# remaining=129.0/130s（寿命剩 99%）；#5 同样 19 拍就被拉黑。三个鱼单
# （#3/#4/#5）一单未送，而鱼单只比黄瓜单多一道同工时的切配 —— 它们被判死
# 纯因为到得晚。
#
# 判死还是自我实现的：判死 → 不投入 → 真做不完 → 过期，预测永不被证伪。
# 而队列会因为前面的单送出而松动（本局 t=3352 order#3 正是这样解禁，只是
# 已经太晚）。给新单留出观察期，让它等到队列真的腾不出位置再判。
#
# 取 0.75：本关寿命 130s，即「前 32 秒不判死」。一份寿司手上工时约 37 秒，
# 所以这段窗口内队首最多送出一单 —— 既给了松动的机会，又不至于让真必死的
# 单白吃掉一整份工时。
DOOM_FRESHNESS_FLOOR = 0.75


def _too_fresh_to_doom(order):
    # type: (Dict) -> bool
    """这单是否「太新，还不该判死」（见 DOOM_FRESHNESS_FLOOR）。"""
    try:
        lifetime = float(order.get("lifetime"))
        remaining = float(order.get("remaining"))
    except (TypeError, ValueError):
        return False                # 缺字段 → 不豁免，按原规则判
    if lifetime <= 0:
        return False
    return remaining / lifetime > DOOM_FRESHNESS_FLOOR


def order_queue(world, book, plate_match=None, cooker_match=None):
    # type: (Any, Any, Optional[Dict], Optional[Dict]) -> List[OrderETA]
    """按最早截止（EDF）排队后，算每单的累计完工时刻与 slack。

    为什么必须排队：P2 只有一双手，每单单独算都「来得及」是幻觉。本局
    order#3 吞掉 1214 拍仍过期，#4/#5 全程饿死——没人算过并发代价。

    P1 的产能不计入：本局 P1 整局只移动 55 拍、几乎站桩，按两人算会系统性
    乐观。等 p1_model 能给出可信的活跃度，再按它折扣这里的累计工时。
    """
    orders = list(getattr(world, "orders", None) or ())
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
        doomed = slack < 0 and not _too_fresh_to_doom(o)
        # **判死的单不占队列容量**：既然决定放弃它，它的工时就该让给后面的
        # 单。少了这一步，判死会沿队列雪崩——真实帧 t=1800 原本把 #4~#7 全
        # 判死，而实况是 #3/#4/#5 过期、#6/#7 送达成功。放开容量后结论变成
        # 「#3 可救、#4/#5 必死、#6/#7 可救」，与实况逐条吻合（#3 当时确实
        # 还有 +320 拍余量，它最后丢掉是执行浪费，不是排不进去）。
        if not doomed:
            busy += hands
        rows.append(OrderETA(o.get("id"), _remaining_of(o), o.get("lifetime"),
                             hands, solo, eta, slack, doomed, rank=rank))
    return rows


def doomed_order_ids(world, book, plate_match=None, cooker_match=None):
    # type: (Any, Any, Optional[Dict], Optional[Dict]) -> List[Any]
    """来不及做完的订单 id。可行性层据此停止投入（别再给它备料）。"""
    return [r.order_id
            for r in order_queue(world, book, plate_match, cooker_match)
            if r.doomed]


def slack_by_order(world, book, plate_match=None, cooker_match=None):
    # type: (Any, Any, Optional[Dict], Optional[Dict]) -> Dict[Any, float]
    """{order_id: 队列累计 slack（拍）}。**判死口径**。

    打分不要用它：队尾单背着全队工时，slack 必然最小，拿它当紧迫度会得出
    「排得越后越紧急」（实机 94% 的候选组倒挂）。打分走 solo_slack_by_order。
    """
    return {r.order_id: r.slack
            for r in order_queue(world, book, plate_match, cooker_match)}


def solo_slack_by_order(world, book, plate_match=None, cooker_match=None):
    # type: (Any, Any, Optional[Dict], Optional[Dict]) -> Dict[Any, float]
    """{order_id: 独立 slack（拍）}。打分的 urgency 口径。

    只看这一单自己的关键路径，不含别单占用 —— 于是同款菜下它对 remaining
    单调，队首最紧迫。「该先做谁」本来就该由 EDF 的排队结论回答，而队列
    累计 slack 恰好把那个结论**反向**排列。
    """
    return {r.order_id: r.solo_slack
            for r in order_queue(world, book, plate_match, cooker_match)}

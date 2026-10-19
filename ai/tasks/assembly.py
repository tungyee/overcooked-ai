"""装盘与出餐两 task：FetchToPlate / DeliverPlate（spec §6.1 第 9/10 号）。

本模块只放装盘出餐这一组，共享的身份机制与跨组谓词在 base.py。
外部不直接 import 本模块，一律 `from ai import tasks as T`
（分组边界是实现细节，见 ai/tasks/__init__.py）。
"""
from typing import Any, Dict, Optional, Tuple

from ai.query import predicates as P
from ai import primitives as PR
from ai.query import stations as S
from ai.exec.bt import Guard, Node, Sequence
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register,
                           ingredient_available, ingredient_source,
                           plate_by_id)


def _plate_on_serving_hatch(ctx, plate):
    # type: (Dict, Optional[Dict]) -> bool
    """这只盘是不是已经放在出餐口上了（即「已经交出去了」）。

    送餐成功后盘还会在快照里滞留约 11 拍（游戏侧动画 + 插件 5 Hz 推帧的
    延迟）。`feasible` 只看「盘存在 + 料齐」时，Planner 就把这只已经交出
    去的盘绑给下一单，11 拍后盘真的消失才 failure——每送一单都白搭一次，
    还往连续失败计数里灌脏数据（`scratch.abandoned` 会拉黑好盘）。
    实机复现：trace-20261014-004742 t=4134→4146、t=3238→3249。

    判据是**当前快照里的观测事实**（盘的坐标落在出餐口站点范围内），不靠
    「上一拍送过餐」这种记忆——符合无状态重算的约定（spec §6.4）。

    站点有 minx/minz/maxx/maxz 时按矩形判（出餐口是多格站点）；只有中心点
    坐标时退化为半格容差。
    """
    if not plate:
        return False
    # 被端着的盘不可能是「已经交出去」：P2 站在出餐口旁、面朝出餐口时，
    # 手持盘的坐标略在角色前方，会落进出餐口矩形框内，被误判成已交付 →
    # DeliverPlate 变不可行 → 送餐振荡（trace-20261019-122319）。
    if plate.get("held_by"):
        return False
    px, pz = plate.get("x"), plate.get("z")
    if px is None or pz is None:
        return False
    for st in ctx["w"].stations or ():
        if not isinstance(st, dict) or st.get("t") != S.PLATE_STATION:
            continue
        minx, minz = st.get("minx"), st.get("minz")
        maxx, maxz = st.get("maxx"), st.get("maxz")
        if None not in (minx, minz, maxx, maxz):
            if minx <= px <= maxx and minz <= pz <= maxz:
                return True
            continue
        sx, sz = st.get("x"), st.get("z")
        if sx is None or sz is None:
            continue
        if abs(px - sx) < 0.7 and abs(pz - sz) < 0.7:
            return True
    return False


def delivery_baseline_from(world):
    # type: (Any) -> Dict
    """抓一份出餐判定的基线快照（订单 id 集合 + 分数计数）。

    为什么需要基线：spec §6.1 第 10 号的两个信号都是"相对上一拍的变化"
    （订单消失 / 分数增加），而 done(ctx) 只看当前快照。基线由 Executor
    在建树时抓一次、塞进 ctx["baseline"]——这是 spec §7.6"Executor 持有
    树"的自然延伸。存黑板会让 done 有副作用，且 Planner 每拍重算时失真。

    只记 deliveries 与 base，不记整个 score dict：实测 combo/multiplier
    会在没送餐时归零（帧 686：gone=set()、(2,120,11,2,2)→(2,120,11,0,0)），
    拿整个 dict 比较会把连击断掉误判成送餐成功。
    """
    score = getattr(world, "score", None) or {}
    return {
        "order_ids": frozenset(
            str(o.get("id")) for o in (getattr(world, "orders", None) or ())),
        "deliveries": _int(score.get("deliveries")),
        "base": _int(score.get("base")),
    }


def _int(v):
    # type: (Any) -> int
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


@_register
class FetchToPlate(Task):
    """第 9 号：把一样料放进已就位的盘。

    item 是**订单成分名**（Seaweed / Cucumber / BoiledSushiRice），与
    订单需求同口径。取料目标与完成判定是同一个 item 的两种形态：
      - 需切的料：取 chop_target(item)（板上的 ChoppedCucumber）
      - 完成判定：book.plate_has(p, item)（盘里的 cucumber）
    见 RecipeBook.plate_tokens / want_tokens 的注释。

    **不认领任何资源**（spec §6.1 第 9 号的认领列为空）：料在箱子里无限
    供应，盘已由 PlaceCleanPlate 认领过。重复认领盘会把自己的下游
    DeliverPlate 挡在外面。
    """

    TYPE = "FetchToPlate"
    HANDS = "empty"       # 要空手去拿料；手里有东西时打分加腾手代价（spec §6.1(b)）

    @property
    def plate_id(self):
        return self._resources[0]

    def claims(self):
        return ()

    def done(self, ctx):
        # type: (Dict) -> bool
        """绑定盘已含该料。按装盘态词汇判（spec §6.0 的绑定参数求值）。"""
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False
        return ctx["book"].plate_has(p, self.item)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False       # 盘被端走（Review Focus 5）
        if self.done(ctx):
            return False
        return ingredient_available(ctx, self._fetch_form(ctx))

    def _fetch_form(self, ctx):
        # type: (Dict) -> Optional[str]
        """该去取哪个形态的料。

        需切的料取切好的产物：生黄瓜进盘做不成寿司，所以没切好时这里
        返回 ChoppedCucumber、ingredient_available 查不到 → 判不可行，
        把机会让给 FetchToBoard / Chop。
        """
        book = ctx["book"]
        if book.needs_chopping(self.item):
            return book.chop_target(self.item)
        return self.item

    def build(self):
        # type: () -> Node
        plate_id, item = self.plate_id, self.item

        def src(c):
            return ingredient_source(c, _fetch_form_of(c, item))

        def holding(c):
            return P.held_matches(c["chef"], _fetch_form_of(c, item))

        def in_plate(c):
            p = plate_by_id(c, plate_id)
            return p is not None and c["book"].plate_has(p, item)

        return PR.Timeout(Sequence([
            # 树首清手：被抢占时手里可能还端着上一 task 的盘子（spec §7.4）；
            # keep：手里已经是目标料就别放下再捡
            PR.EnsureHandsFree(keep=lambda c: P.held_matches(
                c["chef"], _fetch_form_of(c, item))),
            PR.InteractWith(src, "pickup", holding),
            PR.InteractWith(lambda c: plate_by_id(c, plate_id),
                            "placement", in_plate),
        ]), TASK_TIMEOUT_TICKS)


def _fetch_form_of(ctx, item):
    # type: (Dict, Optional[str]) -> Optional[str]
    """FetchToPlate._fetch_form 的自由函数版，供树内闭包用。"""
    book = ctx["book"]
    if book.needs_chopping(item):
        return book.chop_target(item)
    return item


@_register
class DeliverPlate(Task):
    """第 10 号：端起料齐的盘送到出餐口。

    **完成判定用双信号**（spec §6.1）："订单消失 **或** 分数增加，因为
    人类可能同时送餐，单看订单消失会误判。"两者都要求手先空下来——盘
    还在手上时什么都没发生。

    基线见 delivery_baseline_from：只比 deliveries 与 base，不比整个
    score dict，因为 combo/multiplier 会在没送餐时归零。
    """

    TYPE = "DeliverPlate"
    HANDS = "empty"       # 要空手去端盘；手里有东西时打分加腾手代价（spec §6.1(b)）

    @property
    def plate_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.plate_id, "deliver"),)

    def _order(self, ctx):
        # type: (Dict) -> Optional[Dict]
        for o in ctx["w"].orders:
            if str(o.get("id")) == str(self.order_id):
                return o
        return None

    def done(self, ctx):
        # type: (Dict) -> bool
        base = ctx.get("baseline")
        if not base:
            return False       # 没基线不瞎猜：宁可多站一拍，不可误判
        if not P.hands_free(ctx["chef"]):
            return False       # 盘还在手上，什么都没发生
        now_ids = frozenset(
            str(o.get("id")) for o in ctx["w"].orders)
        if str(self.order_id) in base["order_ids"] \
                and str(self.order_id) not in now_ids:
            return True        # 信号一：我这单消失了
        score = getattr(ctx["w"], "score", None) or {}
        return (_int(score.get("deliveries")) > base["deliveries"]
                or _int(score.get("base")) > base["base"])

    def feasible(self, ctx):
        # type: (Dict) -> bool
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False
        if _plate_on_serving_hatch(ctx, p):
            return False       # 已经交出去了，别用它再送下一单（见该函数注释）
        order = self._order(ctx)
        if order is None:
            return False       # 订单没了 → 该走 ResetPlate
        return ctx["book"].missing(order, p) == ()

    def build(self):
        # type: () -> Node
        plate_id = self.plate_id

        def held_target(c):
            # 手里持盘时 chef.held 存的是盘名（equipment_plate_01），不是
            # 实例 id（-78262）。held_matches 是精确名匹配，传 id 恒为假，
            # 会让树首 EnsureHandsFree 把手里目标盘当杂物放下、pickup 又捡回，
            # 送餐那一步永远走不到（实机：端盘在空柜台反复放/捡、出餐口
            # 看起来像找不到）。所以先按 id 反查盘，再用盘名去匹配手。
            p = plate_by_id(c, plate_id)
            return p is not None and P.held_matches(c["chef"], p.get("name"))

        def not_grabbed_by_p1(c):
            # Observer Abort（同灭火 Guard）：P1 把目标盘端走了就立刻 halt 本树、
            # FAILURE 交还 Planner 重新绑定。只排除 P1——P2 自己端着的正是
            # held_target 要送的那块，不能误 abort。
            p = plate_by_id(c, plate_id)
            if p is None:
                return False       # 盘没了 → 交还 Planner
            p1 = P.chef_by_name(c["w"].chefs, P.P1_NAME)
            return not (p1 is not None and P.held_matches(p1, p.get("name")))

        def serving(c):
            # 出餐口是 plate_station；plate_return 是脏盘回收口/晾干架，
            # 把组装好的盘放那里永远不结算（实机：端盘在回收口附近打转）。
            return S.find_station(c["w"].stations, S.PLATE_STATION,
                                  (c["chef"]["x"], c["chef"]["z"]))

        return PR.Timeout(Guard(not_grabbed_by_p1, Sequence([
            # 树首清手：被抢占时手里可能还端着料/别的盘，先放下再端目标盘（spec §7.4）；
            # keep：手里已经是目标盘就别放下再端
            PR.EnsureHandsFree(keep=held_target),
            PR.InteractWith(lambda c: plate_by_id(c, plate_id), "pickup",
                            held_target),
            PR.InteractWith(serving, "placement",
                            lambda c: P.hands_free(c["chef"])),
        ])), TASK_TIMEOUT_TICKS)

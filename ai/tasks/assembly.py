"""装盘与出餐三 task：FetchToPlate / DeliverPlate / MergePlates（spec §6.1
第 9/10 号 + 盘合并 spec 2026-11-01-plate-merge-design §5.1）。

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
                           held_matches_contents, ingredient_available,
                           ingredient_source, plate_by_id, plate_matches_order)


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

    resources = (plate_id,) 或 (plate_id, contents)。contents 是**已装料载体**
    的内容签名（ai.query.carriers.signature，如 ('cooked:burritomeat:fryingpan',)）：
    取的不是一份普通底料，而是包了这些料的那件（包肉的卷饼），放进盘时底料
    与内容一起进盘。签名进身份，所以「包肉卷饼」与「空卷饼」是两个候选、
    分开打分；身份不含位置，拿起后承诺不断。

    **不认领任何资源**（spec §6.1 第 9 号的认领列为空）：料在箱子里无限
    供应，盘已由 PlaceCleanPlate 认领过。重复认领盘会把自己的下游
    DeliverPlate 挡在外面。
    """

    TYPE = "FetchToPlate"
    # 给对话层 LLM 看的一句话说明（ai/dialog/duties.py 渲染 DAG 时带出）。
    DESC = "把这种料（需要切的取切好的）放进盘里"
    LABEL = "装盘"
    HANDS = "empty"       # 要空手去拿料；手里有东西时打分加腾手代价（spec §6.1(b)）

    @property
    def plate_id(self):
        return self._resources[0]

    @property
    def contents(self):
        # type: () -> Tuple[str, ...]
        """载体内容签名；普通料为空元组。"""
        return tuple(self._resources[1]) if len(self._resources) > 1 else ()

    def claims(self):
        return ()

    def done(self, ctx):
        # type: (Dict) -> bool
        """绑定盘已含该料（载体：底料与全部内容都已进盘）。按装盘态词汇判。"""
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False
        if not ctx["book"].plate_has(p, self.item):
            return False
        return set(self.contents) <= ctx["book"].plate_tokens(p)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False       # 盘被端走（Review Focus 5）
        if self.done(ctx):
            return False
        if not plate_matches_order(ctx, p, self.order_id):
            return False       # 盘内容与该单不再兼容（P1 放错料）
        if self.contents and not _carrier_fits_plate(ctx, p, self.order_id,
                                                     self.item, self.contents):
            return False       # 载体的料盘里已有 / 该单不要
        return ingredient_available(ctx, self._fetch_form(ctx), self.contents)

    def _fetch_form(self, ctx):
        # type: (Dict) -> Optional[str]
        """该去取哪个形态的料。

        需切的料取切好的产物：生黄瓜进盘做不成寿司，所以没切好时这里
        返回 ChoppedCucumber、ingredient_available 查不到 → 判不可行，
        把机会让给 FetchToBoard / Chop。
        """
        return _fetch_form_of(ctx, self.item)

    def build(self):
        # type: () -> Node
        plate_id, item, contents = self.plate_id, self.item, self.contents

        def src(c):
            return ingredient_source(c, _fetch_form_of(c, item), contents)

        def holding(c):
            # 名字 + 内容：只比名字时，句柄选中旁边同名的空卷饼也算拿对了
            return held_matches_contents(c, _fetch_form_of(c, item), contents)

        def in_plate(c):
            p = plate_by_id(c, plate_id)
            return p is not None and c["book"].plate_has(p, item) \
                and set(contents) <= c["book"].plate_tokens(p)

        return PR.Timeout(Sequence([
            # 树首清手：被抢占时手里可能还端着上一 task 的盘子（spec §7.4）；
            # keep：手里已经是目标料就别放下再捡（拿错同名不同料的会被放下）
            PR.EnsureHandsFree(keep=holding),
            PR.InteractWith(src, "pickup", holding),
            PR.InteractWith(lambda c: plate_by_id(c, plate_id),
                            "placement", in_plate),
        ]), TASK_TIMEOUT_TICKS)


def _carrier_fits_plate(ctx, plate, order_id, item, contents):
    # type: (Dict, Dict, Any, Optional[str], Any) -> bool
    """载体（底料 item + contents）放进这只盘后仍是该单的子集、且不重复。"""
    from ai.query.carriers import carrier_fits
    order = None
    for o in ctx["w"].orders:
        if str(o.get("id")) == str(order_id):
            order = o
    if order is None:
        return order_id is None
    base = str(_fetch_form_of(ctx, item) or "").strip().lower()
    return carrier_fits(ctx["book"], {"base": base, "contents": frozenset(contents)},
                        order, plate)


def _fetch_form_of(ctx, item):
    # type: (Dict, Optional[str]) -> Optional[str]
    """该去取哪个形态的料（需切的取切好的产物）。FetchToPlate 与树内闭包共用。"""
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
    # 给对话层 LLM 看的一句话说明（ai/dialog/duties.py 渲染 DAG 时带出）。
    DESC = "把配齐的盘送到出餐口"
    LABEL = "送餐"
    # 不分菜的共享节点（ai/dialog/duties.py）。
    SHARED = True
    # 送餐不开放「交给 P1 / 先别做」：谁在旁边谁送，只能让 P2 马上去或优先做。
    DUTY_MODES = ("now", "do")
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
            return P.holds_plate(c["chef"], p)

        def not_grabbed_by_p1(c):
            # Observer Abort（同灭火 Guard）：P1 把目标盘端走了就立刻 halt 本树、
            # FAILURE 交还 Planner 重新绑定。只排除 P1——P2 自己端着的正是
            # held_target 要送的那块，不能误 abort。
            p = plate_by_id(c, plate_id)
            if p is None:
                return False       # 盘没了 → 交还 Planner
            p1 = P.chef_by_name(c["w"].chefs, P.P1_NAME)
            return not P.holds_plate(p1, p)

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


@_register
class MergePlates(Task):
    """把 src 盘的内容倒进 dst 盘（盘倒盘合并）。

    游戏引擎允许手里 A 盘对台面上 B 盘 placement，A 的内容并进 B、A 变空盘
    仍在手（trace-20261101-214045 t=851）。本 task 把「互补的两块半成品盘
    合成一单」纳入行为树：src/dst 由 planner 的 `_gen_merge` 定向——并集
    ⊆ 某单需求且缺料数严格减少（spec 2026-11-01-plate-merge-design §5.1）。
    """

    TYPE = "MergePlates"
    # 给对话层 LLM 看的一句话说明（ai/dialog/duties.py 渲染 DAG 时带出）。
    DESC = "把一块盘的内容倒进另一块盘"
    LABEL = "并盘"
    HANDS = "empty"       # 要空手去端 src 盘；手里有东西时打分加腾手代价

    @property
    def src_id(self):
        return self._resources[0]

    @property
    def dst_id(self):
        return self._resources[1]

    def claims(self):
        return ((self.src_id, "merge"), (self.dst_id, "merge"))

    def done(self, ctx):
        # type: (Dict) -> bool
        src = plate_by_id(ctx, self.src_id)
        return src is not None and P.plate_count(src) == 0

    def feasible(self, ctx):
        # type: (Dict) -> bool
        if self.done(ctx):
            return False
        src = plate_by_id(ctx, self.src_id)
        dst = plate_by_id(ctx, self.dst_id)
        if src is None or dst is None or self.src_id == self.dst_id:
            return False
        if P.plate_count(src) == 0 or P.plate_count(dst) == 0:
            return False
        order = None
        for o in ctx["w"].orders:
            if str(o.get("id")) == str(self.order_id):
                order = o
                break
        if order is None:
            return False
        book = ctx["book"]
        if not (book.plate_tokens(src) | book.plate_tokens(dst)) \
                <= book.order_want_tokens(order):
            return False
        # 严格进展：并集比任一单盘都更接近该单（双方各缺对方有的料，重叠/被
        # 包含的盘对不算）。用顶层成分计数（missing/missing_union），不用
        # token 集差——token 集对熟料会重复计（rich+opaque 两个 token）。
        missing_after = len(book.missing_union(order, [src, dst]))
        if not (missing_after < len(book.missing(order, src))
                and missing_after < len(book.missing(order, dst))):
            return False
        if _plate_on_serving_hatch(ctx, src) or _plate_on_serving_hatch(ctx, dst):
            return False
        p1 = P.chef_by_name(ctx["w"].chefs, P.P1_NAME)
        return not P.holds_plate(p1, dst)

    def build(self):
        # type: () -> Node
        src_id, dst_id = self.src_id, self.dst_id

        def holding_src(c):
            return P.holds_plate(c["chef"], plate_by_id(c, src_id))

        def merged(c):
            src = plate_by_id(c, src_id)
            return src is not None and P.plate_count(src) == 0

        return PR.Timeout(Sequence([
            PR.EnsureHandsFree(keep=holding_src),
            PR.InteractWith(lambda c: plate_by_id(c, src_id), "pickup",
                            holding_src),
            PR.InteractWith(lambda c: plate_by_id(c, dst_id), "placement",
                            merged),
        ]), TASK_TIMEOUT_TICKS)

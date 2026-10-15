"""烹饪四 task：FetchToCooker / TakeCookedToPlate / ReturnPot / RescueCooked（spec §6.1 第 3/4/5/6 号）。

本模块只放烹饪这一组，共享的身份机制与跨组谓词在 base.py。
外部不直接 import 本模块，一律 `from ai import tasks as T`
（分组边界是实现细节，见 ai/tasks/__init__.py）。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai import cook_supply as CS
from ai import predicates as P
from ai import primitives as PR
from ai import stations as S
from ai.bt import Condition, Node, Selector, Sequence
from ai.plate_tokens import parse_plate_detail, parse_pot_contents
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register,
                           cooker_state, cooker_station, cooked_sources,
                           ingredient_available, ingredient_source,
                           loose_count, plate_by_id, placed_plates)

# 端盘在锅前等熟的最长拍数（15s）。煮一锅饭实测约 11s，取盘+走到锅前再
# 留足余量；超时交还 Planner，避免无限等一个可能永远不成立的状态。
WAIT_COOKED_TIMEOUT_TICKS = 300

# TakeCookedToPlate 现在跨「取盘 + 走到锅前 + 等熟 + 盛饭」四段，比普通
# task 长，超时上限单独放宽（20s，比默认 TASK_TIMEOUT_TICKS=200 的 10s 翻倍）。
TAKE_COOKED_TIMEOUT_TICKS = 400

# Python 主循环拍率（AGENTS.md §2：主循环 20 Hz，「拍」=50ms）。
TICKS_PER_SECOND = 20

# 「来不及端盘来盛」的判定阈值（拍）。实测一趟 TakeCookedToPlate（走到盘
# 堆/晾干架取盘 → 端回锅前 → 盛饭）约 120~145 拍（trace-20261014-011202
# 的 t=654→776、1919→2056 两次成功样本），这里取 150 拍（7.5s）含安全余量：
# 烧糊剩余时间短于它就别指望端盘来救，直接端锅离灶停计时。
RESCUE_DEADLINE_TICKS = 150


@_register
class FetchToCooker(Task):
    """第 3 号：把生料下进该灶的空锅。

    item 是**熟料名**（如 BoiledSushiRice / Pasta_TomatoOnly）——订单需求里
    写的是熟料，生料由 RecipeBook.raws_of 反查（多料同锅会返回多个）。进炉
    形态由 cook_input_form 决定（需切的取切好产物）。resources 第二项可显式
    带生料表（DAG 发射时传入），缺省时运行时从 book.raws_of(item) 反查。
    """

    TYPE = "FetchToCooker"

    @property
    def cooker_id(self):
        return self._resources[0]

    @property
    def raws(self):
        # type: () -> Tuple[str, ...]
        if len(self._resources) > 1 and self._resources[1]:
            return tuple(self._resources[1])
        return ()

    def claims(self):
        return ((self.cooker_id, "fetch_to"),)

    def _raws(self, ctx):
        # type: (Dict) -> Tuple[str, ...]
        return self.raws or ctx["book"].raws_of(self.item)

    def done(self, ctx):
        # type: (Dict) -> bool
        """全部进炉形态都已进锅（或该灶已开煮）。"""
        c = cooker_state(ctx, self.cooker_id)
        if c is None:
            return False
        if P.cooker_started(c):
            return True
        raws = self._raws(ctx)
        if not raws:
            return False
        forms = {str(ctx["book"].cook_input_form(r)).lower() for r in raws}
        contents = set(parse_pot_contents(c.get("pot_contents") or []))
        return forms <= contents

    def feasible(self, ctx):
        # type: (Dict) -> bool
        c = cooker_state(ctx, self.cooker_id)
        if c is None or not P.cooker_has_pot(c):
            return False       # cooked=None → 灶上没锅，没法下料
        if P.cooker_started(c):
            return False       # 已在煮/已熟
        if P.cooker_burning(c):
            return False       # 着火/烧糊的锅不放料（spec §5.3）
        raws = self._raws(ctx)
        if not raws:
            return False
        return all(ingredient_available(ctx, ctx["book"].cook_input_form(r))
                   for r in raws)

    def build(self):
        # type: () -> Node
        cooker_id, cooked = self.cooker_id, self.item
        raws = self.raws

        # 单料：散落优先——地上有散落米就直接拿散落米下锅（find_ingredient
        # 的 loose 优先），散落不够才从箱子批量投米兜底。
        if len(raws) <= 1:
            def raw(c):
                return raws[0] if raws else c["book"].raw_of(cooked)

            def form(c):
                return c["book"].cook_input_form(raw(c))

            def crate(c):
                return S.find_crate(c["w"].stations, form(c))

            def holding(c):
                return P.held_matches(c["chef"], form(c))

            def has_loose(c):
                return loose_count(c["w"], form(c)) > 0

            def placed(c):
                cc = cooker_state(c, cooker_id)
                if cc is None:
                    return False
                if P.cooker_started(cc):
                    return True
                return str(form(c)).lower() in parse_pot_contents(
                    cc.get("pot_contents") or [])

            def pickup():
                # 散落料（投掷落地）没有 m_gridLocation 朝向信号——实机：
                # interactions.gx/gz 指向别处，Face 永远转不到位，只会反复
                # 短按方向键把角色一点点挪走、米饭捡不起来。face=False 直接
                # 走到 Tap（pickup 句柄在相邻格已就绪）。
                return PR.InteractWith(
                    lambda x: ingredient_source(x, form(x)), "pickup", holding,
                    face=False)

            def drop():
                return PR.InteractWith(
                    lambda x: cooker_station(x, cooker_id), "placement", placed)

            return PR.Timeout(Selector([
                # 分支1：地上有散落米 → 直接拿散落米下锅（最便宜的一路，
                # 不管米是上一轮剩的还是谁投过去的）
                Sequence([
                    Condition(has_loose),
                    PR.EnsureHandsFree(keep=holding),
                    pickup(),
                    drop(),
                ]),
                # 分支2：地上没散落米 → 从箱子取一份直接下锅。
                #
                # 这里曾是「RepeatUntil 批量取+投 N 份，再捡散落下锅」。
                # 批量投的本意是摊销往返箱子的路程，但树结构决定了必须
                # **投满 N 份才轮到 drop()**，而一轮「走到箱子→取→转向
                # →投」实测约 80 拍，整树却裹在 TASK_TIMEOUT_TICKS=200 里
                # ——第二轮没结束就被砍，planner 换个 order 重建树又从第一
                # 轮开始，`drop()` 一次都没执行过。实机后果：米全堆在地上、
                # 灶始终空着、整局 0 送达（trace-20261014-011202，2527 拍）。
                #
                # 投掷本身也不该留在 N=1 的路径上：摊销为零，却保留了落点
                # 不可控的缺点（米曾落到地图边界行 (4,4)，Approach 够不到
                # 就反复失败）。直接拿着走一次更短。这样单料与多料路径同构。
                #
                # 真要找回摊销，正确的位置是独立的「囤料」task 由 planner
                # 按分数决定，而不是塞进 FetchToCooker 当前置。
                Sequence([
                    PR.EnsureHandsFree(keep=holding),
                    PR.InteractWith(crate, "pickup", holding),
                    drop(),
                ]),
            ]), TASK_TIMEOUT_TICKS)

        # 多料：逐料下锅（原有逻辑）
        def raw_src(c, r):
            return ingredient_source(c, c["book"].cook_input_form(r))

        def holding(c, r):
            return P.held_matches(c["chef"], c["book"].cook_input_form(r))

        def placed(c, r):
            cc = cooker_state(c, cooker_id)
            if cc is None:
                return False
            if P.cooker_started(cc):
                return True
            form = c["book"].cook_input_form(r)
            return str(form).lower() in parse_pot_contents(
                cc.get("pot_contents") or [])

        steps = [PR.EnsureHandsFree(keep=lambda c: holding(c, raws[0]))]
        for r in raws:
            steps.append(PR.InteractWith(
                lambda c, r=r: raw_src(c, r), "pickup",
                lambda c, r=r: holding(c, r)))
            steps.append(PR.InteractWith(
                lambda c: cooker_station(c, cooker_id), "placement",
                lambda c, r=r: placed(c, r)))
        return PR.Timeout(Sequence(steps), TASK_TIMEOUT_TICKS)


@_register
class FillCooker(FetchToCooker):
    """填灶：订单无关地把生料下进空锅，让灶常驻满载。

    **为什么不能靠调权重解决**（第 1 组 1.1）：`FetchToCooker` 绑订单，
    只能由 `recipe_graph.emit` 按单发射，`_in_flight_cook_claim` 压掉同一单
    的第二个下锅候选，`_match_cookers_to_orders` 又是严格 1:1 —— 结构上
    「5 单要米」就不可能变成「3 口灶在烧」。实测 trace-20261014-120406 的
    1700~3270 窗口 **59% 的拍里三口灶一个都没在煮**（峰值也只有 2 口），而
    order#3/4/5 三单全在这个窗口过期。灶空转的机会成本是 12 秒不可压缩的
    时钟，补不回来。

    所以这里换结构而不是换数字：`order_id=None`，谓词是**纯世界谓词**
    （照 `Prefetch` 的形态）——有空灶 + 这种熟料还有缺口 + 料拿得到。
    缺口 = 需求（还有几单要）- 供给（在煮 + 已熟未端 + 手持 + 已装盘），
    见 `ai.cook_supply`。供给够了就不再下，避免把空灶耗光挡住别的料。

    认领沿用 `(cooker_id, "fetch_to")`，与 `FetchToCooker` 同粒度，两者不会
    对同一口灶同时下料。执行树完全复用父类（下料动作一模一样，只是触发
    理由不同），所以这里只覆写身份与谓词。
    """

    TYPE = "FillCooker"
    HANDS = "empty"

    def __init__(self, order_id=None, resources=(), item=None):
        # type: (Any, Any, Any) -> None
        # 订单无关是这个 task 的定义性质，不接受调用方传 order_id：
        # 一旦带上订单就又落回按单发射的那三道结构限制里。
        super(FillCooker, self).__init__(None, resources, item)

    def done(self, ctx):
        # type: (Dict) -> bool
        """锅里已经有料（或已开煮）就算完成——不问是不是"这一单"的料。"""
        c = cooker_state(ctx, self.cooker_id)
        if c is None:
            return False
        if P.cooker_started(c):
            return True
        return bool(parse_pot_contents(c.get("pot_contents") or []))

    def feasible(self, ctx):
        # type: (Dict) -> bool
        w, book = ctx["w"], ctx["book"]
        c = cooker_state(ctx, self.cooker_id)
        if c is None or not P.cooker_has_pot(c):
            return False
        if P.cooker_started(c) or P.cooker_burning(c):
            return False
        if parse_pot_contents(c.get("pot_contents") or []):
            return False       # 锅里已有料，等它煮完
        if CS.cooked_shortfall(w, book, self.item) <= 0:
            return False       # 供给已覆盖需求，再下就是浪费灶
        raws = self._raws(ctx)
        if not raws:
            return False
        return all(ingredient_available(ctx, book.cook_input_form(r))
                   for r in raws)


@_register
class TakeCookedToPlate(Task):
    """第 4 号：端着盘子去熟料源把熟料盛进缺此料的盘。

    resources = (source_id, plate_id)。source_id 是熟料源：灶实例名（锅在
    灶上）或散锅 item 名（锅在台面/地上，RescueCooked 救下后离灶但照样
    能盛饭）。盘绑定在身份里是 spec §6.0 的硬要求：两单都缺同一样料时，
    P1 倒进盘 B 不能让针对盘 A 的 task 误报 SUCCESS。

    全程不把锅端离原处：端盘走到源前交互盛饭，锅（空锅）留在灶上/台面。
    """

    TYPE = "TakeCookedToPlate"
    HANDS = "empty"       # 要空手去端盘；手不空时打分加腾手代价（spec §6.1(b)）

    @property
    def cooker_id(self):
        return self._resources[0]

    @property
    def plate_id(self):
        return self._resources[1]

    def claims(self):
        return ((self.cooker_id, "take_cooked"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        """绑定盘已含该熟料（spec §6.1）。

        判定走 book.plate_has 的**装盘态词汇**，不是字面比较：实测盘里
        记的是不透明 token CookedCompositeAssembledNode（detail 帧里是
        rich cooked:{raw}:{cookware}），而非熟料名。字面比较恒报"还缺"→
        本 task 的 done 永不成立 → AI 把饭倒进盘后以为没倒，无限重倒
        （见 RecipeBook.plate_tokens）。
        """
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False
        return ctx["book"].plate_has(p, self.item)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        """可行性分两段（spec §6.1 第 4 号 + §6.5）：

          - **启动看源**：灶上熟料 / 台面散熟锅才值得去取；
          - **执行中看手**：本 task 第一步就是把盘从原位拿起，盘随即离开
            原位。若可行性绑死在"盘还在原位"，它一动手就把自己判成不可行 →
            被承诺惯性抛弃 → 死循环（实机复现）。故"目标盘已在手上"也算可行。

        无论灶上锅还是散熟锅都走「端盘盛饭」：执行中 = 手持目标盘
        （_holding_plate，按盘名精确匹配，不是实例 id）。
        """
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False                       # 目标盘被端走
        if not ctx["book"].plate_lacks(p, self.item):
            return False                       # 盘里已有这份熟料（done 边界）
        if _holding_plate(ctx, self.plate_id):
            return True                        # 执行中：目标盘已在手上
        return self._source(ctx) is not None

    def _source(self, ctx):
        # type: (Dict) -> Optional[Dict]
        """按 source id 解析熟料源（灶上煮中/熟锅 / 台面散熟锅）。

        返回 {kind, target}；源不存在、无锅或烧糊时返回 None。灶上锅「煮中」
        也算源——端盘盛饭要提前走到锅前等熟（否则等熟了才去拿盘会来不及，
        实机烧糊）。散锅要识别成 cooked:* token（items[].detail），不能只按
        名字匹配——同名散锅可能是生锅。
        """
        sid = self.cooker_id
        c = cooker_state(ctx, sid)
        if c is not None:
            if P.cooker_started(c) and not P.cooker_burning(c):
                return {"kind": "cooker", "target": cooker_station(ctx, sid)}
            return None
        for it in ctx["w"].items:
            if str(it.get("n")) != str(sid):
                continue
            toks = parse_plate_detail(it.get("detail") or [])
            if any(t.startswith("cooked:") for t in toks):
                return {"kind": "loose", "target": it}
            # 与 cooked_sources 同款兜底：detail 只报原始料名（ing:SushiRice）、
            # 没有 cooked: token 时，cooked 字段才是熟的权威信号。漏掉会把熟锅
            # 当生锅 → feasible 恒 False → 有熟饭却取不动（AI 卡死）。
            if str(it.get("cooked") or "").lower() == "cooked":
                return {"kind": "loose", "target": it}
            return None
        return None

    def build(self):
        # type: () -> Node
        plate_id, cooked = self.plate_id, self.item

        def holding_plate(c):
            p = plate_by_id(c, plate_id)
            return p is not None and P.held_matches(c["chef"], p.get("name"))

        def plate_target(c):
            return plate_by_id(c, plate_id)

        def source_target(c):
            # 灶上锅 / 散熟锅都端盘去盛：源就是 _source 解析出的目标
            # （灶站点 dict 或散锅 item dict），不必再区分。
            s = self._source(c)
            return s["target"] if s else None

        def source_ready(c):
            # 散熟锅恒熟；灶上锅 cooked 才算熟（煮中要等）。
            s = self._source(c)
            if s is None:
                return False
            if s["kind"] == "loose":
                return True
            cc = cooker_state(c, self.cooker_id)
            return P.cooker_cooked(cc) and not P.cooker_burning(cc)

        def poured(c):
            p = plate_by_id(c, plate_id)
            return p is not None and c["book"].plate_has(p, cooked)

        # ⚠️ 盛饭的交互句柄 kind 与 FetchToCooker 把料放进锅对称，先用
        # "placement"；实机若 trace 里 Tap 反复 FAILURE（interact_ready 命中
        # 不到锅），说明端盘面对锅时插件报的是 pickup 句柄，改成 "pickup"
        # 或去掉 kind 走通用交互格再验证。
        return PR.Timeout(Sequence([
            PR.EnsureHandsFree(keep=holding_plate),
            PR.InteractWith(plate_target, "pickup", holding_plate),
            # 提前站到锅前等熟：端盘盛饭前置了取盘动作，必须趁煮的时候就
            # 把盘端到锅边、熟了立刻盛（等熟了才去拿盘会来不及，实机烧糊）。
            PR.Approach(source_target, "placement"),
            PR.Face(source_target),
            PR.WaitUntil(source_ready, WAIT_COOKED_TIMEOUT_TICKS),
            PR.InteractWith(source_target, "placement", poured),
            # 树尾不许挂等待节点（与旧实现同款原因）：盘里一有料本 task 就
            # 不可行 → planner 下一拍换 task → executor halt 这棵树；若树尾有
            # 等待节点，成功记账（释放认领）会被静默吞掉，下一拍又去取熟料
            # 重复倒盘。"料已进盘"由 placement 步的 expect=poured 确认。
        ]), TAKE_COOKED_TIMEOUT_TICKS)


def _holding_plate(ctx, plate_id):
    # type: (Dict, Any) -> bool
    """手持的正是本 task 绑定的那块盘（按盘名精确匹配）。

    盘在 chef.held 里存的是盘名（equipment_plate_01），不是实例 id，所以
    先按 id 反查盘、再用盘名匹配手（与 DeliverPlate.held_target 同源）。
    """
    p = plate_by_id(ctx, plate_id)
    return p is not None and P.held_matches(ctx["chef"], p.get("name"))


def _holding_cooked(ctx):
    # type: (Dict) -> bool
    """手里端着的那口锅里有熟料（本 task 的"料已经到手了"判据）。

    只判"手持锅具"不够：空锅也满足，会让 pickup 步骤永远短路。
    """
    return (P.held_cookware_state(ctx["chef"], ctx["w"].items, ctx["w"].recipes)
            == P.COOKWARE_COOKED)


def _about_to_burn(c):
    # type: (Optional[Dict]) -> bool
    """已经糊/着火，或**来不及**端盘来盛了 → 立刻端锅离灶止损。

    判据从「已过烧糊点」改成带余量的预测。旧版要求
    `progress >= 2*ctime`——那一刻锅**已经着火**了，RescueCooked 这个
    兜底等于永远晚一步：实机 trace-20261014-011202 t=2056~2184，灶 (2)
    的饭 progress 16.9 就已熟，P2 取盘再走回来约 130 拍，而锅 140 拍后
    着火；planner 一路让它端着盘跑，直到 t=2184（progress 24、burning
    已置位）才选中 RescueCooked，下一拍 Arbiter 就切进 Firefighting。

    现在改为：烧糊剩余时间 < 端盘那一趟的预计耗时 + 安全余量就算"来不及"。
    端锅只是止损（料留着、停计时），比烧糊着火便宜得多；但也不能太激进，
    否则刚熟的锅全被端走、TakeCookedToPlate 永远轮不到——所以余量按
    「取盘往返」这个具体动作估，不是拍一个大常数。

    烧糊剩余时间的口径与 planner._burn_eta_ticks 一致（读
    cooking_progress，糊点 = 2*cooking_time）。
    """
    if c is None:
        return False
    if c.get("burning") or c.get("onFire"):
        return True
    eta = _burn_eta_ticks(c)
    return eta is not None and eta < RESCUE_DEADLINE_TICKS


def _burn_eta_ticks(c):
    # type: (Optional[Dict]) -> Optional[float]
    """这口灶还有多少拍烧糊；没在煮则 None。

    与 planner._burn_eta_ticks 同一口径（见那边的字段语义注释）。这里单独
    实现是为了保持 tasks 层不依赖 planner（planner 依赖 tasks，反向会成环）。
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
        return None            # 空灶 / 刚放料还没点火
    return max(0.0, (burn_at - prog) * TICKS_PER_SECOND)


@_register
class ReturnPot(Task):
    """第 5 号：手里的锅放回灶台（**熟锅 / 糊锅除外**）。

    可行性是**纯观测事实**"手持锅具"（spec §6.1 第一个设计点）：不依赖
    任何记忆字段。这是火情丢锅能自动回收的机制——AI_REVIEW.md §4 那套
    专门的 RecoverPotAction 恢复逻辑和 dropped_cooked_pot 全局 bool
    都不再需要。

    正确性押在 predicates.held_is_cookware 上（spec §9 要求的配方数据
    驱动，见 Task 4）；**内容**状态押在 held_cookware_state 上：熟锅和
    糊锅都不许返灶——放回去会重新点火接着烧（"熟料在锅里同时是资产和
    负债"，spec §6.1 第 6 号）。熟锅该走 RescueCooked（端起锅放台面留
    料），糊锅没人收就交给 StashHeld 搁台面（返灶＝再烧一次）。
    """

    TYPE = "ReturnPot"

    @property
    def cooker_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.cooker_id, "return_pot"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        """手空 且 该灶有锅（spec §6.1）。"""
        if not P.hands_free(ctx["chef"]):
            return False
        return P.cooker_has_pot(cooker_state(ctx, self.cooker_id))

    def feasible(self, ctx):
        # type: (Dict) -> bool
        state = P.held_cookware_state(ctx["chef"], ctx["w"].items,
                                      ctx["w"].recipes)
        if state is None:
            return False               # 手空 / 手里不是锅
        if state in (P.COOKWARE_COOKED, P.COOKWARE_BURNT):
            return False               # 熟锅/糊锅返灶＝重新点火接着烧
        c = cooker_state(ctx, self.cooker_id)
        if c is None:
            return False
        return not P.cooker_has_pot(c)    # 灶上已有锅就放不下

    def build(self):
        # type: () -> Node
        cooker_id = self.cooker_id
        return PR.Timeout(Sequence([
            PR.InteractWith(
                lambda c: cooker_station(c, cooker_id), "placement",
                lambda c: (P.hands_free(c["chef"])
                           and P.cooker_has_pot(
                               cooker_state(c, cooker_id)))),
        ]), TASK_TIMEOUT_TICKS)


@_register
class RescueCooked(Task):
    """第 6 号：熟了但没盘要它 → 端锅离灶，停止烧糊计时。

    spec §6.1 原话："熟料在锅里同时是**资产**（现成食材，寿司关复用率
    极高）和**负债**（cooked→burning→onFire 计时在跑）。已确认的游戏
    机制：**把锅从灶台端起即停止烧糊，不必倒进盘子**。所以本 task 成本
    极低——端起锅放到就近台面，同时停计时、腾灶台、留料备用。"

    完成判定因此是"锅已离灶"，不是"料进了盘"。
    """

    TYPE = "RescueCooked"

    @property
    def cooker_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.cooker_id, "rescue"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        """锅已离灶（cooked 变 None = 灶上没锅）。"""
        c = cooker_state(ctx, self.cooker_id)
        if c is None:
            return True
        return not P.cooker_has_pot(c)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        """可行性分两段（spec §6.1 第 6 号 + §6.5）：

          - **启动看灶**：该灶 `cooked` 才值得端；
          - **执行看手**：本 task 第一步就是把锅端离灶台，灶随即变
            `cooked=None`。若绑死在"灶 cooked"，它一动手就把自己判成不可行
            → 被承诺惯性抛弃 → ReturnPot 把锅放回 → 死循环（实机复现
            `RescueCooked ↔ ReturnPot`）。故"锅已在我手上"也算可行，且
            此时**不再因"有盘要它"翻脸**——这一步是收尾，不该被中途打断。

        第二段的"在我手上"必须是**端着带料的那口锅**，不能只判"手持锅具"，
        空锅也满足后者，两处都会坏：
          1. 树首 pickup 的 expect 同款短路（`InteractWith` 见 expect 成立
             就 SUCCESS）→ 根本没去把熟锅端下来，本 task 却一路"成功"到底，
             `done()`（该灶没锅）仍是 False，灶上的熟料继续烧糊；
          2. `if holding: return True` 会**绕过**下面"有盘要它就别救"的检查，
             让手持空锅时本 task 与 TakeCookedToPlate 抢执行权。
        判据与树里 pickup 的 expect 共用同一个 `_holding_cooked`。
        """
        c = cooker_state(ctx, self.cooker_id)
        holding = _holding_cooked(ctx)
        if not P.cooker_cooked(c) and not holding:
            return False
        if holding:
            return True          # 执行中：正在把熟料端离灶台，别中途翻脸
        # 快糊/已糊的锅要立刻端离灶台止损，哪怕有盘要这料：它已经是负债
        # （要着火了）。若 TakeCookedToPlate 因没盘匹配而没发射，只有
        # RescueCooked 能兜底，否则活活烧糊（实机复现：多煮出来的锅没人端）。
        if _about_to_burn(c):
            return True
        # 有**已就位**的盘要这料 → 该走 TakeCookedToPlate，别浪费一趟。
        # 用 placed_plates 而不是 plates_lacking：后者遍历所有盘（含盘堆里的
        # 空盘），会永远非空 → RescueCooked 永远不可行 → 没人要的熟料只能
        # 干烧糊（实机：多煮出来的那两口锅没人端）。
        cooked_items = self._cooked_candidates(ctx)
        for item in cooked_items:
            for p in placed_plates(ctx):
                if ctx["book"].plate_lacks(p, item):
                    return False
        return True

    def _cooked_candidates(self, ctx):
        # type: (Dict) -> List[str]
        """这锅里可能是什么熟料。

        快照不报锅内容，只报 cooked 布尔。所以按本关配方里所有需烹饪的
        熟料名去问"有没有盘缺它"——保守但正确：只要还有盘可能要这锅东西
        就不救，宁可多等一拍走 TakeCookedToPlate。
        """
        out = []
        for order in ctx["w"].orders:
            for ing in ctx["book"].requirements(order):
                if ctx["book"].needs_cooking(ing):
                    out.append(ing)
        return out

    def build(self):
        # type: () -> Node
        cooker_id = self.cooker_id
        return PR.Timeout(Sequence([
            PR.InteractWith(
                lambda c: cooker_station(c, cooker_id), "pickup",
                _holding_cooked),
            # 放到就近合法台面：落点约束与 EnsureHandsFree 共用
            # （spec §6.1"落点约束（StashHeld / RescueCooked 共用）"）。
            # 带熟料的锅在落点表里只走台面、不上灶台，正好是"留料备用"。
            PR.EnsureHandsFree(),
        ]), TASK_TIMEOUT_TICKS)

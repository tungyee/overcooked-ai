"""异常清理与预取四 task：Discard / RecoverLoosePot / StashHeld / Prefetch
（spec §6.1 第 11/12/13/15 号）。

本模块只放清理与预取这一组，共享的身份机制与跨组谓词在 base.py。
外部不直接 import 本模块，一律 `from ai import tasks as T`
（分组边界是实现细节，见 ai/tasks/__init__.py）。

Idle 不在这里（spec §6.1 最后一条）：它是 Planner 流水线第 ⑤ 步之后的
fallback，不参与打分，归 Plan 3。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai import predicates as P
from ai import primitives as PR
from ai import stations as S
from ai.bt import Node, Sequence
from ai.plate_tokens import parse_plate_detail
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register,
                           ingredient_source)

# 垃圾桶的资源 id。站点实例名随关卡变（实测 "Bin 1"），但认领只需要一个
# 稳定的标识符——同一关卡内只有一个垃圾桶被认领就够了。
BIN_RESOURCE = "bin"

# Prefetch 的触发门槛：被几张单需要才值得提前取。设 2 的理由是"不会
# 浪费"——两单都要的料取一份必然用得上。设 1 会让 AI 在正常流程之外
# 抢着取料，反而打乱节奏。
PREFETCH_MIN_ORDERS = 2


def loose_cookware(ctx):
    # type: (Dict) -> List[Dict]
    """**地上**散落的锅具（spec §6.1 第 12 号："地上有散落锅"）。

    锅具识别走 predicates.held_is_cookware 的同一套配方数据判定
    （spec §9），不做名字关键词匹配——DeepFatFryer / OvenTray /
    OvenCakeTin / Steamer 四种名字里既无 pot 也无 pan。

    **位置约束**：插件把"放在灶台/台面上的锅"也报进 items[]（实测
    1102/1102 帧，位置正好落在灶台格上），只按名字判会把它们当散落锅，
    于是 RecoverLoosePot 去端**灶台上**的锅、把锅挪来挪去。

    但也不能把所有站点格上的锅都排除：**台面 / 地上的锅是"没归位、该
    捡回灶台"**，排除掉它们会让锅（被放到台面后）再也回不了灶台（实机：
    EnsureHandsFree 把锅放台面 → 灶位空着没锅）。所以只把**灶台格**上的锅
    当"在用"。

    **内容约束**：熟锅（items[].detail 含 `cooked:*` token）不算散锅——
    那是 RescueCooked 救到台面「留料待用」的中间态，端回灶台会重新点火
    烧糊。判据与 cooked_sources 对散熟锅的识别同源。
    """
    cooker_cells = set()
    for st in ctx["w"].stations:
        if st.get("t") != "cooking":
            continue
        gx, gz = st.get("gx"), st.get("gz")
        if gx is not None and gz is not None:
            cooker_cells.add((gx, gz))
    out = []
    recipes = ctx["w"].recipes
    for it in ctx["w"].items:
        if not P.held_is_cookware({"held": it.get("n")}, recipes):
            continue
        toks = parse_plate_detail(it.get("detail") or [])
        if any(t.startswith("cooked:") for t in toks):
            continue            # 熟锅留台面等盘，不算"该捡回灶台的散锅"
        # 与 cooked_sources 同款兜底：detail 只报原始料名时，cooked 字段才是熟
        if str(it.get("cooked") or "").lower() == "cooked":
            continue            # 熟锅（detail 无 cooked token，但 cooked 字段="Cooked"）
        x, z = it.get("x"), it.get("z")
        if x is not None and z is not None and \
                ctx["w"].to_grid(x, z) in cooker_cells:
            continue                       # 在灶台上 → 在用，不是"散落"
        out.append(it)
    return out


def item_wanted_by_orders(ctx, item):
    # type: (Dict, Optional[str]) -> int
    """该料被几张**还有救的**现存订单需要（按订单成分名比较）。

    判死的单不计入（第 4 组 4.2）：给来不及做完的单提前备料是纯损失，取回来
    的料没人要。判死表由 planner.mark_doomed_orders 每拍写入 bb.doomed_orders。
    """
    if not item:
        return 0
    want = str(item).lower()
    book = ctx["book"]
    bb = ctx.get("bb")
    doomed = getattr(bb, "doomed_orders", None) or {}
    tick = getattr(bb, "tick", 0) if bb is not None else 0
    n = 0
    for order in ctx["w"].orders:
        if doomed.get(order.get("id"), 0) > tick:
            continue
        for ing in book.requirements(order):
            if str(ing).lower() == want:
                n += 1
                break
    return n


def loose_item_named(ctx, item):
    # type: (Dict, Optional[str]) -> Optional[Dict]
    """台面/地上是否已散落着一份该料。"""
    if not item:
        return None
    want = str(item).lower()
    for it in ctx["w"].items:
        if str(it.get("n") or "").lower() == want:
            return it
    return None


@_register
class Discard(Task):
    """第 11 号：烧糊的锅内容倒进垃圾桶，空锅放回灶台。

    spec §6.1 原话："烧焦的锅内容**只能倒进垃圾桶**才能清空，不是随便
    放下。现有 _discard_tick（agent.py:852）有完整流程（找 bin → 走过去
    → 倒掉 → 取回锅）……故 Discard 的认领含垃圾桶。"

    完成判定是"锅空 且 在灶上"——倒完还要把锅放回去，否则灶位空着没锅，
    下一单的 FetchToCooker 不可行。
    """

    TYPE = "Discard"

    @property
    def cooker_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.cooker_id, "discard"), (BIN_RESOURCE, "discard"))

    def done(self, ctx):
        # type: (Dict) -> bool
        c = _cooker(ctx, self.cooker_id)
        if c is None:
            return False
        if not P.cooker_has_pot(c):
            return False       # 锅还没放回灶台
        return not P.cooker_burning(c) and not P.cooker_cooked(c)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        c = _cooker(ctx, self.cooker_id)
        if c is None or not P.cooker_burning(c):
            return False
        # 没垃圾桶就倒不掉——spec §6.1："不是随便放下"
        return S.find_station(ctx["w"].stations, "bin") is not None

    def build(self):
        # type: () -> Node
        cooker_id = self.cooker_id

        def cooker_st(c):
            return _cooker_station(c, cooker_id)

        def bin_st(c):
            return S.find_station(c["w"].stations, "bin",
                                  (c["chef"]["x"], c["chef"]["z"]))

        return PR.Timeout(Sequence([
            PR.InteractWith(
                cooker_st, "pickup",
                lambda c: P.held_is_cookware(c["chef"], c["w"].recipes)),
            PR.InteractWith(
                bin_st, "placement",
                lambda c: not P.cooker_burning(_cooker(c, cooker_id))),
            PR.InteractWith(
                cooker_st, "placement",
                lambda c: (P.hands_free(c["chef"])
                           and P.cooker_has_pot(_cooker(c, cooker_id)))),
        ]), TASK_TIMEOUT_TICKS)


@_register
class RecoverLoosePot(Task):
    """第 12 号：地上的散落锅捡回灶台。

    可行性是**纯观测事实**"地上有锅"（spec §6.1 第一个设计点，与第 5 号
    同源）：不依赖任何记忆。这是火情丢锅能自动回收的机制——
    AI_REVIEW.md §4 那套 RecoverPotAction 与 dropped_cooked_pot 不再需要。

    item 是散落锅的实例名，绑定在身份里：地上有两口锅时它们是两个
    不同的 task（spec §6.0）。
    """

    TYPE = "RecoverLoosePot"

    @property
    def cooker_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.cooker_id, "recover"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        """该锅在某灶上。用目标灶判——绑定参数求值（spec §6.0）。"""
        return P.cooker_has_pot(_cooker(ctx, self.cooker_id))

    def feasible(self, ctx):
        # type: (Dict) -> bool
        """可行性分四步校验（修复开局连续失败问题）：

        实机复现 trace-20261009-130459 拍 1-7: 连续执行
        RecoverLoosePot(utensil_pot_01 (2), workstation_cooker_01) 并失败 7 次。
        根因是可行性判定不够严格，让"锅已在灶上"或"灶已有锅"的情况通过。
        """
        c = _cooker(ctx, self.cooker_id)
        if c is None:
            return False

        # ① 目标灶必须是空的（没有锅）
        if P.cooker_has_pot(c):
            return False       # 灶上已有锅，放不下

        # ② 散落锅必须存在
        pot = None
        for it in loose_cookware(ctx):
            if str(it.get("n")) == str(self.item):
                pot = it
                break
        if pot is None:
            return False       # 该锅不在"散落"列表里（可能已被捡起或在灶上）

        # ③ Pot 必须是空生锅（Raw + 无内容）
        # 注：cooked/has_contents 字段可能不存在（测试/旧帧），缺失时视为符合
        if pot.get("cooked") not in (None, "Raw"):
            return False       # 锅已熟，不该回收
        if pot.get("has_contents"):
            return False       # 锅有内容，不该回收

        # ④ Pot 位置与灶位置不同 → 确认 pot 不在灶上
        # （插件会把"在灶上的锅"也报进 items[]，坐标正好落在灶格上）
        cooker_pos = (c.get("x"), c.get("z"))
        pot_pos = (pot.get("x"), pot.get("z"))
        if cooker_pos == pot_pos:
            return False       # Pot 已在该灶上，无需回收

        return True

    def build(self):
        # type: () -> Node
        cooker_id, pot_name = self.cooker_id, self.item

        def pot(c):
            for it in loose_cookware(c):
                if str(it.get("n")) == str(pot_name):
                    return it
            return None

        def holding_that_pot(c):
            # 绑定到**这一口**锅（任务的 item 就是它的实例名）。只判"手里是
            # 锅具"会让手里另一口锅满足它：pickup 短路 → 把那口锅放上灶就算
            # 成功，而地上被点名那口纹丝不动，`done()`（该灶有锅）却是真——
            # 日志说干完了，实际没干。
            return P.held_matches(c["chef"], pot_name)

        return PR.Timeout(Sequence([
            # 地面物不需要精确朝向（旧 _goto_grab_cell 的那条路）
            PR.InteractWith(pot, "pickup", holding_that_pot, face=False),
            PR.InteractWith(
                lambda c: _cooker_station(c, cooker_id), "placement",
                lambda c: (P.hands_free(c["chef"])
                           and P.cooker_has_pot(_cooker(c, cooker_id)))),
        ]), TASK_TIMEOUT_TICKS)


@_register
class StashHeld(Task):
    """第 13 号：手里莫名拿着无用物时清空手。

    spec §6.1 原话："是兜底：手里莫名拿着无用物（误拾、订单被抢后剩的
    料），清空手好继续干活。**但'随便放下'是不够的**，落点必须满足约束。"
    落点走 placement（Task 6），与 RescueCooked 共用。

    手持锅具不走这里（走 ReturnPot / RecoverLoosePot）：锅扔台面上会让
    灶位空着没锅可用。**糊锅是例外**——ReturnPot 不收它（返灶＝再烧
    一次）、RecoverLoosePot 只认地上的锅、Discard 只管灶上还在烧的那口，
    不该让它一直攥在手里占着手。

    与订单无关，order_id 为 None——打分时订单价值项记 0（spec §6.0）。
    """

    TYPE = "StashHeld"

    def claims(self):
        return ()

    def done(self, ctx):
        # type: (Dict) -> bool
        return P.hands_free(ctx["chef"])

    def feasible(self, ctx):
        # type: (Dict) -> bool
        chef = ctx["chef"]
        if P.hands_free(chef):
            return False
        if P.held_is_cookware(chef, ctx["w"].recipes):
            # 例外：糊锅（见类注释）。别的锅走 ReturnPot / RecoverLoosePot。
            state = P.held_cookware_state(chef, ctx["w"].items,
                                          ctx["w"].recipes)
            if state != P.COOKWARE_BURNT:
                return False
        if P.held_is_plate(chef):
            return False       # 盘走 PlaceCleanPlate / DeliverPlate / ResetPlate
        held = P.held_name(chef)

        # 手持生食材 + 有空灶可用 → 不该 stash，应该走 FetchToCooker。
        # 实机复现 trace-20261009-130459 拍 362-363: P2 手持 SushiRice（准备
        # 放入锅），但被 StashHeld 放到台面 (9.6, 1.2)，浪费食材且影响效率。
        # 此时两个灶已有锅在煮米饭，第三个灶空着——本该把米饭放入空灶的锅。
        book = ctx["book"]
        if book.needs_cooking(held):
            for c in ctx["w"].cookers:
                if not P.cooker_has_pot(c) and not c.get("isCooking"):
                    return False  # 有空灶，别 stash，让 FetchToCooker 接手

        # 手里的东西还有单要 → 别放下，去装盘
        return item_wanted_by_orders(ctx, held) == 0

    def build(self):
        # type: () -> Node
        return PR.Timeout(Sequence([PR.EnsureHandsFree()]),
                          TASK_TIMEOUT_TICKS)


@_register
class Prefetch(Task):
    """第 15 号：把多单都要的料提前取一份放台面。

    spec §6.1 最后一条否决了 Idle 的"无其他可行 task"写法（那不是世界
    谓词），但保留 Prefetch 并要求"真正的世界谓词"。本计划的谓词：
    **该料被 ≥2 张现存订单需要 且 手空 且 台面上还没有它**。

    这是"必然需要"的可观测近似——两单都要的料提前取一份不会浪费。
    熟料排除在外：那得先下锅煮，是 FetchToCooker 的活。
    """

    TYPE = "Prefetch"

    def claims(self):
        return ()

    def done(self, ctx):
        # type: (Dict) -> bool
        """料已在台面（spec §6.1 第 15 号）。"""
        return loose_item_named(ctx, self.item) is not None

    def feasible(self, ctx):
        # type: (Dict) -> bool
        if not P.hands_free(ctx["chef"]):
            return False
        if ctx["book"].needs_cooking(self.item):
            return False       # 熟料要下锅，不是"取一份"能解决的
        if self.done(ctx):
            return False
        if item_wanted_by_orders(ctx, self.item) < PREFETCH_MIN_ORDERS:
            return False
        return ingredient_source(ctx, self.item) is not None

    def build(self):
        # type: () -> Node
        item = self.item
        return PR.Timeout(Sequence([
            PR.InteractWith(lambda c: ingredient_source(c, item), "pickup",
                            lambda c: P.held_matches(c["chef"], item)),
            PR.EnsureHandsFree(),
        ]), TASK_TIMEOUT_TICKS)


# ---------- 本组内部辅助 ----------

def _cooker(ctx, cooker_name):
    # type: (Dict, Any) -> Optional[Dict]
    return P.cooker_for(ctx["w"].cookers, str(cooker_name))


def _cooker_station(ctx, cooker_name):
    # type: (Dict, Any) -> Optional[Dict]
    for st in ctx["w"].stations:
        if st.get("t") != "cooking":
            continue
        if P.station_name_matches(str(st.get("n") or ""), str(cooker_name)):
            return st
    return None

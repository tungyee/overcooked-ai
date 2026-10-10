"""烹饪四 task：FetchToCooker / TakeCookedToPlate / ReturnPot / RescueCooked（spec §6.1 第 3/4/5/6 号）。

本模块只放烹饪这一组，共享的身份机制与跨组谓词在 base.py。
外部不直接 import 本模块，一律 `from ai import tasks as T`
（分组边界是实现细节，见 ai/tasks/__init__.py）。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai import predicates as P
from ai import primitives as PR
from ai.bt import Node, Sequence
from ai.plate_tokens import parse_plate_detail, parse_pot_contents
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register,
                           cooked_sources, ingredient_available,
                           ingredient_source, plate_by_id, placed_plates)

# 灭火喷射最长时长（拍）。喷射与切菜同键，但灭火器几秒就该扑灭一灶火，
# 超过这个时长说明没对准/被推离，交给 Sequence 重置重新 Approach。
EXTINGUISH_TIMEOUT_TICKS = 240

# ---------- 灶相关谓词（仅本组用） ----------


def cooker_state(ctx, cooker_name):
    # type: (Dict, Any) -> Optional[Dict]
    """按站点实例名取灶的状态条目（资源 id 规则：站点实例名 n）。"""
    return P.cooker_for(ctx["w"].cookers, str(cooker_name))


def cooker_station(ctx, cooker_name):
    # type: (Dict, Any) -> Optional[Dict]
    """按实例名取灶的站点 dict（原语要 gx/gz 定位）。"""
    for st in ctx["w"].stations:
        if st.get("t") != "cooking":
            continue
        if P.station_name_matches(str(st.get("n") or ""), str(cooker_name)):
            return st
    return None


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

        def raw_src(c, raw):
            form = c["book"].cook_input_form(raw)
            return ingredient_source(c, form)

        def holding(c, raw):
            form = c["book"].cook_input_form(raw)
            return P.held_matches(c["chef"], form)

        def placed(c, raw):
            cc = cooker_state(c, cooker_id)
            if cc is None:
                return False
            if P.cooker_started(cc):
                return True
            form = c["book"].cook_input_form(raw)
            return str(form).lower() in parse_pot_contents(
                cc.get("pot_contents") or [])

        steps = []  # type: List[Any]
        if raws:
            first = raws[0]
            steps.append(PR.EnsureHandsFree(keep=lambda c: holding(c, first)))
            for r in raws:
                steps.append(PR.InteractWith(
                    lambda c, r=r: raw_src(c, r), "pickup",
                    lambda c, r=r: holding(c, r)))
                steps.append(PR.InteractWith(
                    lambda c: cooker_station(c, cooker_id), "placement",
                    lambda c, r=r: placed(c, r)))
        else:
            # 单料回退：运行时反查（与旧行为一致）
            def dyn_raw(c):
                return c["book"].raw_of(cooked)

            def dyn_form(c):
                return c["book"].cook_input_form(dyn_raw(c))

            steps.append(PR.EnsureHandsFree(
                keep=lambda c: P.held_matches(c["chef"], dyn_form(c))))
            steps.append(PR.InteractWith(
                lambda c: ingredient_source(c, dyn_form(c)), "pickup",
                lambda c: P.held_matches(c["chef"], dyn_form(c))))
            steps.append(PR.InteractWith(
                lambda c: cooker_station(c, cooker_id), "placement",
                lambda c: P.cooker_started(cooker_state(c, cooker_id))))
        return PR.Timeout(Sequence(steps), TASK_TIMEOUT_TICKS)


@_register
class TakeCookedToPlate(Task):
    """第 4 号：端起熟料锅倒进缺此料的盘。

    resources = (source_id, plate_id)。source_id 是熟料源：灶实例名（锅在
    灶上）或散锅 item 名（锅在台面/地上）。盘绑定在身份里是 spec §6.0 的
    硬要求：两单都缺同一样料时，P1 倒进盘 B 不能让针对盘 A 的 task 误报
    SUCCESS。

    倒完手里是空锅——由第 5 号 ReturnPot 接手（spec §6.1 的 task 粒度
    说明："倒完饭后'手持空锅'是状态里直接读得到的事实，所以放回灶台
    切成独立 task，中断后看一眼手里就知道该干啥"）。
    """

    TYPE = "TakeCookedToPlate"
    HANDS = "empty"       # 要端起新锅；手里有锅时打分加腾手代价（spec §6.1(b)）

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
          - **执行中看手**：本 task 第一步就是把锅端离灶台/台面，源随即
            变空。若可行性绑死在"源还在"，它一动手就把自己判成不可行 →
            被承诺惯性抛弃 → ReturnPot 把锅放回 → 死循环（实机复现）。
            故"锅已在我手上"也算可行。

        第二段的"在我手上"必须是**端着带熟料的那口锅**，不能只判"手里
        是锅具"：空锅也满足后者，于是倒完一次之后本 task 一直可行——
        树首 pickup 被同一个谓词短路（`InteractWith` 见 expect 成立就
        SUCCESS），AI 从不回灶台取料，只端着空锅去盘子跟前转圈。实机
        死循环就是这么来的。判据见 predicates.held_cookware_state。
        """
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False                       # 目标盘被端走
        if not ctx["book"].plate_lacks(p, self.item):
            return False                       # 盘里已有这份熟料（done 边界）
        if _holding_cooked(ctx):
            return True                        # 执行中：熟锅已在手上
        return self._source(ctx) is not None

    def _source(self, ctx):
        # type: (Dict) -> Optional[Dict]
        """按 source id 解析熟料源（灶上熟锅 / 台面散熟锅）。

        返回 {kind, target}；源不存在或不熟时返回 None。散锅要识别成
        cooked:* token（items[].detail），不能只按名字匹配——同名散锅
        可能是生锅。
        """
        sid = self.cooker_id
        c = cooker_state(ctx, sid)
        if c is not None:
            if P.cooker_cooked(c) and not P.cooker_burning(c):
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

        def pot_target(c):
            src = self._source(c)
            return src["target"] if src else None

        def plate_target(c):
            return plate_by_id(c, plate_id)

        def poured(c):
            p = plate_by_id(c, plate_id)
            return p is not None and c["book"].plate_has(p, cooked)

        return PR.Timeout(Sequence([
            # 端新锅前先清手（spec §6.1(b) 手部前置 / §7.4"抢占方树首
            # EnsureHandsFree"）：手里若还端着上一口（空）锅，不先放下，
            # 树首的 pickup 会被"手持锅具"短路跳过、拿错锅去倒（实机复现）。
            # keep=_holding_cooked：把锅从灶上端起来后，熟料源会从"灶上锅"
            # 变成"散锅"（source_id 变了），task 身份随之变化 → 树被重建；
            # 重建后的树若没有 keep，第一步 EnsureHandsFree 会把刚端起来的
            # 熟锅当成杂物放到台面上（实机：饭熟了不往盘里倒、放到柜台）。
            PR.EnsureHandsFree(keep=_holding_cooked),
            PR.InteractWith(pot_target, "pickup", _holding_cooked),
            PR.InteractWith(plate_target, "placement", poured),
            # ⚠️ 树尾**不许**再挂等待节点（这里原来有个 Settle(6)）。
            # 树是每拍被 planner 和 executor 一起看的：盘里一有料，本 task
            # 自己就不可行了（plate_lacks 为假）→ 下一拍 planner 换成
            # ReturnPot → executor 见身份变化就 halt 掉这棵还没报 SUCCESS 的
            # 树 → agent ⑬ 的成功记账（释放认领）被静默吞掉。实机后果：
            # 认领没释放，下一拍又去取熟料、重复倒盘。
            # 而"等 contents 刷新"这件事，上面 placement 步的 expect=poured
            # 已经确认过了（它会一直 RUNNING 到插件真报出盘里有料），
            # 所以这个等待本来就是多余的。
        ]), TASK_TIMEOUT_TICKS)


def _holding_cooked(ctx):
    # type: (Dict) -> bool
    """手里端着的那口锅里有熟料（本 task 的"料已经到手了"判据）。

    只判"手持锅具"不够：空锅也满足，会让 pickup 步骤永远短路。
    """
    return (P.held_cookware_state(ctx["chef"], ctx["w"].items, ctx["w"].recipes)
            == P.COOKWARE_COOKED)


def _about_to_burn(c):
    # type: (Optional[Dict]) -> bool
    """锅是否快到烧糊点（ratio = progress/(2*time) >= 1.0）或已经糊/着火。

    与 planner._cooker_burn_danger 同一口径，但这里只回答二值：这口锅已经
    是负债（要着火了），RescueCooked 要能兜底把它端离灶台，哪怕有盘要这料。
    """
    if c is None:
        return False
    if c.get("burning") or c.get("onFire"):
        return True
    prog = c.get("cooking_progress")
    ctime = c.get("cooking_time")
    if prog is None or ctime is None:
        return False
    try:
        return float(prog) >= 2.0 * float(ctime)
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def _available_extinguisher(ctx):
    # type: (Dict) -> Optional[Dict]
    """找没被任何人拿着的灭火器（就近取第一个可用的）。"""
    w = ctx["w"]
    best = None
    cx, cz = ctx["chef"].get("x"), ctx["chef"].get("z")
    for e in w.extinguishers:
        held = str(e.get("held") or "").lower()
        if held not in ("", "player 2"):
            continue
        if best is None:
            best = e
            continue
        if cx is not None and cz is not None:
            bx, bz = e.get("x"), e.get("z")
            ox, oz = best.get("x"), best.get("z")
            if None not in (bx, bz, ox, oz) and \
                    (bx - cx) ** 2 + (bz - cz) ** 2 < (ox - cx) ** 2 + (oz - cz) ** 2:
                best = e
    return best


@_register
class Extinguish(Task):
    """第 6.5 号：灭火——拿灭火器走到火源，按住喷射直到火灭。

    旧架构 _fire_tick 状态机的端口：drop_held → goto_ext → goto_fire →
    face_fire → spray。新架构用原语拼：EnsureHandsFree → InteractWith(灭火器)
    → Approach(火) → Face(火) → HoldUntil("chop", 火灭)。喷射与切菜同键
    （P2 分屏 WorkstationInteract）。
    """

    TYPE = "Extinguish"

    @property
    def cooker_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.cooker_id, "extinguish"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        c = cooker_state(ctx, self.cooker_id)
        return c is not None and not P.cooker_burning(c)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        c = cooker_state(ctx, self.cooker_id)
        return c is not None and P.cooker_burning(c)

    def build(self):
        # type: () -> Node
        cooker_id = self.cooker_id

        def fire(c):
            return cooker_station(c, cooker_id)

        def ext(c):
            return _available_extinguisher(c)

        def holding_ext(c):
            return "extinguish" in P.held_name(c["chef"])

        def fire_out(c):
            cc = cooker_state(c, cooker_id)
            return cc is not None and not P.cooker_burning(cc)

        return PR.Timeout(Sequence([
            PR.EnsureHandsFree(),
            PR.InteractWith(ext, "pickup", holding_ext),
            PR.Approach(fire),
            PR.Face(fire),
            # target=fire：喷射期间每拍校验还面向火源。人被滑行/推离带偏后
            # interact_ready 变假 → HoldUntil 松键 FAILURE，Sequence 重置重新
            # Approach+Face，而不是对着空气空喷（实机：一直灭不完火）。
            PR.HoldUntil("chop", fire_out, timeout=EXTINGUISH_TIMEOUT_TICKS,
                         target=fire),
        ]), TASK_TIMEOUT_TICKS)


@_register
class ReturnPot(Task):
    """第 5 号：手里的锅放回灶台（**熟料锅除外**）。

    可行性是**纯观测事实**"手持锅具"（spec §6.1 第一个设计点）：不依赖
    任何记忆字段。这是火情丢锅能自动回收的机制——AI_REVIEW.md §4 那套
    专门的 RecoverPotAction 恢复逻辑和 dropped_cooked_pot 全局 bool
    都不再需要。

    正确性押在 predicates.held_is_cookware 上（spec §9 要求的配方数据
    驱动，见 Task 4）；**内容**状态押在 held_cookware_state 上：熟锅不许
    返灶（放回去会重新点火、接着烧糊——"熟料在锅里同时是资产和负债"，
    spec §6.1 第 6 号）。熟锅该走 RescueCooked，端起锅放台面留料。
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
        if _holding_cooked(ctx):
            return False       # 熟锅返灶会重新点火烧糊 → 走 RescueCooked
        if not P.held_is_cookware(ctx["chef"], ctx["w"].recipes):
            return False
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

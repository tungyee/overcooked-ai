"""盘管理三 task：PlaceCleanPlate / WashPlate / ResetPlate（spec §6.1 第 1/2/14 号）。

本模块只放盘管理这一组，共享的身份机制与跨组谓词在 base.py。
外部不直接 import 本模块，一律 `from ai import tasks as T`
（分组边界是实现细节，见 ai/tasks/__init__.py）。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai import predicates as P
from ai import primitives as PR
from ai import stations as S
from ai.bt import Node, Sequence
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register, plate_by_id,
                           plate_cell, station_at_cell)

# 洗盘按住交互键的上限（拍）。旧实现用 confirm_wait >= 20 做兜底，
# spec §3.4 指出 20 Hz 下那是约 1 秒而非 4 秒。洗一摞盘实测要数秒，
# 故给 120 拍（6 秒）。HoldUntil 的 timeout 无默认值、必须显式传。
WASH_TIMEOUT_TICKS = 120

# ---------- 盘相关谓词（仅本组用） ----------


def any_order_wants(ctx, plate):
    # type: (Dict, Dict) -> bool
    """盘里的料还对得上某张现存订单吗（ResetPlate 的可行性）。

    判据：盘的 token 集 ⊆ 某单的需求 token 集。多放一样不属于该单的料就
    对不上——那张盘再加料也做不成那道菜。

    用 plate_tokens / order_want_tokens 的同一套装盘态词汇（含 rich
    cooked:{raw}:{cookware}），不能字面比较：盘里记的是装盘态词汇、订单写
    的是需求名，字面比较会让正常装配的盘被判成"对不上任何订单"，于是
    ResetPlate 把队友刚倒好的饭扣进垃圾桶。
    """
    have = ctx["book"].plate_tokens(plate)
    if not have:
        return True          # 空盘无所谓，交给 ResetPlate 自己判不可行
    for order in ctx["w"].orders:
        if have <= ctx["book"].order_want_tokens(order):
            return True
    return False


def clean_plate_exists(ctx):
    # type: (Dict) -> bool
    """快照里有没有可用的干净空盘。"""
    for p in ctx["w"].plates:
        if P.plate_count(p) == 0:
            return True
    return False


def dirty_plate_exists(ctx):
    # type: (Dict) -> bool
    """脏盘摞（items 里的 DirtyPlateStack）。"""
    for it in ctx["w"].items:
        if "dirtyplate" in str(it.get("n") or "").lower():
            return True
    return False


@_register
class PlaceCleanPlate(Task):
    """第 1 号：拿一块干净盘摆到拼装格。

    resources = (plate_id, counter_cell)。counter_cell 是 (gx, gz)，由
    Planner 的候选生成绑定——spec §9 记 PLATE_COUNTERS 的替代品是
    "Planner 的多拼装位候选生成 + 认领，不是一个 find 函数"（归 Plan 3）。
    """

    TYPE = "PlaceCleanPlate"

    @property
    def plate_id(self):
        return self._resources[0]

    @property
    def counter_cell(self):
        return self._resources[1]

    def claims(self):
        return ((self.plate_id, "place"), (self.counter_cell, "assemble"))

    def done(self, ctx):
        # type: (Dict) -> bool
        """认领盘的格 == 拼装格（spec §6.1）。**绑定具体盘**求值。"""
        p = plate_by_id(ctx, self.plate_id)
        return plate_cell(ctx, p) == tuple(self.counter_cell)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        if self.done(ctx):
            return False
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False                   # 盘被端走/消失
        return P.plate_count(p) == 0       # 必须是干净空盘

    def build(self):
        # type: () -> Node
        plate_id, cell = self.plate_id, self.counter_cell
        return PR.Timeout(Sequence([
            # 树首清手：被抢占时手里可能端着上一 task 的东西（spec §7.4）；
            # keep：手里已经是盘就别放下再捡
            PR.EnsureHandsFree(keep=lambda c: P.held_is_plate(c["chef"])),
            PR.InteractWith(
                lambda c: plate_by_id(c, plate_id), "pickup",
                lambda c: P.held_is_plate(c["chef"])),
            PR.InteractWith(
                lambda c: station_at_cell(c, cell), "placement",
                lambda c: plate_cell(
                    c, plate_by_id(c, plate_id)) == tuple(cell)),
        ]), TASK_TIMEOUT_TICKS)


@_register
class WashPlate(Task):
    """第 2 号：洗一块脏盘。

    完成判定是"干净盘出现在**晾干架**或盘堆"——spec §6.1 第 2 号注：
    "洗完的盘从晾干架出来，不是盘堆……初版完成判定只写'出现可用干净盘'
    没说去哪取，实施时会漏。"Plan 1.5 的 stations._is_plate_source 已
    把 t=="plate_return" 且名含 drying 的站点算作可取盘位（实测帧确有
    {'t':'plate_return','n':'DryingPart'}）。
    """

    TYPE = "WashPlate"

    @property
    def sink_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.sink_id, "wash"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        src_cells = set()
        for st in ctx["w"].stations:
            if not S._is_plate_source(st):
                continue
            gx, gz = st.get("gx"), st.get("gz")
            if gx is not None and gz is not None:
                src_cells.add((gx, gz))
        if not src_cells:
            return False
        for p in ctx["w"].plates:
            if P.plate_count(p) != 0:
                continue
            if plate_cell(ctx, p) in src_cells:
                return True
        return False

    def feasible(self, ctx):
        # type: (Dict) -> bool
        if clean_plate_exists(ctx):
            return False      # 有干净盘就别洗——洗盘很慢，是最后手段
        return dirty_plate_exists(ctx)

    def build(self):
        # type: () -> Node
        sink_id = self.sink_id
        done = self.done

        def sink(c):
            return S.find_station(c["w"].stations, "washing") or \
                station_at_cell(c, None)

        def dirty(c):
            for it in c["w"].items:
                if "dirtyplate" in str(it.get("n") or "").lower():
                    return it
            return None

        return PR.Timeout(Sequence([
            PR.InteractWith(dirty, "pickup",
                            lambda c: not P.hands_free(c["chef"]),
                            face=False),
            PR.InteractWith(sink, "placement",
                            lambda c: P.hands_free(c["chef"])),
            # target 让 HoldUntil 每拍校验还在水池前：被推走时松键 FAILURE，
            # Sequence 重置重新 Approach，而不是空按 wash 卡死（同 Chop）。
            PR.HoldUntil("chop", done, timeout=WASH_TIMEOUT_TICKS, target=sink),
        ]), TASK_TIMEOUT_TICKS)


@_register
class ResetPlate(Task):
    """第 14 号：清掉内容与任何现存订单都不匹配的盘。

    spec §6.1 原话："第 14 号 ResetPlate 是新增的必要能力（初版遗漏）。
    两个场景会楔死拼装格：① 盘里被放错料 → 对账只'失效认领'，那只错料
    盘仍停在拼装格上，PlaceCleanPlate 于是去放另一只盘，几轮后无格可用；
    ② AI 端着料齐的盘走向出餐口时订单过期 → 承诺被弃 → 手里一只满盘。
    没有这个 task，上述两种状态无法恢复。"

    清法：端起盘送到垃圾桶倒掉（盘里的料只能倒垃圾桶），空盘放回盘堆。
    完成判定放宽为"盘空 **或** 已移出拼装格"——后者已经解除了楔死。
    """

    TYPE = "ResetPlate"

    @property
    def plate_id(self):
        return self._resources[0]

    @property
    def counter_cell(self):
        return self._resources[1]

    def claims(self):
        return ((self.plate_id, "reset"), (self.counter_cell, "assemble"))

    def done(self, ctx):
        # type: (Dict) -> bool
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return True       # 盘不见了，楔死也解除了
        if P.plate_count(p) == 0:
            return True
        return plate_cell(ctx, p) != tuple(self.counter_cell)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False
        contents = P.plate_contents(p)
        if not contents:
            return False      # 空盘没什么要清的
        return not any_order_wants(ctx, p)

    def build(self):
        # type: () -> Node
        plate_id = self.plate_id

        def bin_st(c):
            return S.find_station(c["w"].stations, "bin",
                                  (c["chef"]["x"], c["chef"]["z"]))

        return PR.Timeout(Sequence([
            PR.InteractWith(lambda c: plate_by_id(c, plate_id), "pickup",
                            lambda c: P.held_is_plate(c["chef"])),
            PR.InteractWith(
                bin_st, "placement",
                lambda c: P.plate_count(plate_by_id(c, plate_id)) == 0),
        ]), TASK_TIMEOUT_TICKS)

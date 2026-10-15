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

# 干净盘缓冲阈值：场上干净空盘数低于该值就允许洗盘（提前补，不等到用光）。
# 洗盘慢，是补位动作，不是主线；但也不能卡到 0 块才洗——那时拼装被卡死。
PLATE_BUFFER = 2

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
        # done 已成立就不可行（上下游咬合，同 PlaceCleanPlate / Chop）。
        # 漏掉这一句的后果不是「多洗一趟」而是**彻底空转**：晾干架上已躺着
        # 干净盘时 done 恒真，树一建起来 `HoldUntil` 的谓词当拍就成立 → 整个
        # Sequence 直接 SUCCESS，一次洗盘键都不按；而 feasible 仍为真（clean=1
        # < PLATE_BUFFER 且脏盘摞还在），于是它以 4.00 分反复被选中，把该去
        # 「把那块干净盘摆上拼装格」的拍全吃掉。
        # 实机 trace-20261014-221306：t=6117~6193 与 7228~7370 共 220 拍选
        # WashPlate、0 拍按键；同期 PlaceCleanPlate 可行 1101 拍、选中 0 次。
        if self.done(ctx):
            return False
        # 提前补盘：干净盘数 < PLATE_BUFFER 就允许洗，别等到 0 块拼装被卡死。
        # 洗盘慢是补位动作，不给无限洗：干净盘够缓冲就不洗（交给打分按稀缺度提权）。
        if P.clean_plate_count(ctx["w"].plates) >= PLATE_BUFFER:
            return False
        return dirty_plate_exists(ctx)

    def build(self):
        # type: () -> Node
        sink_id = self.sink_id
        done = self.done

        def sink(c):
            return S.find_station(c["w"].stations, "washing") or \
                station_at_cell(c, None)

        def dirty(c):
            """脏盘摞的取用目标：优先 stations 里的回盘台（有 gx/gz）。

            items[] 的 DirtyPlateStack 只有世界坐标，没有 gx/gz；同位置的
            stations 条目（实测 {'t':'plate_return','n':'workstation_plate_return',
            'gx':-1,'gz':-5}）才是带网格的权威目标，Approach / interact_ready
            都按它判。先确认脏盘摞真的存在（items），再把坐标换成站点那条。
            """
            stack = None
            for it in c["w"].items:
                if "dirtyplate" in str(it.get("n") or "").lower():
                    stack = it
                    break
            if stack is None:
                return None
            sx, sz = stack.get("x"), stack.get("z")
            if sx is None or sz is None:
                return stack
            for st in c["w"].stations:
                if not isinstance(st, dict) or st.get("t") != "plate_return":
                    continue
                if "drying" in str(st.get("n") or "").lower():
                    continue        # 晾干架是取干净盘的，不是脏盘摞
                if st.get("gx") is None or st.get("gz") is None:
                    continue
                # 同位置（回盘台与脏盘摞重合）才认
                if abs(float(st.get("x", 1e9)) - float(sx)) < 0.7 and \
                        abs(float(st.get("z", 1e9)) - float(sz)) < 0.7:
                    return st
            return stack

        return PR.Timeout(Sequence([
            # face=True：脏盘摞是**固定站点**，站对位置但朝向不对时
            # `interactions.pickup` 为 null，不转身就永远按不到（实机
            # trace-20261014-004742：WashPlate 11 次全部 failure，每次都
            # 横穿整张地图到 (-1,-4) 再立刻失败。对比 t=3753 yaw=117 句柄
            # 就绪 vs t=8421 yaw=270 句柄全 null）。
            PR.InteractWith(dirty, "pickup",
                            lambda c: not P.hands_free(c["chef"])),
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

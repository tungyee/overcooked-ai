# -*- coding: utf-8 -*-
"""中转交接 task：PlaceToRelay / ThrowBatch。

PlaceToRelay：1-3 关卡的「分工 + 传递」落地，不可投掷的锅/盘放中间中转台。

ThrowBatch：把一种可投掷料批量投向下一站消费站（砧板/灶）。两类触发：
  - 分割地图：消费站只有对面够得着（Rapids_2_1 木筏 ↔ 厨房）；
  - 连通但远：料到消费站 > THROW_MIN_DIST，投过去比来回跑快。
历史教训（tests/test_tasks.py 的两条 no_batch_throw 钉子）：批量取+投曾塞在
FetchToBoard/FetchToCooker 里，「投满 N 份才 drop」+ 整树 200 拍，drop 一次
没执行过。这里的差别：每投一份就是一次兑现（朝消费站投，被接住或落在旁边
都算已就位），没有尾部 drop；每轮单独限时，整树按批量放宽。

外部不直接 import 本模块，一律 `from ai import tasks as T`。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai.query import placement
from ai.query import predicates as P
from ai import primitives as PR
from ai.query import stations as S
from ai.exec.bt import Condition, Node, Selector, Sequence
from ai.revenue import throw_supply as TS
from ai.space import regions as RG
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register,
                           _grid_matches, _station_for_item,
                           _unpack_resource_id,
                           plate_by_id, plate_cell)

# 一轮「取 → 走到出手位 → 投」的拍数上限（20Hz）：取料 ~40 拍 + 走位 ~60 拍
# + 转身投出 ~25 拍，留一倍余量。单轮超时 = 进度看门狗：一轮投不出去就交还
# Planner，不让整批空耗。
THROW_ROUND_TICKS = 260
# 拿着料等出手位的上限（拍）：篮子/木筏漂走后通常 1~2 秒内转回来
# （trace-20261029-220619 t=706→720 约 0.7 秒）。等满仍没有就失败交还，
# 不让 P2 端着料一直站着。
LAUNCH_WAIT_TICKS = 60
# 整批上限：N 不在身份里（每拍重算），按「足够大的一批」给。真正的收敛靠
# done（缺口归零）与「没有可投的料」。
THROW_BATCH_TICKS = THROW_ROUND_TICKS * 12
# 一批最多几份。暂不做批量：一次只投一份，用在「这一步非投不可」的场景
# （Rapids_2_1 分割地图把料投给对面的 P1）。批量的问题——缺口不按锅位封顶、
# batch_bonus 叠加后压过别的备料、中途常被打断——等单份跑稳再说。
MAX_BATCH_UNITS = 1


def _relay_drop(c):
    # type: (Dict) -> Optional[Dict]
    """中转台落点：遍历 relay_cells 找第一个空且可达的格，返回合成站点 dict。"""
    for cell in S.relay_cells_for(c["w"]):
        st = placement.find_drop_at(c["w"], c["field"], cell)
        if st is not None:
            return st
    return None


@_register
class PlaceToRelay(Task):
    """把不可投掷物（干净盘子）放到中间中转台交接（1-3 递盘子）。

    item 是盘实例名（plate_id）。盘子不可投掷，走近中转台放下。落点用
    find_drop_at 找第一个空的中转台格，满了（pick 返回 None）就 FAILURE
    交还 Planner，不 fallback 到别处乱放。
    """

    TYPE = "PlaceToRelay"
    # 给对话层 LLM 看的一句话说明（ai/dialog/duties.py 渲染 DAG 时带出）。
    DESC = "把东西放到中转台交给对面"
    LABEL = "放中转台"
    HANDS = "empty"

    def claims(self):
        return ()

    def done(self, ctx):
        # type: (Dict) -> bool
        cell = plate_cell(ctx, plate_by_id(ctx, self.item))
        if cell is None:
            return False
        return cell in set(S.relay_cells_for(ctx["w"]))

    def feasible(self, ctx):
        # type: (Dict) -> bool
        relay_cells = S.relay_cells_for(ctx["w"])
        if not relay_cells:
            return False
        p = plate_by_id(ctx, self.item)
        if p is None or P.plate_count(p) != 0:
            return False            # 盘消失 / 不是干净盘
        if self.done(ctx):
            return False
        # 中转台至少要有一个空格可放
        occupied = placement.occupied_cells(ctx["w"])
        return any(c not in occupied for c in relay_cells)

    def build(self):
        # type: () -> Node
        plate_id = self.item
        return PR.Timeout(Sequence([
            PR.EnsureHandsFree(keep=lambda c: P.held_is_plate(c["chef"])),
            PR.InteractWith(
                lambda c: plate_by_id(c, plate_id), "pickup",
                lambda c: P.held_is_plate(c["chef"])),
            PR.EnsureHandsFree(pick=_relay_drop, strict=True),
        ]), TASK_TIMEOUT_TICKS)


def _dest_station(ctx, dest_id):
    # type: (Dict, Any) -> Optional[Dict]
    """按 (grid, name) 资源 id 找消费站的站点 dict（砧板 / 灶）。"""
    grid, name = _unpack_resource_id(dest_id)
    for st in ctx["w"].stations:
        if st.get("t") not in ("chopping", "cooking"):
            continue
        if str(st.get("n") or "") != name:
            continue
        if not _grid_matches(st.get("grid"), grid):
            continue
        return st
    return None


@_register
class ThrowBatch(Task):
    """把一种可投掷料批量投向消费站（砧板 / 灶）：一份一投，直到缺口归零。

    身份 = (dest_id, consumer, raw, product) + item（要投的形态）。N 不进身份：
    缺口每拍重算，投一份少一份，进身份的话每投一次就换一个 task、承诺惯性失效。
    """

    TYPE = "ThrowBatch"
    DESC = "把这种料批量扔到对面/远处的砧板或灶旁"
    LABEL = "扔过去"
    HANDS = "any"

    @property
    def dest_id(self):
        return self._resources[0]

    @property
    def consumer(self):
        return self._resources[1]

    @property
    def raw(self):
        return self._resources[2]

    @property
    def product(self):
        return self._resources[3]

    def claims(self):
        return ((self.dest_id, "throw_to"),)

    # ---------- 每拍求值 ----------
    def _state(self, ctx):
        # type: (Dict) -> Optional[Tuple[Dict, str, List[Dict], int]]
        """(消费站, 模式, 可取来源, 缺口)；不构成投掷场景返回 None。"""
        w, bb = ctx["w"], ctx.get("bb")
        dest = _dest_station(ctx, self.dest_id)
        if dest is None:
            return None
        # 先按 split 口径找来源（分割与否看消费站可达性，不看来源）
        probe = TS.MODE_SPLIT if not RG.reachable_by(w, dest, P.P2_NAME, bb) \
            else TS.MODE_FAR
        dests = TS.dest_candidates(w, ctx["book"], self.consumer, self.product)
        srcs = TS.sources(w, self.item, self.raw, dest, probe, bb, dests)
        mode = TS.mode_for(w, dest, srcs, bb)
        if mode is None and self._carrying_this_batch(ctx, srcs, probe):
            # 本批已在进行、料拿在手上：按探测口径继续投完这份，不因单帧判据
            # 翻转就放下。far：走向出手位时人离灶近了（trace-20261029-130632）；
            # split：篮子在动，「P1 能到砧板」逐帧翻转，拿起→StashHeld 放下→
            # 再拿起循环（trace-20261029-220619 t=685~942）。落点仍按 P1 可达
            # 格判，P1 暂时到不了砧板也能先捡起来。等不到出手位由树里的
            # WaitUntil 兜底（LAUNCH_WAIT_TICKS）。
            mode = probe
        if mode is None:
            return None
        n = TS.gap(w, ctx["book"], self.product, self.item, self.consumer,
                   self.raw, dest, mode, bb)
        return dest, mode, srcs, min(n, MAX_BATCH_UNITS)

    def _carrying_this_batch(self, ctx, srcs, probe):
        # type: (Dict, List[Dict], str) -> bool
        """P2 手上拿着本批的料，且当前承诺就是这个 ThrowBatch（far / split 都适用）。"""
        if probe not in (TS.MODE_FAR, TS.MODE_SPLIT) \
                or not any(s["kind"] == "held" for s in srcs):
            return False
        bb = ctx.get("bb")
        commit = getattr(bb, "commitment", None)
        active = getattr(commit, "active_task", None)
        return getattr(active, "key", None) == self.key

    def batch_size(self, ctx):
        # type: (Dict) -> int
        st = self._state(ctx)
        return st[3] if st else 0

    def done(self, ctx):
        # type: (Dict) -> bool
        st = self._state(ctx)
        return st is not None and st[3] <= 0

    def feasible(self, ctx):
        # type: (Dict) -> bool
        st = self._state(ctx)
        if st is None:
            return False
        dest, mode, srcs, n = st
        if n <= 0 or not srcs:
            return False
        if TS.launch_plan(ctx["w"], dest, mode, ctx.get("bb")) is not None:
            return True
        # 拿着本批料、这一帧暂无出手位（平台漂走）：仍可行，交给树里的
        # WaitUntil 等 LAUNCH_WAIT_TICKS；等不到树失败、承诺清空，保护随之解除。
        probe = TS.MODE_SPLIT if mode == TS.MODE_SPLIT else TS.MODE_FAR
        return self._carrying_this_batch(ctx, srcs, probe)

    # ---------- 行为树 ----------
    def build(self):
        # type: () -> Node
        task, form = self, self.item

        def holding(c):
            return P.held_matches(c["chef"], form)

        def source(c):
            st = task._state(c)
            if st is None:
                return None
            for s in st[2]:
                if s["kind"] == "loose":
                    return _station_for_item(c["w"], s["entry"]) or s["entry"]
            for s in st[2]:
                if s["kind"] == "crate":
                    return s["entry"]
            return None

        def launch_cell(c):
            st = task._state(c)
            if st is None:
                return None
            plan = TS.launch_plan(c["w"], st[0], st[1], c.get("bb"))
            return plan["cell"] if plan else None

        def dest(c):
            return _dest_station(c, task.dest_id)

        def throw_ok_here(c):
            st = task._state(c)
            return st is not None and TS.throw_ok_here(c["w"], st[0], st[1], c.get("bb"))

        # 单份：一棵树只投一轮（取 → 走到出手位 → 投），投完 SUCCESS 交还
        # Planner，要不要再投下一份每拍重新决定，中间可以穿插别的事。
        return PR.Timeout(Sequence([
            PR.EnsureHandsFree(keep=holding),
            Selector([
                Condition(holding),
                PR.InteractWith(source, "pickup", holding, face=True),
            ]),
            # 平台漂走时出手位可能暂时没有：拿着料等一会儿，等不到就失败交还
            # Planner（承诺清空 → 拿着料的保护解除 → StashHeld 放下）。
            PR.WaitUntil(lambda c: throw_ok_here(c) or launch_cell(c) is not None,
                         LAUNCH_WAIT_TICKS),
            PR.GoToCell(launch_cell, arrived=throw_ok_here),
            PR.ThrowTo(dest),
        ]), THROW_ROUND_TICKS)

    TIMEOUT_TICKS = THROW_ROUND_TICKS

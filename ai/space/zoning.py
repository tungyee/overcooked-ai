# -*- coding: utf-8 -*-
"""区域分工：把可走格切成连通块，看每个角色的活路落在哪一块。

3-3 实机（trace-20261109-011932）暴露的问题：中央料箱在「竖列 / 横排」
之间每 34 秒换一次，把厨房切成**左右两半或上下两半**；站点本身全程不动，
分工也不用改，但每个人该待的半场每 34 秒翻一次。原来的动态复核只看站点
签名（`ai/dialog/division.py:station_signature`），站点不动就永远不触发，
于是换布局后没人提醒、也没人挪位。

本模块只回答「这一帧每个角色该在哪一半」，不碰 World 写操作、不发命令：

- `regions`：可走格的连通块 + 每块能提供哪些功能区（砧板/灶/水槽/盘）；
- `role_kits`：当前分工下每个角色需要哪些功能区；
- `pick`：给每个角色选一块（另一角色已占的优先避开）；
- `ZoneTracker`：跨帧记住见过的布局，算「两种布局都对」的安全格，
  并在只剩一个连通块（转场窗口）时给出可用上一版安全格。

**为什么安全格取交集**：实测转场时全图只连通 2.0–3.4 秒，等布局稳定再走
就已经被关在另一侧 31 秒。取「所有见过的布局里都属于自己」的格，转场时
走进去，不管最后落在哪种布局都对。
"""
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from ai.dialog.actions import DIVISION_ACTIONS
from ai.dialog.division import OWNER_PLAYER, OWNER_YOU
from ai.query import predicates as P
from ai.query import stations as ST
from ai.space import occupancy

BOARD = "board"
PLATE = "plate"
OVEN = "oven"
SINK = "sink"
MIXING = "mixing"

# 站点类型 → 功能区。
STATION_KINDS = {
    "chopping": BOARD,
    "cooking": OVEN,
    "washing": SINK,
    "mixing": MIXING,
}  # type: Dict[str, str]

# 可分工动作 → 需要的功能区（送餐/洗盘不落分工，见 ai/dialog/actions.py）。
#
# **「装盘」故意不映射成 plate**：干净盘是会到处跑的动态物（trace-20261109-
# 011932 t=94.8 全部干净盘都在左半边），拿它当区域判据会让「右半边没有盘」
# 被判成「你该去左半边」，和玩家抢同一半。盘由取盘/洗盘 task 自己找，区域
# 只看不动的砧板/灶/水槽。
KIND_BY_ACTION = {
    "切": BOARD,
    "下锅": OVEN,
    "拌": MIXING,
}  # type: Dict[str, str]

# 分工表为空（开局还没定）时的角色装备：你切，玩家烤+洗（灶和水槽在一起，
# 洗完的盘从沥碗架出来，所以烤的一侧也承担洗）。
DEFAULT_KITS = (frozenset([BOARD]), frozenset([OVEN, SINK]))

# 小于这个格数的连通块不算半场：3-3 转场时会出现 4 格的小碎块，把它当
# 半边会算出荒谬的目标区。
MIN_REGION_CELLS = 8
# 站点包围盒外扩的格数：留出「站点在墙外、交互格在墙内」的余量（实测 1 格
# 足够，取 2 兜住站点摆在角落的关卡）。
BOUNDS_MARGIN = 10
# 最多记住几种布局。3-3 只有两种；留 4 给多段切换的关卡。
MAX_LAYOUTS = 4
# 同一套布局连续观察到几次才记进历史：转场动画期间每帧的连通块形状都不一样
# （实测 83.6~86.6 依次是 67/4、66/4、62/4 格），记进去会把真正的两种布局挤掉。
# 主循环每秒调一次，2 次 ≈ 2 秒，远短于 31 秒的稳定段。
CONFIRM_OBSERVATIONS = 2
# 同一角色的区域重合度超过这个比例就算同一套布局（过渡帧差一两格）。
LAYOUT_SAME_RATIO = 0.8

_NEIGHBORS = ((1, 0), (-1, 0), (0, 1), (0, -1))


@dataclass(frozen=True)
class Region(object):
    """一个连通块：可走格集合 + 能提供的功能区 + 方位名。"""
    cells: FrozenSet[Tuple[int, int]]
    kinds: FrozenSet[str] = frozenset()
    label: str = ""

    @property
    def size(self):
        # type: () -> int
        return len(self.cells)

    @property
    def center(self):
        # type: () -> Tuple[float, float]
        if not self.cells:
            return (0.0, 0.0)
        xs = [c[0] for c in self.cells]
        zs = [c[1] for c in self.cells]
        return (sum(xs) / len(xs), sum(zs) / len(zs))


@dataclass(frozen=True)
class RepositionGoal(object):
    """一次换边的目标：走到 `goal`，进 `safe`（两种布局都算对的格）即完成。"""
    goal: Tuple[int, int]
    safe: FrozenSet[Tuple[int, int]] = frozenset()
    until_tick: int = 0


@dataclass
class ZoneReport(object):
    """一帧的区域判定结果；给复核事件、reposition 和 trace 共用。"""
    district: str = ""                 # LR / UD / ""（转场或没有两块）
    open: bool = False                 # 只剩一个大连通块（转场窗口）
    labels: Tuple[str, ...] = ()
    p2_label: str = ""                 # 你这帧该待的区
    p1_label: str = ""                 # 玩家这帧该待的区
    p2_current: str = ""               # 你实际所在的区
    p1_current: str = ""
    p2_wrong: bool = False
    p1_wrong: bool = False
    p2_cells: FrozenSet[Tuple[int, int]] = frozenset()
    p1_cells: FrozenSet[Tuple[int, int]] = frozenset()
    p2_need: Tuple[str, ...] = ()
    p1_need: Tuple[str, ...] = ()
    safe_p2: FrozenSet[Tuple[int, int]] = frozenset()
    safe_p1: FrozenSet[Tuple[int, int]] = frozenset()
    signature: Tuple = ()

    def facts(self):
        # type: () -> str
        """给 LLM 看的一句话（不念内部代号）。

        开头一定是「谁守哪半边」：真机反馈（voice-20261109-124818）是玩家
        根本不知道自己该站哪，因为只在有人站错时才提一句、而且只说一边。

        玩家那句只给**目的半边**、不给来源：写成「从A换到B」时实测模型会
        抓住第一个提到的半边反着说（「从上半边换到下半边」→「你去上半边」，
        6 次里 4 次说反）。只留「请让玩家去B」，实测 6/6 方向正确。
        """
        parts = []  # type: List[str]

        def half(label):
            # type: (str) -> str
            return "%s半边" % label if label else "别处"

        if self.district and self.p2_label:
            parts.append("厨房分成%s两半：你守%s，玩家守%s。" % (
                "左右" if self.district == "LR" else "上下",
                half(self.p2_label), half(self.p1_label)))
        elif self.open:
            parts.append("中间的路正在转开。")
        # 已经在正确半边的角色不占句子：说「你在上半边，应该去上半边」
        # 会让模型以为要动。
        if self.p1_label and self.p1_current != self.p1_label:
            parts.append("请让玩家去%s" % half(self.p1_label))
            if self.p1_current:
                parts.append("提醒玩家时只说「去%s」，不要提%s" % (
                    half(self.p1_label), half(self.p1_current)))
        if self.p2_wrong:
            parts.append("你被隔开了，够不着自己的站点")
        return "；".join(parts) + "。" if parts else ""


def _bounds(world):
    # type: (Any) -> Optional[Tuple[int, int, int, int]]
    """扫描范围（格坐标）。优先用 level_bounds（真实帧有），否则站点包围盒外扩。

    **不能只按站点包围盒紧贴取**：站点常常只沿墙摆一排，房间纵深会被裁掉，
    连通块被切成碎块（测试里 10x6 的房间按站点裁只剩 4 行）。没有 level_bounds
    时按站点包围盒外扩 `BOUNDS_MARGIN` 格——`walkable` 本身会把范围外的格判成
    不可走，多扫不会算错。
    """
    bounds = occupancy.parse_level_bounds(
        (getattr(world, "raw", None) or {}).get("level_bounds"))
    if bounds:
        box = occupancy.playable_bounds(getattr(world, "stations", None))
        to_grid = world.to_grid
        xs = []  # type: List[int]
        zs = []  # type: List[int]
        for minx, maxx, minz, maxz in bounds:
            for x in (minx, maxx):
                for z in (minz, maxz):
                    gx, gz = to_grid(x, z)
                    xs.append(gx)
                    zs.append(gz)
        if box is not None:
            xs += [box[0], box[1]]
            zs += [box[2], box[3]]
        return (min(xs) - 1, max(xs) + 1, min(zs) - 1, max(zs) + 1)
    box = occupancy.playable_bounds(getattr(world, "stations", None))
    if box is None:
        return None
    return (box[0] - BOUNDS_MARGIN, box[1] + BOUNDS_MARGIN,
            box[2] - BOUNDS_MARGIN, box[3] + BOUNDS_MARGIN)


def _walkable_cells(world):
    # type: (Any) -> List[Tuple[int, int]]
    box = _bounds(world)
    if box is None:
        return []
    walkable = world.walkable
    cells = []  # type: List[Tuple[int, int]]
    for gx in range(box[0], box[1] + 1):
        for gz in range(box[2], box[3] + 1):
            if walkable(gx, gz):
                cells.append((gx, gz))
    return cells


def _components(cells):
    # type: (Iterable[Tuple[int, int]]) -> List[set]
    rest = set(cells)
    out = []  # type: List[set]
    while rest:
        start = rest.pop()
        comp = {start}
        stack = [start]
        while stack:
            cur = stack.pop()
            for dx, dz in _NEIGHBORS:
                nxt = (cur[0] + dx, cur[1] + dz)
                if nxt in rest:
                    rest.remove(nxt)
                    comp.add(nxt)
                    stack.append(nxt)
        out.append(comp)
    return out


def _touch_cells(cell):
    # type: (Tuple[int, int]) -> List[Tuple[int, int]]
    gx, gz = cell
    return [(gx + dx, gz + dz) for dx, dz in _NEIGHBORS]


def _kinds(world):
    # type: (Any) -> Dict[str, List[Tuple[int, int]]]
    """功能区 → 可站上去干活的格（站点/盘的四邻）。"""
    out = {}  # type: Dict[str, List[Tuple[int, int]]]

    def add(kind, gx, gz):
        if gx is None or gz is None:
            return
        for cell in _touch_cells((int(gx), int(gz))):
            out.setdefault(kind, []).append(cell)

    for station in getattr(world, "stations", None) or ():
        if not isinstance(station, dict):
            continue
        kind = STATION_KINDS.get(str(station.get("t") or ""))
        if kind is None:
            continue
        add(kind, station.get("gx"), station.get("gz"))
    for station in getattr(world, "stations", None) or ():
        if not isinstance(station, dict) or not ST._is_plate_source(station):
            continue
        add(PLATE, station.get("gx"), station.get("gz"))
    to_grid = world.to_grid
    for plate in getattr(world, "plates", None) or ():
        if not isinstance(plate, dict) or P.plate_count(plate) > 0:
            continue
        gx, gz = plate.get("gx"), plate.get("gz")
        if gx is None or gz is None:
            x, z = plate.get("x"), plate.get("z")
            if x is None or z is None:
                continue
            gx, gz = to_grid(x, z)
        add(PLATE, gx, gz)
    return out


def _labels(regions):
    # type: (List[Region]) -> Tuple[str, ...]
    """按切分轴给方位名。左右切 → 左/右；上下切 → 下/上。"""
    if not regions:
        return ()
    if len(regions) == 1:
        return ("",)
    centers = [r.center for r in regions]
    cx = sum(c[0] for c in centers) / len(centers)
    cz = sum(c[1] for c in centers) / len(centers)
    spread_x = max(c[0] for c in centers) - min(c[0] for c in centers)
    spread_z = max(c[1] for c in centers) - min(c[1] for c in centers)
    left_right = spread_x >= spread_z
    out = []
    for x, z in centers:
        if left_right:
            out.append("右" if x > cx else "左")
        else:
            out.append("上" if z > cz else "下")
    return tuple(out)


def regions(world):
    # type: (Any) -> List[Region]
    """可走格的连通块（按坐标排序，方位名在 `label`）。"""
    comps = [c for c in _components(_walkable_cells(world))
             if len(c) >= MIN_REGION_CELLS]
    if not comps:
        return []
    kinds = _kinds(world)
    raw = []  # type: List[Region]
    for comp in comps:
        hit = frozenset(kind for kind, cells in kinds.items()
                        if any(cell in comp for cell in cells))
        raw.append(Region(cells=frozenset(comp), kinds=hit))
    raw.sort(key=lambda r: (round(r.center[0], 1), round(r.center[1], 1)))
    labels = _labels(raw)
    return [Region(cells=r.cells, kinds=r.kinds, label=labels[i])
            for i, r in enumerate(raw)]


def role_kits(division):
    # type: (Any) -> Tuple[FrozenSet[str], FrozenSet[str]]
    """当前分工下两个角色各自需要的功能区。

    分工表只覆盖「切/下锅/拌/装盘」；没写的角色回落到默认装备。有灶的一侧
    补上水槽——洗完的盘才接得上装盘。
    """
    p2 = set()  # type: set
    p1 = set()  # type: set
    for entry in getattr(division, "entries", None) or ():
        kind = KIND_BY_ACTION.get(entry.action)
        if kind is None:
            continue
        if entry.owner == OWNER_YOU:
            p2.add(kind)
        elif entry.owner == OWNER_PLAYER:
            p1.add(kind)
    default_p2, default_p1 = DEFAULT_KITS
    out2 = frozenset(p2) if p2 else default_p2
    out1 = frozenset(p1) if p1 else default_p1
    if OVEN in out1 and SINK not in out2:
        out1 = frozenset(set(out1) | {SINK})
    return out2, out1


def _fits(region, kit):
    # type: (Region, FrozenSet[str]) -> int
    """缺几个功能区；0 表示这块能独立干完。"""
    return len(frozenset(kit) - region.kinds)


def pick(regions_list, kit, avoid=None):
    # type: (List[Region], FrozenSet[str], Optional[int]) -> Optional[int]
    """给一个角色选连通块：先要功能齐，再避开另一角色，最后取更大的。

    功能齐的一块都没有时返回 None——**不猜**。猜出来的目标区会把角色往
    错误的一半赶，比不动更糟（3-3 实测：分不清左右时模型会给出反方向的
    提醒）。
    """
    if not regions_list or not kit:
        return None
    fits = [i for i, r in enumerate(regions_list) if _fits(r, kit) == 0]
    if not fits:
        return None
    if avoid is not None:
        other = [i for i in fits if i != avoid]
        if other:
            fits = other
    return max(fits, key=lambda i: (regions_list[i].size, -i))


def _similar(old, new):
    # type: (FrozenSet[Tuple[int, int]], FrozenSet[Tuple[int, int]]) -> bool
    """两套布局的同一角色区域是否基本重合（过渡帧共用一套格，差一两格）。"""
    if not old or not new:
        return False
    return len(old & new) / float(min(len(old), len(new))) >= LAYOUT_SAME_RATIO


def _region_index(regions_list, cell):
    # type: (List[Region], Any) -> Optional[int]
    if cell is None:
        return None
    for i, region in enumerate(regions_list):
        if cell in region.cells:
            return i
    return None


def _chef_cells(world):
    # type: (Any) -> Tuple[Optional[Tuple[int, int]], Optional[Tuple[int, int]]]
    to_grid = world.to_grid
    out = []
    for name in (P.P2_NAME, P.P1_NAME):
        chef = P.chef_by_name(getattr(world, "chefs", None), name)
        out.append(P.chef_cell(chef, to_grid))
    return (out[0], out[1])


class ZoneTracker(object):
    """跨帧记住见过的布局，给出目标区与安全格。"""

    def __init__(self):
        # type: () -> None
        self.layouts = []  # type: List[Dict[str, Any]]
        self._seen_multi = False
        self._last_signature = None
        self._hits = 0
        self._assign = {}  # type: Dict[str, FrozenSet[Tuple[int, int]]]

    def reset(self):
        # type: () -> None
        self.layouts[:] = []
        self._seen_multi = False
        self._last_signature = None
        self._hits = 0
        self._assign = {}
        self._assign = {}  # type: Dict[str, FrozenSet[Tuple[int, int]]]

    def _pick(self, regions_list, kit, role, avoid=None):
        # type: (List[Region], FrozenSet[str], str, Optional[int]) -> Optional[int]
        """优先沿用上一次的分区，装不下再重选。

        两边都满足装备时，`pick` 只能按格数决胜负，而转场收尾时格数每秒抖
        一两格 → 目标区每秒翻一次（真机 trace-20261109-124818 t=18~21：
        右/左 与 左/右 交替）。分区一旦定下就钉住，只有旧的那半不再满足
        装备时才换。
        """
        last = self._assign.get(role) or frozenset()
        if last:
            for index, region in enumerate(regions_list):
                if index == avoid or _fits(region, kit):
                    continue
                overlap = len(region.cells & last)
                if overlap >= LAYOUT_SAME_RATIO * min(len(last), len(region.cells)):
                    return index
        return pick(regions_list, kit, avoid)

    def _record(self, signature, regions_list, p2, p1):
        # type: (Tuple, List[Region], Optional[int], Optional[int]) -> None
        """记一套布局；和已有布局高度重合就并进去（取交集，保守）。"""
        if p2 is None and p1 is None:
            return
        p2_cells = frozenset(regions_list[p2].cells) if p2 is not None else frozenset()
        p1_cells = frozenset(regions_list[p1].cells) if p1 is not None else frozenset()
        for item in self.layouts:
            if item["signature"] == signature:
                item["p2"], item["p1"] = p2_cells, p1_cells
                return
            if _similar(item["p2"], p2_cells) and _similar(item["p1"], p1_cells):
                # 同一套布局的过渡形状（边界差一两格）：并进去并取交集，
                # 不然 4 个名额会被过渡帧占满，真正的两种布局反而被挤掉。
                item["p2"] = item["p2"] & p2_cells
                item["p1"] = item["p1"] & p1_cells
                return
        self.layouts.append({"signature": signature, "p2": p2_cells, "p1": p1_cells})
        del self.layouts[:-MAX_LAYOUTS]

    def _safe(self, key):
        # type: (str) -> FrozenSet[Tuple[int, int]]
        """所有见过的布局里都属于该角色的格；交集为空就退回最近一版。"""
        sets = [item[key] for item in self.layouts if item[key]]
        if not sets:
            return frozenset()
        safe = set(sets[0])
        for item in sets[1:]:
            safe &= item
        return frozenset(safe) if safe else frozenset(self.layouts[-1][key])

    def observe(self, world, division=None):
        # type: (Any, Any) -> ZoneReport
        regions_list = regions(world)
        p2_cell, p1_cell = _chef_cells(world)
        report = ZoneReport(safe_p2=self._safe("p2"), safe_p1=self._safe("p1"))
        if len(regions_list) < 2:
            report.open = bool(self._seen_multi)
            report.signature = tuple(sorted(
                (r.size, round(r.center[0], 1), round(r.center[1], 1))
                for r in regions_list))
            return report
        self._seen_multi = True
        kits = role_kits(division)
        # 先给玩家选：玩家的装备（灶+水槽）通常更受限，选完再让你避开。
        # 反过来先选你，会在「两边都能切+装盘、只有一边有灶和水槽」时把你
        # 塞进玩家唯一能干活的半边，两人挤在一起。
        p1_index = self._pick(regions_list, kits[1], "p1")
        p2_index = self._pick(regions_list, kits[0], "p2", avoid=p1_index)
        if p2_index is None:
            p2_index = self._pick(regions_list, kits[0], "p2")
        if p2_index is not None and p2_index == p1_index:
            # 两个人的活路落在同一半：这份分工分不开两侧，给建议只会把两人
            # 挤在一起（真机 trace-20261109-124818：chop=both + 装盘不加功能
            # 区，两个人的装备都只剩灶，UD 布局下都只能去下半边）。
            p1_index = p2_index = None
        report.district = "LR" if any(
            r.label in ("左", "右") for r in regions_list) else "UD"
        report.labels = tuple(r.label for r in regions_list)
        report.p2_need = tuple(sorted(kits[0]))
        report.p1_need = tuple(sorted(kits[1]))
        report.signature = tuple(sorted(
            (r.size, round(r.center[0], 1), round(r.center[1], 1))
            for r in regions_list))
        if p2_index is not None:
            self._assign["p2"] = regions_list[p2_index].cells
        if p1_index is not None:
            self._assign["p1"] = regions_list[p1_index].cells
        if p2_index is not None:
            report.p2_cells = regions_list[p2_index].cells
            report.p2_label = regions_list[p2_index].label
            report.p2_wrong = p2_cell not in report.p2_cells
        if p1_index is not None:
            report.p1_cells = regions_list[p1_index].cells
            report.p1_label = regions_list[p1_index].label
            report.p1_wrong = p1_cell not in report.p1_cells
        p2_here = _region_index(regions_list, p2_cell)
        p1_here = _region_index(regions_list, p1_cell)
        report.p2_current = regions_list[p2_here].label if p2_here is not None else ""
        report.p1_current = regions_list[p1_here].label if p1_here is not None else ""
        if report.signature == self._last_signature:
            self._hits += 1
        else:
            self._last_signature = report.signature
            self._hits = 1
        if self._hits >= CONFIRM_OBSERVATIONS:
            self._record(report.signature, regions_list, p2_index, p1_index)
        report.safe_p2 = self._safe("p2")
        report.safe_p1 = self._safe("p1")
        return report

"""切菜两 task：FetchToBoard / Chop（spec §6.1 第 7/8 号）。

本模块只放切菜这一组，共享的身份机制与跨组谓词在 base.py。
外部不直接 import 本模块，一律 `from ai import tasks as T`
（分组边界是实现细节，见 ai/tasks/__init__.py）。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai.query import predicates as P
from ai import primitives as PR
from ai.query import stations as S
from ai.exec.bt import Condition, Node, Selector, Sequence
from ai.query.recipes import find_ingredient
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register,
                           ingredient_available, ingredient_source,
                           loose_count)

# ---------- 切菜常量 ----------

# 切菜：每个 chop_stage 的按住拍数 + 余量。实测 Cucumber / SushiFish 的
# chop_stages 都是 8。timeout 太小会让 HoldUntil 在切完前判 FAILURE，
# Planner 重选同一 task 又切一半——无限循环。所以取能覆盖最多阶段的
# 保守上限；这不浪费时间，因为早停靠 done 谓词而非 timeout。
CHOP_TICKS_PER_STAGE = 8
CHOP_TIMEOUT_SLACK = 20
CHOP_MAX_STAGES = 12

# ---------- 板相关谓词（仅本组用） ----------


def board_station(ctx, board_id):
    # type: (Dict, Any) -> Optional[Dict]
    """按资源 id 取切菜板的站点 dict（spec §5.2：id 可能是 (grid,name)
    元组，过渡期也接受裸字符串）。"""
    from ai.tasks.base import _grid_matches, _unpack_resource_id
    grid, name = _unpack_resource_id(board_id)
    for st in ctx["w"].stations:
        if st.get("t") != "chopping":
            continue
        if not P.station_name_matches(str(st.get("n") or ""), name):
            continue
        if not _grid_matches(st.get("grid"), grid):
            continue
        return st
    return None


@_register
class FetchToBoard(Task):
    """第 7 号：把生料放到切菜板上。

    item 是**生料名**（Cucumber），与 Chop 同口径——本 task 的 done
    （板上是生料）正是 Chop 的 feasible（spec §6.1 上下游咬合）。
    """

    TYPE = "FetchToBoard"
    HANDS = "empty"       # 要空手去拿生料；手里有东西时打分加腾手代价（spec §6.1(b)）

    @property
    def board_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.board_id, "fetch_to"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        """板上 item == 该生料（spec §6.1）。绑定具体板求值。"""
        return P.board_item(ctx["w"].workstations,
                            str(self.board_id)) == str(self.item).lower()

    def feasible(self, ctx):
        # type: (Dict) -> bool
        if not ctx["book"].needs_chopping(self.item):
            return False       # 不可切的料没理由放板上
        if self.done(ctx):
            return False
        if P.board_item(ctx["w"].workstations, str(self.board_id)):
            return False       # 板上已有东西，放不下
        return ingredient_available(ctx, self.item)

    def build(self):
        # type: () -> Node
        board_id, raw = self.board_id, self.item

        def crate(c):
            return S.find_crate(c["w"].stations, raw)

        def holding(c):
            return P.held_matches(c["chef"], raw)

        def has_loose(c):
            return loose_count(c["w"], raw) > 0

        def source_is_crate(c):
            # 只有箱子有料时才投：find_ingredient 优先级 held→board→loose→
            # crate，能落到 crate 说明别处都没有（散落/在手走别的分支）。
            loc = find_ingredient(c["w"], raw, exclude_plate=True)
            return loc is not None and loc.get("kind") == "crate"

        def board(c):
            return board_station(c, board_id)

        def placed(c):
            return P.board_item(c["w"].workstations,
                                str(board_id)) == str(raw).lower()

        def pickup():
            # ingredient_source 现在对「挂在站点上的散落料」返回站点（带朝向
            # 信号），故 face=True 让 Face 转身面向站点；裸散落料（无站点）
            # 时 interact_ready 走名字兜底，Face 仍能到位。
            return PR.InteractWith(
                lambda x: ingredient_source(x, raw), "pickup", holding,
                face=True)

        def drop():
            return PR.InteractWith(board, "placement", placed)

        return PR.Timeout(Selector([
            # 分支1：地上有散落生料 → 直接捡散落放上砧板（不投、不碰箱子）
            Sequence([
                Condition(has_loose),
                PR.EnsureHandsFree(keep=holding),
                pickup(),
                drop(),
            ]),
            # 分支2：只有箱子有 → 从箱子取一份直接放上砧板。
            #
            # 这里曾是「RepeatUntil 批量取+投 N 份，再捡散落上板」，与
            # FetchToCooker 单料同构、也同样坏：必须投满 N 份才轮到 drop()，
            # 一轮取+投约 80 拍而整树只有 TASK_TIMEOUT_TICKS=200，第二轮
            # 没结束就被砍 → 料全堆在地上、板上一直空着。
            # 砧板一次只放一根（feasible 要求板上为空），批量投更没有意义。
            Sequence([
                Condition(source_is_crate),
                PR.EnsureHandsFree(keep=holding),
                PR.InteractWith(crate, "pickup", holding),
                drop(),
            ]),
            # 分支3：生料已在手上（pickup 靠 expect 短路）→ 直接放上砧板
            Sequence([
                PR.EnsureHandsFree(keep=holding),
                pickup(),
                drop(),
            ]),
        ]), TASK_TIMEOUT_TICKS)


@_register
class Chop(Task):
    """第 8 号：在板上把生料切好。

    完成判定是"板上产物 == **配方期望的加工态**"（spec §7.3 定稿），
    由 RecipeBook.chop_target 从箱子的 chop 字段取。spec §7.3 原话：
    "初版写 item == Chopped*，但多阶段切配（需切两轮）或产物命名不以
    Chopped 开头时会误判。"

    另一半同样重要：绑定 Cucumber 的 Chop 看到板上是 ChoppedSushiFish
    不算自己干完了——这就是 §6.0"谓词按绑定参数求值"。
    """

    TYPE = "Chop"
    HANDS = "empty"       # 切菜要空手；手里有东西时打分加腾手代价（spec §6.1(b)）

    @property
    def board_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.board_id, "chop"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        target = ctx["book"].chop_target(self.item)
        if not target:
            return False
        on_board = P.board_item(ctx["w"].workstations, str(self.board_id))
        return on_board == str(target).lower()

    def feasible(self, ctx):
        # type: (Dict) -> bool
        if not ctx["book"].needs_chopping(self.item):
            return False
        if self.done(ctx):
            return False
        on_board = P.board_item(ctx["w"].workstations, str(self.board_id))
        return on_board == str(self.item).lower()

    def build(self):
        # type: () -> Node
        board_id, raw = self.board_id, self.item
        done = self.done

        def board_target(c):
            return board_station(c, board_id)

        # HoldUntil 的 timeout 必须在构造时确定（spec §7.2 不许有默认值），
        # 而 chop_stages 要查 ctx。折中：构造时用最大阶段数的保守上限，
        # 真正的早停由 done 谓词负责——切好那一拍 HoldUntil 立刻 SUCCESS。
        timeout = CHOP_TICKS_PER_STAGE * CHOP_MAX_STAGES + CHOP_TIMEOUT_SLACK
        return PR.Timeout(Sequence([
            # 切菜前先清手（spec §6.1(b)/§7.4）：手里端着锅/盘直接站到砧板前，
            # HoldUntil 切不动 → 卡住（实机复现）。
            PR.EnsureHandsFree(),
            PR.Approach(board_target, None),
            PR.Face(board_target),
            # target 让 HoldUntil 每拍校验还在砧板前：被传送带推走时松键 FAILURE，
            # Sequence 重置重新 Approach，而不是空按 chop 卡死（实机复现）。
            PR.HoldUntil("chop", done, timeout=timeout, target=board_target),
        ]), TASK_TIMEOUT_TICKS)

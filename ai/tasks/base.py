"""task 的四元身份、基类与认领前置（spec §6.0 / §6.2 / §4.2）。

task 粒度 = **厨师的一趟差事**（spec §6.1）。边界切在"世界状态可明确
观测"的地方——例如倒完饭后"手持空锅"是状态里直接读得到的事实，所以
放回灶台切成独立 task，中断后看一眼手里就知道该干啥。

**task 实例 = (type, order_id, bound_resources, bound_item)**（spec §6.0）。
两个 task 只有四项全同才是同一个。FetchToPlate(Cucumber→plate_A,
order#1) 与 FetchToPlate(Cucumber→plate_B, order#2) 是两个不同的 task。

为什么身份必须这样定义（spec §6.0 给的具体 bug）：两单都缺黄瓜时，若
FetchToPlate 的完成判定不绑定具体盘，P1 把黄瓜放进盘 B 会让 AI 针对盘 A
的 task 误报 SUCCESS——AI 以为自己干完了，盘 A 其实还空着。所以下面所有
谓词都是**绑定参数上的函数**，不是全局谓词。

**不建显式 DAG、不做拓扑排序**（spec §6.2）：每个 task 自己声明"此刻我
是否可做"，执行顺序从可行性中自然涌现。饭没熟 → TakeCookedToPlate 自动
不在候选里。因此不需要记录"我做到第几步"——无状态重算、中断自愈、用户
抢单自适应全部源于这一点。

**上下游咬合**（spec §6.1）：每个 task 的 done 即下游 task 的 feasible
（1 的完成 = 4/9 的可行；4 的完成 = 5 的可行）。同一谓词写一份两处用，
前后校验口径天然一致。

Idle 不在这里（spec §6.1 最后一条）：初版把 Idle 的可行性写成"无其他可行
task"，那不是世界谓词而是对候选集本身的谓词。它是 Planner 流水线第 ⑤ 步
之后的 fallback，归 Plan 3。

**为什么 tasks 是包而非单文件**：16 个 task 的实现合计 1100 行上下，远超
本项目"无单个文件 > 400 行"的硬指标（spec §11.5）。本模块放身份与基类，
plate / cook / chop / assembly / cleanup 各放一组 task。外部一律
`from ai import tasks as T`——子模块边界是实现细节，TASK_TYPES 的完整性
由 `ai/tasks/__init__.py` 的导入保证。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai import predicates as P
from ai import recipes as R
from ai.bt import Node
from ai.plate_tokens import parse_plate_detail, parse_pot_contents

# 认领的所有者标识。AI 控制的是 P2（spec §6.7：AI 自己的意图写在 claims
# 里，不需要推断）。
OWNER = "P2"

# task 级超时的默认上限（拍）。20 Hz 下 200 拍 = 10 秒，够走完全图最长
# 路径再做一次交互；超过说明卡住了，交还 Planner（spec §7.2）。
TASK_TIMEOUT_TICKS = 200


class Task(object):
    """task 基类：不可变值对象。

    不可变的理由：Planner 每拍重算、把 task 当字典键与集合元素用
    （承诺惯性要比较"与上拍是否同一个 task"）。可变对象做键会出错。
    """

    TYPE = "Task"
    # 手部前置（spec §6.1(b)）：需要什么样的手才能做。"any" / "empty"。
    # "empty" 表示要空手去拿东西；手不空时**不是不可行**，由 Planner 在
    # 打分上加"腾手代价"（见 planner._score_candidate），执行时由树首
    # EnsureHandsFree 清手。
    HANDS = "any"

    __slots__ = ("_order_id", "_resources", "_item")

    def __init__(self, order_id=None, resources=(), item=None):
        # type: (Any, Any, Any) -> None
        object.__setattr__(self, "_order_id", order_id)
        object.__setattr__(self, "_resources", tuple(resources or ()))
        object.__setattr__(self, "_item", item)

    def __setattr__(self, name, value):
        raise AttributeError("Task 是不可变值对象：%s" % name)

    @property
    def order_id(self):
        return self._order_id

    @property
    def resources(self):
        # type: () -> Tuple[Any, ...]
        return self._resources

    @property
    def item(self):
        return self._item

    @property
    def key(self):
        # type: () -> Tuple
        """spec §6.0 的四元身份。"""
        return (self.TYPE, self._order_id, self._resources, self._item)

    def __eq__(self, other):
        return isinstance(other, Task) and self.key == other.key

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.key)

    def __repr__(self):
        parts = []
        if self._item is not None:
            parts.append(str(self._item))
        parts.extend(str(r) for r in self._resources)
        inner = ", ".join(parts)
        if self._order_id is not None:
            inner = "%s, order#%s" % (inner, self._order_id) if inner \
                else "order#%s" % self._order_id
        return "%s(%s)" % (self.TYPE, inner)

    # ---------- 子类契约 ----------
    def claims(self):
        # type: () -> Tuple[Tuple[Any, str], ...]
        """本 task 需要的 (resource_id, action) 键。"""
        raise NotImplementedError

    def feasible(self, ctx):
        # type: (Dict) -> bool
        """此刻我是否可做（spec §6.2）。按绑定参数求值。"""
        raise NotImplementedError

    def done(self, ctx):
        # type: (Dict) -> bool
        """完成判定。即下游 task 的 feasible（上下游咬合）。"""
        raise NotImplementedError

    def build(self):
        # type: () -> Node
        """构建执行树。Executor 持有它（spec §7.6），不进黑板。"""
        raise NotImplementedError


def claims_available(task, ctx):
    # type: (Task, Dict) -> bool
    """全局可行性前置（spec §4.2）：所需资源均未被他人有效认领。

    spec §4.2 原话："设计稿初版的致命缺陷：定义了认领表，却没有任何一条
    可行性谓词读它。那么 claims 是一张只写不读的表，'多锅/多台资源管理'
    这项能力没有任何落地机制。"

    这一条**优先于各 task 自己的谓词**——Planner 的可行性过滤最前面调它。

    粒度是 (resource, action)（spec §4.0）：P1 往 1 号锅放了生饭只锁
    (cooker1,"fetch_to")，(cooker1,"take_cooked") 依然开放——取熟饭
    恰恰是最有价值的接力。
    """
    bb = ctx["bb"]
    tick = ctx.get("tick") or 0
    for resource_id, action in task.claims():
        if bb.is_claimed_by_other(resource_id, action, OWNER, tick):
            return False
    return True


TASK_TYPES = {}   # type: Dict[str, type]


def _register(cls):
    # type: (type) -> type
    """登记到 TASK_TYPES。tree_viz 以它为入口枚举全部 task 类型。"""
    TASK_TYPES[cls.TYPE] = cls
    return cls


# ---------- 跨组共享谓词 ----------
# 只放**两组以上** task 都要用的。单组专用的谓词留在该组模块里
# （晾干架/脏盘在 plate.py、灶状态在 cook.py、板状态在 chop.py），
# 否则 base.py 会变成又一个什么都装的大文件。
#
# plate_by_id / plate_cell / station_at_cell 在这里的理由：plate 组定位
# 拼装格、cook 组的 TakeCookedToPlate 找目标盘、assembly 组装盘与出餐
# 都要用。放 plate.py 会让 cook.py 反向 import 它，组间依赖就乱了。

def plate_by_id(ctx, plate_id):
    # type: (Dict, Any) -> Optional[Dict]
    """按实例名取盘。取不到 = 盘不在快照里（被端走/消失）。

    资源 id 规则（spec §4.4）：盘用实例名 n 为主。
    """
    for p in ctx["w"].plates:
        if str(p.get("n")) == str(plate_id):
            return p
    return None


def plate_cell(ctx, p):
    # type: (Dict, Optional[Dict]) -> Optional[Tuple[int, int]]
    if not p:
        return None
    x, z = p.get("x"), p.get("z")
    if x is None or z is None:
        return None
    return ctx["w"].to_grid(x, z)


def placed_plates(ctx):
    # type: (Dict) -> List[Dict]
    """已就位（落在某个 counter 拼装格上）的盘。

    "放/取"动作只对摆在台面上的盘有意义：还在盘堆里的盘既不能往上倒料，
    也不该算作"某张单的拼装盘"。
    """
    counters = set()
    for st in ctx["w"].stations:
        if st.get("t") != "counter":
            continue
        gx, gz = st.get("gx"), st.get("gz")
        if gx is not None and gz is not None:
            counters.add((gx, gz))
    out = []  # type: List[Dict]
    for p in ctx["w"].plates:
        if plate_cell(ctx, p) in counters:
            out.append(p)
    return out


def station_at_cell(ctx, cell, kinds=None):
    # type: (Dict, Optional[Tuple[int, int]], Optional[Tuple[str, ...]]) -> Optional[Dict]
    """取某格上的站点。cell 是 (gx, gz)；kinds 限定类型。

    拼装格的资源 id 就是网格坐标（spec §4.4"坐标即身份"），所以要能从
    坐标反查站点 dict 以喂给原语。
    """
    if cell is None:
        return None
    for st in ctx["w"].stations:
        if st.get("gx") == cell[0] and st.get("gz") == cell[1]:
            if kinds is None or st.get("t") in kinds:
                return st
    # 快照里没有该格的站点条目：合成一个最小 dict（原语只要 n/gx/gz/x/z）
    x, z = ctx["w"].to_world(cell[0], cell[1])
    return {"t": "counter", "n": "", "gx": cell[0], "gz": cell[1],
            "x": x, "z": z}


def ingredient_available(ctx, raw):
    # type: (Dict, Optional[str]) -> bool
    """生料能否取到（手上 / 板上 / 散落 / 箱子，不含已装盘的）。

    排除已装盘的：那是别人的半成品，从盘里抠料出来不是合法操作。但"盘上
    有一份"不该挡住"箱子里再取一份"——exclude_plate 让 find_ingredient 跳过
    盘继续找箱子（实机：order#1 盘里已有海苔/鱼，order#2 就再也取不到料）。
    cook / chop / assembly / cleanup 四组都要用，故放在 base。
    """
    if not raw:
        return False
    loc = R.find_ingredient(ctx["w"], raw, exclude_plate=True)
    return loc is not None and loc.get("kind") != "plate"


def ingredient_source(ctx, raw):
    # type: (Dict, Optional[str]) -> Optional[Dict]
    """生料的取用站点 dict。在手上时返回 None（无需再取）。"""
    loc = R.find_ingredient(ctx["w"], raw, exclude_plate=True)
    if loc is None or loc.get("kind") in ("plate", "held"):
        return None
    return loc.get("st")


def cooked_sources(world, book):
    # type: (Any, Any) -> List[Dict]
    """可倒进盘的熟料源：灶上熟锅（未糊）+ 台面/地上的散熟锅。

    返回 [{kind, id, tokens}]：
      kind:   "cooker"（灶上） | "loose"（台面/地上的散锅）
      id:     认领资源 id（灶实例名 / 散锅 item 名）
      tokens: 盘态 token 集，熟料是 cooked:{raw}:{cookware}

    内容精确读取：灶读 cookers[].pot_contents 反查；散锅读 items[].detail。
    旧帧两者都没有时不产出源（旧帧已删，见 AGENTS.md §7）。
    """
    out = []  # type: List[Dict]

    for c in world.cookers:
        if not P.cooker_cooked(c) or P.cooker_burning(c):
            continue
        cid = c.get("n")
        if not cid:
            continue
        toks = _cooker_cooked_tokens(c, book)
        if toks:
            out.append({"kind": "cooker", "id": cid, "tokens": toks})

    cooker_cells = _cooker_cells(world)
    for it in world.items:
        x, z = it.get("x"), it.get("z")
        if x is None or z is None:
            continue
        if not P.held_is_cookware({"held": it.get("n")}, world.recipes):
            continue
        if world.to_grid(x, z) in cooker_cells:
            continue                       # 在灶台上 = 在用，不是散锅
        toks = parse_plate_detail(it.get("detail") or [])
        if not any(t.startswith("cooked:") for t in toks):
            # 手持/散熟锅的 detail 可能只报原始料名（ing:SushiRice），
            # 而 cooked 字段才是熟的权威信号（实机复现：手持熟饭锅
            # detail 是 ['ing:SushiRice']、cooked="Cooked"，只看 detail 会漏掉）。
            if str(it.get("cooked") or "").lower() != "cooked":
                continue
            raws = parse_pot_contents(it.get("detail") or [])
            toks = set()
            for raw in raws:
                for cooked in book.cooked_of_raw(raw):
                    t = book.rich_token(cooked)
                    if t:
                        toks.add(t)
        if any(t.startswith("cooked:") for t in toks):
            out.append({"kind": "loose", "id": it.get("n"), "tokens": toks})
    return out


def _cooker_cooked_tokens(c, book):
    # type: (Dict, Any) -> Set[str]
    """灶上熟锅的 rich token 集：pot_contents 生料名 → cooked_of_raw → rich_token。"""
    raw_names = parse_pot_contents(c.get("pot_contents") or [])
    toks = set()  # type: Set[str]
    for raw in raw_names:
        for cooked in book.cooked_of_raw(raw):
            t = book.rich_token(cooked)
            if t:
                toks.add(t)
    return toks


def _cooker_cells(world):
    # type: (Any) -> Set[Tuple[int, int]]
    """所有灶台格（散锅判定的排除区）。"""
    cells = set()  # type: Set[Tuple[int, int]]
    for st in world.stations:
        if st.get("t") != "cooking":
            continue
        gx, gz = st.get("gx"), st.get("gz")
        if gx is not None and gz is not None:
            cells.add((gx, gz))
    return cells

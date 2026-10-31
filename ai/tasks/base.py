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

from ai.query import predicates as P
from ai.query import recipes as R
from ai.exec.bt import Node
from ai.query.plate_tokens import parse_plate_detail, parse_pot_contents

# 认领的所有者标识。AI 控制的是 P2（spec §6.7：AI 自己的意图写在 claims
# 里，不需要推断）。
OWNER = "P2"


def _unpack_resource_id(rid):
    # type: (Any) -> Tuple[Optional[str], str]
    """兼容过渡：rid 可能是 (grid, name) 元组，也可能是裸名字字符串
    （Task 12 之前的旧调用点）。统一拆成 (grid_or_None, name)。

    grid 为 None 时查找退化为"只比名字"（旧行为，向后兼容）；grid 非
    None 时要求站点/灶的 grid 字段也匹配，解决同名跨网格撞车（spec §5.2）。
    """
    if isinstance(rid, tuple) and len(rid) == 2:
        return (rid[0], str(rid[1]))
    return (None, str(rid))


def _grid_matches(entry_grid, want_grid):
    # type: (Any, Optional[str]) -> bool
    """比较站点/灶的 grid 字段与候选 id 携带的 grid 分量是否一致。

    want_grid 为 None 时不比较（旧裸字符串调用点，向后兼容，恒 True）。
    两侧都按"缺失 → 空字符串"归一化后再比：`P.resource_id` 在构造候选 id
    时已把缺字段归一化成 ""，但真实世界 dict（旧 fixture、尚未带 grid
    字段的测试）里缺字段读出来是 None，不归一化会让 "" != None 恒假
    （正是 task-12-brief 提醒要修的那个坑：候选侧 "" vs 世界侧 None）。
    """
    if want_grid is None:
        return True
    return str(entry_grid or "") == str(want_grid or "")

# task 级超时的默认上限（拍）。20 Hz 下 200 拍 = 10 秒，够走完全图最长
# 路径再做一次交互；超过说明卡住了，交还 Planner（spec §7.2）。
TASK_TIMEOUT_TICKS = 200


class Task(object):
    """task 基类：不可变值对象。

    不可变的理由：Planner 每拍重算、把 task 当字典键与集合元素用
    （承诺惯性要比较"与上拍是否同一个 task"）。可变对象做键会出错。
    """

    TYPE = "Task"
    # 一句话中文说明：对话层把 DAG 节点渲染给 LLM 时带出（`Kitchen.render`），
    # 让模型不用猜英文类型名的语义。每个注册的 task 都必须覆盖。
    DESC = ""
    # 步骤短名（两三个字的动词），对话层渲染物料加工链时用：「取料 → 切 → 装盘」。
    LABEL = ""
    # 这一步决定物料的加工方式时填（切 / 煮 / 拌）：对话层据此给物料打标签，
    # 让 LLM 知道「我负责煮的」指哪些物料。
    PROCESS = ""
    # 对话层账本的 DAG 元数据（ai/dialog/duties.py 读取，planner 不用）：
    # - SHARED：不分菜的共享节点（洗盘/摆盘/送餐），作用域是 (类型, None)；
    # - STATION：本关要有这类站点（frame stations[].t）才进目录；
    SHARED = False
    STATION = None      # type: Optional[str]
    # 共享动作允许的分工档位（update_duties 的 mode）。None = 四档都接受；
    # 例如送餐设 ("now", "do")：不能「交给 P1」也不能「先别做」——谁在旁边谁送。
    DUTY_MODES = None   # type: Optional[tuple]
    # 手部前置（spec §6.1(b)）：需要什么样的手才能做。"any" / "empty"。
    # "empty" 表示要空手去拿东西；手不空时**不是不可行**，由 Planner 在
    # 打分上加"腾手代价"（见 planner._score_candidate），执行时由树首
    # EnsureHandsFree 清手。
    HANDS = "any"
    # 声明为 True 的任务不受 L3 拉黑（scratch.abandoned）约束：端锅/盛饭/
    # 灭火这类停火关键任务，拉黑期（200 拍）比烧糊倒计时还长，拉黑就等于
    # 放弃救锅 → 必着火。写侧（_note_failure / note_motion_failure）与读侧
    # （planner._is_abandoned）都据此跳过。
    NO_ABANDON = False

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
    for p in P.all_plates(ctx["w"]):
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


def plate_matches_order(ctx, plate, order_id):
    # type: (Dict, Optional[Dict], Any) -> bool
    """盘内容是否仍是该订单需求的子集（装盘类 task 的可行性补充）。

    P1 往 P2 正在装料的盘里放了别的料 → 盘↔订单匹配会把盘改绑走
    （planner._match_plates_to_orders 只认 plate_tokens ⊆ order_want_tokens），
    但已承诺的 FetchToPlate / TakeCookedToPlate 的 feasible 若不校验这一点，
    就会继续往这张盘倒不属于该单的料（实机 trace-20261021-231257：
    往有虾的盘里倒鱼）。

    订单找不到（已送掉/过期）也返回 False：别往一张没主的盘倒料，交给
    ResetPlate 清盘。order_id 为 None（无订单绑定的防御调用）不拦。
    """
    if plate is None:
        return False
    if order_id is None:
        return True
    book = ctx["book"]
    for o in ctx["w"].orders:
        if str(o.get("id")) == str(order_id):
            return book.plate_tokens(plate) <= book.order_want_tokens(o)
    return False


def plate_on_counter(ctx, p, counter):
    # type: (Dict, Optional[Dict], Any) -> bool
    """盘此刻是否落在拼装位 counter 上（(gx, gz) 或移动台面的 (grid, 站名)）。

    移动台面按同一帧的站点投影比：盘和台面一起随篮子走，同帧投影一致。
    """
    from ai.query.placement import counter_cell_now
    cell = counter_cell_now(ctx["w"], counter)
    return cell is not None and plate_cell(ctx, p) == tuple(cell)


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
    from ai.query.placement import _station_cell, is_station_key, station_by_key
    w = ctx["w"]
    # 移动网格台面的拼装位 id 是 (grid, 站名)：按名字取，不按格——篮子一动
    # 格就对不上（placement.counter_key）。取不到返回 None，交给 feasible 判。
    if is_station_key(cell):
        st = station_by_key(w, cell)
        if st is not None and (kinds is None or st.get("t") in kinds):
            return st
        return None
    # 拼装格是主网格坐标（recipe_graph._counter_cells）：按 x/z 投影比，不比
    # 站点自带的 gx/gz——篮子台面的 gx/gz 是篮子本地坐标。
    for st in w.stations:
        sc = _station_cell(w, st)
        if sc is not None and tuple(sc) == tuple(cell):
            if kinds is None or st.get("t") in kinds:
                return st
    # 快照里没有该格的站点条目：合成一个最小 dict（原语只要 n/gx/gz/x/z）
    x, z = ctx["w"].to_world(cell[0], cell[1])
    return {"t": "counter", "n": "", "gx": cell[0], "gz": cell[1],
            "x": x, "z": z}


def cooker_state(ctx, cooker_id):
    # type: (Dict, Any) -> Optional[Dict]
    """按资源 id 取灶的状态条目（spec §5.2：id 可能是 (grid,name) 元组，
    过渡期也接受裸字符串——见 _unpack_resource_id）。

    灶的**状态**在 `cookers[]` 里、**位置**在 `stations[]` 里，两个 dict
    各取所需。cook / fire 两组都要用，故放在 base。
    """
    grid, name = _unpack_resource_id(cooker_id)
    for c in ctx["w"].cookers:
        if not P.station_name_matches(str(c.get("n") or ""), name):
            continue
        if not _grid_matches(c.get("grid"), grid):
            continue
        return c
    return None


def cooker_station(ctx, cooker_id):
    # type: (Dict, Any) -> Optional[Dict]
    """按资源 id 取灶的站点 dict（原语要 gx/gz 定位）。"""
    grid, name = _unpack_resource_id(cooker_id)
    for st in ctx["w"].stations:
        if st.get("t") != "cooking":
            continue
        if not P.station_name_matches(str(st.get("n") or ""), name):
            continue
        if not _grid_matches(st.get("grid"), grid):
            continue
        return st
    return None


def _source_on_moving_platform(ctx, loc):
    # type: (Dict, Optional[Dict]) -> bool
    """生料来源是否在「正在移动的非主网格平台」（气球篮）上。

    共享判据见 P.on_moving_platform；这里只从 find_ingredient 的 loc 里
    取出来源站点的 grid 再委托过去。

    生料来源过滤（方案 B）已禁用：恒返回 False。调用点等价于
    `reachability.can_interact` 原样判定，不再因来源在移动篮子上而否决。
    """
    return False
    # if not loc:
    #     return False
    # st = loc.get("st")
    # if not isinstance(st, dict):
    #     return False
    # return P.on_moving_platform(st.get("grid"), ctx)


def ingredient_available(ctx, raw, contents=()):
    # type: (Dict, Optional[str], Any) -> bool
    """生料能否取到（手上 / 板上 / 散落 / 箱子，不含已装盘的）。

    排除已装盘的：那是别人的半成品，从盘里抠料出来不是合法操作。但"盘上
    有一份"不该挡住"箱子里再取一份"——exclude_plate 让 find_ingredient 跳过
    盘继续找箱子（实机：order#1 盘里已有海苔/鱼，order#2 就再也取不到料）。
    cook / chop / assembly / cleanup 四组都要用，故放在 base。
    """
    if not raw:
        return False
    from ai.space import reachability
    loc = R.find_ingredient(ctx["w"], raw, exclude_plate=True,
        can_access=lambda st: not _source_on_moving_platform(ctx, {'st': st}) and reachability.can_interact(ctx, st),
        contents=contents)
    if loc is None or loc.get("kind") == "plate":
        return False
    if _source_on_moving_platform(ctx, loc):
        return False
    return True


def ingredient_source(ctx, raw, contents=()):
    # type: (Dict, Optional[str], Any) -> Optional[Dict]
    """生料的取用站点 dict。在手上时返回 None（无需再取）。

    contents 非空 = 取已装料载体（包了熟肉的卷饼），见 find_ingredient。
    """
    from ai.space import reachability
    loc = R.find_ingredient(ctx["w"], raw, exclude_plate=True,
        can_access=lambda st: not _source_on_moving_platform(ctx, {'st': st}) and reachability.can_interact(ctx, st),
        contents=contents)
    if loc is None or loc.get("kind") in ("plate", "held"):
        return None
    st = loc.get("st")
    if loc.get("kind") == "loose":
        # 散落物若挂在某个站点上（on_station），返回站点而非裸 item——
        # 站点有 grid/gx/gz 朝向信号，interact_ready/AlignToCell/Face 才能用
        # 本地网格判对。裸 item 只有 x/z，投主网格会跟游戏句柄的本地坐标
        # 恒不等（trace-20261018-005334 t=32314：番茄在篮子台面上，句柄
        # 指向旁边的箱子，永远拿不到料）。
        resolved = _station_for_item(ctx["w"], st)
        if resolved is not None:
            return resolved
    return st


def held_matches_contents(ctx, item, contents=()):
    # type: (Dict, Optional[str], Any) -> bool
    """P2 手里是不是「名字 = item、内容 = contents」的那件。

    只比名字会把同名不同料的物品当成一件：去拿包肉卷饼时游戏句柄选中旁边
    的空卷饼，确认照样通过。内容读 world.held_items（插件给手持物打 held_by）。
    """
    from ai.query.carriers import held_contents
    chef = ctx["chef"]
    if not P.held_matches(chef, item):
        return False
    return held_contents(ctx["w"], chef.get("name")) == frozenset(contents or ())


def _station_for_item(world, item):
    # type: (Any, Optional[Dict]) -> Optional[Dict]
    """散落 item 所在站点（插件的 on_station 字段反查）。找不到返回 None。"""
    if not isinstance(item, dict):
        return None
    os = str(item.get("on_station") or "")
    if ":" not in os:
        return None
    # on_station 形如 "attach:<站点名>" / "cooking:<灶名>"，取冒号后的名字
    name = os.split(":", 1)[1].strip()
    if not name:
        return None
    x, z = item.get("x"), item.get("z")
    named = [st for st in getattr(world, "stations", None) or ()
             if str(st.get("n") or "") == name]
    # 同名站点跨网格并存（Rapids_2_1 厨房与木筏各有一张 countertop_01_standard_old
    # (4)/(5)/(6)/(11)）：只认第一个会把木筏台面上的鸡块解析成厨房台面，P2 走
    # 不过去、FetchToBoard 反复失败被拉黑（trace-20261029-122831 t=1268~1996）。
    # 按离物品最近选。不能按 item.grid：插件也是按站点名反查网格，同名时
    # 一样取错（实测鸡块在木筏台面 (31.3,10.1)，item.grid 却报 GridManager）。
    if len(named) > 1 and x is not None and z is not None:
        named.sort(key=lambda st: (st.get("x") is None,
                                   (float(st.get("x") or 0) - x) ** 2
                                   + (float(st.get("z") or 0) - z) ** 2))
    if named:
        return named[0]
    # 名字没匹配到（功能站 GameObject 命名可能不一致）时，退一步按位置匹配
    # 传送带段：物料就挂在段上，两者世界坐标接近。
    if x is None or z is None:
        return None
    for st in getattr(world, "stations", None) or ():
        if st.get("t") != "conveyor":
            continue
        sx, sz = st.get("x"), st.get("z")
        if sx is None or sz is None:
            continue
        if abs(sx - x) < 1.0 and abs(sz - z) < 1.0:
            return st
    return None


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

    for it in world.items:
        if not P.item_is_cookware(it, world.recipes):
            continue
        if P.item_on_cooker(it):
            continue                       # 在灶台上 = 在用，不是散锅（字段判据）
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
        toks = _only_cookware(toks, it.get("n"), world.recipes)
        if any(t.startswith("cooked:") for t in toks):
            out.append({"kind": "loose", "id": it.get("n"), "tokens": toks})
    return out


def _only_cookware(toks, item_name, recipes):
    # type: (Set[str], Any, Any) -> Set[str]
    """散锅 token 只留与这口锅炊具类型一致的：cooked_of_raw(prawn) 同时给出
    BoiledPrawns/PanFriedPrawns，平底锅里的虾不该同时声称是 cooked:prawn:pot
    （否则会被配给要「煮虾」的单）。认不出锅的类型时原样返回。"""
    kinds = [k for k in P.cookware_names(recipes)
             if P.cookware_matches(str(item_name or "").lower(), k)]
    if not kinds:
        return toks
    keep = set()
    for t in toks:
        parts = t.split(":")
        if len(parts) < 3 or parts[0] != "cooked" or parts[-1] in kinds:
            keep.add(t)
    return keep


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


# 批量取+投的 buffer：散落料够「订单数 + buffer」就停（实机调）。
RELAY_BUFFER = 1


def loose_count(world, raw):
    # type: (Any, str) -> int
    """场上散落的该料数量（投掷落地后变成 loose；飞行中的不算，与
    find_ingredient 的散落口径一致）。"""
    want = str(raw).lower()
    n = 0
    for it in world.items or ():
        if it.get("flying") is True:
            continue
        if str(it.get("n") or "").lower() == want:
            n += 1
    return n


def loose_on_station(world, raw, segment):
    # type: (Any, str, Any) -> bool
    """散落生料 raw 当前是否挂在站点 segment 上（传送带拦截等料判据）。

    传送带上的料（1-6 的米/鱼/海苔）在持续移动：`ingredient_source` 每拍把
    料解析成它当前所在的传送带段，段随料跨段移动而变。要改成「走到固定段
    旁等料经过」就必须能问「这份料现在是不是在我锁定的这一段上」。判据与
    `_station_for_item` 同源：优先 `on_station` 名称匹配，名字对不上退回按
    世界坐标就近匹配 conveyor 段。
    """
    if not segment:
        return False
    seg_name = str(segment.get("n") or "") if isinstance(segment, dict) else str(segment)
    if not seg_name:
        return False
    want = str(raw or "").lower()
    for it in getattr(world, "items", None) or ():
        if str(it.get("n") or "").lower() != want:
            continue
        st = _station_for_item(world, it)
        if st is not None and P.station_name_matches(str(st.get("n") or ""), seg_name):
            return True
    return False


def need_count(world, name):
    # type: (Any, str) -> int
    """订单里需要该料的份数（需煮的熟料、需切的生料都在 composition 里，通用）。"""
    want = str(name).lower()
    n = 0
    for order in world.orders:
        comp = [str(c).lower() for c in (order.get("composition") or [])]
        if want in comp:
            n += 1
    return n

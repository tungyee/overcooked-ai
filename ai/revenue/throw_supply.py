# -*- coding: utf-8 -*-
"""投掷备料的供需计数：这种料还要往消费站那边投几份（ThrowBatch 的 N）。

一份料从箱子到盘子要经过若干「消费站」（砧板、灶），每过一站形态变一次：
  Potato --砧板--> Chopped_Potato --炸锅--> FriedChips --> 盘
每一段都可能需要投掷：料在 P2 这边、下一站在对面（Rapids_2_1 不连通），或
同侧但很远（批量投过去比来回跑快）。

缺口 = 需求 − 已越过这一站的 − 已在这一站附近的（含飞行中）
  - 需求按可见订单的成分逐份计，外加 THROW_EXTRA_UNITS 份预备（订单可见数
    有限，想多备料就加大它）。**不按接料空位封顶**：站点满了，料落在旁边地
    上同样是提前备好的料。
  - 「越过这一站」= 已切好/在锅里/在盘里的更下游形态，按 cook_supply 的
    口径计（下锅 → cooked_supply；切 → 下游形态 + cooked_supply）。

按项目约定：只读入参，不写 World / Blackboard。
"""
import math
from typing import Any, Dict, List, Optional, Tuple

from ai.query import predicates as P
from ai.query import stations as S
from ai.revenue import cook_supply as CS
from ai.space import regions as RG
from ai.space import throw as TH

# 订单之外多投的预备份数（每条加工链）。0 = 只投可见订单要的份数：远投打开后
# 多投的那份常落在锅旁地上没人用，先不留余量（2026-10-30）。
THROW_EXTRA_UNITS = 0
# 连通地图上「远」的门槛（m）：料到消费站的直线距离超过它才投。射程固定
# ≈7.5m，近了投不过去还得退到 7.5m 外出手，不如走过去放。
THROW_MIN_DIST = 9.0
# 连通地图的远距投掷开关。分割地图（Rapids）投掷是必需的，不受它控制。
# 曾关闭：一次只投一份（relay.MAX_BATCH_UNITS=1）时远投没有摊销，料还落在
# 锅旁地上要再捡一次（trace-20261029-202903）。2026-10-30 重新打开试 2-2
# （Balloon_2_3）：料箱在左/中篮、灶全在右篮，中间两道会关的门，攻略打法
# 就是隔缝把料扔过去。近距「投进锅」仍由 FillCooker 末段 ThrowOntoStation 负责。
ENABLE_FAR_THROW = True
# 「在消费站附近」的半径（m，连通地图口径）：约 3 格，落地的料算已就位。
# 2.6m 时砸在锅边弹开、落在 ~3m 处的意面不算就位，被当来源捡起来重投
# （trace-20261029-130632）；落在这个圈内的料由 FetchToCooker 走过去放。
NEAR_DEST_RADIUS = 3.6
# 分割地图的落点容忍距离（m）：落在对面地上即可，P1 去捡。木筏离炸锅
# 9~10m、射程 7.5m，按连通口径 2.6m 会把「木筏中部」整片判成不能投。
SPLIT_MAX_LAND = 4.5
# 分割地图第二档落点容忍（m）：4.5m 内找不到出手位时放宽到它再找一次。
# Balloon_2_2 左地板→右篮炸锅，从门洞斜穿的最近落点离炸锅 4.49m，贴着 4.5
# 的线，站位差一点就判「投不了」（trace-20261031-150043 t=153/257）。料只要
# 落在右篮地板上 P1 就能捡；先按 4.5 找，Rapids 等原有方案不变。
SPLIT_MAX_LAND_RELAXED = 6.0
# 第三档（m）：消费站在对面深处、怎么投都落不到它旁边时，落在对面能捡的地方
# 就投，P1 捡起走过去。Balloon_2_2 两口煮锅在右篮深处，左地板射程最远落到
# 右篮入口 x≈14.4，最近落点离煮锅 (2)/(1) 7.15/8.74m，前两档全判「投不了」，
# 意面一份都没投（trace-20261031-152300，ThrowBatch(Pasta) 全局 infeasible）。
# 前两档找得到就不用它，炸锅 / Rapids 的方案不变。
SPLIT_MAX_LAND_FAR = 9.0

CONSUMER_BOARD = "board"
CONSUMER_COOKER = "cooker"

MODE_SPLIT = "split"
MODE_FAR = "far"


def stages(book, product):
    # type: (Any, str) -> List[Tuple[str, str, str]]
    """product 的加工链：[(形态, 消费站类型, 生料)]，按上游到下游排列。"""
    out = []  # type: List[Tuple[str, str, str]]
    if book.needs_cooking(product):
        # 整份进炉的菜（披萨）：料放在载体上、载体整份进炉，没有「生料投进灶」
        # 这一站。装了料的饼游戏不让扔（ServerPreparationContainer.AllowThrowing
        # = 内容为空），烤箱是普通挂载站、只接停稳的物品（ServerAttachStation.
        # CanHandleCatch 要 !IsFlying）——投不进去。只留切这一站。
        carrier = book.carrier_base(product) is not None
        for raw in book.raws_of(product):
            if book.needs_chopping(raw):
                out.append((raw, CONSUMER_BOARD, raw))
            if not carrier:
                out.append((book.cook_input_form(raw), CONSUMER_COOKER, raw))
    elif book.needs_chopping(product):
        out.append((product, CONSUMER_BOARD, product))
    return out


def _lower(name):
    # type: (Any) -> str
    return str(name or "").lower()


def demand(world, book, product, raw):
    # type: (Any, Any, str, str) -> int
    """可见订单里 product 要用几份 raw（多料同锅按份数计）。"""
    per = 1
    if book.needs_cooking(product):
        per = max(1, [_lower(r) for r in book.raws_of(product)].count(_lower(raw)))
    n = 0
    for o in getattr(world, "orders", None) or ():
        n += sum(1 for r in book.requirements(o) if _lower(r) == _lower(product))
    return n * per


def _items_named(world, name):
    # type: (Any, str) -> List[Dict]
    want = _lower(name)
    out = [it for it in getattr(world, "items", None) or ()
           if _lower(it.get("n")) == want]
    for ch in getattr(world, "chefs", None) or ():
        if _lower(ch.get("held")) == want:
            out.append(dict(n=ch.get("held"), x=ch.get("x"), z=ch.get("z"),
                            held_by=ch.get("name")))
    return out


def downstream(world, book, product, form, consumer):
    # type: (Any, Any, str, str, str) -> int
    """已越过 form 这一站的份数。"""
    cooked = CS.cooked_supply(world, book, product) if book.needs_cooking(product) else 0
    if consumer == CONSUMER_COOKER:
        return cooked
    # 切：切好的（不论在哪）+ 已在锅里/盘里的
    chopped = book.chop_target(form)
    n = len(_items_named(world, chopped)) if chopped else 0
    # 整份进炉的菜：已放到饼上的配料也越过了切这一站
    if book.carrier_base(product) is not None:
        from ai.query import carriers as CR
        want = book.want_tokens(form)
        n += sum(1 for c in CR.prep_carriers(world, book)
                 if c["cooker"] is None and want & set(c["contents"]))
    if not book.needs_cooking(product):
        n += sum(1 for p in getattr(world, "plates", None) or ()
                 if book.plate_has(p, product))
    return n + cooked


def _point(world, entry):
    # type: (Any, Dict) -> Optional[Tuple[float, float]]
    if entry.get("x") is None or entry.get("z") is None:
        return None
    return (float(entry["x"]), float(entry["z"]))


def near_dest(world, entry, dest, mode, bb=None):
    # type: (Any, Dict, Dict, str, Any) -> bool
    """entry（料）已在消费站这一侧了吗。飞行中的料一律算已投出。"""
    if entry.get("flying") is True:
        return True
    holder = entry.get("held_by")
    if mode == MODE_SPLIT:
        if holder:
            return holder != P.P2_NAME
        return (not RG.reachable_by(world, entry, P.P2_NAME, bb)
                and RG.reachable_by(world, entry, P.P1_NAME, bb))
    if holder:
        return False
    a, b = _point(world, entry), _point(world, dest)
    if a is None or b is None:
        return False
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= NEAR_DEST_RADIUS


def at_dest(world, form, dest, mode, bb=None):
    # type: (Any, str, Dict, str, Any) -> int
    return sum(1 for it in _items_named(world, form)
               if near_dest(world, it, dest, mode, bb))


def gap(world, book, product, form, consumer, raw, dest, mode, bb=None):
    # type: (Any, Any, str, str, str, str, Dict, str, Any) -> int
    """还要往 dest 投几份 form（下界 0）。"""
    need = demand(world, book, product, raw)
    if need <= 0:
        return 0
    # 已就位按「这一站的任一消费站附近」计、每份只算一次：只看本站的话，
    # 投满 1 号灶旁边后 2、3 号灶仍各报满缺口，同一批料会被重复投三遍。
    dests = dest_candidates(world, book, consumer, product) or [dest]
    placed = sum(1 for it in _items_named(world, form)
                 if any(near_dest(world, it, d, mode, bb) for d in dests))
    have = downstream(world, book, product, form, consumer) + placed
    return max(0, need + THROW_EXTRA_UNITS - have)


def sources(world, form, raw, dest, mode, bb=None, dests=None):
    # type: (Any, str, str, Dict, str, Any, Optional[List[Dict]]) -> List[Dict]
    """P2 这边可取的 form：手上 / 可达散落（不在消费站附近）/ 可达料箱。

    料箱只给生料形态（form == raw）。手上有就只返回手上那一份。
    """
    chef = P.chef_by_name(world.chefs, P.P2_NAME)
    if chef is not None and _lower(chef.get("held")) == _lower(form):
        # 手上这份此刻扔不出去（插件按游戏 CanHandleThrow 报）就不算来源
        return [] if chef.get("held_throwable") is False else [dict(kind="held")]
    out = []  # type: List[Dict]
    for it in getattr(world, "items", None) or ():
        if _lower(it.get("n")) != _lower(form) or it.get("flying") is True:
            continue
        if it.get("throwable") is False:
            continue   # 扔不出去的（装了料的饼）拿起来也投不了
        if any(near_dest(world, it, d, mode, bb) for d in (dests or [dest])):
            continue   # 已投到（这一站任一消费站旁）的料不再捡起来重投
        if RG.reachable_by(world, it, P.P2_NAME, bb):
            out.append(dict(kind="loose", entry=it))
    if _lower(form) == _lower(raw):
        crate = S.find_crate(world.stations, raw)
        if crate is not None and RG.reachable_by(world, crate, P.P2_NAME, bb):
            out.append(dict(kind="crate", entry=crate))
    return out


def mode_for(world, dest, srcs, bb=None):
    # type: (Any, Dict, List[Dict], Any) -> Optional[str]
    """这一投属于哪类：split（对面才够得着消费站）/ far（同侧但远）/ None。"""
    if not srcs:
        return None
    if not RG.reachable_by(world, dest, P.P2_NAME, bb):
        return MODE_SPLIT if RG.reachable_by(world, dest, P.P1_NAME, bb) else None
    if not ENABLE_FAR_THROW:
        return None
    if RG.field_of(world, bb, P.P2_NAME) is None:
        return None
    b = _point(world, dest)
    if b is None:
        return None
    # 按「料从哪来」判远近，手上那份不算：P2 从料箱拿起意面走向出手位，
    # 人一走近灶，按人的位置判就不再是远投，ThrowBatch 没投就收尾，料留在
    # 手上又没人接（trace-20261029-130632 t=112/120）。手上那份是不是正在
    # 投的那一批，由 ThrowBatch._state 按承诺判。
    for s in srcs:
        if s["kind"] == "held":
            continue
        a = _point(world, s["entry"] or {})
        if a is not None and math.hypot(a[0] - b[0], a[1] - b[1]) >= THROW_MIN_DIST:
            return MODE_FAR
    return None


def _station_cells(world):
    # type: (Any) -> set
    cells = set()
    for st in world.stations:
        if st.get("t") in ("wall", "floor") or st.get("x") is None:
            continue
        cells.add(world.to_grid(st["x"], st["z"]))
    return cells


def _landing_test(world, target, mode, bb=None, floor_y=None):
    # type: (Any, Tuple[float, float], str, Any, Optional[float]) -> Optional[Any]
    """落点判定；floor_y 是出手点脚下高度（缺省 P2 此刻脚下）。"""
    f2 = RG.field_of(world, bb, P.P2_NAME)
    if not f2:
        return None
    if floor_y is None:
        floor_y = _p2_floor_y(world)
    floor_ok = TH.floor_reachable(getattr(world, "terrain_y", None), floor_y)
    if mode == MODE_SPLIT:
        f1 = RG.field_of(world, bb, P.P1_NAME) or {}
        return TH.region_test(world.to_grid, set(f1), set(f2), _station_cells(world),
                              floor_ok=floor_ok)
    near = {c for c in f2
            if math.hypot(*(_d(world.to_world(*c), target))) <= NEAR_DEST_RADIUS}
    return TH.region_test(world.to_grid, near, None, _station_cells(world),
                          floor_ok=floor_ok)


def _p2_floor_y(world):
    # type: (Any) -> float
    """P2 脚下高度（插件 0.23.5+ 报 chefs[].y；缺省 0）。"""
    chef = P.chef_by_name(getattr(world, "chefs", None) or (), P.P2_NAME) or {}
    y = chef.get("y")
    return float(y) if isinstance(y, (int, float)) else 0.0


def _cell_floor_y(world, cell):
    # type: (Any, Tuple[int, int]) -> float
    """出手格脚下高度：格心处地面盒顶；查不到（旧帧 / 台阶盒缝）用 P2 此刻脚下。"""
    wx, wz = world.to_world(*cell)
    top = TH.ground_top(getattr(world, "terrain_y", None), wx, wz)
    return _p2_floor_y(world) if top is None else top


def walls_of(world, floor_y=None):
    # type: (Any, Optional[float]) -> List[Tuple[float, ...]]
    """当前帧挡飞行料的障碍（每帧按出手高度缓存）：高墙 + 比出手点脚下高的
    地面盒（楼梯台阶、高台地板）。移动篮子的墙随帧更新。

    floor_y 缺省为 P2 此刻脚下（插件 0.23.5+ 报 chefs[].y）。规划别的出手格时
    按那格的地面高度给——高度不同，挡不挡也不同。旧帧没有高度时返回空列表。
    """
    if floor_y is None:
        floor_y = _p2_floor_y(world)
    floor_y = round(floor_y, 2)

    def compute():
        terrain_y = getattr(world, "terrain_y", None)
        return TH.throw_walls(terrain_y, floor_y) + TH.raised_ground(terrain_y, floor_y)
    return _cached(world, ("walls", floor_y), compute)


def _cached(world, key, fn):
    cache = getattr(world, "_throw_plans", None)
    if cache is None:
        cache = {}
        try:
            world._throw_plans = cache
        except AttributeError:
            pass
    if key not in cache:
        cache[key] = fn()
    return cache[key]


def _max_lands(mode):
    # type: (str) -> Tuple[float, ...]
    """出手方案依次尝试的落点容忍距离：分割地图落在对面地上就算送到（P1
    捡），放宽；连通地图要落在消费站旁，不然等于没省路。"""
    if mode == MODE_SPLIT:
        return (SPLIT_MAX_LAND, SPLIT_MAX_LAND_RELAXED, SPLIT_MAX_LAND_FAR)
    return (TH.MAX_LAND_DIST,)


def launch_plan(world, dest, mode, bb=None):
    # type: (Any, Dict, str, Any) -> Optional[Dict]
    """当前帧的出手方案（TH.plan_launch 的结果），每帧按 (dest, mode) 缓存。
    结果多带 max_land（用的是哪一档容忍距离）。"""
    def compute():
        target = _point(world, dest)
        f2 = RG.field_of(world, bb, P.P2_NAME)
        if target is None or not f2:
            return None
        # 出手格按脚下高度分组：障碍与落点能否够着都相对出手点算（一楼格与
        # 楼上格看到的「高出脚下的地面」不同）
        by_floor = {}  # type: Dict[float, List[Tuple[int, int]]]
        for c in f2:
            by_floor.setdefault(round(_cell_floor_y(world, c), 2), []).append(c)
        tests = {}
        for max_land in _max_lands(mode):
            lo = TH.THROW_RANGE - 2 * TH.THROW_RANGE_TOL - max_land
            hi = TH.THROW_RANGE + max_land
            best = None
            for fy, group in by_floor.items():
                cells = [c for c in group
                         if lo <= math.hypot(*_d(world.to_world(*c), target)) <= hi]
                if not cells:
                    continue
                if fy not in tests:
                    tests[fy] = _landing_test(world, target, mode, bb, floor_y=fy)
                if tests[fy] is None:
                    continue
                plan = TH.plan_launch(cells, f2.get, world.to_world, target, tests[fy],
                                      max_land, walls=walls_of(world, fy))
                if plan is not None and (best is None or plan["score"] < best["score"]):
                    best = plan
            if best is not None:
                return dict(best, max_land=max_land)
        return None
    return _cached(world, ("plan", str(dest.get("n")), str(dest.get("grid")), mode), compute)


def aim_here(world, dest, mode, bb=None):
    # type: (Any, Dict, str, Any) -> Optional[Dict]
    """P2 **此刻的真实位置**往哪儿瞄能安全落到对面/消费站旁：TH.aim_ok 的
    结果（含 aim / aim_far）；投不到返回 None。

    站在漂流木筏上，出手格的世界坐标会被木筏带走：走向固定格心的 move 命令
    追不上，P2 拿着料原地耗到 move 超时（trace-20261029-122831 t=351~798，
    含暂停约 12s）。所以出手前先看当下站的位置能不能投，能就直接投。
    有墙挡时瞄准方向可能偏离消费站（从门洞斜穿，见 TH.best_aim）。
    """
    def compute():
        chef = P.chef_by_name(world.chefs, P.P2_NAME)
        target = _point(world, dest)
        a = _point(world, chef or {})
        if a is None or target is None:
            return None
        ok = _landing_test(world, target, mode, bb)
        if ok is None:
            return None
        walls = walls_of(world)
        for max_land in _max_lands(mode):
            got = TH.best_aim(a, target, ok, max_land, walls)
            if got is not None:
                return got
        return None
    return _cached(world, ("here", str(dest.get("n")), str(dest.get("grid")), mode), compute)


def aim_still_ok(world, dest, mode, toward, bb=None):
    # type: (Any, Dict, str, Tuple[float, float], Any) -> Optional[Dict]
    """按 P2 此刻位置复核一个已锁定的瞄准方向（朝 toward）还投不投得过去。"""
    chef = P.chef_by_name(world.chefs, P.P2_NAME)
    target = _point(world, dest)
    a = _point(world, chef or {})
    if a is None or target is None:
        return None
    ok = _landing_test(world, target, mode, bb)
    if ok is None:
        return None
    return TH.aim_ok(a, toward, target, ok, _max_lands(mode)[-1], walls_of(world))


def throw_ok_here(world, dest, mode, bb=None):
    # type: (Any, Dict, str, Any) -> bool
    """P2 此刻的位置能投（见 aim_here）。"""
    return aim_here(world, dest, mode, bb) is not None


def _d(a, b):
    # type: (Tuple[float, float], Tuple[float, float]) -> Tuple[float, float]
    return (a[0] - b[0], a[1] - b[1])


def dest_candidates(world, book, consumer, product):
    # type: (Any, Any, str, str) -> List[Dict]
    """能接 product 这一站的消费站：砧板全部；灶按炊具匹配。"""
    if consumer == CONSUMER_BOARD:
        return [st for st in world.stations if st.get("t") == "chopping"]
    out = []
    want = (book.cookware_of(product) or "").lower()
    for c in world.cookers:
        ck = P.cooker_cookware(c)
        if want and ck and not P.cookware_matches(ck, want):
            continue
        st = next((s for s in world.stations
                   if s.get("t") == "cooking" and str(s.get("n")) == str(c.get("n"))), None)
        if st is not None:
            out.append(st)
    return out


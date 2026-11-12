"""跨区供需：对面区缺什么、我这边有没有富余，决定要不要往通道上送。

地图被分成互不相通的区（P2 一块、P1 一块），东西只能经通道过去：能投的生料走
投掷（ThrowBatch 自己算缺口，见 throw_supply），不能投的放到通道台面上
（ai/space/channels.py：渡运篮子 / 两边都够得着的静态共享台面，SendAcross
task）。本模块回答通道无关的问题：

    send_deficit(world, kind) -> 这一种东西现在该往对面送几份（0 = 不送）

kind：
  - KIND_CLEAN_PLATE：对面缺干净盘（send_deficit / far_plate_need）。Balloon_2_2
    （游戏内 2-3）灶全在右边，装盘只能在右边做；脏盘回到左边洗，洗好的干净盘
    只能经篮子送回右边（trace-20261031-204801 t≈132~172）。Wizard_3_1
    （游戏内 3-1）开局盘子全在 P2 这边、烤箱全在 P1 那边，熟披萨只能在 P1
    那边装盘（trace-20261101-002050 t≈7~44s 三块盘没动过）。
  - KIND_CARRIER：料齐 / 补不了料的生载体卡在 P2 这边（stuck_carriers）。
  - KIND_DIRTY_PLATE：洗碗池只在对面，P2 这侧有脏盘（dirty_plate_sources）。
  - KIND_PLATED_DISH：出餐口只在对面，P2 这侧有料齐的成品盘
    （plated_dish_sources）。

本模块只回答“该不该送、送什么”；取放/完成/落点策略在
ai/revenue/handover.py 的规格表里。

不读 Blackboard、不写 World；可达区用每帧缓存的可达场（regions.field_of）。
"""
from typing import Dict, List, Optional, Set, Tuple

from ai.query import predicates as P
from ai.space import channels as CH
from ai.space import reachability

KIND_CLEAN_PLATE = "clean_plate"
# 可烹饪载体（披萨饼）：我这边补不了料、对应的灶又只在对面时送过去
#（trace-20261101-130851 t≈32s：料齐的饼在 P2 台面 (21) 上，烤箱在 P1 那边，
# 没有任何 task 能把它交出去，P2 原地不动到局末）。
KIND_CARRIER = "carrier"
# 脏盘：洗碗池只在对面、P2 这侧有脏盘时，送到共享台面让 P1 洗。
KIND_DIRTY_PLATE = "dirty_plate"
# 成品盘：出餐口只在对面、P2 这侧有料齐的盘时，送到共享台面让 P1 送。
KIND_PLATED_DISH = "plated_dish"
KINDS = (KIND_CLEAN_PLATE, KIND_CARRIER, KIND_DIRTY_PLATE, KIND_PLATED_DISH)

# 对面最多备几块空盘（含在途）：P1 一般同时装 1~2 单，再多就是把盘从我这边
# 搬去对面闲着，回头我这边要用（洗好的盘也在这边）还得送回来。
FAR_PLATE_TARGET = 2


def _reach(world, field, entry, name):
    ctx = dict(w=world, chef=P.chef_by_name(world.chefs, name), field=field)
    return reachability.can_interact(ctx, entry)


def _side_of(world, near, far, entry, bb=None):
    # type: (object, Dict, Dict, Dict, object) -> Optional[str]
    """entry 在哪一区：'transit'（在通道台面上）/ 'near'（P2 够得着）/
    'far'（只有 P1 够得着）/ None（谁都够不着）。"""
    if CH.slot_of(world, entry, bb) is not None:
        return "transit"
    if _reach(world, near, entry, P.P2_NAME):
        return "near"
    if _reach(world, far, entry, P.P1_NAME):
        return "far"
    return None


def _usable_clean(p):
    # type: (Dict) -> bool
    """能拿来装盘的干净空盘：没装东西、不压在盘堆中间。"""
    return P.plate_count(p) == 0 and not P.plate_buried_in_stack(p)


def _cookers_side(world, near, far, bb=None):
    # type: (object, Dict, Dict, object) -> Set[str]
    return {s for s in (_side_of(world, near, far, c, bb) for c in world.cookers) if s}


def _order_needs_cooking(book, order):
    # type: (object, Dict) -> bool
    """订单要过灶：成分里有熟料，或整道菜本身要烤（披萨整份进炉）。"""
    if book.needs_cooking(order.get("dish")):
        return True
    return any(book.needs_cooking(ing) for ing in book.requirements(order))


def _far_assembly_orders(world, near, far, book, bb=None):
    # type: (object, Dict, Dict, object, object) -> int
    """要在对面装盘的订单数：订单要过灶，而灶只在对面（我这边一口都够不着）。

    TODO：按灶具类型细分（我这边有煮锅、对面只有煎锅之类）；目前的关灶全在一边。
    """
    if _cookers_side(world, near, far, bb) != {"far"}:
        return 0
    return sum(1 for o in world.orders if _order_needs_cooking(book, o))


def far_plate_need(world, book, bb=None):
    # type: (object, object, object) -> Dict[str, int]
    """干净盘的跨区账：need（对面该有几块空盘）/ far（对面现有 + 通道上在途）/
    near_spare（我这边能送的）。拓扑未知 / 不分割 / 无通道时全 0。"""
    zero = {"need": 0, "far": 0, "near_spare": 0}
    if not CH.is_split(world, bb) or not CH.channel_keys(world, bb):
        return zero
    near, far = CH._fields(world, bb)
    orders = _far_assembly_orders(world, near, far, book, bb)
    far_clean = far_busy = near_clean = 0
    for p in P.all_plates(world):
        if p.get("held_by") == P.P1_NAME:
            side = "far"
        elif p.get("held_by") == P.P2_NAME:
            side = "near"
        else:
            side = _side_of(world, near, far, p, bb)
        if side in ("far", "transit"):
            if _usable_clean(p):
                far_clean += 1
            elif P.plate_count(p) > 0 and side == "far":
                far_busy += 1          # 对面正在装的盘：它那单不用新盘
        elif side == "near" and _usable_clean(p):
            near_clean += 1
    need = min(FAR_PLATE_TARGET, max(0, orders - far_busy))
    return {"need": need, "far": far_clean, "near_spare": near_clean}


def send_deficit(world, kind, book, bb=None):
    # type: (object, str, object, object) -> int
    """kind 现在该往对面送几份：min(对面缺口, 我这边富余)。"""
    if kind != KIND_CLEAN_PLATE:
        return 0
    acc = far_plate_need(world, book, bb)
    return max(0, min(acc["need"] - acc["far"], acc["near_spare"]))


def free_slots(world, key=None, bb=None):
    # type: (object, Optional[str], object) -> List[Dict]
    """通道台面上空着的格（没盘、没料、没锅）；key=None 时全部通道。"""
    return CH.free_slots(world, key, bb)


def has_send_capacity(world, key=None, bb=None):
    # type: (object, Optional[str], object) -> bool
    """key 这条通道（None = 任一通道）还能再放一份。"""
    keys = [key] if key is not None else CH.channel_keys(world, bb)
    return any(CH.has_capacity(world, k, bb) for k in keys)


def _dish_for_carrier(world, book, carrier):
    # type: (object, object, Dict) -> Optional[str]
    """载体能做的那道订单菜（第一张对得上的单）。"""
    from ai.query import carriers as CR
    for o in world.orders:
        dish = o.get("dish")
        if CR.prep_fits(book, carrier, dish):
            return dish
    return None


def stuck_carriers(world, book, bb=None):
    # type: (object, object, object) -> List[Dict]
    """该往对面送的可烹饪载体（carriers.prep_carriers 的条目）：

      - 地图分割；载体没上灶，在 P2 手里，或 P2 够得着而 P1 够不着；
      - 站型对得上的灶我这边一口都够不着、对面够得着；
      - 饼上还缺的料，我这边此刻一样都拿不到（能补的先补完再送）。
    """
    from ai.query import carriers as CR
    if not CH.is_split(world, bb):
        return []
    near, far = CH._fields(world, bb)
    ctx = dict(reachability.context(world, bb, P.P2_NAME), book=book)
    out = []  # type: List[Dict]
    for c in CR.prep_carriers(world, book):
        if c["cooker"] is not None or not CR.is_raw_carrier(c):
            continue                       # 已上灶 / 已烤熟出炉：不再是待进炉的饼
        holder = c["held_by"]
        if holder and not P.station_name_matches(str(holder), P.P2_NAME):
            continue
        if not holder and (not _reach(world, near, c["item"], P.P2_NAME)
                           or _reach(world, far, c["item"], P.P1_NAME)):
            continue
        dish = _dish_for_carrier(world, book, c)
        if dish is None:
            continue
        station = book.carrier_base(dish)[2]
        cookers = [k for k in world.cookers
                   if str(k.get("station_type") or "").lower() == station.lower()]
        if not cookers or _cookers_side_of(world, near, far, cookers, bb) != {"far"}:
            continue
        if any(_can_supply(ctx, book, m) for m in CR.prep_missing(book, c, dish)):
            continue
        out.append(c)
    return out


def _is_dirty_plate(entry):
    # type: (Dict) -> bool
    name = str(entry.get("n") or "").lower()
    return "dirty" in name and "plate" in name


def _station_sides(world, near, far, station_type, bb=None):
    # type: (object, Dict, Dict, str, object) -> Tuple[List[Dict], bool, bool]
    """某类站点 + (P2 能否交互, P1 能否交互)。"""
    out = []
    p2 = p1 = False
    for st in getattr(world, "stations", ()) or ():
        if not isinstance(st, dict) or st.get("t") != station_type:
            continue
        out.append(st)
        p2 = p2 or _reach(world, near, st, P.P2_NAME)
        p1 = p1 or _reach(world, far, st, P.P1_NAME)
    return out, p2, p1


def dirty_plate_sources(world, bb=None):
    # type: (object, object) -> List[Dict]
    """P2 这侧有脏盘、洗碗池只在对面时，该送过去的脏盘条目。

    只要 P2 够得着任何一个洗碗池，就自己洗，不占中转台；对面也没有洗碗池
    时同样不送。脏盘已经在通道上（在途）不算来源。
    """
    if not CH.is_split(world, bb) or not CH.channel_keys(world, bb):
        return []
    near, far = CH._fields(world, bb)
    sinks, p2_can, p1_can = _station_sides(world, near, far, "washing", bb)
    if not sinks or p2_can or not p1_can:
        return []
    out = []  # type: List[Dict]
    for it in (list(getattr(world, "items", ()) or ())
               + list(getattr(world, "held_items", ()) or ())):
        if not isinstance(it, dict) or not _is_dirty_plate(it):
            continue
        if it.get("held_by") == P.P1_NAME:
            continue
        if _side_of(world, near, far, it, bb) == "near":
            out.append(it)
    return out


def plated_dish_sources(world, book, bb=None):
    # type: (object, object, object) -> List[Tuple[Dict, Dict]]
    """P2 这侧有料齐的盘、出餐口只在对面时，该送过去的 (盘, 订单)。

    只要 P2 够得着任何一个出餐口就自己送；盘已在通道上（在途）不算来源。
    """
    if not CH.is_split(world, bb) or not CH.channel_keys(world, bb):
        return []
    near, far = CH._fields(world, bb)
    hatches, p2_can, p1_can = _station_sides(world, near, far, "plate_station", bb)
    if not hatches or p2_can or not p1_can:
        return []
    plate_match = getattr(world, "_plate_match", None) or {}
    out = []  # type: List[Tuple[Dict, Dict]]
    for p in P.all_plates(world):
        if p.get("held_by") == P.P1_NAME:
            continue
        if P.plate_count(p) <= 0:
            continue
        if CH.slot_of(world, p, bb) is not None:
            continue
        if _side_of(world, near, far, p, bb) != "near":
            continue
        # 盘已经被 1:1 配给某张单（例如只放了薯条的 All 单）时，不要再把它
        # 当另一张简单单的成品送走：SendAcross 和 TakeCookedToPlate 会同时
        # 抢同一块盘，拿起又放下（trace-20261110-231906 t=512~652）。盘还缺
        # 料就留给绑定单继续装；已料齐才按绑定单送。
        bound = plate_match.get(str(p.get("n")))
        if bound is not None:
            order = next((o for o in world.orders
                          if str(o.get("id")) == str(bound)), None)
            if order is None:
                continue
            if book.matches(order, p):
                out.append((p, order))
            continue
        for order in world.orders:
            if book.matches(order, p):
                out.append((p, order))
                break
    # 半成品盘：缺的料里有「只在 P1 侧」的（比如米饭）→ 也送过去补。
    out.extend(partial_plate_sources(world, book, bb))
    return out


def _ingredient_far_only(world, book, near, far, ing, bb=None):
    # type: (object, object, Dict, Dict, str, object) -> bool
    """ing（订单顶层成分、最终形态）是否只在 P1 那侧能补——P2 这侧拿不到也做不出。

    熟料（如 BoiledSushiRice）额外查「有没有 P2 够得着的匹配灶」：灶只在 P1 侧
    且熟的没现成散落在 P2 侧 → P2 煮不了 → 只在对面。切料/生料复用 _can_supply。
    """
    from ai.tasks.base import ingredient_available
    ctx = dict(reachability.context(world, bb, P.P2_NAME), book=book)
    if book.needs_cooking(ing):
        if ingredient_available(ctx, ing):
            return False               # 熟的已经在 P2 侧
        want = (book.cookware_of(ing) or "").lower()
        cookers = []
        for c in world.cookers:
            ck = P.cooker_cookware(c)
            if want and ck and not P.cookware_matches(ck, want):
                continue               # 灶明确不配这种锅，跳过
            cookers.append(c)          # 匹配，或灶上没报炊具（未知，当作兼容）
        return bool(cookers) and _cookers_side_of(world, near, far, cookers, bb) == {"far"}
    return not _can_supply(ctx, book, ing)


def partial_plate_sources(world, book, bb=None):
    # type: (object, object, object) -> List[Tuple[Dict, Dict]]
    """P2 这侧有半成品盘、缺的料里有一种「只在 P1 侧」→ 送过去补（仿 stuck_carriers）。

    寿司是两边协作组装：P2 放海苔/鱼/黄瓜、P1 放米饭。盘只差米饭时米饭在 P1
    的灶，P2 补不了，就该把半成品盘送上带子交给 P1（trace-20261112-222007 末段
    盘 -20580 黄瓜+鱼缺米饭，一直送不出去）。料齐的盘不在这里处理（走
    plated_dish 的 book.matches 分支）。
    """
    if not CH.is_split(world, bb) or not CH.channel_keys(world, bb):
        return []
    near, far = CH._fields(world, bb)
    hatches, p2_can, p1_can = _station_sides(world, near, far, "plate_station", bb)
    if not hatches or p2_can or not p1_can:
        return []
    out = []  # type: List[Tuple[Dict, Dict]]
    for p in P.all_plates(world):
        if p.get("held_by") == P.P1_NAME or P.plate_count(p) <= 0:
            continue
        if CH.slot_of(world, p, bb) is not None:
            continue
        if _side_of(world, near, far, p, bb) != "near":
            continue
        if any(book.matches(o, p) for o in world.orders):
            continue                      # 料齐的走 plated_dish 成品分支
        tokens = book.plate_tokens(p)
        if not tokens:
            continue
        for order in world.orders:
            if not tokens <= book.order_want_tokens(order):
                continue                  # 盘里有订单外的料，不是这张单的前缀
            missing = book.missing(order, p)
            if not missing:
                continue
            if any(_ingredient_far_only(world, book, near, far, m, bb)
                   for m in missing):
                out.append((p, order))
                break
    return out


def dirty_plate_to_receive(world, bb=None):
    # type: (object, object) -> bool
    """P1 那侧 / 传送带上有脏盘、洗碗池只在 P2 这侧——P2 该把方向设成朝自己收。

    4-5 的镜像场景（dirty_plate_sources 的反向）：洗碗池在 P2 这侧、P1 洗不了，
    P1 把脏盘放上带子送过来，P2 要先按自己的开关把方向翻到朝 P2 才能接到。
    """
    if not CH.is_split(world, bb):
        return False
    near, far = CH._fields(world, bb)
    sinks, p2_can, p1_can = _station_sides(world, near, far, "washing", bb)
    if not sinks or not p2_can or p1_can:
        return False
    for it in (list(getattr(world, "items", ()) or ())
               + list(getattr(world, "held_items", ()) or ())):
        if not isinstance(it, dict) or not _is_dirty_plate(it):
            continue
        if _side_of(world, near, far, it, bb) in ("far", "transit"):
            return True
    return False


def _can_supply(ctx, book, ing):
    # type: (Dict, object, str) -> bool
    """我这边此刻能不能拿出一份「放得上饼」的 ing：切好的现成可取，或生料
    可取且有我够得着的砧板（取来切）。"""
    from ai.tasks.base import ingredient_available
    if not book.needs_chopping(ing):
        return ingredient_available(ctx, ing)
    if ingredient_available(ctx, book.chop_target(ing)):
        return True
    if not ingredient_available(ctx, ing):
        return False
    return any(st.get("t") == "chopping" and reachability.can_interact(ctx, st)
               for st in ctx["w"].stations)


def _cookers_side_of(world, near, far, cookers, bb=None):
    # type: (object, Dict, Dict, List[Dict], object) -> Set[str]
    return {s for s in (_side_of(world, near, far, c, bb) for c in cookers) if s}

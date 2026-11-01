"""跨区供需：对面区缺什么、我这边有没有富余，决定要不要往通道上送。

地图被分成互不相通的区（P2 一块、P1 一块），东西只能经通道过去：能投的生料走
投掷（ThrowBatch 自己算缺口，见 throw_supply），不能投的放到通道台面上
（ai/space/channels.py：渡运篮子 / 两边都够得着的静态共享台面，SendAcross
task）。本模块回答通道无关的问题：

    send_deficit(world, kind) -> 这一种东西现在该往对面送几份（0 = 不送）

kind 目前只有 KIND_CLEAN_PLATE：
  - Balloon_2_2（游戏内 2-3）灶全在右边，装盘只能在右边做；脏盘回到左边洗，洗好
    的干净盘只能经篮子送回右边（trace-20261031-204801 t≈132~172）。
  - Wizard_3_1（游戏内 3-1）开局盘子全在 P2 这边、烤箱全在 P1 那边，熟披萨只能
    在 P1 那边装盘（trace-20261101-002050 t≈7~44s 三块盘没动过）。

不读 Blackboard、不写 World；可达区用每帧缓存的可达场（regions.field_of）。
"""
from typing import Dict, List, Optional, Set

from ai.query import predicates as P
from ai.space import channels as CH
from ai.space import reachability

KIND_CLEAN_PLATE = "clean_plate"
# 可烹饪载体（披萨饼）：我这边补不了料、对应的灶又只在对面时送过去
#（trace-20261101-130851 t≈32s：料齐的饼在 P2 台面 (21) 上，烤箱在 P1 那边，
# 没有任何 task 能把它交出去，P2 原地不动到局末）。
KIND_CARRIER = "carrier"
KINDS = (KIND_CLEAN_PLATE, KIND_CARRIER)

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

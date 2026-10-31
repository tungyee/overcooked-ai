"""跨区供需：对面区缺什么、我这边有没有富余，决定要不要往通道上送。

地图被分成互不相通的区（P2 一块、P1 一块），东西只能经通道过去：能投的生料走
投掷（ThrowBatch 自己算缺口，见 throw_supply），不能投的走渡运篮子
（ai/space/shuttle.py，SendAcross task）。本模块回答通道无关的问题：

    send_deficit(world, kind) -> 这一种东西现在该往对面送几份（0 = 不送）

kind 目前只有 KIND_CLEAN_PLATE：Balloon_2_2（游戏内 2-3）灶全在右边，装盘只能在
右边做；脏盘回到左边洗，洗好的干净盘只能经篮子送回右边（trace-20261031-204801
t≈132~172：右边干净盘为 0、左边积了 3 块，P1 没盘可装，40s 一单没出）。

不读 Blackboard、不写 World；可达区用每帧缓存的可达场（regions.field_of）。
"""
from typing import Dict, List, Optional, Set

from ai.query import predicates as P
from ai.space import reachability
from ai.space import shuttle

KIND_CLEAN_PLATE = "clean_plate"
KINDS = (KIND_CLEAN_PLATE,)

# 对面最多备几块空盘（含在途）：P1 一般同时装 1~2 单，再多就是把盘从我这边
# 搬去对面闲着，回头我这边要用（洗好的盘也在这边）还得送回来。
FAR_PLATE_TARGET = 2
# 渡运台面至少留几格空着给对面往这边送菜。
SHUTTLE_KEEP_FREE = 1


def _fields(world, bb=None):
    near = reachability.field_for(world, bb, P.P2_NAME)
    far = reachability.field_for(world, bb, P.P1_NAME)
    return near, far


def _reach(world, field, entry, name):
    ctx = dict(w=world, chef=P.chef_by_name(world.chefs, name), field=field)
    return reachability.can_interact(ctx, entry)


def _side_of(world, near, far, entry):
    # type: (object, Dict, Dict, Dict) -> Optional[str]
    """entry 在哪一区：'near'（P2 够得着）/ 'far'（只有 P1 够得着）/ None（谁都够不着）。"""
    if _reach(world, near, entry, P.P2_NAME):
        return "near"
    if _reach(world, far, entry, P.P1_NAME):
        return "far"
    return None


def _usable_clean(p):
    # type: (Dict) -> bool
    """能拿来装盘的干净空盘：没装东西、不压在盘堆中间。"""
    return P.plate_count(p) == 0 and not P.plate_buried_in_stack(p)


def _cookers_side(world, near, far):
    # type: (object, Dict, Dict) -> Set[str]
    return {s for s in (_side_of(world, near, far, c) for c in world.cookers) if s}


def _far_assembly_orders(world, near, far, book):
    # type: (object, Dict, Dict, object) -> int
    """要在对面装盘的订单数：订单要熟料，而灶只在对面（我这边一口都够不着）。

    TODO：按灶具类型细分（我这边有煮锅、对面只有煎锅之类）；本关灶全在一边。
    """
    if _cookers_side(world, near, far) != {"far"}:
        return 0
    n = 0
    for o in world.orders:
        if any(book.needs_cooking(ing) for ing in book.requirements(o)):
            n += 1
    return n


def far_plate_need(world, book, bb=None):
    # type: (object, object, object) -> Dict[str, int]
    """干净盘的跨区账：need（对面该有几块空盘）/ far（对面现有 + 篮子上在途）/
    near_spare（我这边能送的）。拓扑未知 / 无渡运时全 0。"""
    zero = {"need": 0, "far": 0, "near_spare": 0}
    if not shuttle.shuttle_grids(world):
        return zero
    near, far = _fields(world, bb)
    if not near or not far or set(near) & set(far):
        return zero
    orders = _far_assembly_orders(world, near, far, book)
    far_clean = far_busy = near_clean = 0
    for p in P.all_plates(world):
        if p.get("held_by") == P.P1_NAME:
            side = "far"
        elif p.get("held_by") == P.P2_NAME:
            side = "near"
        elif shuttle.station_of(world, p) is not None:
            side = "transit"
        else:
            side = _side_of(world, near, far, p)
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


def free_slots(world):
    # type: (object) -> List[Dict]
    """渡运台面上空着的格（没盘、没料、没锅）。"""
    from ai.query.placement import counter_key, counter_occupied
    return [st for st in shuttle.shuttle_stations(world)
            if not counter_occupied(world, counter_key(world, st))]


def has_send_capacity(world):
    # type: (object) -> bool
    return len(free_slots(world)) > SHUTTLE_KEEP_FREE

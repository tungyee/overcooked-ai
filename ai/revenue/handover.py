"""跨区送件规格表：SendAcross 的 kind → 来源 / 取放 / 完成 / 落点策略。

`SendAcross` 本身只做「拿起来 → 放到通道格」这一件通用事；每种东西的
“该不该送、从哪拿、怎么算送完、能不能放共享砧板”都放在本模块。加一种新
东西时只写一个 spec，任务树、planner 候选生成、命令 scope 不用再改。

四种 kind：
  - clean_plate：对面缺干净盘（revenue/cross_supply.far_plate_need）；
  - carrier：料齐的生载体（披萨饼），对应灶只在对面；
  - dirty_plate：洗碗池只在对面，P2 这侧的脏盘先送到共享台面；
  - plated_dish：出餐口只在对面，P2 这侧的成品盘先送到共享台面。

不 import ai.tasks（避免 tasks 包反向依赖）；只读 World / RecipeBook，
取放谓词全部转发给 ai.query.predicates 与 ai.space.channels。
"""
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ai.query import predicates as P
from ai.query import recipes as R
from ai.space import channels as CH
from ai.revenue import cross_supply as XS

# 切好的非烹饪料（沙拉的生菜/番茄）：出餐口在对面、P2 自己送不了时，
# 放到传送带上交给玩家装盘/上菜。烹饪料（土豆/鸡块）不走这里——那是 P2 自己炸。
KIND_INGREDIENT = "ingredient"


@dataclass(frozen=True)
class SendSource:
    """一个可送来源。source_id 进 task 身份；payload 是解析时顺手带上的东西。"""
    kind: str
    source_id: Any
    order_id: Any = None
    payload: Any = None


@dataclass(frozen=True)
class SendRequest:
    """planner 的一行送件候选：kind + 来源 + 通道。"""
    kind: str
    source: SendSource
    channel: str


@dataclass(frozen=True)
class SendSpec:
    kind: str
    label: str
    sources: Callable
    claims: Callable
    pickup: Callable
    held: Callable
    done: Callable
    resolve: Callable
    channels: Callable
    capacity: Callable
    allow_boards: bool = False


SEND_SPECS = {}  # type: Dict[str, SendSpec]


# ---------------------------------------------------------------- 通用谓词


def _plate_by_id(world, plate_id):
    # type: (Any, Any) -> Optional[Dict]
    for p in P.all_plates(world):
        if str(p.get("n")) == str(plate_id):
            return p
    return None


def _station_for_item(world, item):
    # type: (Any, Optional[Dict]) -> Optional[Dict]
    """散落物所在站点（on_station 形如 attach:<站名>），找不到返回 None。"""
    if not isinstance(item, dict):
        return None
    on = str(item.get("on_station") or "")
    if ":" not in on:
        return None
    name = on.split(":", 1)[1].strip()
    if not name:
        return None
    x, z = item.get("x"), item.get("z")
    named = [st for st in getattr(world, "stations", ()) or ()
             if str(st.get("n") or "") == name]
    if len(named) > 1 and x is not None and z is not None:
        named.sort(key=lambda st: (st.get("x") is None,
                                   (float(st.get("x") or 0) - x) ** 2
                                   + (float(st.get("z") or 0) - z) ** 2))
    return named[0] if named else None


def _plate_on_channel(world, plate, key, bb=None):
    # type: (Any, Optional[Dict], str, Any) -> bool
    if plate is None:
        return False
    st = CH.slot_of(world, plate, bb)
    return st is not None and CH.channel_of_slot(world, st, bb) == key


def _clean_held(ctx):
    # type: (Dict) -> bool
    """P2 手里端着干净空盘（实例 id 可能变，不认 id）。"""
    if not P.held_is_plate(ctx["chef"]):
        return False
    held = [p for p in P.all_plates(ctx["w"])
            if p.get("held_by") == P.P2_NAME]
    return bool(held) and all(P.plate_count(p) == 0 for p in held)


def _dirty_held(ctx):
    # type: (Dict) -> bool
    name = P.held_name(ctx["chef"]).lower()
    return "dirty" in name and "plate" in name


def _clean_on_channel(world, key, bb=None):
    # type: (Any, str, Any) -> bool
    for p in P.all_plates(world):
        if p.get("held_by") or P.plate_count(p) != 0:
            continue
        if _plate_on_channel(world, p, key, bb):
            return True
    return False


def _dirty_on_channel(world, bb=None):
    # type: (Any, Any) -> bool
    for it in (list(getattr(world, "items", ()) or ())
               + list(getattr(world, "held_items", ()) or ())):
        if not isinstance(it, dict) or it.get("held_by"):
            continue
        name = str(it.get("n") or "").lower()
        if "dirty" not in name or "plate" not in name:
            continue
        if CH.slot_of(world, it, bb) is not None:
            return True
    return False


def _carrier_stuck(world, book, bb, name):
    # type: (Any, Any, Any, Any) -> List[Dict]
    return [c for c in XS.stuck_carriers(world, book, bb)
            if P.station_name_matches(c["name"], name)]


# ---------------------------------------------------------------- clean_plate


def _clean_sources(world, book, bb):
    # type: (Any, Any, Any) -> List[SendSource]
    if XS.send_deficit(world, XS.KIND_CLEAN_PLATE, book, bb) <= 0:
        return []
    out = []  # type: List[SendSource]
    for p in P.all_plates(world):
        pid = p.get("n")
        if (not pid or P.plate_count(p) != 0 or P.plate_buried_in_stack(p)
                or p.get("held_by") == P.P1_NAME
                or CH.slot_of(world, p, bb) is not None):
            continue
        out.append(SendSource(XS.KIND_CLEAN_PLATE, pid, None, p))
    return out


def _clean_resolve(ctx, source_id, order_id):
    # type: (Dict, Any, Any) -> Optional[SendSource]
    w, book, bb = ctx["w"], ctx.get("book"), ctx.get("bb")
    if XS.send_deficit(w, XS.KIND_CLEAN_PLATE, book, bb) <= 0:
        return None
    p = _plate_by_id(w, source_id)
    if p is not None:
        if (P.plate_count(p) != 0 or P.plate_buried_in_stack(p)
                or p.get("held_by") == P.P1_NAME
                or CH.slot_of(w, p, bb) is not None):
            return None
        return SendSource(XS.KIND_CLEAN_PLATE, source_id, None, p)
    if _clean_held(ctx):
        return SendSource(XS.KIND_CLEAN_PLATE, source_id, None, None)
    return None


def _clean_pickup(ctx, source):
    # type: (Dict, SendSource) -> Optional[Dict]
    p = _plate_by_id(ctx["w"], source.source_id)
    if p is not None:
        return p
    for p in P.all_plates(ctx["w"]):
        if p.get("held_by") == P.P2_NAME and P.plate_count(p) == 0:
            return p
    return None


def _clean_done(ctx, source, key):
    # type: (Dict, SendSource, str) -> bool
    if not P.hands_free(ctx["chef"]):
        return False
    p = _plate_by_id(ctx["w"], source.source_id)
    if p is not None:
        return _plate_on_channel(ctx["w"], p, key, ctx.get("bb"))
    return _clean_on_channel(ctx["w"], key, ctx.get("bb"))


def _clean_capacity(ctx, key):
    # type: (Dict, str) -> bool
    return XS.has_send_capacity(ctx["w"], key, ctx.get("bb"))


SEND_SPECS[XS.KIND_CLEAN_PLATE] = SendSpec(
    kind=XS.KIND_CLEAN_PLATE,
    label="送干净盘",
    sources=_clean_sources,
    claims=lambda source_id, order_id: ((source_id, "send"),),
    pickup=_clean_pickup,
    held=lambda ctx, source: _clean_held(ctx),
    done=_clean_done,
    resolve=_clean_resolve,
    channels=lambda world, bb: CH.channel_keys(world, bb),
    capacity=_clean_capacity,
)


# ---------------------------------------------------------------- carrier


def _carrier_sources(world, book, bb):
    # type: (Any, Any, Any) -> List[SendSource]
    out = []  # type: List[SendSource]
    seen = set()
    for c in XS.stuck_carriers(world, book, bb):
        name = c["name"]
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(SendSource(XS.KIND_CARRIER, name, None, c))
    return out


def _carrier_resolve(ctx, source_id, order_id):
    # type: (Dict, Any, Any) -> Optional[SendSource]
    stuck = _carrier_stuck(ctx["w"], ctx.get("book"), ctx.get("bb"), source_id)
    return SendSource(XS.KIND_CARRIER, source_id, None, stuck[0]) if stuck else None


def _carrier_pickup(ctx, source):
    # type: (Dict, SendSource) -> Optional[Dict]
    c = source.payload
    if c is None:
        stuck = _carrier_stuck(ctx["w"], ctx.get("book"), ctx.get("bb"),
                               source.source_id)
        c = stuck[0] if stuck else None
    if c is None:
        return None
    item = c["item"]
    return _station_for_item(ctx["w"], item) or item


def _carrier_done(ctx, source, key):
    # type: (Dict, SendSource, str) -> bool
    if not P.hands_free(ctx["chef"]):
        return False
    return not _carrier_stuck(ctx["w"], ctx.get("book"), ctx.get("bb"),
                              source.source_id)


def _carrier_capacity(ctx, key):
    # type: (Dict, str) -> bool
    return bool(CH.free_carrier_slots(ctx["w"], ctx.get("bb")))


SEND_SPECS[XS.KIND_CARRIER] = SendSpec(
    kind=XS.KIND_CARRIER,
    label="送载体",
    sources=_carrier_sources,
    claims=lambda source_id, order_id: (),
    pickup=_carrier_pickup,
    held=lambda ctx, source: P.held_matches(ctx["chef"], source.source_id),
    done=_carrier_done,
    resolve=_carrier_resolve,
    channels=lambda world, bb: ([CH.STATIC] if CH.is_split(world, bb) else []),
    capacity=_carrier_capacity,
    allow_boards=True,
)


# ---------------------------------------------------------------- dirty_plate


def _dirty_sources(world, book, bb):
    # type: (Any, Any, Any) -> List[SendSource]
    return ([SendSource(XS.KIND_DIRTY_PLATE, XS.KIND_DIRTY_PLATE)]
            if XS.dirty_plate_sources(world, bb) else [])


def _dirty_resolve(ctx, source_id, order_id):
    # type: (Dict, Any, Any) -> Optional[SendSource]
    if not XS.dirty_plate_sources(ctx["w"], ctx.get("bb")):
        return None
    return SendSource(XS.KIND_DIRTY_PLATE, source_id)


def _dirty_pickup(ctx, source):
    # type: (Dict, SendSource) -> Optional[Dict]
    items = XS.dirty_plate_sources(ctx["w"], ctx.get("bb"))
    if not items:
        return None
    item = items[0]
    return _station_for_item(ctx["w"], item) or item


def _dirty_done(ctx, source, key):
    # type: (Dict, SendSource, str) -> bool
    if not P.hands_free(ctx["chef"]):
        return False
    if _dirty_on_channel(ctx["w"], ctx.get("bb")):
        return True
    return not XS.dirty_plate_sources(ctx["w"], ctx.get("bb"))


def _dirty_capacity(ctx, key):
    # type: (Dict, str) -> bool
    return XS.has_send_capacity(ctx["w"], key, ctx.get("bb"))


SEND_SPECS[XS.KIND_DIRTY_PLATE] = SendSpec(
    kind=XS.KIND_DIRTY_PLATE,
    label="送脏盘",
    sources=_dirty_sources,
    claims=lambda source_id, order_id: (),
    pickup=_dirty_pickup,
    held=lambda ctx, source: _dirty_held(ctx),
    done=_dirty_done,
    resolve=_dirty_resolve,
    channels=lambda world, bb: CH.channel_keys(world, bb),
    capacity=_dirty_capacity,
)


# ---------------------------------------------------------------- plated_dish


def _plated_sources(world, book, bb):
    # type: (Any, Any, Any) -> List[SendSource]
    return [SendSource(XS.KIND_PLATED_DISH, str(p.get("n")),
                       str(o.get("id")), p)
            for p, o in XS.plated_dish_sources(world, book, bb)]


def _plated_resolve(ctx, source_id, order_id):
    # type: (Dict, Any, Any) -> Optional[SendSource]
    for p, o in XS.plated_dish_sources(ctx["w"], ctx.get("book"),
                                       ctx.get("bb")):
        if str(p.get("n")) == str(source_id):
            return SendSource(XS.KIND_PLATED_DISH, str(p.get("n")),
                              str(o.get("id")), p)
    return None


def _plated_pickup(ctx, source):
    # type: (Dict, SendSource) -> Optional[Dict]
    return _plate_by_id(ctx["w"], source.source_id)


def _plated_held(ctx, source):
    # type: (Dict, SendSource) -> bool
    return P.holds_plate(ctx["chef"], _plate_by_id(ctx["w"], source.source_id))


def _plated_done(ctx, source, key):
    # type: (Dict, SendSource, str) -> bool
    if not P.hands_free(ctx["chef"]):
        return False
    return _plate_on_channel(ctx["w"], _plate_by_id(ctx["w"], source.source_id),
                             key, ctx.get("bb"))


def _plated_capacity(ctx, key):
    # type: (Dict, str) -> bool
    return XS.has_send_capacity(ctx["w"], key, ctx.get("bb"))


SEND_SPECS[XS.KIND_PLATED_DISH] = SendSpec(
    kind=XS.KIND_PLATED_DISH,
    label="送成品盘",
    sources=_plated_sources,
    claims=lambda source_id, order_id: ((source_id, "send"),),
    pickup=_plated_pickup,
    held=_plated_held,
    done=_plated_done,
    resolve=_plated_resolve,
    channels=lambda world, bb: CH.channel_keys(world, bb),
    capacity=_plated_capacity,
)


# ---------------------------------------------------------------- ingredient


def _p2_can_deliver(world, bb=None):
    # type: (Any, Any) -> bool
    """P2 这侧够得着出餐口：够得着就自己装盘上菜，不把切好的料送走。"""
    near, _far = CH._fields(world, bb)
    return any(CH._reach(world, near, st, P.P2_NAME)
               for st in getattr(world, "stations", ()) or ()
               if isinstance(st, dict) and st.get("t") == "plate_station")


def _chopped_form(book, raw):
    # type: (Any, str) -> Optional[str]
    if not raw or book.needs_cooking(raw) or not book.needs_chopping(raw):
        return None
    return book.chop_target(raw)


def _ingredient_available(world, form):
    # type: (Any, str) -> Optional[Dict]
    """场上可取的切好料：砧板上 / 台面 / 地上（不含盘里 / 手里）。"""
    loc = R.find_ingredient(world, form)
    if loc is None or loc.get("kind") in ("held", "plate"):
        return None
    return loc.get("st")


def _ingredient_on_channel(world, form, bb=None):
    # type: (Any, str, Any) -> bool
    for it in getattr(world, "items", ()) or ():
        if str(it.get("n") or "").lower() == str(form).lower() \
                and CH.slot_of(world, it, bb) is not None:
            return True
    return False


def _ingredient_sources(world, book, bb):
    # type: (Any, Any, Any) -> List[SendSource]
    if not CH.is_split(world, bb) or CH.CONVEYOR not in CH.channel_keys(world, bb):
        return []
    if _p2_can_deliver(world, bb):
        return []
    out = []  # type: List[SendSource]
    seen = set()
    for order in getattr(world, "orders", ()) or ():
        for raw in book.requirements(order):
            form = _chopped_form(book, raw)
            if not form or form.lower() in seen:
                continue
            if _ingredient_available(world, form) is None:
                continue
            if _ingredient_on_channel(world, form, bb):
                continue
            seen.add(form.lower())
            out.append(SendSource(KIND_INGREDIENT, form,
                                  str(order.get("id") or "")))
    return out


def _ingredient_resolve(ctx, source_id, order_id):
    # type: (Dict, Any, Any) -> Optional[SendSource]
    w, bb = ctx["w"], ctx.get("bb")
    if _p2_can_deliver(w, bb):
        return None
    if _ingredient_available(w, source_id) is None:
        return None
    return SendSource(KIND_INGREDIENT, source_id, order_id)


def _ingredient_pickup(ctx, source):
    # type: (Dict, SendSource) -> Optional[Dict]
    return _ingredient_available(ctx["w"], source.source_id)


def _ingredient_done(ctx, source, key):
    # type: (Dict, SendSource, str) -> bool
    if _ingredient_on_channel(ctx["w"], source.source_id, ctx.get("bb")):
        return True
    if P.held_matches(ctx["chef"], source.source_id):
        return False                       # 拿在手里还没放上去
    # 砧板/台面/地上还有 = 没送走；完全找不到（已被拿走/送到对面）= 完成。
    return _ingredient_available(ctx["w"], source.source_id) is None


SEND_SPECS[KIND_INGREDIENT] = SendSpec(
    kind=KIND_INGREDIENT,
    label="送切好的料",
    sources=_ingredient_sources,
    claims=lambda source_id, order_id: ((source_id, "send"),),
    pickup=_ingredient_pickup,
    held=lambda ctx, source: P.held_matches(ctx["chef"], source.source_id),
    done=_ingredient_done,
    resolve=_ingredient_resolve,
    channels=lambda world, bb: ([CH.CONVEYOR]
                                if CH.CONVEYOR in CH.channel_keys(world, bb)
                                else []),
    capacity=lambda ctx, key: CH.has_capacity(ctx["w"], key, ctx.get("bb")),
)


# ---------------------------------------------------------------- 对外入口


def spec_for(kind):
    # type: (str) -> Optional[SendSpec]
    return SEND_SPECS.get(kind)


def send_requests(world, book, bb=None):
    # type: (Any, Any, Any) -> List[SendRequest]
    """此刻所有该发起的送件。planner 只为这些行生成 SendAcross 候选。"""
    out = []  # type: List[SendRequest]
    seen = set()
    for spec in SEND_SPECS.values():
        for source in spec.sources(world, book, bb):
            for channel in spec.channels(world, bb):
                key = (spec.kind, source.source_id, source.order_id, channel)
                if key in seen:
                    continue
                seen.add(key)
                out.append(SendRequest(spec.kind, source, channel))
    return out

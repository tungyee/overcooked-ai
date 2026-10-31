# -*- coding: utf-8 -*-
"""已装料载体：本身带内容的非盘非锅物品。

有些菜的「底料」本身能装东西：卷饼里先放熟肉、汉堡胚里先夹生菜……这种
物品在 items[] 里名字仍是底料名（`Tortilla` / `Bun`），但 detail 非空。
它不再是普通的「一份底料」，而是**一份半成品**：放进盘时，底料 + 它装着
的全部料一起进盘（游戏把盘折叠成一个 CompositeAssembledNode，见
plate_tokens._parse_detail_item）。

不按菜名特判：载体 = 不在飞、非锅具、非盘、detail 解析出 token 的物品。
它的身份是 **(底料名, 内容签名)**：同名同内容的两张饼可以互换，拿哪张都对；
物品没有实例 id，位置在拾取后会变，都不适合当身份。内容签名 = detail 经
parse_plate_detail 得到的 token 集（与盘内容同一套词汇），排序后成元组，
可直接放进 task 身份。

按项目约定：谓词无副作用、只读 duck-typed 属性，不依赖 World 类。
"""
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from ai.query import predicates as P
from ai.query.plate_tokens import parse_plate_detail


def item_contents(item):
    # type: (Optional[Dict]) -> FrozenSet[str]
    """物品装着的料（不含底料本身）。空集 = 普通料（或不是载体）。"""
    if not isinstance(item, dict):
        return frozenset()
    return frozenset(parse_plate_detail(item.get("detail") or []))


def signature(contents):
    # type: (Any) -> Tuple[str, ...]
    """内容签名：排序元组，可哈希、可进 task 身份、repr 稳定。"""
    return tuple(sorted(contents or ()))


def held_item(world, chef_name):
    # type: (Any, str) -> Optional[Dict]
    """某厨师手里那件物品在 held_items 里的条目（插件给手持物打 held_by）。"""
    for it in getattr(world, "held_items", None) or ():
        if _same_name(it.get("held_by"), chef_name):
            return it
    return None


def held_contents(world, chef_name):
    # type: (Any, str) -> FrozenSet[str]
    """厨师手里那件装着的料。旧帧没有 held_items 时视为空（普通料）。"""
    return item_contents(held_item(world, chef_name))


def _same_name(a, b):
    # type: (Any, Any) -> bool
    return str(a or "").strip().lower() == str(b or "").strip().lower()


def _is_carrier(item, recipes=None):
    # type: (Optional[Dict], Optional[List[Dict]]) -> bool
    if not isinstance(item, dict) or item.get("flying") is True:
        return False
    if P.item_is_cookware(item, recipes):
        return False
    if P.PLATE_KEYWORD in str(item.get("n") or "").lower():
        return False
    return bool(item_contents(item))


def carriers(world, include_held=True):
    # type: (Any, bool) -> List[Dict]
    """场上的载体：[{"name", "base", "contents", "sig", "held_by", "item"}]。

    name = 物品名（取料用，与 find_ingredient 的 ing 同口径）；
    base = 底料进盘后的 token（物品名小写，与盘里 `ing:Tortilla` 同口径）；
    contents = 装着的料；held_by = 持有者名（散落为 None）。
    include_held 控制是否含手持的：供给计数要含（P1 手里那张也是在路上的
    一份熟料）；取料只认散落 + P2 自己手里的，由调用方再筛。
    """
    recipes = getattr(world, "recipes", None)
    pools = [getattr(world, "items", None) or ()]
    if include_held:
        pools.append(getattr(world, "held_items", None) or ())
    out = []  # type: List[Dict]
    for pool in pools:
        for it in pool:
            if not _is_carrier(it, recipes):
                continue
            contents = item_contents(it)
            name = str(it.get("n") or "")
            out.append({"name": name, "base": name.strip().lower(),
                        "contents": contents, "sig": signature(contents),
                        "held_by": it.get("held_by") or None, "item": it})
    return out


def carrier_fits(book, carrier, order, plate=None):
    # type: (Any, Dict, Dict, Optional[Dict]) -> bool
    """载体能否放进这一单（的这只盘）：底料 + 内容 ⊆ 订单需求，且与盘已有的
    不重复（同一份料放两次，盘就不再是该单的子集）。"""
    tokens = set(carrier["contents"]) | {carrier["base"]}
    if not tokens <= book.order_want_tokens(order):
        return False
    if plate is not None and tokens & book.plate_tokens(plate):
        return False
    return True


def match_carriers_to_orders(world, book, plate_match, orders=None):
    # type: (Any, Any, Optional[Dict[Any, Any]], Optional[List[Dict]]) -> Dict[Any, Dict]
    """载体↔订单 1:1 匹配（每拍重算）。返回 {order_id: carrier}。

    只配 P2 拿得到的：散落的 + P2 手里的（P1 手里的 P2 拿不到）。按订单列表
    顺序（屏幕顺序，combo 队首在前）贪心；同一单有多个可用载体时，P2 手里
    的优先，其次装料最多的。订单没盘也照配：载体覆盖的料对该单而言已在路上，
    不该再备一份。
    """
    pool = [c for c in carriers(world)
            if c["held_by"] is None or _same_name(c["held_by"], P.P2_NAME)]
    if not pool:
        return {}
    pid_of = {}  # type: Dict[Any, Any]
    for pid, oid in (plate_match or {}).items():
        pid_of[oid] = pid
    plates = {str(p.get("n")): p for p in P.all_plates(world)}
    used = set()  # type: Set[int]
    out = {}  # type: Dict[Any, Dict]
    for o in (orders if orders is not None else getattr(world, "orders", None) or ()):
        oid = o.get("id")
        if oid is None:
            continue
        pid = pid_of.get(oid)
        plate = plates.get(str(pid)) if pid is not None else None
        best, best_key = None, None
        for i, c in enumerate(pool):
            if i in used or not carrier_fits(book, c, o, plate):
                continue
            key = (0 if c["held_by"] else 1, -len(c["contents"]), i)
            if best_key is None or key < best_key:
                best, best_key = i, key
        if best is not None:
            used.add(best)
            out[oid] = pool[best]
    return out


def carrier_covers(book, carrier, ing):
    # type: (Any, Optional[Dict], Optional[str]) -> bool
    """载体装着的料里是否已有订单成分 ing（底料本身不算：底料还要被放进盘）。"""
    if not carrier or not ing:
        return False
    return bool(book.want_tokens(ing) & carrier["contents"])


def carrier_is_base_of(carrier, item, fetch_form):
    # type: (Optional[Dict], Optional[str], Optional[str]) -> bool
    """载体是不是订单成分 item 的底料（按取料形态比名字）。"""
    if not carrier or not item:
        return False
    return carrier["base"] == str(fetch_form or item).strip().lower()

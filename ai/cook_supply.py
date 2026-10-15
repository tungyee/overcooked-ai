# -*- coding: utf-8 -*-
"""熟料供需计数（第 1 组 1.2）。

**为什么需要这一层**：本局 3 个 miss 的根因不是权重而是结构。
`FetchToCooker` 只能由 `recipe_graph.emit` 按单发射，`_in_flight_cook_claim`
压掉同一单的第二个下锅候选，`_match_cookers_to_orders` 又是严格 1:1 ——
于是「5 单要米」永远不等于「3 口灶在烧」。实测 trace-20261014-120406 的
1700~3270 窗口（正是 order#3/4/5 三单过期的窗口）**59% 的拍里三口灶一个都
没在煮**，峰值也只有 2 口。灶空转的机会成本是 12 秒不可压缩的时钟。

要让「填灶」成为订单无关的世界谓词（而不是某一单的子步骤），先得能回答
两个纯计数问题：这种熟料还缺几份、已经有几份在路上。

按项目约定：**无副作用、不依赖 World 类**（只读 duck-typed 属性），这样
单测不必造真游戏帧。
"""
from typing import Any, Dict, List, Optional

from ai import predicates as P
from ai.plate_tokens import parse_plate_detail, parse_pot_contents


def _rich(book, item):
    # type: (Any, str) -> Optional[str]
    return book.rich_token(item)


def cooked_demand(world, book, item):
    # type: (Any, Any, str) -> int
    """一共有几份这种熟料的需求（按订单的成分计数）。

    **这里刻意不扣除"盘里已有的"**。已装盘的那份由 `cooked_supply` 作为供给
    计入，两边只能扣一次——曾经两边都扣，于是缺口被重复减去：真实帧 t=3000
    的 5 单要米、2 份已装盘、缺口应为 3，却算成 1（加分 4.0 掉到 1.33，填灶
    选不上）。这个坑单测抓不到，因为两个函数各自的 fixture 互不相交，只有
    在真实帧上把 demand/supply 一起跑才会暴露。

    这样 `cooked_shortfall` 也不再依赖 plate_match：有没有匹配结果算出来都
    一样，少一个会错的入参。
    """
    if not book.needs_cooking(item):
        return 0
    want = str(item).lower()
    n = 0
    for o in (getattr(world, "orders", None) or ()):
        n += sum(1 for r in book.requirements(o) if str(r).lower() == want)
    return n


def _pot_matches(book, item, raws):
    # type: (Any, str, Any) -> bool
    """锅里的生料能煮成 item 吗。"""
    for raw in raws or ():
        if item in book.cooked_of_raw(raw):
            return True
    return False


def cooked_supply(world, book, item):
    # type: (Any, Any, str) -> int
    """这种熟料已经有几份"在路上"：在煮 + 已熟未端 + 手持 + 已装盘未送。

    四个来源都要算，否则会重复下锅：只看"在煮"的话，端着熟锅往盘走的那
    几十拍里灶已空，planner 会再下一锅，灶全占满却没人端（本局的另一种
    死法）。糊锅不算供给——它要被倒掉，指望它等于少煮一份。
    """
    if not book.needs_cooking(item):
        return 0
    token = _rich(book, item)
    n = 0

    # ① 灶上：在煮 / 已熟未端（糊的不算）
    for c in (getattr(world, "cookers", None) or ()):
        if P.cooker_burning(c):
            continue
        raws = parse_pot_contents(c.get("pot_contents") or [])
        if not raws:
            continue
        if not (c.get("isCooking") or P.cooker_cooked(c)):
            continue
        if _pot_matches(book, item, raws):
            n += 1

    # ② 手持的熟锅
    for ch in (getattr(world, "chefs", None) or ()):
        if not P.held_is_cookware(ch, getattr(world, "recipes", None)):
            continue
        detail = ch.get("detail") or []
        toks = parse_plate_detail(detail)
        if token and token in toks:
            n += 1
        elif _pot_matches(book, item, parse_pot_contents(detail)):
            n += 1

    # ③ 台面/地上的散熟锅
    for it in (getattr(world, "items", None) or ()):
        if not P.held_is_cookware({"held": it.get("n")},
                                  getattr(world, "recipes", None)):
            continue
        detail = it.get("detail") or []
        toks = parse_plate_detail(detail)
        cooked_flag = str(it.get("cooked") or "").lower() == "cooked"
        if token and token in toks:
            n += 1
        elif cooked_flag and _pot_matches(book, item, parse_pot_contents(detail)):
            n += 1

    # ④ 已装盘未送
    for p in (getattr(world, "plates", None) or ()):
        if book.plate_has(p, item):
            n += 1

    return n


def cooked_shortfall(world, book, item):
    # type: (Any, Any, str) -> int
    """缺口 = 需求 - 供给，下界 0。

    负缺口会在打分里反向变成加分，必须夹住。
    """
    return max(0, cooked_demand(world, book, item) - cooked_supply(world, book, item))


def idle_cookers(world):
    # type: (Any) -> List[Dict]
    """有锅、锅是空的、没在煮、没着火的灶——可以立刻下料的那些。"""
    out = []  # type: List[Dict]
    for c in (getattr(world, "cookers", None) or ()):
        if not P.cooker_has_pot(c):
            continue
        if P.cooker_started(c) or P.cooker_burning(c):
            continue
        if parse_pot_contents(c.get("pot_contents") or []):
            continue
        out.append(c)
    return out


def cooked_items_wanted(world, book):
    # type: (Any, Any) -> List[str]
    """当前订单里所有需烹饪的成分（去重保序）。填灶候选按它枚举。"""
    seen = []  # type: List[str]
    lower = set()
    for o in (getattr(world, "orders", None) or ()):
        for ing in book.requirements(o):
            if not book.needs_cooking(ing):
                continue
            key = str(ing).lower()
            if key not in lower:
                lower.add(key)
                seen.append(str(ing))
    return seen

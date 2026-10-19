"""配方 → 任务依赖 DAG（HTN 编译）：compile + emit。

把 recipes + crates 编译成一张任务依赖 DAG（结构是数据），候选生成变成
对 DAG 的通用「依赖全 done、自身未 done → 发射」遍历。

节点 kind 的「已完成（存在性 done）/ 实例化（绑资源）」是固定小表，不写
per-recipe 代码。本模块只返回 task 列表；Candidate 包装与打分元数据在
planner（避免反向 import 造成环）。
"""
from typing import Any, Dict, List, Optional, Set, Tuple

from ai.revenue import cook_supply as CS
from ai.query import predicates as P
from ai import tasks as T
from ai.query.plate_tokens import parse_pot_contents
from ai.query.recipes import RecipeBook
from ai.tasks.base import _grid_matches, cooked_sources

# 拼装格候选上限（与 planner.MAX_ASSEMBLY_CELLS 同义；这里不 import planner）
_MAX_ASSEMBLY_CELLS = 6


# ---------- 资源枚举辅助（与各 task 数据源对齐） ----------

def _counter_cells(world):
    # type: (Any) -> List[Tuple[int, int]]
    cells = []  # type: List[Tuple[int, int]]
    seen = set()
    for st in world.stations:
        if st.get("t") != "counter":
            continue
        gx, gz = st.get("gx"), st.get("gz")
        if gx is None or gz is None:
            continue
        cell = (gx, gz)
        if cell in seen:
            continue
        seen.add(cell)
        cells.append(cell)
    return cells


def _assembly_cells(world, counters):
    # type: (Any, List[Tuple[int, int]]) -> List[Tuple[int, int]]
    if len(counters) <= _MAX_ASSEMBLY_CELLS:
        return counters
    p2 = P.chef_by_name(world.chefs, P.P2_NAME)
    if not p2 or p2.get("x") is None or p2.get("z") is None:
        return counters[:_MAX_ASSEMBLY_CELLS]
    pc = world.to_grid(p2["x"], p2["z"])
    return sorted(
        counters, key=lambda c: (c[0] - pc[0]) ** 2 + (c[1] - pc[1]) ** 2
    )[:_MAX_ASSEMBLY_CELLS]


def _plate_cell(world, plate):
    # type: (Any, Dict) -> Optional[Tuple[int, int]]
    x, z = plate.get("x"), plate.get("z")
    if x is None or z is None:
        return None
    return world.to_grid(x, z)


def _placed_plates(world, counters):
    # type: (Any, List[Tuple[int, int]]) -> List[Dict]
    cset = set(counters)
    out = []  # type: List[Dict]
    for p in world.plates:
        if not p.get("n"):
            continue
        c = _plate_cell(world, p)
        if c is not None and c in cset:
            out.append(p)
    return out


def _board_items(world):
    # type: (Any) -> Dict[str, Any]
    """板名 → 板上物品。workstations 权威；stations 兜底（部分单测只给 stations）。"""
    out = {}  # type: Dict[str, Any]
    for ws in world.workstations:
        n = ws.get("n")
        if n:
            out[n] = ws.get("item")
    for st in world.stations:
        if st.get("t") in ("board", "chopping"):
            n = st.get("n")
            if n and n not in out:
                out[n] = st.get("item")
    return out


def _cookers(world):
    # type: (Any) -> List[Dict]
    """灶状态条目。cookers 权威；stations 兜底（部分单测只给 stations）。"""
    out = list(world.cookers)
    names = set(str(c.get("n")) for c in out if c.get("n"))
    for st in world.stations:
        if st.get("t") in ("cooker", "cooking"):
            n = st.get("n")
            if n and str(n) not in names:
                names.add(str(n))
                out.append(st)
    return out


def _order_plate_id(plate_match, oid):
    # type: (Dict[Any, Any], Any) -> Optional[Any]
    for pid, o in plate_match.items():
        if o == oid:
            return pid
    return None


def _plate_by_n(world, pid):
    # type: (Any, Any) -> Optional[Dict]
    for p in P.all_plates(world):
        if p.get("n") == pid:
            return p
    return None


def _claimed(res_id, action, bb):
    # type: (Any, str, Any) -> bool
    return bb.is_claimed_by_other(res_id, action, T.OWNER, bb.tick)


# ---------- 编译：配方 → DAG（纯静态） ----------

def _leaf_node(book, ing):
    # type: (RecipeBook, str) -> Dict
    """直接上盘的叶子：需切则 FetchToPlate←Chop←FetchToBoard，否则 FetchToPlate。"""
    if book.needs_chopping(ing):
        return {
            "type": "FetchToPlate", "item": ing,
            "children": [{
                "type": "Chop", "item": ing,
                "children": [{"type": "FetchToBoard", "item": ing, "children": []}],
            }],
        }
    return {"type": "FetchToPlate", "item": ing, "children": []}


def _raw_production(book, raw):
    # type: (RecipeBook, str) -> Optional[Dict]
    """生料进炉前的生产链：可切叶子→Chop 链；拌合/嵌套熟料→递归；不切叶子→None。

    raw 不在配方表时按生料叶子处理（切不切看箱子），与旧 _gen_fetch_raw 的口径一致。
    """
    kind = book.node_kind(raw)
    if kind in (None, "leaf"):
        if book.needs_chopping(raw):
            return {
                "type": "Chop", "item": raw,
                "children": [{"type": "FetchToBoard", "item": raw, "children": []}],
            }
        return None
    if kind in ("mix", "cook"):
        return _compile_node(book, raw)
    return None


def _compile_node(book, name):
    # type: (RecipeBook, str) -> Optional[Dict]
    kind = book.node_kind(name)
    if kind in (None, "leaf"):
        return _leaf_node(book, name)
    if kind == "cook":
        children = [n for n in
                    (_raw_production(book, r) for r in book.raws_of(name))
                    if n]
        return {
            "type": "TakeCookedToPlate", "item": name,
            "children": [{"type": "FetchToCooker", "item": name,
                          "children": children}],
        }
    if kind == "mix":
        children = [n for n in
                    (_raw_production(book, c) for c in book.components(name))
                    if n]
        return {
            "type": "Mix", "item": name,
            "children": children,
        }
    if kind == "assemble":
        return {
            "type": "DeliverPlate", "item": name,
            "children": [_compile_node(book, c) for c in book.components(name)],
        }
    return None


# ---------- 落地（存在性 done） ----------

def _raw_on_board(world, raw):
    # type: (Any, str) -> bool
    for _name, item in _board_items(world).items():
        if str(item or "").lower() == str(raw).lower():
            return True
    return False


def _chopped_available(world, book, raw):
    # type: (Any, RecipeBook, str) -> bool
    """切好形态是否可用（板上 / 手上 / 散落）。不含盘（盘里记的是生料名）。"""
    form = book.chop_target(raw)
    if not form:
        return False
    low = str(form).lower()
    for _name, item in _board_items(world).items():
        if str(item or "").lower() == low:
            return True
    for c in world.chefs:
        if str(P.held_name(c)).lower() == low:
            return True
    for it in world.items:
        if str(it.get("n") or "").lower() == low:
            return True
    return False


def _node_done(node, ctx):
    # type: (Dict, Dict) -> bool
    t = node["type"]
    item = node.get("item")
    world = ctx["world"]
    book = ctx["book"]
    plate_id = ctx["plate_id"]
    if t == "FetchToBoard":
        return _raw_on_board(world, item)
    if t == "Chop":
        return _chopped_available(world, book, item)
    if t == "FetchToPlate":
        p = _plate_by_n(world, plate_id) if plate_id is not None else None
        return p is not None and book.plate_has(p, item)
    if t == "FetchToCooker":
        # 该单已有熟料源（灶上锅/散熟锅）在产 → 下锅这条链已完成，不再发
        return ctx["order_id"] in ctx["cooker_match"].values()
    if t == "TakeCookedToPlate":
        p = _plate_by_n(world, plate_id) if plate_id is not None else None
        return p is not None and book.plate_has(p, item)
    if t == "Mix":
        return _mix_done(world, book, node)
    return False


def _mix_done(world, book, node):
    # type: (Any, RecipeBook, Dict) -> bool
    """拌合完成 = 存在一个拌合容器，其内容已含全部成分的进炉形态。

    注意：当前关卡无 mixing 类站点，这里按「world.items 里某容器 detail 含全部
    成分」判；拌合容器的世界表示（站点类型/字段）尚未有帧可验证，属假设性实现。
    """
    want = set()  # type: Set[str]
    for c in book.components(node["item"]):
        form = book.cook_input_form(c)
        if form:
            want.add(str(form).lower())
    if not want:
        return False
    for it in world.items:
        contents = set(parse_pot_contents(it.get("detail") or []))
        if want <= contents:
            return True
    return False


# ---------- 发射（实例化：绑资源 + 认领） ----------

def _in_flight_cook_claim(ctx, item=None):
    # type: (Dict, Any) -> bool
    """该单是否已有在途的 下锅/取熟/救熟 认领（等价 _order_needs_cooked 第 2 源）。

    作用域按**熟料**而非整单（第 1 组 1.3）：原先只问「这单有没有任意烹饪
    类认领」，于是一单要两份熟料（或多料菜）时，第一份一下锅就把第二份的
    候选全压掉——AI 站着等、灶空着。认领的本意是「同一份熟料别重复下锅」。

    认领没记 item（老认领 / 非烹饪路径）时退化回整单作用域：宁可少发一个
    候选，也不要重复下锅。
    """
    want = str(item).lower() if item is not None else None
    for (_res, action), claim in ctx["bb"].claims.items():
        if getattr(claim, "order_id", None) != ctx["order_id"]:
            continue
        if action not in ("fetch_to", "take_cooked", "rescue"):
            continue
        claimed = getattr(claim, "item", None)
        if want is None or claimed is None:
            return True
        if str(claimed).lower() == want:
            return True
    return False


def _takeable_cooked_source(world, book, cid):
    # type: (Any, RecipeBook, Any) -> bool
    c = _cooker_by_id(world, cid)
    if c is not None:
        # 煮中也算可去取：TakeCookedToPlate 现在「提前端盘到锅前等熟」，
        # 不能等熟了才发射，否则取盘那几秒会让饭烧糊（实机复现）。
        return P.cooker_started(c) and not P.cooker_burning(c)
    for src in cooked_sources(world, book):
        if src["kind"] == "loose" and src["id"] == cid:
            return True
    return False


def _cooker_by_id(world, cid):
    # type: (Any, Any) -> Optional[Dict]
    """按资源 id 取灶（spec §5.2：cid 可能是 (grid,name) 元组，散锅不走
    这里——散锅的 id 是裸 item 名，由调用方的 `cooked_sources` 分支兜底）。

    与 `ai.tasks.base._unpack_resource_id` 同一套解包规则，这里不直接
    依赖 tasks 模块（避免反向 import），改用 `P.resource_id` 的逆向比较：
    cid 是元组时要求 grid 也匹配；是裸字符串时退化为只比名字。
    """
    grid, name = (cid[0], str(cid[1])) if isinstance(cid, tuple) and len(cid) == 2 \
        else (None, str(cid))
    for c in world.cookers:
        if not P.station_name_matches(str(c.get("n") or ""), name):
            continue
        if not _grid_matches(c.get("grid"), grid):
            continue
        return c
    return None


def _inst_fetch_board(node, ctx):
    # type: (Dict, Dict) -> List[Any]
    item = node["item"]
    tasks = []  # type: List[Any]
    for name, board_item in _board_items(ctx["world"]).items():
        if board_item:
            continue
        if _claimed(name, "fetch_to", ctx["bb"]):
            continue
        tasks.append(T.FetchToBoard(ctx["order_id"], (name,), item))
    return tasks


def _inst_chop(node, ctx):
    # type: (Dict, Dict) -> List[Any]
    item = node["item"]
    tasks = []  # type: List[Any]
    for name, board_item in _board_items(ctx["world"]).items():
        if str(board_item or "").lower() != str(item).lower():
            continue
        if _claimed(name, "chop", ctx["bb"]):
            continue
        tasks.append(T.Chop(ctx["order_id"], (name,), item))
    return tasks


def _inst_fetch_to_plate(node, ctx):
    # type: (Dict, Dict) -> List[Any]
    plate_id = ctx["plate_id"]
    if plate_id is None:
        return []
    return [T.FetchToPlate(ctx["order_id"], (plate_id,), node["item"])]


def _inst_fetch_to_cooker(node, ctx):
    # type: (Dict, Dict) -> List[Any]
    item = node["item"]
    if _in_flight_cook_claim(ctx, item):
        return []
    tasks = []  # type: List[Any]
    # 去重按**熟料**分桶：`used_cookers` 原先是整单共享的一个 set，而本函数
    # 会遍历所有空灶各发一个候选——于是第一样熟料就把所有空灶"用掉"，第二样
    # 一个候选都发不出来（第 1 组 1.3 的第二层结构限制，由
    # tests/test_cook_claim_scope.py 暴露）。候选只是选项，真正的互斥由
    # claims 负责，所以同一口灶出现在不同熟料的候选里没有问题。
    used = ctx["used_cookers"].setdefault(str(item).lower(), set())
    want_cookware = (ctx["book"].cookware_of(item) or "").lower()
    for c in _cookers(ctx["world"]):
        cid = P.resource_id(c)
        if not cid[1] or _claimed(cid, "fetch_to", ctx["bb"]):
            continue
        if not P.cooker_has_pot(c):
            continue
        # 炊具类型要匹配：煮意面只配炖锅、煎西红柿只配煎锅。灶上报的 cookware
        # 未知（旧帧/单测 fixture）时不过滤，保持向后兼容。
        ck = P.cooker_cookware(c)
        if want_cookware and ck and not P.cookware_matches(ck, want_cookware):
            continue
        if P.cooker_started(c) or P.cooker_burning(c):
            continue
        if cid in used:
            continue
        used.add(cid)
        raws = ctx["book"].raws_of(item)
        tasks.append(T.FetchToCooker(ctx["order_id"], (cid, tuple(raws)), item))

    # 散锅回收：空灶 + 可捡的散锅（空/生，且对应本节点这份熟料）→ 捡锅上灶再下料。
    # 与 FillCooker 的 recoverable_cookers 同源，但这里按单绑定 order_id；
    # 认领 (pot_name, "recover") 防止同口散锅被两条路径同时回收。
    for c, pot_name, _it, pot_raws in CS.recoverable_cookers(
            ctx["world"], ctx["book"]):
        if _it != item:
            continue
        cid = P.resource_id(c)
        if _claimed(cid, "fetch_to", ctx["bb"]):
            continue
        if _claimed(pot_name, "recover", ctx["bb"]):
            continue
        tasks.append(T.FetchToCooker(ctx["order_id"],
                                     (cid, tuple(pot_raws), pot_name), item))
    return tasks


def _inst_take_cooked(node, ctx):
    # type: (Dict, Dict) -> List[Any]
    plate_id = ctx["plate_id"]
    if plate_id is None:
        return []
    item = node["item"]
    tasks = []  # type: List[Any]
    for cid, oid in ctx["cooker_match"].items():
        if oid != ctx["order_id"]:
            continue
        if not _takeable_cooked_source(ctx["world"], ctx["book"], cid):
            continue
        if _claimed(cid, "take_cooked", ctx["bb"]):
            continue
        tasks.append(T.TakeCookedToPlate(ctx["order_id"], (cid, plate_id), item))
    return tasks


def _inst_deliver(node, ctx):
    # type: (Dict, Dict) -> List[Any]
    tasks = []  # type: List[Any]
    for p in P.all_plates(ctx["world"]):
        pid = p.get("n")
        if not pid:
            continue
        if ctx["book"].missing(ctx["order"], p) != ():
            continue
        if _claimed(pid, "deliver", ctx["bb"]):
            continue
        tasks.append(T.DeliverPlate(ctx["order_id"], (pid,)))
    return tasks


def _inst_mix(node, ctx):
    # type: (Dict, Dict) -> List[Any]
    """拌合实例化：找 mixing 类站点（当前关卡无此站点 → 返回空，惰性待数据）。"""
    tasks = []  # type: List[Any]
    for st in ctx["world"].stations:
        if st.get("t") != "mixing" or not st.get("n"):
            continue
        sid = P.resource_id(st)
        if _claimed(sid, "mix", ctx["bb"]):
            continue
        comps = ctx["book"].components(node["item"])
        tasks.append(T.Mix(ctx["order_id"], (sid, tuple(comps)),
                           node["item"]))
    return tasks


def _instantiate(node, ctx):
    # type: (Dict, Dict) -> List[Any]
    t = node["type"]
    if t == "FetchToBoard":
        return _inst_fetch_board(node, ctx)
    if t == "Chop":
        return _inst_chop(node, ctx)
    if t == "FetchToPlate":
        return _inst_fetch_to_plate(node, ctx)
    if t == "FetchToCooker":
        return _inst_fetch_to_cooker(node, ctx)
    if t == "TakeCookedToPlate":
        return _inst_take_cooked(node, ctx)
    if t == "DeliverPlate":
        return _inst_deliver(node, ctx)
    if t == "Mix":
        return _inst_mix(node, ctx)
    return []


def _walk(node, ctx, out):
    # type: (Dict, Dict, List[Any]) -> None
    if node["type"] == "DeliverPlate":
        # 送餐是末端：存在料齐的盘就发（任意料齐盘，与旧 _gen_serve 对齐），
        # 同时仍下沉到 comp 链——旧代码里 _gen_serve 与备菜循环是相互独立的，
        # 盘齐与「另一只盘还缺料」可以同时成立。
        out.extend(_instantiate(node, ctx))
        for c in node.get("children", []):
            if not _node_done(c, ctx):
                _walk(c, ctx, out)
        return
    if _node_done(node, ctx):
        return
    deps_not_done = [c for c in node.get("children", [])
                     if not _node_done(c, ctx)]
    if deps_not_done:
        for c in deps_not_done:
            _walk(c, ctx, out)
    else:
        out.extend(_instantiate(node, ctx))


def _emit_plate(world, bb, order_id):
    # type: (Any, Any, Any) -> List[Any]
    """订单没盘 → 拿干净盘摆上（第 1 号 PlaceCleanPlate）。"""
    counters = _counter_cells(world)
    placed_ids = set(p.get("n") for p in _placed_plates(world, counters))
    tasks = []  # type: List[Any]
    for cell in _assembly_cells(world, counters):
        if _claimed(cell, "assemble", bb):
            continue
        for p in world.plates:
            pid = p.get("n")
            if not pid or P.plate_count(p) != 0 or pid in placed_ids:
                continue
            if _claimed(pid, "place", bb):
                continue
            tasks.append(T.PlaceCleanPlate(order_id, (pid, cell)))
    return tasks


def emit(world, bb, book, order, plate_match, cooker_match):
    # type: (Any, Any, RecipeBook, Dict, Dict[Any, Any], Dict[Any, Any]) -> List[Any]
    """为一个订单发射 recipe 驱动的候选 task 列表（含放盘，不含 housekeeping）。"""
    order_id = order.get("id")
    plate_id = _order_plate_id(plate_match, order_id)
    dish = order.get("dish")

    out = []  # type: List[Any]
    if order_id not in plate_match.values():
        out.extend(_emit_plate(world, bb, order_id))

    root = None
    if book.recipe(dish) is not None:
        root = _compile_node(book, dish)
    if root is None:
        # 订单 dish 不在配方表（单测/异常帧）→ 退回按 composition 当拼装表
        children = [_compile_node(book, ing) for ing in book.requirements(order)]
        children = [c for c in children if c]
        if children:
            root = {"type": "DeliverPlate", "item": dish, "children": children}
    if root is not None:
        ctx = {
            "world": world, "bb": bb, "book": book, "order": order,
            "order_id": order_id, "plate_id": plate_id,
            "cooker_match": cooker_match, "used_cookers": {},
        }
        _walk(root, ctx, out)
    return out

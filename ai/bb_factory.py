"""黑板与对账视图的构造（spec §11.2 阶段 2 的 make_bb）。

把"从快照派生出对账所需视图"这件事收到一处。Plan 3 的主循环直接调这里，
不自己拼装——否则资源 id 的构造规则会散落多处，对账就不可靠了。

资源 id 规则（spec §4.4）：
  灶/板/洗碗池 → 站点实例名 n（稳定）
  拼装格       → 网格坐标 (gx, gz)（坐标即身份）
  盘           → 实例名 n 为主
"""
from typing import Any, Dict, Set

from ai import predicates as P
from ai.blackboard import Blackboard
from ai.reconcile import ResourceView


def make_bb(tick=0, collab_mode="relay"):
    # type: (int, str) -> Blackboard
    """新建一块干净黑板。"""
    bb = Blackboard()
    bb.tick = tick
    bb.intents.collab_mode = collab_mode
    return bb


def make_resource_view(state):
    # type: (Dict) -> ResourceView
    """从快照派生对账视图。"""
    view = ResourceView()
    state = state or {}

    for st in state.get("stations") or []:
        name = st.get("n")
        if name:
            view.existing_ids.add(str(name))
        gx, gz = st.get("gx"), st.get("gz")
        if gx is not None and gz is not None:
            view.existing_ids.add((gx, gz))

    for c in state.get("cookers") or []:
        name = c.get("n")
        if name:
            view.existing_ids.add(str(name))

    for p in state.get("plates") or []:
        name = p.get("n")
        if not name:
            continue
        name = str(name)
        view.existing_ids.add(name)
        view.contents[name] = P.plate_contents(p)

    return view


def live_order_ids(state):
    # type: (Dict) -> Set[Any]
    """当前在场订单的 id 集合。无 id 字段时用下标兜底。"""
    out = set()
    for i, o in enumerate((state or {}).get("orders") or []):
        oid = o.get("id") if isinstance(o, dict) else None
        out.add(oid if oid is not None else i)
    return out

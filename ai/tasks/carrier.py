"""可烹饪载体两 task：FetchToCarrier / PlaceCarrierInCooker（披萨这一类）。

反编译依据见 ai/query/carriers.py「可烹饪载体」一节：面团切成饼（Pizza_Base_Raw，
CookablePreparationContainer），料放在饼上，整份放进站型对得上的空灶（烤箱），
熟了由 TakeCookedToPlate 端盘去取。流程只读插件上报的能力（cookable / 灶的
station_type），不按菜名特判。

载体实例没有稳定 id，task 身份只带**载体形态名**（resources[0]），目标实例每拍
按 carriers.match_prep_carriers 现找；料一放上去内容就变，身份不跟着变，承诺
不断。

外部不直接 import 本模块，一律 `from ai import tasks as T`。
"""
from typing import Any, Dict, Optional

from ai.query import carriers as CR
from ai.query import predicates as P
from ai import primitives as PR
from ai.exec.bt import Node, Sequence
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register,
                           _station_for_item, cooker_state, cooker_station,
                           ingredient_available, ingredient_source)


def _order(ctx, order_id):
    # type: (Dict, object) -> Optional[Dict]
    for o in ctx["w"].orders:
        if str(o.get("id")) == str(order_id):
            return o
    return None


def _carrier_for(ctx, order_id):
    # type: (Dict, object) -> Optional[Dict]
    """这一单此刻配到的（还没上灶的）载体。"""
    return CR.match_prep_carriers(ctx["w"], ctx["book"]).get(order_id)


def _carrier_target(ctx, carrier):
    # type: (Dict, Optional[Dict]) -> Optional[Dict]
    """交互目标：载体挂在站点上时给站点（句柄按站点格匹配），否则给物品本身。"""
    if carrier is None:
        return None
    it = carrier["item"]
    return _station_for_item(ctx["w"], it) or it


def _fetch_form(ctx, item):
    # type: (Dict, Optional[str]) -> Optional[str]
    book = ctx["book"]
    return book.chop_target(item) if book.needs_chopping(item) else item


def cooker_accepts(cooker, station):
    # type: (Optional[Dict], Optional[str]) -> bool
    """灶的站型能收这个载体（插件协议 8 cookers[].station_type）。"""
    st = (cooker or {}).get("station_type")
    return bool(st) and bool(station) and str(st).lower() == str(station).lower()


def cooker_free(cooker):
    # type: (Optional[Dict]) -> bool
    """灶空着、能放东西：上面没有炊具/载体、没着火。"""
    return bool(cooker) and not P.cooker_has_pot(cooker) \
        and not P.cooker_burning(cooker)


@_register
class FetchToCarrier(Task):
    """把一样料（需切的取切好的）放到这一单的载体上（番茄放上披萨饼）。

    item = 订单成分名（Tomato）；resources = (载体形态名,)。不认领：料在箱子里
    无限供应，载体由每拍匹配定给这一单。
    """

    TYPE = "FetchToCarrier"
    DESC = "把这种料（需要切的取切好的）放到饼上"
    LABEL = "放上饼"
    HANDS = "empty"

    @property
    def base_form(self):
        return self._resources[0]

    def claims(self):
        return ()

    def done(self, ctx):
        # type: (Dict) -> bool
        c = _carrier_for(ctx, self.order_id)
        return c is not None and bool(ctx["book"].want_tokens(self.item) & set(c["contents"]))

    def feasible(self, ctx):
        # type: (Dict) -> bool
        o = _order(ctx, self.order_id)
        if o is None or self.item not in ctx["book"].carrier_toppings(o.get("dish")):
            return False
        c = _carrier_for(ctx, self.order_id)
        if c is None or c["held_by"]:
            return False                   # 没饼 / 饼在谁手里（P1 端着走了）
        if self.done(ctx):
            return False
        from ai.space import reachability
        if not reachability.can_interact(ctx, c["item"]):
            return False
        return ingredient_available(ctx, _fetch_form(ctx, self.item))

    def build(self):
        # type: () -> Node
        item, order_id = self.item, self.order_id

        def holding(c):
            return P.held_matches(c["chef"], _fetch_form(c, item))

        def target(c):
            return _carrier_target(c, _carrier_for(c, order_id))

        def placed(c):
            if not P.hands_free(c["chef"]):
                return False
            car = _carrier_for(c, order_id)
            return car is not None and bool(c["book"].want_tokens(item) & set(car["contents"]))

        return PR.Timeout(Sequence([
            PR.EnsureHandsFree(keep=holding),
            PR.InteractWith(lambda c: ingredient_source(c, _fetch_form(c, item)),
                            "pickup", holding),
            PR.InteractWith(target, "placement", placed),
        ]), TASK_TIMEOUT_TICKS)


@_register
class PlaceCarrierInCooker(Task):
    """端起这一单装好料的载体，放进站型对得上的空灶（披萨进烤箱）。

    item = 熟料名（MargheritaPizza）；resources = (灶资源 id, 载体形态名)。
    认领 (灶, "fetch_to")：与 FetchToCooker 同一动作，下锅类认领互斥。
    """

    TYPE = "PlaceCarrierInCooker"
    DESC = "把放好料的饼整个放进烤箱"
    LABEL = "进炉"
    PROCESS = "烤"
    HANDS = "empty"

    @property
    def cooker_id(self):
        return self._resources[0]

    @property
    def base_form(self):
        return self._resources[1]

    def claims(self):
        return ((self.cooker_id, "fetch_to"),)

    def _station(self, ctx):
        # type: (Dict) -> Optional[str]
        base = ctx["book"].carrier_base(self.item)
        return base[2] if base else None

    def done(self, ctx):
        # type: (Dict) -> bool
        c = cooker_state(ctx, self.cooker_id)
        if c is None or not P.cooker_has_pot(c):
            return False
        toks = set()
        for car in CR.prep_carriers(ctx["w"], ctx["book"]):
            if car["cooker"] and P.station_name_matches(car["cooker"], str(c.get("n") or "")):
                toks = set(car["contents"])
        return not CR.prep_missing(ctx["book"], {"contents": toks}, self.item)

    def _ready_carrier(self, ctx):
        # type: (Dict) -> Optional[Dict]
        """这一单装齐料、还没上灶的载体（P2 手里的也算）。"""
        car = _carrier_for(ctx, self.order_id)
        if car is None or CR.prep_missing(ctx["book"], car, self.item):
            return None
        if car["held_by"] and not P.station_name_matches(str(car["held_by"]), P.P2_NAME):
            return None
        return car

    def feasible(self, ctx):
        # type: (Dict) -> bool
        c = cooker_state(ctx, self.cooker_id)
        if not cooker_accepts(c, self._station(ctx)):
            return False
        if self._ready_carrier(ctx) is None:
            return False
        held = self._holding(ctx)
        return held or cooker_free(c)

    def _holding(self, ctx):
        # type: (Dict) -> bool
        return P.held_matches(ctx["chef"], self.base_form)

    def build(self):
        # type: () -> Node
        task = self

        def holding(c):
            return task._holding(c)

        def carrier(c):
            car = task._ready_carrier(c)
            return _carrier_target(c, car) if car and not car["held_by"] else None

        def placed(c):
            cc = cooker_state(c, task.cooker_id)
            return P.hands_free(c["chef"]) and cc is not None and P.cooker_has_pot(cc)

        return PR.Timeout(Sequence([
            PR.EnsureHandsFree(keep=holding),
            PR.InteractWith(carrier, "pickup", holding),
            PR.InteractWith(lambda c: cooker_station(c, task.cooker_id),
                            "placement", placed),
        ]), TASK_TIMEOUT_TICKS)


# ---------------------------------------------------------------- 送载体过去
# SendAcross(item=KIND_CARRIER, resources=(载体形态名, 通道 key)) 的行为放在这里，
# 由 relay.SendAcross 按 kind 分派——task 仍是同一个通用送件 TYPE。目前只走静态
# 通道（共享台面 / 空的共享砧板）；渡运篮子送饼等有关卡再说。


def _stuck(ctx, base_form):
    # type: (Dict, str) -> Optional[Dict]
    from ai.revenue import cross_supply as XS
    for c in XS.stuck_carriers(ctx["w"], ctx["book"], ctx.get("bb")):
        if P.station_name_matches(c["name"], base_form):
            return c
    return None


def _holding_carrier(ctx, base_form):
    # type: (Dict, str) -> bool
    return P.held_matches(ctx["chef"], base_form)


def carrier_slot(ctx, lock):
    # type: (Dict, Dict) -> Optional[Dict]
    """放载体的通道格：锁住选中的那格；被占了重选。共享台面优先，其次空砧板，
    同类里走得最近的。"""
    from ai.space import channels as CH
    from ai.space import reachability
    w, field, bb = ctx["w"], ctx.get("field"), ctx.get("bb")
    free = CH.free_carrier_slots(w, bb)
    name = lock.get("n")
    if name is not None:
        hit = next((s for s in free if s.get("n") == name), None)
        if hit is not None:
            return hit
        lock.clear()

    def cost(i_st):
        i, st = i_st
        goals = reachability._interact_goals(ctx, st)
        if goals is True:
            return (0, i)
        got = [field[g] for g in goals or () if field and g in field]
        return (0 if st.get("t") == "counter" else 1, min(got)) if got else (9, 1e9)
    if not free:
        return None
    best = min(enumerate(free), key=cost)
    if cost(best)[0] == 9:
        return None
    lock["n"] = best[1].get("n")
    return best[1]


def send_carrier_feasible(task, ctx):
    # type: (Any, Dict) -> bool
    from ai.space import channels as CH
    if task.channel != CH.STATIC:
        return False
    if _stuck(ctx, task.source_id) is None:
        return False
    return carrier_slot(ctx, {}) is not None


def send_carrier_done(task, ctx):
    # type: (Any, Dict) -> bool
    """手空了，且这种载体已没有卡在我这边的（放到 P1 够得着的格上了）。"""
    return P.hands_free(ctx["chef"]) and _stuck(ctx, task.source_id) is None


def send_carrier_tree(task):
    # type: (Any) -> Node
    base_form = task.source_id
    lock = {}  # type: Dict[str, Any]

    def holding(c):
        return _holding_carrier(c, base_form)

    def carrier(c):
        car = _stuck(c, base_form)
        if car is None or car["held_by"]:
            return None
        return _carrier_target(c, car)

    def placed(c):
        return P.hands_free(c["chef"]) and _stuck(c, base_form) is None

    return PR.Timeout(Sequence([
        PR.EnsureHandsFree(keep=holding),
        PR.InteractWith(carrier, "pickup", holding),
        PR.InteractWith(lambda c: carrier_slot(c, lock), "placement", placed),
    ]), TASK_TIMEOUT_TICKS)

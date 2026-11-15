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
from typing import Any, Dict, Optional, Tuple

from ai.query import carriers as CR
from ai.query import predicates as P
from ai import primitives as PR
from ai.exec.bt import Node, Sequence
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register,
                           _station_for_item, conveyor_pickup,
                           cooker_state, cooker_station,
                           cooked_sources, held_matches_contents,
                           ingredient_available, ingredient_source,
                           loose_on_station)
from ai.tasks.cook import (_cooker_ready_to_take, TAKE_COOKED_TIMEOUT_TICKS,
                           WAIT_COOKED_TIMEOUT_TICKS)


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
            conveyor_pickup(
                resolve=lambda c: ingredient_source(c, _fetch_form(c, item)),
                held=holding,
                arrived=lambda c, seg: loose_on_station(
                    c["w"], _fetch_form(c, item), seg),
                face=True),
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
        """手里这份就是这一单要进炉的载体：形态名对、**还是生的**、料齐。

        只比名字会把 RescueCooked 刚端出炉的熟披萨当成待进炉载体：熟饼名字
        仍是 Pizza_Base_Raw、cookable.prep 仍为真。于是树首 keep 住它、拿生饼
        那步 expect 短路，最后整份塞回烤箱；RescueCooked 再端出来——无限拿进
        拿出（t=3156~3307、
        t=3928~4440，最后烧着）。料也要对：别把另一道菜的饼塞进这单的炉子。
        """
        if not P.held_matches(ctx["chef"], self.base_form):
            return False
        held = CR.held_item(ctx["w"], P.P2_NAME)
        if held is None:
            return True       # 旧帧没有 held_items：保持只看名字的旧行为
        if not CR.is_raw_carrier({"item": held}):
            return False
        car = {"name": str(held.get("n") or ""),
               "contents": CR.item_contents(held)}
        return CR.prep_fits(ctx["book"], car, self.item) \
            and not CR.prep_missing(ctx["book"], car, self.item)

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


@_register
class FillCarrier(Task):
    """端着包裹底料（卷饼皮）去熟料源，把熟料直接装进底料（wrap 类载体菜）。

    wrap 类载体菜（卷饼/汉堡）的「装馅」：与 TakeCookedToPlate 同一套
    「端容器 → 走到锅前等熟 → 盛进去」机制，只是手里的容器从盘换成底料
    （PreparationContainer，非盘非锅、能直接装熟料）。装完的底料是「已装料
    载体」，由 FetchToPlate 带内容签名端进盘、送餐。

    item = 要盛的熟料名（BoiledRice / FriedMushrooms）；
    resources = (source_id, base_form, pre_contents)：
      source_id  熟料源（灶实例名 / 散锅 item 名，与 TakeCookedToPlate 同口径）；
      base_form  底料的取料形态名（Tortilla）；
      pre_contents 盛这份料之前底料里已有的内容签名（第一份为空元组）。
    不认领底料：底料实例每拍现找（空卷饼在箱子里无限供应、装了一半的是
    载体按内容签名匹配），与 FetchToCarrier 同一理由。
    """

    TYPE = "FillCarrier"
    # 给对话层 LLM 看的一句话说明（ai/dialog/catalog.py 渲染 DAG 时带出）。
    DESC = "端着卷饼把煮好的料装进去"
    LABEL = "装馅"
    HANDS = "empty"       # 要空手去端卷饼；手不空时打分加腾手代价（spec §6.1(b)）
    NO_ABANDON = True     # 盛料是停烧糊的必经之路（与 TakeCookedToPlate 同源）

    @property
    def source_id(self):
        return self._resources[0]

    @property
    def base_form(self):
        return self._resources[1]

    @property
    def pre_contents(self):
        # type: () -> Tuple[str, ...]
        return tuple(self._resources[2]) if len(self._resources) > 2 else ()

    def claims(self):
        return ((self.source_id, "take_cooked"),)

    def _after(self, ctx):
        # type: (Dict) -> Tuple[str, ...]
        """盛完这份料后底料应有的内容签名（前置内容 + 这份熟料的 token）。"""
        token = ctx["book"].rich_token(self.item)
        toks = set(self.pre_contents)
        if token:
            toks.add(token)
        return CR.signature(toks)

    def done(self, ctx):
        # type: (Dict) -> bool
        """盛料一进底料，底料就从「空卷饼」（不是载体）变成「装了料的载体」。"""
        return CR.wrap_carrier_exists(ctx["w"], self.base_form, self._after(ctx))

    def _source(self, ctx):
        # type: (Dict) -> Optional[Dict]
        """熟料源（灶上煮中/熟锅 / 台面散熟锅），与 TakeCookedToPlate._source 同源。"""
        sid = self.source_id
        c = cooker_state(ctx, sid)
        if c is not None:
            if P.cooker_started(c) and not P.cooker_burning(c):
                return {"kind": "cooker", "target": cooker_station(ctx, sid)}
            return None
        token = ctx["book"].rich_token(self.item)
        for src in cooked_sources(ctx["w"], ctx["book"]):
            if src["kind"] != "loose" or str(src["id"]) != str(sid):
                continue
            if token and token not in src["tokens"]:
                continue
            # 散熟源挂在台面上时句柄是台面、不是源物品本身（与
            # TakeCookedToPlate._source 同理由）；散在地上才对 item 盛。
            station = _station_for_item(ctx["w"], src["item"])
            return {"kind": "loose", "target": station or src["item"]}
        return None

    def _holding_wrapper(self, ctx):
        # type: (Dict) -> bool
        """手里正是「底料 + 前置内容」的那件（空卷饼 / 装了部分料的卷饼）。"""
        return held_matches_contents(ctx, self.base_form, self.pre_contents)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        """分两段（与 TakeCookedToPlate 同理由）：

          - 启动看源 + 时机：灶上熟料 / 散熟锅才值得去盛；煮中只等到离熟
            TAKE_COOKED_LEAD_S 秒以内才放行，别端卷饼去锅边干等；
          - 执行中看手：手里已经是「前置内容对得上」的卷饼也算可行，别因
            卷饼离开原位而把自己判成不可行。
        """
        if self.done(ctx):
            return False
        if self._holding_wrapper(ctx):
            return True
        if not ingredient_available(ctx, self.base_form, self.pre_contents):
            return False
        s = self._source(ctx)
        if s is None:
            return False
        if s["kind"] == "cooker":
            if not _cooker_ready_to_take(cooker_state(ctx, self.source_id)):
                return False
        return True

    def build(self):
        # type: () -> Node
        base_form, pre = self.base_form, self.pre_contents

        def holding(c):
            return held_matches_contents(c, base_form, pre)

        def wrapper_source(c):
            return ingredient_source(c, base_form, pre)

        def source_target(c):
            s = self._source(c)
            return s["target"] if s else None

        def source_ready(c):
            s = self._source(c)
            if s is None:
                return False
            if s["kind"] == "loose":
                return True
            cc = cooker_state(c, self.source_id)
            return P.cooker_cooked(cc) and not P.cooker_burning(cc)

        def filled(c):
            return held_matches_contents(c, base_form, self._after(c))

        # 端卷饼盛料：端卷饼 → 提前站到锅前等熟 → 熟了盛进卷饼。交互用
        # kind="use"，与 TakeCookedToPlate 同理由（端容器面对锅时 placement
        # 句柄为 null、pickup 会被脚边散料抢走）。
        return PR.Timeout(Sequence([
            PR.EnsureHandsFree(keep=holding),
            conveyor_pickup(
                resolve=wrapper_source,
                held=holding,
                arrived=lambda c, seg: loose_on_station(c["w"], base_form, seg),
                face=True),
            PR.Approach(source_target, None),
            PR.Face(source_target),
            PR.WaitUntil(source_ready, WAIT_COOKED_TIMEOUT_TICKS),
            PR.InteractWith(source_target, "use", filled),
        ]), TAKE_COOKED_TIMEOUT_TICKS)

"""配方语义：订单需要什么、什么要下锅、什么要切、料在哪。

从 oc/ai/task.py 抽取（spec §8.2 "配方语义查找 → planner + predicates"）。
两处结构性改动：

  1. **CHOPPABLE 硬编码表删除**。旧代码有一张 {"SushiFish","Cucumber",
     "SushiCarrot","SushiPrawn"} 的可切食材表（oc/ai/task.py:21）。
     spec §9 记它"已泛化（主路径读 crate 的 chop/chop_stages），该表是
     遗留，随 task.py 拆解删除"。这里不带它。
  2. **一次构造、整帧复用**。旧 find_* 每次调用都重建 {name: recipe}
     索引，而新架构里每个 task 的 feasible/done 每拍要问好几次。

decompose() 不在这里——spec §8.2 明确"decompose() 删除"。它输出的线性
步骤序列正是 §6.2 要换掉的东西：执行顺序从可行性涌现，不预先排序。
"""
from typing import Any, Dict, List, Optional, Set, Tuple

from ai.query import predicates as P
from ai.query.plate_tokens import parse_plate_detail

_NULL = ("null", "none", "")

# 熟料进盘后的不透明 token（旧帧 contents 里的形态，实测 801 帧，小写比较）。
# 旧帧盘里不记"是哪种熟料"，只记"有一份熟的复合料"。新帧 detail 里是 rich
# token `cooked:{raw}:{cookware}`，见 plate_tokens / want_tokens。
COOKED_PLATE_TOKEN = "cookedcompositeassemblednode"


def _is_null(v):
    # type: (object) -> bool
    """插件把"无"写成字符串 "null" 而不是 JSON null，两种都要拦。"""
    return v is None or str(v).strip().lower() in _NULL


class RecipeBook(object):
    """一帧的配方视图：成分树（recipes）+ 箱子的加工信息（stations）。"""

    def __init__(self, recipes, stations=None, items=None):
        # type: (Optional[List[Dict]], Optional[List[Dict]], Optional[List[Dict]]) -> None
        self._by_name = {}      # type: Dict[str, Dict]
        for r in recipes or ():
            if isinstance(r, dict) and r.get("name"):
                self._by_name[str(r["name"]).lower()] = r
        # 箱子按产出食材索引，取 chop / chop_stages
        self._crates = {}       # type: Dict[str, Dict]
        for st in stations or ():
            if not isinstance(st, dict) or st.get("t") != "crate":
                continue
            ing = st.get("ing")
            if ing:
                self._crates[str(ing).lower()] = st
        # 物品形态 → 烹饪能力（插件协议 8：crate.chop_cookable / items[].cookable，
        # 见 Oc2AIPlugin.CookableJson）。键是物品名小写（Pizza_Base_Raw）。
        self._cookable = {}     # type: Dict[str, Dict]
        for st in stations or ():
            if isinstance(st, dict) and st.get("t") == "crate" \
                    and isinstance(st.get("chop_cookable"), dict) and st.get("chop"):
                self._cookable[str(st["chop"]).lower()] = st["chop_cookable"]
        for it in items or ():
            if isinstance(it, dict) and isinstance(it.get("cookable"), dict) and it.get("n"):
                self._cookable.setdefault(str(it["n"]).lower(), it["cookable"])
        self._branches = None   # type: Optional[Dict[str, str]]

    def recipe(self, ing):
        # type: (Optional[str]) -> Optional[Dict]
        if not ing:
            return None
        return self._by_name.get(str(ing).lower())

    # ---------- 配方图（HTN 编译用）----------
    def node_kind(self, ing):
        # type: (Optional[str]) -> Optional[str]
        """配方节点类型：'leaf'|'cook'|'mix'|'assemble'|None。"""
        r = self.recipe(ing)
        if not r:
            return None
        t = r.get("t")
        if t is None:
            # 最小配方（单测/异常帧无 t 字段）：从 cook/comp 推断。
            if not _is_null(r.get("cook")):
                return "cook"
            return "assemble" if (r.get("comp") or []) else "leaf"
        return {
            "IngredientOrderNode": "leaf",
            "CookedCompositeOrderNode": "cook",
            "MixedCompositeOrderNode": "mix",
            "CompositeOrderNode": "assemble",
        }.get(t)

    def components(self, ing):
        # type: (Optional[str]) -> Tuple[str, ...]
        """该节点的全部 comp 项（保序）。叶子返回 ()。"""
        if not ing:
            return ()
        return tuple(str(c) for c in ((self.recipe(ing) or {}).get("comp") or ()))

    # ---------- 订单需求 ----------
    def requirements(self, order):
        # type: (Optional[Dict]) -> Tuple[str, ...]
        """订单所需的顶层成分，保留原始大小写（下游要用它查配方/箱子）。

        优先 order["composition"]（实测帧都有）；缺失时按 dish 名查配方
        的 comp。两条路都要有：composition 不保证永远在，只走它的话，
        缺字段的帧会让整单无候选、AI 站着不动。
        """
        if not order:
            return ()
        dish = order.get("dish")
        # 整道菜本身是熟料节点（披萨 = CookedCompositeOrderNode{Dough,Tomato,
        # Cheese}，整份进炉）：进盘的是**一个**熟料节点，不是三样料。插件给的
        # composition 是扁平的生料表，按它判「盘里缺什么」会恒报缺三样（盘里
        # 只有 cooked:cheese+dough+tomato:oventray）。生料由 raws_of(dish) 展开。
        # 判据：composition 恰好是该熟料节点 comp 的扁平展开（或缺省）。
        comp = order.get("composition")
        if self.node_kind(dish) == "cook":
            own = sorted(c.lower() for c in self.components(dish))
            if not comp or sorted(str(c).lower() for c in comp) == own:
                return (str(self.recipe(dish).get("name") or dish),)
        if comp:
            return tuple(str(c) for c in comp)
        r = self.recipe(dish)
        if not r:
            return ()
        return tuple(str(c) for c in (r.get("comp") or ()))

    def missing(self, order, plate):
        # type: (Optional[Dict], Optional[Dict]) -> Tuple[str, ...]
        """盘里还缺哪些顶层成分（spec §6.0 的反向展开）。

        走 want_tokens / plate_tokens 同一套装盘态词汇，不是字面比较——实测
        盘里的词汇与订单需求的词汇是两套。字面比较会让熟料永远报缺，AI
        无限重倒。detail 帧里熟料是 rich token，这里能区分不同熟料。

        盘里多放的料不影响判断——这里只管"还缺什么"。
        """
        have = self.plate_tokens(plate)
        return tuple(ing for ing in self.requirements(order)
                     if not (self.want_tokens(ing) & have))

    def missing_union(self, order, plates):
        # type: (Optional[Dict], List[Dict]) -> Tuple[str, ...]
        """多盘并集口径的 `missing`：还缺哪些顶层成分（盘合并用）。

        与 `missing` 同一套装盘态词汇判断，只是把 `have` 换成 `plates`
        里所有盘的 token 并集。用于 MergePlates 判「两块盘合起来离订单还差
        几样料」——单盘口径的 `missing` 永远看不到 src 盘上的料。
        """
        have = set()  # type: Set[str]
        for p in plates or ():
            have |= self.plate_tokens(p)
        return tuple(ing for ing in self.requirements(order)
                     if not (self.want_tokens(ing) & have))

    # ---------- 分支归一（dialog 分支作用域，spec §4.5）----------
    def ingredient_branch(self, item):
        # type: (Optional[str]) -> Optional[str]
        """熟料/切好的名字归一到生料分支；生料返回自身，未知原样返回。

        dialog 的分支作用域只认生料名（spec §4.5）：说「饭我来」时，
        BoiledSushiRice / ChoppedSushiFish 这类名字要先折回 SushiRice /
        SushiFish，整条链上的候选才命中同一条分支。多生料的熟料节点
        不拆分支，保持熟料名本身。空输入给 None。
        """
        if not item:
            return None
        if self._branches is None:
            self._branches = self._build_branches()
        return self._branches.get(str(item).lower(), item)

    def _build_branches(self):
        # type: () -> Dict[str, str]
        """反向索引：生料→自身、chop 产物→生料、单生料熟料→生料。

        多生料的熟料节点取熟料名本身作 branch（spec §4.5），不强行拆成
        多个生料——否则「我来做这道混合菜」会被折成其中某一种生料。
        数据全部来自本帧已有的 _by_name / _crates，不另建配方表。键统一
        小写（与 _by_name 一致），值保留配方里的原始大小写。
        """
        out = {}  # type: Dict[str, str]
        for name, r in self._by_name.items():
            canonical = r.get("name") or name
            if self.needs_cooking(name):
                raws = self.raws_of(canonical)
                out[str(canonical).lower()] = (
                    raws[0] if len(raws) == 1 else canonical)
            elif self.node_kind(name) == "leaf":
                out[name] = canonical
                # chop 产物也折回生料分支：chop_target 优先读配方字段、箱子兜底，
                # 这样「鱼我来」能命中传送带供料、无箱子的 Chop 链。
                chop = self.chop_target(canonical)
                if chop:
                    out[str(chop).lower()] = canonical
        for ing, st in self._crates.items():
            raw = st.get("ing") or ing
            out[ing] = raw
            chop = st.get("chop")
            if not _is_null(chop):
                out[str(chop).lower()] = raw
        return out

    # ---------- 烹饪 ----------
    def needs_cooking(self, ing):
        # type: (Optional[str]) -> bool
        r = self.recipe(ing)
        return bool(r) and not _is_null(r.get("cook"))

    def raws_of(self, ing):
        # type: (Optional[str]) -> Tuple[str, ...]
        """熟料节点的全部生料成分（多料同锅用）。非熟料返回 ()。"""
        if not self.needs_cooking(ing):
            return ()
        return self.components(ing)

    def raw_of(self, ing):
        # type: (Optional[str]) -> Optional[str]
        """熟料对应的生料（多成分取第一个，向后兼容）。"""
        raws = self.raws_of(ing)
        return raws[0] if raws else None

    def cook_input_form(self, raw):
        # type: (Optional[str]) -> Optional[str]
        """生料进炉前的形态：需切 → chop_target（ChoppedX），否则原样。"""
        if not raw:
            return raw
        if self.needs_chopping(raw):
            return self.chop_target(raw)
        return raw

    def cookware_of(self, ing):
        # type: (Optional[str]) -> Optional[str]
        if not self.needs_cooking(ing):
            return None
        return str((self.recipe(ing) or {}).get("cook"))

    def cooked_of_raws(self, raws):
        # type: (Any) -> List[str]
        """生料集合**恰好**等于其 comp 的熟料（多料同炉的精确反查）。

        cooked_of_raw 只看 comp[0]：面团同时是十几种披萨的 comp[0]，烤箱里的
        {cheese, dough, tomato} 只该认成 MargheritaPizza。
        """
        want = sorted(str(r).lower() for r in raws or ())
        if not want:
            return []
        out = []  # type: List[str]
        for name, r in self._by_name.items():
            if not self.needs_cooking(name):
                continue
            comp = sorted(str(c).lower() for c in (r.get("comp") or ()))
            if comp == want:
                out.append(str(r.get("name") or name))
        return out

    # ---------- 可烹饪载体（协议 8） ----------
    def cookable_of(self, form):
        # type: (Optional[str]) -> Optional[Dict]
        """物品形态的烹饪能力 {station, step, prep, vessel, time}；不能上灶 None。"""
        if not form:
            return None
        return self._cookable.get(str(form).lower())

    def carrier_base(self, dish):
        # type: (Optional[str]) -> Optional[Tuple[str, str, str]]
        """整份进炉的熟料：(底料成分, 载体形态, 要求站型)；不是这种菜返回 None。

        反编译：CookablePreparationContainer = 可放料的载体 + CookingHandler。
        披萨的面团切成 Pizza_Base_Raw（prep=True, station=Oven），番茄/芝士
        放在饼上、整份进烤箱。判据只读插件上报的能力，不按菜名。
        """
        if self.node_kind(dish) != "cook":
            return None
        for comp in self.components(dish):
            form = self.cook_input_form(comp)
            cap = self.cookable_of(form)
            if cap and cap.get("prep") and cap.get("station"):
                return (comp, str(form), str(cap["station"]))
        return None

    def carrier_toppings(self, dish):
        # type: (Optional[str]) -> Tuple[str, ...]
        """整份进炉菜里放在载体上的料（除底料外的 comp）。"""
        base = self.carrier_base(dish)
        if base is None:
            return ()
        return tuple(c for c in self.components(dish) if c != base[0])

    def cooked_of_raw(self, raw):
        # type: (Optional[str]) -> List[str]
        """生料 raw 能煮成哪些熟料（返回配方名，保留原始大小写）。

        反向索引 raw_of：扫描配方表，找 needs_cooking 且 comp[0] == raw 的。
        本关 SushiRice -> ["BoiledSushiRice"]；多灶具关（UncookedFish ->
        FriedFish/PanFriedFish）会返回多个，需配合 cookware 二次区分。
        """
        if not raw:
            return []
        rl = str(raw).lower()
        out = []  # type: List[str]
        for name, r in self._by_name.items():
            if not self.needs_cooking(name):
                continue
            if (self.raw_of(name) or "").lower() == rl:
                out.append(r.get("name") or name)
        return out

    # ---------- 切配 ----------
    def chop_target(self, ing):
        # type: (Optional[str]) -> Optional[str]
        """该生料切好后的产物名；不可切则 None。

        优先读配方叶子节点自带的 chop 字段（新插件把「切成什么/几刀」随
        配方一起下发，跟着食材走、不依赖箱子，传送带供料也覆盖）；旧插件/
        旧帧配方没有该字段时退回箱子的 chop。不做 "Chopped" 前缀匹配
        （spec §7.3）：多阶段切配或产物命名不以 Chopped 开头时前缀匹配会
        误判"已切好"。实测 {"ing":"Cucumber","chop":"ChoppedCucumber","chop_stages":8}。
        """
        if not ing:
            return None
        key = str(ing).lower()
        r = self._by_name.get(key)
        if r is not None and "chop" in r:
            chop = r.get("chop")
            return None if _is_null(chop) else str(chop)
        st = self._crates.get(key)
        if not st:
            return None
        chop = st.get("chop")
        return None if _is_null(chop) else str(chop)

    def chop_stages(self, ing):
        # type: (Optional[str]) -> int
        """需切几轮。缺失给 0（= 不可切）。与 chop_target 同源：优先配方
        字段，旧帧退回箱子。"""
        if not ing:
            return 0
        key = str(ing).lower()
        r = self._by_name.get(key)
        if r is not None and "chop_stages" in r:
            n = r.get("chop_stages")
            try:
                return int(n or 0)
            except (TypeError, ValueError):
                return 0
        st = self._crates.get(key)
        if not st:
            return 0
        n = st.get("chop_stages")
        try:
            return int(n or 0)
        except (TypeError, ValueError):
            return 0

    def needs_chopping(self, ing):
        # type: (Optional[str]) -> bool
        return self.chop_target(ing) is not None

    # ---------- 装盘态词汇 ----------
    def rich_token(self, ing):
        # type: (Optional[str]) -> Optional[str]
        """熟料的 rich token `cooked:{raw}:{cookware}`；非熟料返回 None。

        多料同锅（Pasta_TomatoOnly 的 comp=['Pasta','Tomato']）时把全部
        生料排序后用 `+` 连接，避免与单料 token 混淆、也避免丢料：
        `cooked:pasta+tomato:pot`。

        与 want_tokens 的区别：rich_token 不含 opaque COOKED_PLATE_TOKEN，
        用于「这口锅/这张盘里到底是哪种熟料」的精确判定。
        """
        if not self.needs_cooking(ing):
            return None
        raws = self.raws_of(ing)
        if not raws:
            return None
        raw_key = "+".join(sorted(str(r).lower() for r in raws))
        ck = (self.cookware_of(ing) or "").lower()
        return "cooked:%s:%s" % (raw_key, ck)

    def want_tokens(self, ing):
        # type: (Optional[str]) -> Set[str]
        """订单成分 ing 进盘后可能对应的 token 集（与 plate_tokens 同一套词汇）。

        熟料给两个 token：
          - rich `cooked:{raw}:{cookware}`——detail 帧里盘内容的实际形态，
            能区分同一盘里不同的熟料；
          - opaque COOKED_PLATE_TOKEN——旧帧 contents 里的形态。
        检查「盘里有没有这份料」时两者都要认，否则 detail 帧 / 旧帧会
        各漏一边。非熟料只有小写原名一个 token。
        """
        if not ing:
            return set()
        rich = self.rich_token(ing)
        if rich is not None:
            return {rich, COOKED_PLATE_TOKEN}
        return {str(ing).lower()}

    def plate_has(self, plate, ing):
        # type: (Optional[Dict], Optional[str]) -> bool
        """盘里是否已有订单成分 ing（按装盘态词汇判断）。

        用 want_tokens(ing) ∩ plate_tokens(plate) 判「有没有」：
        - detail 帧里熟料是 rich token `cooked:{raw}:{cookware}`，能区分
          同一盘里两份不同的熟料（如 Pasta_Marinara_New）；
        - 旧帧没有 detail，熟料退回不透明 token，仍按单熟料口径匹配。
        """
        tokens = self.want_tokens(ing)
        if not tokens:
            return False
        return bool(tokens & self.plate_tokens(plate))

    def plate_lacks(self, plate, ing):
        # type: (Optional[Dict], Optional[str]) -> bool
        """plate_has 的反面。与 predicates.plate_lacks 的区别是**走装盘态
        词汇**——后者是字面比较，对熟料永远报缺。"""
        return not self.plate_has(plate, ing)

    # ---------- 盘内容 token（盘↔订单匹配用，spec 每拍 1:1）----------
    def order_want_tokens(self, order):
        # type: (Optional[Dict]) -> Set[str]
        """订单需求的 token 集，与 plate_tokens 同一套词汇。

        就是 want_tokens 对所有顶层成分取并集——熟料同时给 rich token
        （cooked:{raw}:{cookware}）和旧不透明 token（COOKED_PLATE_TOKEN）：
        rich 匹配新插件的 detail，opaque 兼容旧帧。
        """
        want = set()  # type: Set[str]
        for ing in self.requirements(order):
            want |= self.want_tokens(ing)
        return want

    def plate_tokens(self, plate):
        # type: (Optional[Dict]) -> Set[str]
        """盘内容 token 集：优先 detail（新插件，能区分熟料），否则退回
        contents（旧帧，熟料是不透明 token）。

        盘里词汇与订单需求是两套（实测 801 帧）：熟料在 detail 里是
        cooked:{raw}:{cookware}、在旧帧 contents 里是不透明 token；需切的料
        进盘后回落生料名（不是 chop_target）。统一转成 token 后与
        want_tokens / order_want_tokens 做集合比较，字面比较会死循环。
        """
        if not plate:
            return set()
        detail = plate.get("detail")
        if detail:
            return parse_plate_detail(detail)
        return set(P.plate_contents(plate))


def _merge_station_coords(entry, stations):
    # type: (Dict, Optional[List[Dict]]) -> Dict
    """给没有坐标的条目补上 stations[] 里同名条目的坐标与站点身份字段。

    为什么需要：workstations[]（砧板等）只报 n/item/interacting/
    interacters，不报位置；同名的 stations[] 条目才有 grid/gx/gz/x/z/t。而
    Approach 靠 target_cell 解析目标格——拿到没坐标的条目会立即 FAILURE，
    表现为「切好的料摆在砧板上，AI 却站在原地不去捡」的死循环（实机复现）。

    grid / t 也必须拷：缺 grid 会让 resource_id / target_identity 产出
    ('', name) 空网格键，认领与 bad_pairs 按 (grid, name) 比对时错位；缺 t
    会让 _avoid_occupied 把砧板当成散落物、跳过交互位避让。
    """
    if not isinstance(entry, dict):
        return entry
    if entry.get("gx") is not None and entry.get("gz") is not None:
        return entry
    n = entry.get("n")
    if not n:
        return entry
    for st in stations or ():
        if isinstance(st, dict) and st.get("n") == n:
            merged = dict(entry)
            for k in ("grid", "t", "gx", "gz", "x", "z"):
                v = st.get(k)
                if v is not None:
                    merged[k] = v
            return merged
    return entry


def find_ingredient(world, ing, self_name=P.P2_NAME, exclude_plate=False,
                    can_access=None, contents=frozenset()):
    # type: (object, Optional[str], str, bool, object, Any) -> Optional[Dict]
    """定位食材当前位置，返回 {"kind": ..., "st"/"chef": ...}。

    可达来源的 kind 优先级：held（被**自己**拿着）→ board（砧板上）→ plate（已装盘）
    → loose（散落）→ crate（箱子里）。优先级就是"离可用最近"的顺序：
    已经拿着的东西不该再去箱子取一份；P1 手里的不算 held（拿不到）。

    exclude_plate=True 时**跳过盘**继续往下找散落/箱子：盘里的是别的单的
    半成品，不能拿来复用；但"盘上有一份"也不该挡住"箱子里再取一份给另一
    个订单"（实机：order#1 的盘里已有海苔/鱼，order#2 就再也取不到料）。

    **精确名匹配**，不做子串：Cucumber 与 ChoppedCucumber 是两种不同
    的料，宽松匹配会让"去拿生黄瓜"的 task 看到板上的切好黄瓜就以为
    料到手了。生/熟/切好的区分由调用方传对名字。

    **内容也精确匹配**（contents，见 ai.query.carriers）：物品 detail 解析出
    的 token 集必须与 contents 完全相等。缺省空集 = 普通料，包了熟肉的卷饼
    不会被当成空卷饼拿去补蘑菇卷；contents={cooked:burritomeat:fryingpan}
    = 只认包了熟肉的那张。箱子/砧板只出普通料，contents 非空时跳过。

    与 oc/ai/task.py:find_ingredient 的差异：kind=="plate" 时**返回盘
    本身**（旧实现只返回 {"kind":"plate"}）。spec §6.0 要求谓词按绑定
    参数求值——两单都缺黄瓜时，必须能区分料在哪只盘里。
    """
    from ai.query.carriers import held_contents, item_contents
    il = str(ing or "").strip().lower()
    if not il:
        return None
    want = frozenset(contents or ())

    for c in getattr(world, "chefs", None) or ():
        # 只认自己手里的：P1 手里的料 P2 拿不到，跳过继续找箱子/板/散落
        if str(c.get("name") or "").strip().lower() != str(self_name).strip().lower():
            continue
        if P.held_name(c) == il and held_contents(world, c.get("name")) == want:
            return {"kind": "held", "chef": c.get("name")}

    if can_access is None:
        from ai.space import reachability
        route = reachability.context(world, name=self_name)
        can_access = lambda st: reachability.can_interact(route, st)

    for ws in getattr(world, "workstations", None) or ():
        if not want and str(ws.get("item") or "").lower() == il:
            station = _merge_station_coords(ws, getattr(world, "stations", None))
            if can_access(station):
                return {"kind": "board", "st": station}

    if not exclude_plate and not want:
        for p in getattr(world, "plates", None) or ():
            if il in P.plate_contents(p) and can_access(p):
                return {"kind": "plate", "st": p}

    for it in getattr(world, "items", None) or ():
        # 飞行中的料（投掷途中）不算散落来源：坐标是空中的过渡点，走过去
        # 料早已落在别处（trace-20261029-202903 t=231：锁定 x=18.7 的飞行
        # 意面，实际落在 x≈23.6，P2 对空地反复拾取到关卡结束）。
        if it.get("flying") is True:
            continue
        # 内容不符不算：包了肉的卷饼不是「一份卷饼」，拿它去补别的单会把
        # 不相干的料一起倒进盘（trace-20261031-233010）。
        if item_contents(it) != want:
            continue
        if str(it.get("n") or "").lower() == il and can_access(it):
            return {"kind": "loose", "st": it}

    if want:
        return None
    for st in getattr(world, "stations", None) or ():
        if not isinstance(st, dict) or st.get("t") != "crate":
            continue
        if str(st.get("ing") or "").lower() == il and can_access(st):
            return {"kind": "crate", "st": st}
    return None

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
from typing import Dict, List, Optional, Set, Tuple

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

    def __init__(self, recipes, stations=None):
        # type: (Optional[List[Dict]], Optional[List[Dict]]) -> None
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
        comp = order.get("composition")
        if comp:
            return tuple(str(c) for c in comp)
        r = self.recipe(order.get("dish"))
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

        从**箱子**的 chop 字段读，不做 "Chopped" 前缀匹配（spec §7.3）：
        多阶段切配或产物命名不以 Chopped 开头时前缀匹配会误判"已切好"。
        实测 {"ing":"Cucumber","chop":"ChoppedCucumber","chop_stages":8}。
        """
        if not ing:
            return None
        st = self._crates.get(str(ing).lower())
        if not st:
            return None
        chop = st.get("chop")
        return None if _is_null(chop) else str(chop)

    def chop_stages(self, ing):
        # type: (Optional[str]) -> int
        """需切几轮。缺失给 0（= 不可切）。"""
        if not ing:
            return 0
        st = self._crates.get(str(ing).lower())
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
    """给没有坐标的条目补上 stations[] 里同名条目的坐标。

    为什么需要：workstations[]（砧板等）只报 n/item/interacting/
    interacters，不报位置；同名的 stations[] 条目才有 gx/gz/x/z。而
    Approach 靠 target_cell 解析目标格——拿到没坐标的条目会立即 FAILURE，
    表现为「切好的料摆在砧板上，AI 却站在原地不去捡」的死循环（实机复现）。
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
            for k in ("gx", "gz", "x", "z"):
                v = st.get(k)
                if v is not None:
                    merged[k] = v
            return merged
    return entry


def find_ingredient(world, ing, self_name=P.P2_NAME, exclude_plate=False, can_access=None):
    # type: (object, Optional[str], str, bool, object) -> Optional[Dict]
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

    与 oc/ai/task.py:find_ingredient 的差异：kind=="plate" 时**返回盘
    本身**（旧实现只返回 {"kind":"plate"}）。spec §6.0 要求谓词按绑定
    参数求值——两单都缺黄瓜时，必须能区分料在哪只盘里。
    """
    il = str(ing or "").strip().lower()
    if not il:
        return None

    for c in getattr(world, "chefs", None) or ():
        # 只认自己手里的：P1 手里的料 P2 拿不到，跳过继续找箱子/板/散落
        if str(c.get("name") or "").strip().lower() != str(self_name).strip().lower():
            continue
        if P.held_name(c) == il:
            return {"kind": "held", "chef": c.get("name")}

    if can_access is None:
        from ai.space import reachability
        route = reachability.context(world, name=self_name)
        can_access = lambda st: reachability.can_interact(route, st)

    for ws in getattr(world, "workstations", None) or ():
        if str(ws.get("item") or "").lower() == il:
            station = _merge_station_coords(ws, getattr(world, "stations", None))
            if can_access(station):
                return {"kind": "board", "st": station}

    if not exclude_plate:
        for p in getattr(world, "plates", None) or ():
            if il in P.plate_contents(p) and can_access(p):
                return {"kind": "plate", "st": p}

    for it in getattr(world, "items", None) or ():
        if str(it.get("n") or "").lower() == il and can_access(it):
            return {"kind": "loose", "st": it}

    for st in getattr(world, "stations", None) or ():
        if not isinstance(st, dict) or st.get("t") != "crate":
            continue
        if str(st.get("ing") or "").lower() == il and can_access(st):
            return {"kind": "crate", "st": st}
    return None

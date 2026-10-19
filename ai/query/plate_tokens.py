"""盘/锅内容 token 解析（配合插件新输出的 detail / pot_contents 字段）。

插件 `Oc2AIPlugin.cs` 的 `DescribeNode` 把 AssembledDefinitionNode 递归展开成
字符串，三类叶子：

  - `ing:XXX` —— 生料/切配料（IngredientAssembledNode）。
  - `item:XXX` —— 整件物品（ItemAssembledNode）。
  - `CookedCompositeAssembledNode(COOKWARE){ ing:YYY, ... }` —— 熟料，
    COOKWARE 是锅具（Pot/FryingPan/…），花括号里是递归展开的生料。

这里的函数把上述字符串统一成「**盘内容 token**」：
  生料 = 小写原名；熟料 = `cooked:{raw}:{cookware}`。
与 `RecipeBook.order_want_tokens` 输出同一套词汇，供盘↔订单匹配直接做集合比较。
"""
from typing import List, Optional, Set, Tuple

ING_PREFIX = "ing:"
ITEM_PREFIX = "item:"


def parse_pot_contents(pot_contents):
    # type: (Optional[List]) -> Tuple[str, ...]
    """锅/散落物的 pot_contents/detail 里的生料名（剥 ing:/item: 前缀）。

    ["ing:SushiRice"] -> ("sushirice",)
    ["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"] -> ("sushirice",)

    复合节点（熟锅的 pot_contents 就是这个形态）要把花括号里的生料名剥
    出来，否则整串 lower 后 `cooked_of_raw` 反查不到 → 熟锅在
    ``_match_cookers_to_orders`` / ``cooked_sources`` 里被当成无料跳过，
    ``TakeCookedToPlate`` 永不发射（实机复现：米熟了不去端）。
    """
    out = []  # type: List[str]
    for s in pot_contents or ():
        s = str(s).strip()
        if s.startswith(ING_PREFIX):
            out.append(s[len(ING_PREFIX):].lower())
        elif s.startswith(ITEM_PREFIX):
            out.append(s[len(ITEM_PREFIX):].lower())
        elif s and s != "null":
            lb = s.find("{")
            rb = s.find("}", lb + 1)
            if lb != -1 and rb != -1:
                out.extend(_raw_names(s[lb + 1:rb]))
            else:
                out.append(s.lower())
    return tuple(out)


def parse_plate_detail(detail):
    # type: (Optional[List]) -> Set[str]
    """把插件的 detail 展开成规范 token 集。

    ["ing:SushiFish", "CookedCompositeAssembledNode(Pot){ ing:SushiRice }", "ing:Seaweed"]
    -> {"sushifish", "cooked:sushirice:pot", "seaweed"}
    """
    out = set()  # type: Set[str]
    for s in detail or ():
        out.update(_parse_detail_item(str(s)))
    return out


def _parse_detail_item(s):
    # type: (str) -> Set[str]
    s = s.strip()
    if not s or s == "null":
        return set()
    if s.startswith(ING_PREFIX):
        return {s[len(ING_PREFIX):].lower()}
    if s.startswith(ITEM_PREFIX):
        return {s[len(ITEM_PREFIX):].lower()}
    # 复合/熟料节点：TypeName(Cookware){ ing:A, ing:B, ... }
    lp = s.find("(")
    rp = s.find(")", lp + 1)
    lb = s.find("{", rp + 1)
    rb = s.find("}", lb + 1)
    if lp != -1 and rp != -1 and lb != -1 and rb != -1:
        cookware = s[lp + 1:rp].strip().lower()
        raw_names = _raw_names(s[lb + 1:rb])
        # 多料同锅：全部生料排序后 `+` 连接（与 RecipeBook.rich_token 同口径），
        # 单料时退化为 `cooked:{raw}:{cookware}`，与旧帧词汇兼容。
        if raw_names:
            return {"cooked:%s:%s" % ("+".join(sorted(raw_names)), cookware)}
        return {"cooked:%s" % cookware}
    return {s.lower()}


def _raw_names(inner):
    # type: (str) -> List[str]
    """花括号内 `ing:A, item:B` 里的生料名列表。"""
    names = []  # type: List[str]
    for part in inner.split(","):
        p = part.strip()
        if p.startswith(ING_PREFIX):
            names.append(p[len(ING_PREFIX):].lower())
        elif p.startswith(ITEM_PREFIX):
            names.append(p[len(ITEM_PREFIX):].lower())
    return names

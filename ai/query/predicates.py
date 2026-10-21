"""世界查询谓词：全部无副作用。

从 oc/ai/agent.py 抽取。原代码有两个谓词偷偷写状态——`_station()` 写
self.pot / self.plate_pos，`_should_rush_cooked()` 写 self.pot。spec
§11.2 阶段 1 要求改为"纯查询 + 调用点显式写入"，所以这里的函数一律
只读入参、不碰任何外部状态。

入参是快照 dict / List[dict]，不依赖 World 类，这样单测不需要真游戏帧。
"""
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

# 六种锅具（PLAN.md §7.3）。原代码只认 pot/pan，会漏掉后四种（spec §9 待修项）。
POT_KEYWORDS = ("pot", "pan", "fryer", "tray", "tin", "steamer")

PLATE_KEYWORD = "plate"

# 厨师按 name 区分，不是 slot。实测两局共 2204 条 chef 条目，slot 全为 null，
# 旧实现也是用 w.chef("Player 1") 按名字取的。曾用 P1_SLOT/P2_SLOT=1/2 配
# chefs[].slot，导致 chef_by_slot 恒返回 None、p1_holds* 全程失效。
P1_NAME = "Player 1"
P2_NAME = "Player 2"


# ---------- 手持 ----------
def held_name(chef):
    # type: (Optional[Dict]) -> str
    if not chef:
        return ""
    return str(chef.get("held") or "").lower()


def held_matches(chef, item):
    # type: (Optional[Dict], Optional[str]) -> bool
    """手上拿的是否就是目标物（精确名匹配）。生/熟由调用方传对名字。"""
    h = held_name(chef)
    it = str(item or "").lower()
    return bool(h) and bool(it) and h == it


def holds_plate(chef, plate):
    # type: (Optional[Dict], Optional[Dict]) -> bool
    """按盘实例的持有者判断；同名盘不能通过物品名互相冒认。

    新协议显式给出 held_by（含未持有时的 null）。只有旧帧没有该字段
    才沿用按名字匹配，不能拿名字覆盖当前协议的归属事实。
    """
    if not chef or not plate:
        return False
    if "held_by" in plate:
        return bool(plate["held_by"]) and plate["held_by"] == chef.get("name")
    return held_matches(chef, plate.get("name"))


def held_is_pot(chef):
    # type: (Optional[Dict]) -> bool
    h = held_name(chef)
    return any(k in h for k in POT_KEYWORDS)


# 锅具识别兜底关键词。主路径走配方数据（见 cookware_names）——
# spec §9 点名这块必须改成数据驱动：AI_REVIEW.md:78 自标 ⚠️「名字清单
# 未在游戏实测」，而 DeepFatFryer / OvenTray / OvenCakeTin / Steamer
# 四种灶具的名字里既无 pot 也无 pan，换关卡会直接漏。
COOKWARE_KEYWORDS = ("pot", "pan", "fryer", "tray", "tin", "steamer")

# 手持盘绝不是锅具。盘名 equipment_plate_01 不含上述关键词，
# 但配方驱动路径是子串匹配，显式排除一次更稳。
_NOT_COOKWARE = ("plate",)


def cookware_names(recipes):
    # type: (Optional[List[Dict]]) -> Set[str]
    """从配方数据取本关会用到的锅具类型名（小写）。

    recipes[].cook 是锅具类型（"Pot" / "FryingPan" / "DeepFatFryer" /
    "OvenTray" / "OvenCakeTin" / "Steamer"）。插件把"无"写成字符串
    "null" 而不是 JSON null，两种都要拦。
    """
    out = set()  # type: Set[str]
    for r in recipes or ():
        if not isinstance(r, dict):
            continue
        cook = r.get("cook")
        if not cook or str(cook).lower() == "null":
            continue
        out.add(str(cook).lower())
    return out


def cookware_matches(name, want):
    # type: (Optional[str], Optional[str]) -> bool
    """`name`（物品名 / 灶上炊具名）是否对应 `want`（配方 cook 类型）这一种锅具。

    归一化掉非字母数字后做子串匹配：解决 `"fryingpan"`（配方 cook）与
    `"utensil_frying_pan_01"`（物品名）之间下划线导致普通子串匹配失败的问题。
    用于「煮意面只配炖锅、煎西红柿只配煎锅」的灶↔炊具匹配。
    """
    if not name or not want:
        return False
    n = re.sub(r"[^a-z0-9]", "", str(name).lower())
    w = re.sub(r"[^a-z0-9]", "", str(want).lower())
    return bool(w) and w in n


def held_is_cookware(chef, recipes=None):
    # type: (Optional[Dict], Optional[List[Dict]]) -> bool
    """手里拿的是不是锅具（锅 / 平底锅 / 油炸篮 / 烤盘 / 蛋糕模 / 蒸笼）。

    传 recipes 时以配方数据为准（spec §9 要求的数据驱动）；不传时退化
    为关键词表，保证既有调用点行为不变。

    为什么这个谓词必须可靠：spec §6.1 第 5、12 号 task（ReturnPot /
    RecoverLoosePot）用"手持空锅 / 地上有锅"作为**纯观测可行性**，以此
    替代旧代码的 pot_cooked / dropped_cooked_pot 两个记忆字段。替代的
    正确性完全押在这里。
    """
    h = held_name(chef)
    if not h:
        return False
    if any(k in h for k in _NOT_COOKWARE):
        return False
    names = cookware_names(recipes)
    if names:
        return any(n in h for n in names)
    return any(k in h for k in COOKWARE_KEYWORDS)



def held_is_plate(chef):
    # type: (Optional[Dict]) -> bool
    return PLATE_KEYWORD in held_name(chef)


def held_throwable(chef):
    # type: (Optional[Dict]) -> bool
    """手里拿的是否可投掷（插件输出 held_throwable；旧帧无字段 → False）。"""
    if not chef:
        return False
    return chef.get("held_throwable") is True


def item_throwable(item):
    # type: (Optional[Dict]) -> bool
    """散落物是否可投掷（插件输出 items[].throwable；旧帧无字段 → False）。"""
    if not item:
        return False
    return item.get("throwable") is True


def item_on_cooker(item):
    # type: (Optional[Dict]) -> bool
    """物品是否在灶上（插件 items[].on_station 字段，替代坐标判断）。

    on_station 三档："cooking:<灶名>"（在灶上）、"attach:<站点名>"（台面/站点）、
    null（地上/手持）。旧帧无该字段 → False（不排除，退化为全当散落；旧帧已删，
    仅测试 fixture 需要补这个字段）。
    """
    return str((item or {}).get("on_station") or "").startswith("cooking:")


# 手持锅具的内容状态。插件在 items[] 里给每口锅都带 has_contents / cooked
# （实测 27725 帧里手持锅那条跟着厨师的位置走，倒完饭同一拍就翻成
# has_contents=False）——所以"我手里这口锅是空的还是有料的"是**可观测**的。
COOKWARE_EMPTY = "empty"
COOKWARE_RAW = "raw"          # 有料但没熟（在煮 / 刚下锅）
COOKWARE_COOKED = "cooked"
COOKWARE_BURNT = "burnt"      # 糊了（items[].cooked == "Burnt"）
COOKWARE_UNKNOWN = "unknown"  # 是锅具，但 items[] 里找不到它的状态


def held_items(world):
    # type: (Any) -> List[Dict]
    """手持物列表（held_cookware_state 的数据源）。

    真实帧里 World 按 held_by 把手持物拆到 self.held_items（散落物在
    self.items）；测试 FakeWorld 没有 held_items 属性，回退到 self.items——
    旧结构里手持物与散落物混在一起，held_cookware_state 靠"距离最近"选
    手持那条。
    """
    held = getattr(world, "held_items", None)
    if held is not None:
        return held
    return getattr(world, "items", None) or []


def all_plates(world):
    # type: (Any) -> List[Dict]
    """全量盘列表（含手持盘）：按 id 找盘 / 盘↔订单匹配 / 送餐 / pinned 用。

    真实帧里 World 把盘拆成 self.plates（非手持）+ self.held_plates（手持），
    self.all_plates 是两者之和；测试 FakeWorld 没有 all_plates 属性时回退到
    self.plates（旧结构里手持盘与散落盘混在一起）。
    """
    ap = getattr(world, "all_plates", None)
    if ap is not None:
        return ap
    return getattr(world, "plates", None) or []


def held_cookware_state(chef, items, recipes=None):
    # type: (Optional[Dict], Optional[List[Dict]], Optional[List[Dict]]) -> Optional[str]
    """手持锅具的内容状态：不是锅具 → None；否则 empty / raw / cooked /
    burnt / unknown。

    为什么要单独一个谓词：`held_is_cookware` 只回答"手里是不是锅"。拿它
    当"我拿到熟料了"用（`TakeCookedToPlate.feasible` 与它树首 pickup 的
    expect 都这么用）会让**空锅**也满足——AI 于是从不回灶台取料，端着空锅
    反复去盘子倒，实机死循环。task 的落点选择同样要分这个：空锅该回灶台，
    熟锅绝不能上灶台（会重新点火接着烧糊）。

    状态取值（实测）：has_contents=False → cooked 字段是 "Raw"；有熟料时
    cooked="Cooked"；糊了是 "Burnt"（69 个历史 trace 里出现 4861 条）。

    糊锅为什么得单独一档：它既不是"生料锅"（不能回灶台归位——糊料留在
    灶上会接着烧），也不是"熟锅"（不能拿去装盘）。早期实现在这两个分支
    之间把它并进 `raw`，于是落点表把它当生料锅处理：EnsureHandsFree 优先
    挑**空灶**、ReturnPot 也愿意把它带回灶台——火灭之后等于又点一次火。
    """
    if not held_is_cookware(chef, recipes):
        return None
    name = held_name(chef)
    best, best_d = None, None
    for it in items or ():
        if not isinstance(it, dict):
            continue
        if str(it.get("n") or "").lower() != name:
            continue
        x, z = it.get("x"), it.get("z")
        cx, cz = chef.get("x"), chef.get("z")
        if None in (x, z, cx, cz):
            best = it
            break
        d = abs(x - cx) + abs(z - cz)
        if best_d is None or d < best_d:
            best, best_d = it, d
    if best is None:
        return COOKWARE_UNKNOWN
    if not best.get("has_contents"):
        return COOKWARE_EMPTY
    cooked = str(best.get("cooked") or "").lower()
    if cooked == "cooked":
        return COOKWARE_COOKED
    if cooked == "burnt":
        return COOKWARE_BURNT
    return COOKWARE_RAW


def hands_free(chef):
    # type: (Optional[Dict]) -> bool
    return held_name(chef) == ""


# ---------- 盘 ----------
def plate_contents(plate):
    # type: (Optional[Dict]) -> Tuple[str, ...]
    if not plate:
        return ()
    raw = plate.get("contents") or []
    return tuple(str(c).lower() for c in raw)


def plate_count(plate):
    # type: (Optional[Dict]) -> int
    if not plate:
        return 0
    contents = plate.get("contents")
    if contents:
        return len(contents)
    return plate.get("count") or 0


def clean_plate_count(plates):
    # type: (Optional[List[Dict]]) -> int
    """干净空盘数量（count==0 且 contents 空）。"""
    n = 0
    for p in plates or ():
        if plate_count(p) == 0:
            n += 1
    return n


def plate_lacks(plate, ingredient):
    # type: (Optional[Dict], str) -> bool
    return str(ingredient or "").lower() not in plate_contents(plate)


def plate_complete(plate, required):
    # type: (Optional[Dict], Sequence[str]) -> bool
    """盘里是否已含全部必需料。多放了料仍算齐——这里只管"不缺"。"""
    have = plate_contents(plate)
    return all(str(r).lower() in have for r in (required or ()))


# ---------- 锅 ----------
def cooker_for(cookers, station_name):
    # type: (List[Dict], str) -> Optional[Dict]
    for c in cookers or ():
        if station_name_matches(str(c.get("n") or ""), station_name):
            return c
    return None


def cooker_has_pot(cooker):
    # type: (Optional[Dict]) -> bool
    """插件约定：cooked=null → 灶上没锅（m_itemPot 空）；false/true → 有锅。"""
    if not cooker:
        return False
    return cooker.get("cooked") is not None


def cooker_cookware(cooker):
    # type: (Optional[Dict]) -> Optional[str]
    """灶上炊具类型（小写，如 "pot"/"fryingpan"）；无炊具 / 无字段返回 None。

    优先读 `cook_type`（插件 `get_AccessCookingType()`，与配方 `cook` 同一套
    词汇，能做精确匹配）；拿不到时退回 `cookware`（炊具 GameObject 名，如
    `utensil_pot_01`，靠 `cookware_matches` 的子串约定）。旧帧 / 单测 fixture
    两者都没有时返回 None——调用方把 None 当「未知」，不过滤（向后兼容）。
    """
    if not cooker:
        return None
    v = cooker.get("cook_type")
    if v is None or str(v).strip().lower() in ("null", "none", ""):
        v = cooker.get("cookware")
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("null", "none", ""):
        return None
    return s


def cooker_cooked(cooker):
    # type: (Optional[Dict]) -> bool
    """只有明确 True 才算熟。空锅的 cooked=False 不能误判为熟。"""
    return bool(cooker) and cooker.get("cooked") is True


def cooker_burning(cooker):
    # type: (Optional[Dict]) -> bool
    if not cooker:
        return False
    return cooker.get("burning") is True or cooker.get("onFire") is True


def cooker_started(cooker):
    # type: (Optional[Dict]) -> bool
    """已下锅在煮，或已经煮熟（熟了也说明启动过）。"""
    if not cooker:
        return False
    return cooker.get("isCooking") is True or cooker.get("cooked") is True


# ---------- 站点名匹配 ----------
_INSTANCE_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")


def _instance_id(name):
    # type: (str) -> str
    """取 Unity 实例后缀 " (n)"，没有则返回 ""。

    关卡里同类站点被命名为 "workstation_cooker_01"、"...(2)"、"...(3)"，
    它们是**不同的物理站点**，必须区分。
    """
    m = _INSTANCE_SUFFIX.search(name)
    return m.group(0).strip() if m else ""


def station_name_matches(a, b):
    # type: (Optional[str], Optional[str]) -> bool
    """站点名匹配：容忍父节点前缀差异，但绝不跨实例。

    插件对同一站点在不同数组里可能报不同层级的名字（带/不带父节点前缀），
    所以需要比"完全相等"宽松一点。但原先的"互为子串"过宽：
    "workstation_cooker_01" in "workstation_cooker_01 (3)" 为真，导致查
    1 号灶拿到 3 号灶的状态（已在真实帧上复现）。

    规则：
    1. 两边的实例后缀 " (n)" 必须一致（都没有，或完全相同）；
    2. 去掉实例后缀后，要求相等，或一方是另一方的**路径段级**后缀
       （即在 "/" 或 "." 边界处结束），而不是任意子串。
    """
    a = str(a or "")
    b = str(b or "")
    if not a or not b:
        return False
    if a == b:
        return True
    # 1. 实例后缀必须一致——不同实例是不同站点
    if _instance_id(a) != _instance_id(b):
        return False
    a_base = _INSTANCE_SUFFIX.sub("", a)
    b_base = _INSTANCE_SUFFIX.sub("", b)
    if a_base == b_base:
        return True
    # 2. 允许父节点前缀差异：短的那个须是长的在段边界处的后缀
    short, long = (a_base, b_base) if len(a_base) <= len(b_base) else (b_base, a_base)
    if not short:
        return False
    if long.endswith(short):
        boundary = long[: -len(short)]
        return boundary.endswith("/") or boundary.endswith(".")
    return False


# ---------- 板 ----------
def board_item(workstations, station_name):
    # type: (List[Dict], str) -> str
    for ws in workstations or ():
        if station_name_matches(str(ws.get("n") or ""), station_name):
            return str(ws.get("item") or "").lower()
    return ""


def board_chopped(workstations, station_name):
    # type: (List[Dict], str) -> bool
    return "chopped" in board_item(workstations, station_name)


# ---------- 厨师 / P1 ----------
def chef_by_name(chefs, name):
    # type: (List[Dict], str) -> Optional[Dict]
    """按 name 取厨师（"Player 1" / "Player 2"）。

    真实帧里 chefs[].slot 全为 null，只有 name 可用；旧实现同样是
    w.chef("Player 1")。名字比较忽略大小写与首尾空白，容忍插件侧微小差异。
    """
    want = str(name or "").strip().lower()
    if not want:
        return None
    for c in chefs or ():
        if str(c.get("name") or "").strip().lower() == want:
            return c
    return None


def chef_by_slot(chefs, slot):
    # type: (List[Dict], Any) -> Optional[Dict]
    """已废弃：真实帧没有 slot 字段。保留只为兼容旧调用点。

    接受 1/2 或 "Player 1"/"Player 2"，一律转为按 name 查找。
    新代码请直接用 chef_by_name。
    """
    if slot == 1 or slot == P1_NAME:
        return chef_by_name(chefs, P1_NAME)
    if slot == 2 or slot == P2_NAME:
        return chef_by_name(chefs, P2_NAME)
    return chef_by_name(chefs, slot)


def p1_holds(chefs, keyword):
    # type: (List[Dict], str) -> bool
    """P1 手里是否拿着含该关键字的物品（协作避让用）。"""
    p1 = chef_by_name(chefs, P1_NAME)
    return p1 is not None and str(keyword or "").lower() in held_name(p1)


def p1_holds_plate(chefs):
    # type: (List[Dict]) -> bool
    return p1_holds(chefs, PLATE_KEYWORD)


# ---------- 交互前置校验（spec §7.5）----------

PLATE_STATION_KEYWORD = "plate_station"
_EXT_KEYWORD = "extinguish"
_HANDLE_KEYS = ("interactable", "pickup", "placement", "grid")


def chef_cell(chef, to_grid):
    # type: (Optional[Dict], Any) -> Optional[Tuple[int, int]]
    """厨师所在格。to_grid 是 (x, z) -> (gx, gz) 的回调（World.to_grid）。"""
    if not chef:
        return None
    x, z = chef.get("x"), chef.get("z")
    if x is None or z is None:
        return None
    return to_grid(x, z)


def facing_cell(chef):
    # type: (Optional[Dict]) -> Optional[Tuple[int, int]]
    """厨师朝向的那一格，由 interactions.gx/gz 给出。

    **不从 yaw 推**：实测 801 帧里 yaw 与交互格的对应关系不稳定
    （yaw=180 时交互格 706 次在 (+1,0)、60 次在 (-1,0)），yaw 是渲染
    朝向而非交互判据。interactions.gx/gz 是游戏自己算出的交互目标，
    1367/1602 帧非空，这才是"面朝哪"的权威信号。
    """
    if not chef:
        return None
    inter = chef.get("interactions")
    if not isinstance(inter, dict):
        return None
    gx, gz = inter.get("gx"), inter.get("gz")
    if gx is None or gz is None:
        return None
    return (int(round(gx)), int(round(gz)))


def _target_cell(target, to_grid):
    # type: (Optional[Dict], Any) -> Tuple[Optional[int], Optional[int]]
    """目标格：优先 gx/gz，缺失时用 to_grid 从世界坐标换算。

    **与 primitives/base.py 的 target_cell 刻意不同**（Task 19 实机回归
    发现，trace-20261016-122733：篮子料箱永远拿不到料）：`interact_ready`
    比对的是游戏自己上报的 `interactions.pickup_gx/gz` 等句柄字段，而这些
    字段是游戏按**该站点自身所在网格**的局部坐标系给的（气球关料箱
    局部 (2,-1) 恒定，不管篮子摇到哪个世界位置）——不是我们 Python 侧
    "主网格" 的坐标系。所以这里必须优先用站点自带的 gx/gz 跟游戏上报的
    局部坐标直接比对；若改成先把 x/z 投影到主网格坐标系（像
    target_cell 那样），会产出一个游戏永远不会上报的值（主网格下的
    (10,-1) vs 游戏报的局部 (2,-1)），`interact_ready` 从此恒为 False，
    站在料箱正前方也拿不到料。

    `target_cell`（primitives/base.py）服务的是 Approach/distance_field，
    那边需要的是"这个目标在主网格坐标系里是哪一格"，用世界坐标投影是对的；
    这里服务的是"游戏说的局部网格坐标是否等于我认为的局部网格坐标"，两者
    不是同一个问题，不能共用同一个优先级。
    """
    if not target:
        return (None, None)
    # 台面盘/锅没有自己的 GridLocation，执行层可提供同位置容器的原生坐标。
    ix, iz = target.get('interaction_gx'), target.get('interaction_gz')
    if ix is not None and iz is not None:
        return (int(round(ix)), int(round(iz)))
    gx, gz = target.get("gx"), target.get("gz")
    if gx is not None and gz is not None:
        return (int(round(gx)), int(round(gz)))
    if to_grid is None:
        return (None, None)
    x, z = target.get("x"), target.get("z")
    if x is None or z is None:
        return (None, None)
    cell = to_grid(x, z)
    return (cell[0], cell[1])


def cell_center_offset(chef, cell, to_world):
    # type: (Optional[Dict], Optional[Tuple[int, int]], Any) -> Optional[float]
    """角色到格 `cell` 中心的平面距离（世界单位）。缺坐标返回 None。

    服务 `AlignToCell`：偏离格心会让相邻的其它可交互物抢走 placement 句柄
    （详见 align_epsilon 的注释），这个量就是「偏得有多远」。
    """
    if not chef or cell is None or to_world is None:
        return None
    x, z = chef.get("x"), chef.get("z")
    if x is None or z is None:
        return None
    cx, cz = to_world(cell[0], cell[1])
    return math.hypot(x - cx, z - cz)


def align_epsilon(cell_size):
    # type: (float) -> float
    """格心对齐容差：偏离超过 1/4 格就先收紧站位。

    实机依据（trace-20261016-220409 t=9523）：P2 在格 (1,-4) 偏离格心
    (-0.30, -0.17) 时，游戏的 placement 句柄被 1.07 vs 1.12 之差（1.4%）
    的出餐口抢走，放不到正对着的台面；而把坐标换成格心 (15.6,-10.8) 后，
    90°/116°/130° 三个朝向全部是台面赢。

    所以 cell=1.2 时取 0.3 —— 比实测失效点（0.30）保守一档。写成格宽的
    函数而不是常数 0.3：换关卡格宽可能不同（geometry 的 cell 来自
    grid.size，不保证 1.2），写死会在 cell=0.8 的关卡变成 3/8 格。

    **这个值只有一个实机样本**，标定状态见
    docs/superpowers/specs/2026-10-16-interact-handle-feedback-design.md §6。
    """
    return cell_size / 4.0


def interact_ready(chef, target, kind=None, to_grid=None):
    # type: (Optional[Dict], Optional[Dict], Optional[str], Any) -> bool
    """"现在能跟 target 交互吗"——用游戏自己的交互信号判断。

    判定顺序：名字优先（句柄名 = 对象身份，FindNearbyObjects 权威结果），
    坐标兜底（句柄专属网格 pickup_gx/gz / placement_gx/gz，或通用 gx/gz）。
    名字优先是因为站点的 gx/gz 可能算错（WashingPart 报 gz=2 而非 -4，实机
    trace-20261020-223413），而句柄报的名字不受坐标系影响、也涵盖篮子本地坐标。

    从 oc/ai/agent.py:1092 移植，两处改动：
      1. 不接 World，to_grid 作为可选回调注入 → 纯函数，单测不造 World。
      2. 名匹配兜底改走 station_name_matches。旧实现用"互为子串"，
         "workstation_cooker_01" in "workstation_cooker_01 (3)" 为真，
         查 1 号灶会拿到 3 号灶的状态（Plan 1 已在真实帧复现）。

    target 为 None / 空（动作中途被 P1 端走）一律 False，不抛异常——
    spec §7.6 纪律 1：失败交还 Planner，不在执行层写恢复逻辑。
    """
    if not chef or not target:
        return False
    inter = chef.get("interactions")
    if not isinstance(inter, dict):
        return False

    tgx, tgz = _target_cell(target, to_grid)
    tn = str(target.get('interaction_surface') or target.get("n") or "").strip()

    # 放置句柄指向出餐口而目标不是出餐口 → 拒绝（防盘子误放上菜口）
    if kind == "placement":
        pl = str(inter.get("placement") or "").lower()
        if PLATE_STATION_KEYWORD in pl and PLATE_STATION_KEYWORD not in tn.lower():
            return False

    # 名字优先：句柄报的名字就是对象身份（FindNearbyObjects 的权威结果），
    # 不受坐标系影响——主网格站点 gz 坏值（WashingPart 报 gz=2 而非 -4）与
    # 篮子本地坐标都成立。按 kind 只比对对应句柄名，避免「面向 A 但放置句柄
    # 被邻近 B 抢走」时误放行。
    if tn:
        if kind in ("pickup", "placement"):
            hname = str(inter.get(kind) or "").strip()
            if hname and station_name_matches(hname, tn):
                return True
        else:
            for key in ("grid", "interactable"):
                if station_name_matches(str(inter.get(key) or "").strip(), tn):
                    return True

    # 坐标兜底（放错格仍要拦）：句柄专属网格最精确。
    if kind in ("pickup", "placement"):
        hgx = inter.get("%s_gx" % kind)
        hgz = inter.get("%s_gz" % kind)
        if None not in (hgx, hgz, tgx, tgz):
            return int(round(hgx)) == tgx and int(round(hgz)) == tgz

    # 通用交互网格
    igx, igz = inter.get("gx"), inter.get("gz")
    if None not in (igx, igz, tgx, tgz):
        return int(round(igx)) == tgx and int(round(igz)) == tgz

    # 兜底：名匹配（无网格信号时，如动态物 / 灭火器，句柄名可能报在任一字段）
    if not tn:
        return False
    for key in _HANDLE_KEYS:
        if station_name_matches(str(inter.get(key) or "").strip(), tn):
            return True
    # 灭火器名可能带 (n) 后缀或 _Rigidbody 变体，保留一个窄的关键字兜底
    if _EXT_KEYWORD in tn.lower():
        for key in _HANDLE_KEYS:
            if _EXT_KEYWORD in str(inter.get(key) or "").lower():
                return True
    return False


# ---------- 资源 id 构造（§5.2）----------

def resource_id(station):
    # type: (Optional[Dict]) -> Tuple[str, str]
    """站点的认领资源 id（spec §5.2）：(网格名, 实例名)。

    取代"站点用实例名 n"的旧规则——同名站点出现在不同网格（如气球关左右
    各一张 countertop_01_standard_dark_wood (4)）时，旧规则会共享认领键、
    互相误锁（实测，§4.1/§5.2）。grid 字段缺失（旧协议帧）退化为空字符串，
    不是 None，保证旧 fixture 下 key 仍确定。
    """
    if not station:
        return ("", "")
    return (str(station.get("grid") or ""), str(station.get("n") or ""))


def resource_id_for(grid_name, instance_name):
    # type: (Any, Any) -> Tuple[str, str]
    """直接从 (grid, name) 构造资源 id，供已知两个值分开传的调用点使用。"""
    return (str(grid_name or ""), str(instance_name or ""))


def on_moving_platform(grid, ctx):
    # type: (Any, Dict) -> bool
    """grid 是否在「正在移动的非主网格平台」（气球篮）上，且角色不在其上。

    方案 B 的共享判据：篮子移动时，任何需要跨到篮子的资源（料箱、水池、
    台面等）都判不可达，planner 据此把对应 task 否决，而不是选出来再执行
    失败。主网格由 stations[].grid 众数判定（World._majority_grid）。
    角色已经站在篮子上时放行——它随篮子一起动，仍能操作篮子上的站点。
    """
    if not grid:
        return False
    bb = ctx.get("bb")
    if not getattr(bb, "balloon_moving", False):
        return False
    w = ctx.get("w")
    primary = w._majority_grid() if hasattr(w, "_majority_grid") else None
    if primary is None or str(grid) == primary:
        return False
    chef = ctx.get("chef")
    if chef is not None and hasattr(w, "balloon_cells"):
        cell = chef_cell(chef, w.to_grid)
        if cell is not None and cell in w.balloon_cells():
            return False
    return True


"""世界查询谓词：全部无副作用。

从 oc/ai/agent.py 抽取。原代码有两个谓词偷偷写状态——`_station()` 写
self.pot / self.plate_pos，`_should_rush_cooked()` 写 self.pot。spec
§11.2 阶段 1 要求改为"纯查询 + 调用点显式写入"，所以这里的函数一律
只读入参、不碰任何外部状态。

入参是快照 dict / List[dict]，不依赖 World 类，这样单测不需要真游戏帧。
"""
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


# 手持锅具的内容状态。插件在 items[] 里给每口锅都带 has_contents / cooked
# （实测 27725 帧里手持锅那条跟着厨师的位置走，倒完饭同一拍就翻成
# has_contents=False）——所以"我手里这口锅是空的还是有料的"是**可观测**的。
COOKWARE_EMPTY = "empty"
COOKWARE_RAW = "raw"          # 有料但没熟（在煮 / 刚下锅）
COOKWARE_COOKED = "cooked"
COOKWARE_BURNT = "burnt"      # 糊了（items[].cooked == "Burnt"）
COOKWARE_UNKNOWN = "unknown"  # 是锅具，但 items[] 里找不到它的状态


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
    """目标格：优先 gx/gz，缺失时用 to_grid 从世界坐标换算。"""
    if not target:
        return (None, None)
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


def interact_ready(chef, target, kind=None, to_grid=None):
    # type: (Optional[Dict], Optional[Dict], Optional[str], Any) -> bool
    """"现在能跟 target 交互吗"——用游戏自己的交互信号判断。

    kind=None 用通用网格 interactions.gx/gz；kind="pickup"/"placement"
    优先用对应句柄的网格（pickup_gx/gz、placement_gx/gz），这是
    AGENTS.md §6 称的"能否交互的最精确信号"。

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
    tn = str(target.get("n") or "").strip()

    # 放置句柄指向出餐口而目标不是出餐口 → 拒绝（防盘子误放上菜口）
    if kind == "placement":
        pl = str(inter.get("placement") or "").lower()
        if PLATE_STATION_KEYWORD in pl and PLATE_STATION_KEYWORD not in tn.lower():
            return False

    # 句柄专属网格（最精确）
    if kind in ("pickup", "placement"):
        hgx = inter.get("%s_gx" % kind)
        hgz = inter.get("%s_gz" % kind)
        if None not in (hgx, hgz, tgx, tgz):
            return int(round(hgx)) == tgx and int(round(hgz)) == tgz

    # 通用交互网格
    igx, igz = inter.get("gx"), inter.get("gz")
    if None not in (igx, igz, tgx, tgz):
        return int(round(igx)) == tgx and int(round(igz)) == tgz

    # 兜底：名匹配（无网格信号时，如动态物 / 灭火器）
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


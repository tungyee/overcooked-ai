# -*- coding: utf-8 -*-
"""分工表：动作/物料 → 谁负责。

分工的唯一写入入口是 LLM 的 `set_division` 工具；主循环不再自己猜。
本模块只做三件事：

- `DivisionState`：存 `(动作, 物料)` 的归属，编译成 planner 的 `Preference`；
- `station_signature` / `reach_signature`：动态地图复核用的两级签名；
- `validate_entry`：工具落地的物理校验（动作/物料/可达性）。

送餐和洗盘不在分工里：它们谁近谁做，交给 planner 自由选。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ai.dialog.actions import (ACTION_STATION_TYPES, ACTION_TOOL_NAMES,
                               DIVISION_ACTIONS, TOOL_ACTION_NAMES,
                               action_scope)
from ai.dialog.schema import Preference
from ai.query import predicates as P

OWNER_YOU = "P2"
OWNER_PLAYER = "P1"
OWNER_BOTH = "BOTH"

OWNER_TOOL_NAMES = {
    OWNER_YOU: "you",
    OWNER_PLAYER: "player",
    OWNER_BOTH: "both",
}  # type: Dict[str, str]
TOOL_OWNER_NAMES = {v: k for k, v in OWNER_TOOL_NAMES.items()}

# 分工只做「别抢玩家的活」，**不给自己加分**：自己的活本来就不会被玩家的
# 负分挡住；加分反而让它压过更值钱但没分给任何人的事（组装/送餐/救锅），
# 实机 trace-20261109-124818 里 55% 的时间都在切菜、披萨组装不起来。
WEIGHT_CLAIM = -6.0      # player 负责：P2 避开，订单要崩时仍可补位


def _key(action, item=None):
    # type: (str, Optional[str]) -> str
    action = str(action)
    item = str(item or "").strip().lower()
    return "%s:%s" % (action, item) if item else action


@dataclass(frozen=True)
class Entry(object):
    """一条分工：某个动作（可限定物料）归谁。"""
    action: str              # 中文动作名（切/下锅/拌/装盘）
    owner: str               # P2 / P1 / BOTH
    item: str = ""           # 小写物料名；空 = 动作级

    @property
    def key(self):
        # type: () -> str
        return _key(self.action, self.item)

    def tool_action(self):
        # type: () -> str
        return ACTION_TOOL_NAMES.get(self.action, self.action)


@dataclass
class DivisionState(object):
    """当前分工；顺序即最后一次写入顺序（同 key 覆盖）。"""
    entries: List[Entry] = field(default_factory=list)

    def set(self, action, owner, item=""):
        # type: (str, str, str) -> bool
        """写入一条；同 (action, item) 覆盖。返回是否真的变了。"""
        entry = Entry(str(action), str(owner), str(item or "").strip().lower())
        for index, old in enumerate(self.entries):
            if old.key == entry.key:
                if old == entry:
                    return False
                self.entries[index] = entry
                return True
        self.entries.append(entry)
        return True

    def clear(self, action=None, item=None):
        # type: (Optional[str], Optional[str]) -> bool
        """清一条/一类/全部。返回是否有改动。"""
        before = list(self.entries)
        if action is None:
            self.entries = []
            return bool(before)
        action = str(action)
        item_l = str(item or "").strip().lower()
        kept = []  # type: List[Entry]
        for entry in self.entries:
            if entry.action != action:
                kept.append(entry)
            elif item is not None and entry.item != item_l:
                kept.append(entry)
            # item is None：删掉这个动作的所有条目（含物料级）
        self.entries = kept
        return len(kept) != len(before)

    def clear_all(self):
        # type: () -> bool
        return self.clear()

    def snapshot(self):
        # type: () -> Dict[str, List[str]]
        """给 prompt / trace 的分工视图。"""
        out = {"you": [], "player": [], "both": []}  # type: Dict[str, List[str]]
        for entry in self.entries:
            bucket = out.get(OWNER_TOOL_NAMES.get(entry.owner, "you"))
            if bucket is None:
                continue
            label = entry.tool_action()
            if entry.item:
                label += ":%s" % (entry.item,)
            bucket.append(label)
        return out

    def render(self):
        # type: () -> str
        """一行中文说明；没分工时返回空串。"""
        parts = []  # type: List[str]
        snap = self.snapshot()
        for owner, label in (("you", "你"), ("player", "玩家"), ("both", "两人")):
            actions = snap.get(owner) or []
            if actions:
                parts.append("%s负责 %s" % (label, "、".join(actions)))
        return "；".join(parts)

    def preferences(self):
        # type: () -> List[Preference]
        """编译成 planner 的偏好：只给玩家负责的动作扣分。

        分给你自己的动作**不加分**（见 WEIGHT_CLAIM 上方注释）；BOTH 两边都
        不产生偏好。
        """
        out = []  # type: List[Preference]
        for entry in self.entries:
            if entry.owner != OWNER_PLAYER:
                continue
            out.append(Preference(kind="claim", owner=OWNER_PLAYER,
                                  weight=WEIGHT_CLAIM,
                                  scope=action_scope(entry.action,
                                                     entry.item or None)))
        return out

    def copy(self):
        # type: () -> "DivisionState"
        return DivisionState(list(self.entries))


def diff_entries(old, new):
    # type: (DivisionState, DivisionState) -> List[Dict[str, Any]]
    """两条分工表的差异；给 review 事件 / 硬静音判断用。"""
    old_map = {entry.key: entry for entry in (old.entries if old else [])}
    new_map = {entry.key: entry for entry in (new.entries if new else [])}
    out = []  # type: List[Dict[str, Any]]
    for key in sorted(set(old_map) | set(new_map)):
        before = old_map.get(key)
        after = new_map.get(key)
        if before == after:
            continue
        out.append({
            "action": (after or before).tool_action(),
            "item": (after or before).item or None,
            "from": OWNER_TOOL_NAMES.get(before.owner) if before else None,
            "to": OWNER_TOOL_NAMES.get(after.owner) if after else None,
        })
    return out


def tool_actions(stations):
    # type: (Any) -> List[str]
    """本关可分工的英文动作名；没有对应站点的不进 enum，送餐/洗盘永不进。"""
    present = {str(s.get("t")) for s in (stations or ())
               if isinstance(s, dict)}
    return [ACTION_TOOL_NAMES[action] for action in DIVISION_ACTIONS
            if ACTION_STATION_TYPES[action] in present]


def _q(value, scale=1.0):
    # type: (Any, float) -> Any
    try:
        return int(round(float(value) / scale))
    except (TypeError, ValueError):
        return None


def _moving_grids(world):
    # type: (Any) -> set
    """插件帧里 moving=true 的网格 id；旧帧没有 grids 时返回空集。"""
    raw = getattr(world, "raw", None) or {}
    return {str(g.get("id")) for g in raw.get("grids") or ()
            if isinstance(g, dict) and g.get("moving") is True}


def station_signature(world):
    # type: (Any) -> Tuple
    """站点拓扑签名：站点所属网格 + 量化位置。

    梯子/转盘这类移动障碍不改变站点，签名不变；热气球篮/木筏把站点
    带走时签名变化。**移动网格上的站点只看局部 gx/gz**——木筏巡航会
    让世界坐标每帧变，按 x/z 比会每 160 拍误发一次 division_review
    （trace-20261109-221602：225 秒里 24 次）。
    """
    moving = _moving_grids(world)
    parts = []  # type: List[Tuple]
    for action in DIVISION_ACTIONS:
        station_type = ACTION_STATION_TYPES[action]
        for station in getattr(world, "stations", None) or ():
            if not isinstance(station, dict) or station.get("t") != station_type:
                continue
            grid = str(station.get("grid") or "")
            # 3-5 的出餐口挂在移动出餐船上，grid=null，gx/gz 和 x/z 都随船漂；
            # 它不是分工资源，整条从签名里去掉，否则每帧都判「站点搬家」。
            if station_type == "plate_station" and not grid and moving:
                parts.append((action, grid))
                continue
            row = (action, grid,
                   _q(station.get("gx"), 1.2),
                   _q(station.get("gz"), 1.2))
            if grid not in moving:
                row += (_q(station.get("x"), 1.2),
                        _q(station.get("z"), 1.2))
            parts.append(row)
    return tuple(sorted(parts, key=repr))


def reach_signature(world, bb):
    # type: (Any, Any) -> Tuple
    """每个可分工动作的 (P1可达, P2可达)，仅诊断用。

    单网格寻路对 3-3 左半场/旋转桌不准，不能拿它决定分工复核；
    动态复核只看站点拓扑签名。
    """
    from ai.dialog.help import cost_to

    out = []  # type: List[Tuple[str, bool, bool]]
    for action in DIVISION_ACTIONS:
        station_type = ACTION_STATION_TYPES[action]
        stations = [s for s in (getattr(world, "stations", None) or ())
                    if isinstance(s, dict) and s.get("t") == station_type]
        if not stations:
            continue
        p1 = any(cost_to(world, bb, P.P1_NAME, s) is not None
                 for s in stations)
        p2 = any(cost_to(world, bb, P.P2_NAME, s) is not None
                 for s in stations)
        out.append((action, bool(p1), bool(p2)))
    return tuple(sorted(out))


def _reachable(world, bb, owner, action):
    # type: (Any, Any, str, str) -> bool
    from ai.dialog.help import cost_to

    station_type = ACTION_STATION_TYPES[action]
    stations = [s for s in (getattr(world, "stations", None) or ())
                if isinstance(s, dict) and s.get("t") == station_type]
    if not stations:
        return False
    names = [P.P2_NAME] if owner == OWNER_YOU else [P.P1_NAME]
    if owner == OWNER_BOTH:
        names = [P.P1_NAME, P.P2_NAME]
    return any(cost_to(world, bb, name, s) is not None
               for name in names for s in stations)


def _known_materials(kitchen, world=None):
    # type: (Any, Any) -> Dict[str, str]
    """本关可认领的物料名：{小写: 原始写法}。

    不能只看当前订单 DAG：开局订单轮换，4-2 先刷到沙拉时 DAG 里只有
    生菜/番茄，玩家那侧的土豆/鸡块会被误判「本关没有这个物料」
    （voice-20261110-221304）。料箱是关卡静态事实，全部并进来。
    """
    known = {}  # type: Dict[str, str]

    def add(name):
        # type: (Any) -> None
        text = str(name or "").strip()
        if text:
            known.setdefault(text.lower(), text)

    for name, material in (getattr(kitchen, "materials", None) or {}).items():
        add(name)
        for item in getattr(material, "items", None) or ():
            add(item)
    for st in getattr(world, "stations", None) or ():
        if not isinstance(st, dict) or st.get("t") != "crate":
            continue
        add(st.get("ing"))
    return known


def resolve_material(world, kitchen, item):
    # type: (Any, Any, Any) -> Any
    """把模型写的物料名归到本关料箱的真实名（只做唯一匹配）。

    例：4-2 玩家说「鸡块」，模型写 `Chicken`，而这关料箱是
    `NuggetChicken`（`Chicken` 是另一关披萨的叶子料）。`chicken` 是
    `NuggetChicken` 的子串且料箱里唯一，就归一到后者。匹配不唯一时
    原样返回，交给 `validate_entry` 报「本关没有物料 + 可用列表」。
    """
    text = str(item or "").strip()
    if not text:
        return item
    known = _known_materials(kitchen, world)
    if text.lower() in known:
        return known[text.lower()]
    crates = []  # type: List[str]
    for st in getattr(world, "stations", None) or ():
        if isinstance(st, dict) and st.get("t") == "crate" and st.get("ing"):
            crates.append(str(st["ing"]))
    matches = sorted({c for c in crates if text.lower() in c.lower()})
    if len(matches) == 1:
        return matches[0]
    return text


def validate_entry(world, bb, kitchen, book, action_tool, owner_tool,
                   item=None):
    # type: (Any, Any, Any, Any, str, str, Optional[str]) -> Tuple[bool, str, str]
    """校验一条工具写入。返回 (ok, reason, warning)。

    - 动作/物料/归属不合法 → ok=False；
    - 结构上可行、但此刻够不着 → ok=True + warning（动态地图可能只是临时挡路）。
    """
    action = TOOL_ACTION_NAMES.get(str(action_tool or "").strip())
    if action not in DIVISION_ACTIONS:
        return False, "本关不开放这个分工动作：%s" % (action_tool,), ""
    owner = TOOL_OWNER_NAMES.get(str(owner_tool or "").strip())
    if owner is None:
        return False, "owner 只能是 you / player / both", ""
    station_type = ACTION_STATION_TYPES[action]
    stations = [s for s in (getattr(world, "stations", None) or ())
                if isinstance(s, dict) and s.get("t") == station_type]
    if not stations:
        return False, "本关没有%s这个动作" % (action,), ""

    item_l = str(item or "").strip().lower()
    if item_l:
        known = _known_materials(kitchen, world)
        if known and item_l not in known:
            hint = "；可用：%s" % "、".join(sorted(known.values())) if known else ""
            return False, "本关没有物料 %s%s" % (item, hint), ""
        if action == "切" and book is not None:
            try:
                needed = bool(book.needs_chopping(item))
            except Exception:                # noqa: BLE001
                needed = True
            if not needed:
                return False, "%s 不需要切" % (item,), ""
        if action == "下锅" and book is not None:
            try:
                needed = bool(book.needs_cooking(item))
            except Exception:                # noqa: BLE001
                needed = True
            if not needed:
                return False, "%s 不需要下锅" % (item,), ""

    if not _reachable(world, bb, owner, action):
        who = {"P2": "你", "P1": "玩家", "BOTH": "两人"}.get(owner, owner)
        return True, "", "%s现在够不着%s的站点（可能只是暂时挡路）" % (
            who, action)
    return True, "", ""

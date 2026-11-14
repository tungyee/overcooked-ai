# -*- coding: utf-8 -*-
"""本关目录（Kitchen / DAG 物料目录）。

- `Kitchen`：把订单 DAG 压成「菜 = 物料组合」与物料处理顺序，渲染给 LLM；
- `TaskMeta` / `Material` / `node_key`：目录的元数据与键构造。

分工账本已删除：分工只由 LLM 的 `set_division` 工具写入 `bb.division`。
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ai.dialog.actions import ACTION_STATION_TYPES, ACTION_TASK_TYPES


def node_key(task_type, item=None):
    # type: (str, Optional[str]) -> str
    return task_type if not item else "%s:%s" % (task_type, item)


@dataclass(frozen=True)
class TaskMeta(object):
    """task 类上与分工目录有关的元数据（调用方从 `T.TASK_TYPES` 汇总）。"""
    desc: str = ""
    label: str = ""             # 步骤短名（「切」「下锅」）
    process: str = ""           # 决定物料加工方式时：切 / 煮 / 拌
    shared: bool = False
    station: Optional[str] = None
    # 救援类（L1/L2 免疫认领负分）：认领时要在结果里提醒，planner 仍会接手
    rescue: bool = False


@dataclass
class Material(object):
    """一种物料：DAG 根下的一个直接成分及其整棵子树。"""
    name: str
    items: set = field(default_factory=set)    # 子树里出现的 item（小写）
    types: set = field(default_factory=set)    # 子树里出现的 task 类型
    # 渲染用：(task 类型, item) 先做的在前
    steps: List[Tuple[str, Any]] = field(default_factory=list)


class Kitchen(object):
    """本关见过的菜的物料目录；按需重建（便宜，纯数据）。"""

    def __init__(self, dags, meta=None, stations=()):
        # type: (Dict[str, Dict], Optional[Dict[str, TaskMeta]], Iterable[str]) -> None
        self.dags = dict(dags)          # 菜名 → DAG 根节点（按首次出现顺序）
        self.meta = dict(meta or {})    # task 类型 → TaskMeta
        self.stations = set(stations or ())   # 本关站点类型（frame stations[].t）
        self.materials = {}             # type: Dict[str, Material]
        self.dish_materials = {}        # type: Dict[str, List[str]]
        for dish, root in self.dags.items():
            # 根是共享的送餐节点时，物料是它的子节点；否则根本身就是物料
            tops = (root.get("children") or ()) \
                if self.is_shared(root.get("type")) else (root,)
            names = []  # type: List[str]
            for node in tops:
                name = node.get("item")
                if not name:
                    continue
                name = str(name)
                mat = self.materials.setdefault(name, Material(name))
                steps = []  # type: List[str]
                self._walk(node, name, mat, steps)
                if not mat.steps:
                    mat.steps = steps
                if name not in names:
                    names.append(name)
            self.dish_materials[dish] = names

    @classmethod
    def from_orders(cls, book, orders, compile_node, meta=None, stations=()):
        # type: (Any, Iterable[dict], Callable[[Any, str], Optional[Dict]], Optional[Dict[str, TaskMeta]], Iterable[str]) -> Kitchen
        """按订单菜名编译 DAG；与 `recipe_graph.emit` 同口径（含 composition 兜底）。"""
        dags = {}  # type: Dict[str, Dict]
        for order in orders or ():
            dish = order.get("dish")
            if not dish or dish in dags:
                continue
            root = compile_node(book, dish) if book.recipe(dish) is not None else None
            if root is None:
                children = [compile_node(book, ing)
                            for ing in book.requirements(order)]
                children = [c for c in children if c]
                if children:
                    root = {"type": "DeliverPlate", "item": dish,
                            "children": children}
            if root is not None:
                dags[dish] = root
        return cls(dags, meta, stations)

    def _walk(self, node, name, mat, steps):
        # type: (Dict, str, Material, List[str]) -> None
        """后序遍历：子节点（上游）先记，步骤顺序就是加工顺序。"""
        for child in node.get("children") or ():
            self._walk(child, name, mat, steps)
        task_type, item = node.get("type"), node.get("item")
        mat.types.add(task_type)
        if item:
            mat.items.add(str(item).lower())
        steps.append((task_type, item))

    # ---- 目录

    def _meta(self, task_type):
        # type: (Any) -> TaskMeta
        return self.meta.get(task_type) or TaskMeta()

    def is_shared(self, task_type):
        # type: (Any) -> bool
        return self._meta(task_type).shared

    def shared_keys(self):
        # type: () -> List[str]
        """本关可用的共享动作（task 类 SHARED，且 STATION 站点存在）。"""
        return [name for name, m in self.meta.items()
                if m.shared and (m.station is None or m.station in self.stations)]

    def unavailable(self):
        # type: () -> List[str]
        """本关做不了的共享动作（缺站点），渲染给 LLM 让它如实拒绝。"""
        return [name for name, m in self.meta.items()
                if m.shared and m.station is not None
                and m.station not in self.stations]

    def auto_only(self):
        # type: () -> List[str]
        """不开放认领、由 P2 自动处理的救援动作（灭火 / 救锅）。

        在某条物料链里的救援步骤（饭链的盛盘）随物料一起认领，不算在内。
        """
        in_chain = set()  # type: set
        for mat in self.materials.values():
            in_chain |= mat.types
        return [name for name, m in self.meta.items()
                if m.rescue and not m.shared and name not in in_chain]

    def targets(self):
        # type: () -> List[str]
        """工具 `target` 的 enum：物料 + 步骤 + 共享动作。"""
        return list(self.materials) + self.step_targets() + self.shared_keys()

    def step_targets(self):
        # type: () -> List[str]
        """步骤级 target 的枚举：`TaskType:物料名`（只认领这一步，非共享、item 非空）。"""
        out = []  # type: List[str]
        seen = set()
        for name, mat in self.materials.items():
            for task_type, item in mat.steps:
                if self._meta(task_type).shared or not item:
                    continue
                key = "%s:%s" % (task_type, name)
                if key in seen:
                    continue
                seen.add(key)
                out.append(key)
        return out

    def expand(self, target):
        # type: (Any) -> Tuple[Optional[Scope], str]
        """target → 作用域；不在目录里返回 (None, 原因)。

        步骤级 target 形如 `TaskType:物料名`：按物料名找该链上这个 task 类型
        实际用到的 item（生/熟形态），作用域精确到 `(task_type, item)`。
        """
        name = str(target or "").strip()
        if ":" in name:
            task_type, mat_name = name.split(":", 1)
            task_type = task_type.strip()
            mat_name = mat_name.strip()
            mat = self.materials.get(mat_name)
            if mat is None:
                return None, "本关没有 %s（可选：%s）" % (
                    mat_name, "、".join(self.targets()))
            if task_type not in self.meta:
                return None, "没有 %s 这一步" % (task_type,)
            if self._meta(task_type).shared:
                return None, "%s 是共享动作，直接用不带物料" % (task_type,)
            items = {str(item).lower() for t, item in mat.steps
                     if t == task_type and item}
            if not items:
                return None, "%s 里没有 %s 这一步" % (mat_name, task_type)
            return frozenset((task_type, it) for it in items), ""
        mat = self.materials.get(name)
        if mat is not None:
            return frozenset((None, item) for item in mat.items), ""
        if name in self.shared_keys():
            return frozenset({(name, None)}), ""
        return None, "本关没有 %s（可选：%s）" % (name, "、".join(self.targets()))

    def rescue_types(self, target):
        # type: (Any) -> List[str]
        """target 的链里属于救援类的 task 类型（认领对它们不起作用）。"""
        mat = self.materials.get(str(target or ""))
        if mat is None:
            return []
        return sorted(t for t in mat.types if self._meta(t).rescue)

    # ---- 渲染

    def processes(self, name):
        # type: (str) -> List[str]
        """物料的加工方式（按链上第一次出现的顺序）：['切'] / ['切', '煮'] / []。"""
        out = []  # type: List[str]
        for task_type, _item in self.materials[name].steps:
            p = self._meta(task_type).process
            if p and p not in out:
                out.append(p)
        return out

    def _step_text(self, name, task_type, item):
        # type: (str, str, Any) -> str
        label = self._meta(task_type).label or task_type
        # 中间产物（意面链里的 Tomato）在步骤里点名，物料本身不重复
        if item and str(item) != name:
            return "%s %s" % (label, item)
        return label

    def render(self, dishes=None, shared=True):
        # type: (Optional[Iterable[str]], bool) -> str
        """给 LLM 看的本关目录：菜 = 物料组合；本关有哪些动作（及缺哪些）。

        `shared` 参数保留签名兼容、不再使用：共享动作（洗盘/送餐/装盘）已
        并入动作枚举。
        """
        wanted = None if dishes is None else set(dishes)
        lines = ["本关菜："]
        wanted_materials = []  # type: List[str]
        for dish, mats in self.dish_materials.items():
            if wanted is not None and dish not in wanted:
                continue
            lines.append("  %s = %s" % (dish, " + ".join(mats)))
            for name in mats:
                if name not in wanted_materials:
                    wanted_materials.append(name)
        lines.append("物料处理顺序（从上到下；最后一步是装盘）：")
        for name in (wanted_materials if wanted is not None
                     else list(self.materials)):
            mat = self.materials.get(name)
            if mat is None:
                continue
            steps = [self._step_text(name, task_type, item)
                     for task_type, item in mat.steps
                     if not self._meta(task_type).shared]
            lines.append("  %s：%s" % (name, " → ".join(steps) or "直接装盘"))
        lines.append("本关动作（分工用这些名字，对应一类站点）：")
        for action in ACTION_TASK_TYPES:
            st = ACTION_STATION_TYPES[action]
            if st in self.stations:
                lines.append("  %s（%s）" % (action, _ACTION_TEXT[action]))
            else:
                lines.append("  本关没有：%s" % (action,))
        return "\n".join(lines)


_ACTION_TEXT = {
    "切": "砧板切",
    "下锅": "灶/炸锅/烤箱（炸煮烤）",
    "拌": "搅拌站拌",
    "装盘": "盘/出餐台装盘盛盘",
    "送餐": "出餐口送餐",
    "洗盘": "洗碗池洗盘",
    "按开关": "方向开关把传送带调向自己",
}

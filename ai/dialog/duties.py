# -*- coding: utf-8 -*-
"""本关目录与 LLM 工具 schema。

- `Kitchen`：把订单 DAG 压成「菜 = 物料组合」与物料处理顺序，渲染给 LLM；
- `tool_schemas`：新工具协议（execute_actions / cancel_action / speak /
  get_world_state / set_division / read_cookbook）。

分工账本已删除：分工只由 LLM 的 `set_division` 工具写入 `bb.division`。
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple

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
}

# 工具协议的动作枚举（英文标识，内部翻译成中文 key）。
TOOL_ACTIONS = ["chop", "cook", "mix", "plate", "deliver", "wash", "fetch"]
# 可分工动作的默认枚举；实际由本关站点过滤（见 agent._division_tool_actions）。
DIVISION_TOOL_ACTIONS = ["chop", "cook", "mix", "plate"]
# check_action 暂时不暴露给模型：execute_actions 内部已经原子校验。
ENABLE_CHECK_ACTION = False
COOKBOOK_SECTIONS = ["summary", "division", "mechanics", "traps", "recipes", "all"]


def _set_division_schema(actions):
    # type: (list) -> dict
    return {
        "type": "function",
        "function": {
            "name": "set_division",
            "description": (
                "设置或调整长期分工：把某一类动作（可按物料细分）归给你或玩家。"
                "只影响没有明确命令时的自主选活，不会取消正在执行的命令。"
                "只包含本关可分工的动作；"
                "送餐和洗盘谁近谁做，不在分工里。开局按攻略里「玩家/你」的"
                "对应关系写，不要按出生点猜。同一动作分物料时在 item 里写物料名"
                "（例如 chop Meat 给玩家、chop Lettuce 给你）；两人都要做、又分不清"
                "物料的动作整个不写，不要写成一个人的动作级分工；不确定的动作不要写。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "assignments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": actions},
                                "owner": {
                                    "type": "string",
                                    "enum": ["you", "player", "both"],
                                    "description": "you=你自己（AI）；player=人类玩家；both=两人都做",
                                },
                                "item": {
                                    "type": "string",
                                    "description": "可选：同一动作分物料时填，例如 Meat / Lettuce / Cheese。",
                                },
                            },
                            "required": ["action", "owner"],
                        },
                        "description": "要设置的动作归属；没列出的动作保持不变。",
                    },
                    "clear": {
                        "type": "array",
                        "items": {"type": "string", "enum": actions},
                        "description": "要清掉的动作归属。",
                    },
                    "clear_all": {
                        "type": "boolean",
                        "description": "清空全部分工。",
                    },
                },
            },
        },
    }


def _read_cookbook_schema():
    # type: () -> dict
    return {
        "type": "function",
        "function": {
            "name": "read_cookbook",
            "description": (
                "读取本关攻略的某一节。系统提示里已有摘要；需要完整机制、"
                "陷阱或菜谱时再调用，不要每轮都调。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": list(COOKBOOK_SECTIONS),
                        "description": "summary 摘要 / division 打法与分工 / mechanics 机制地形 / traps 陷阱 / recipes 菜谱 / all 全文",
                    }
                },
                "required": ["section"],
            },
        },
    }


def tool_schemas(division_actions=None, with_cookbook=False):
    # type: (Any, bool) -> List[dict]
    """新工具协议：execute_actions / cancel_action / speak / get_world_state。

    `division_actions` 是本关可分工的英文动作名；为空时不暴露 set_division。
    `with_cookbook` 为真时暴露 read_cookbook。
    """
    action_prop = {
        "type": "string",
        "enum": list(TOOL_ACTIONS),
        "description": "动作：chop 切 / cook 煮炸烤下锅 / mix 拌 / plate 装盘（铺/放/摆料也是 plate）/ deliver 送餐 / wash 洗盘 / fetch 取料",
    }
    target_prop = {
        "type": "string",
        "description": "物料名或菜名，使用本关目录里的英文名（Cucumber、Seaweed、BoiledSushiRice、Sushi_Cucumber）。chop / cook 必填；deliver / wash 可省略。",
    }
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "check_action",
                "description": "只读检查：此刻能不能做这个动作、实际会作用到哪个物料或菜。玩家让你做事时先调用；它不执行任何动作。",
                "parameters": {
                    "type": "object",
                    "properties": {"action": action_prop, "target": target_prop},
                    "required": ["action"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "execute_actions",
                "description": "只在玩家明确要求「现在去做某件事」时调用：把动作数组按顺序排成持久命令并执行。玩家说「你也来切 / 你也负责煮」这类是在**调整分工**，用 set_division，不要用这个工具。自主干活也不要用它派活，planner 会按分工自动选活。chop / cook 必须带 target（说清切哪种料、煮哪道菜）；deliver / wash 可以不带。单动作也传长度为 1 的数组。没有命令在跑时第一条立即开始；已有命令时按顺序排队（最多 3 条待执行）。每条返回 ok/status/reason；不可行的那条不影响后面的条目。命令最多执行 60 秒，超时会失败并把执行权交回自动分工。要替换当前工作请先 cancel_action。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action": action_prop,
                                    "target": target_prop,
                                },
                                "required": ["action"],
                            },
                            "description": "按执行顺序排列的动作；一句话有多个动作时全部放进来。",
                        }
                    },
                    "required": ["actions"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "cancel_action",
                "description": "撤销当前命令和所有排队命令，并停手约 5 秒。玩家说“停下 / 算了 / 别做了”时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": list(TOOL_ACTIONS),
                            "description": "可选：只撤销这个动作；不填则撤销当前命令和整个队列。",
                        }
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "speak",
                "description": "对玩家说话。这是唯一发言通道，不调用就不会出声。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "一两句中文口语，30 字以内；不要念内部名词，不要出现内部代号。",
                        }
                    },
                    "required": ["text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_world_state",
                "description": "读取最新世界状态：订单、灶、盘子内容、砧板、双方手持/动作、可做的动作。需要判断“好了没/还有什么/现在该做什么”时调用；不要每轮都调。",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        },
    ]
    actions = [str(a) for a in (division_actions or []) if a]
    if actions:
        schemas.append(_set_division_schema(actions))
    if with_cookbook:
        schemas.append(_read_cookbook_schema())
    if not ENABLE_CHECK_ACTION:
        schemas = [s for s in schemas
                   if s["function"]["name"] != "check_action"]
    return schemas

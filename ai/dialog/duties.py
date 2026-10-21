"""分工账本：本关物料目录、作用域、账本条目与 op 应用。

LLM 只通过 `update_duties` 工具改账本：`add` / `edit` / `remove` 三种 op，
每条只描述 **P2 自己**的行为（mode）：

- `do`          —— P2 优先做（「你负责 X」「X 归你」），有空就做；
- `now`         —— P2 马上去（「去 X」「马上 X」「先 X」「现在 X」「帮我 X」）；
- `leave_to_p1` —— P1 在做，P2 避开（「我来 X」「X 交给我」），截止闸仍可抢回；
- `stop`        —— P2 先别做（「先别 X」「别做 X」），30 秒后自动解除。

「做什么」= `target`，只有两类：

- **物料**：本关各道菜配方 DAG 根下的直接成分（`Cucumber`、`BoiledSushiRice`
  …），代表这种料的**整条加工链**：取料、切、下锅、盛盘、装盘，以及同 item
  的预取 / 填灶。作用域是这棵子树里出现的 item 名集合（含中间产物，如意面链
  里的 Tomato），按 `task.item` 匹配候选；
- **共享动作**：task 类声明 `SHARED` 的类型（洗盘 / 摆盘 / 送餐），不分菜，
  按 task 类型匹配；`STATION` 声明的站点本关没有时不进目录。

灭火等救援动作不开放认领：由 arbiter / planner 按物理证据决定。

同一 target 只有一条约定：再次 add 就是改写它。不同物料的 item 集合重叠时
（两道菜共用中间产物），**新条目优先**：编译偏好时旧条目扣掉被新条目覆盖的
item（`Preference.exclude`）。

本模块只依赖标准库与 `ai.dialog.schema`；RecipeBook 按鸭子类型读，DAG 编译
函数与 task 元数据由调用方注入（避免 `ai.dialog` 反向导入 `ai.query` /
`ai.tasks`）。
"""
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple

from ai.dialog.schema import Preference, scope_matches

MODES = ("do", "leave_to_p1", "now", "stop")
OPS = ("add", "edit", "remove")

TICKS_PER_SECOND = 20
# stop 是「先别」：30 秒后自动解除。prompt / schema 文案从这里格式化。
STOP_TTL_S = 30
STOP_TTL_TICKS = STOP_TTL_S * TICKS_PER_SECOND
# 约定挡住 P2 可做的活超过这么久，LLM 在回话时顺带提议调整（prompt 用）。
BLOCKED_NUDGE_S = 10

# 编译权重（spec §4.5）：direct +4、claim -8。
WEIGHT_DO = 4.0
WEIGHT_CLAIM = -8.0
# now 权重仅 trace 展示；实际「马上去」的选中由 planner 闸消费（Task 2）。
WEIGHT_NOW = 20.0

# 作用域元素（与 `schema.scope_matches` 同形）：
#   (None, item 小写) —— 物料：任意 task 类型、该 item；
#   (类型, None)      —— 共享动作：该类型、任意 item。
Pair = Tuple[Optional[str], Optional[str]]
Scope = FrozenSet[Pair]


def node_key(task_type, item=None):
    # type: (str, Optional[str]) -> str
    return task_type if not item else "%s:%s" % (task_type, item)


# ---------------------------------------------------------------- 本关目录


@dataclass(frozen=True)
class TaskMeta(object):
    """task 类上与账本有关的元数据（调用方从 `T.TASK_TYPES` 汇总）。"""
    desc: str = ""
    label: str = ""             # 步骤短名（「切」「下锅」）
    process: str = ""           # 决定物料加工方式时：切 / 煮 / 拌
    shared: bool = False
    station: Optional[str] = None
    # 救援类（L1/L2 免疫认领负分）：认领时要在结果里提醒，planner 仍会接手
    rescue: bool = False
    # 共享动作允许的分工档位（None = 四档都接受）；送餐只放行 now/do。
    allowed_modes: Optional[Tuple[str, ...]] = None


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
        """工具 `target` 的 enum：物料（按首次出现顺序）+ 共享动作。"""
        return list(self.materials) + self.shared_keys()

    def expand(self, target):
        # type: (Any) -> Tuple[Optional[Scope], str]
        """target → 作用域；不在目录里返回 (None, 原因)。"""
        name = str(target or "").strip()
        mat = self.materials.get(name)
        if mat is not None:
            return frozenset((None, item) for item in mat.items), ""
        if name in self.shared_keys():
            return frozenset({(name, None)}), ""
        return None, "本关没有 %s（可选：%s）" % (name, "、".join(self.targets()))

    def allowed_modes(self, target):
        # type: (Any) -> Optional[Tuple[str, ...]]
        """target 是受限共享动作时返回允许的分工档位；否则 None（不限）。"""
        name = str(target or "").strip()
        if name in self.shared_keys():
            return self._meta(name).allowed_modes
        return None

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
        """给 LLM 看的本关目录：菜 = 物料组合；每种物料带加工方式和步骤。

        加工方式（要切 / 要煮 / 要拌 / 直接装盘）与步骤短名都来自 task 类的
        `PROCESS` / `LABEL`，不写死类型名。`dishes` 只渲染这些菜（new_order
        用），`shared` 控制是否带共享动作。
        """
        wanted = None if dishes is None else set(dishes)
        lines = ["本关菜："]
        names = []  # type: List[str]
        for dish, mats in self.dish_materials.items():
            if wanted is not None and dish not in wanted:
                continue
            lines.append("  %s = %s" % (dish, " + ".join(mats)))
            names.extend(m for m in mats if m not in names)
        lines.append("物料（target 用这些名字，代表这种料的整条加工链）：")
        for name in names:
            procs = self.processes(name)
            tag = "要" + "、".join(procs) if procs else "直接装盘"
            chain = " → ".join(self._step_text(name, t, i)
                               for t, i in self.materials[name].steps)
            lines.append("  %s（%s）：%s" % (name, tag, chain))
        if shared:
            lines.append("共享动作（不分菜）：")
            for key in self.shared_keys():
                m = self._meta(key)
                line = "  %s（%s）" % (key, m.label or m.desc)
                if m.allowed_modes:
                    line += "（只接受：%s）" % "、".join(
                        _MODE_TEXT[md] for md in m.allowed_modes)
                lines.append(line)
            missing = ["%s（%s）" % (self._meta(k).label or k, k)
                       for k in self.unavailable()]
            if missing:
                lines.append("本关没有对应站点、做不了：" + "、".join(missing))
            auto = sorted({self._meta(k).label for k in self.auto_only()
                           if self._meta(k).label})
            if auto:
                lines.append("不开放认领（谁在旁边谁处理，你会自动去做）："
                             + "、".join(auto))
        return "\n".join(lines)


# ---------------------------------------------------------------- 账本


@dataclass(frozen=True)
class Duty(object):
    """账本里的一条约定（不可变；改动时整条替换）。"""
    id: str
    mode: str
    target: str
    scope: Scope
    source: str = ""
    created_tick: int = 0
    expires_tick: int = 0       # 0 = 长期；只有 stop 有到期


_MODE_TEXT = {"do": "P2 优先做", "now": "P2 马上去",
              "leave_to_p1": "交给 P1", "stop": "P2 先别做"}


class Ledger(object):
    """P2 的分工账本；主循环线程独占读写。

    `duties` 按「最后一次被声明」排序：越靠后越新，重叠时新条目优先。
    """

    def __init__(self):
        # type: () -> None
        self.duties = []        # type: List[Duty]
        self._seq = 0

    def get(self, duty_id):
        # type: (Any) -> Optional[Duty]
        target = str(duty_id or "").strip()
        for duty in self.duties:
            if duty.id == target:
                return duty
        return None

    def ids(self):
        # type: () -> List[str]
        return [duty.id for duty in self.duties]

    def clear(self):
        # type: () -> None
        """换关：清空账本，id 从 a1 重新编号。"""
        self.duties = []
        self._seq = 0

    # ---- 修改

    def apply(self, args, kitchen, tick, source=""):
        # type: (Any, Kitchen, int, str) -> Dict[str, Any]
        """应用一条 `update_duties` 调用，返回给 LLM 的结果 dict。

        结果形如 `{"ok": bool, "id"?, "text"?, "reason"?, "warning"?}`；
        失败时账本不变。
        """
        if not isinstance(args, dict):
            return {"ok": False, "reason": "参数必须是对象"}
        op = args.get("op")
        if op not in OPS:
            return {"ok": False, "reason": "未知 op：%s" % (op,)}
        if op == "remove":
            duty = self.get(args.get("id"))
            if duty is None:
                return {"ok": False, "reason": "没有 %s 这条约定" % (args.get("id"),)}
            self.duties = [d for d in self.duties if d.id != duty.id]
            return {"ok": True, "id": duty.id, "removed": True}

        old = None
        if op == "edit":
            old = self.get(args.get("id"))
            if old is None:
                return {"ok": False, "reason": "没有 %s 这条约定" % (args.get("id"),)}
            mode = args.get("mode") or old.mode
            target = args.get("target") or old.target
        else:
            mode = args.get("mode")
            target = args.get("target")
        if mode not in MODES:
            return {"ok": False, "reason": "未知 mode：%s" % (mode,)}
        if not target:
            return {"ok": False, "reason": "缺少 target"}
        target = str(target)
        scope, reason = kitchen.expand(target)
        if scope is None:
            return {"ok": False, "reason": reason}
        allowed = kitchen.allowed_modes(target)
        if allowed is not None and mode not in allowed:
            return {"ok": False,
                    "reason": "%s 只接受 %s（不能 %s）" % (
                        target,
                        "、".join(_MODE_TEXT[m] for m in allowed),
                        _MODE_TEXT[mode])}

        # 同一 target 只有一条约定：add 到已有 target 上等于改写它
        same = next((d for d in self.duties if d.target == target), None)
        if old is None and same is not None:
            if same.mode == mode:
                return {"ok": True, "id": same.id, "unchanged": True,
                        "text": "%s %s" % (_MODE_TEXT[mode], target)}
            old = same
        if old is not None and old.target != target:
            # edit 换了 target：新 target 上原有的约定被它取代
            self.duties = [d for d in self.duties
                           if d.target != target or d.id == old.id]

        if old is None:
            self._seq += 1
            duty_id = "a%d" % (self._seq,)
        else:
            duty_id = old.id
        expires = tick + STOP_TTL_TICKS if mode == "stop" else 0
        duty = Duty(id=duty_id, mode=mode, target=target, scope=scope,
                    source=source or (old.source if old else ""),
                    created_tick=tick, expires_tick=expires)
        # 重新声明的条目挪到末尾：它是最新的说法，重叠时优先
        self.duties = [d for d in self.duties if d.id != duty_id] + [duty]

        result = {"ok": True, "id": duty_id,
                  "text": "%s %s" % (_MODE_TEXT[mode], target)}
        rescue = kitchen.rescue_types(target)
        if mode in ("leave_to_p1", "stop") and rescue:
            result["warning"] = (
                "%s 是救锅动作（锅刚熟或快糊时），P2 仍会去做"
                % ("、".join(rescue),))
        return result

    def prune(self, tick):
        # type: (int) -> List[Duty]
        """删掉到期条目（stop 到期），返回被删的。"""
        expired = [d for d in self.duties
                   if d.expires_tick and tick >= d.expires_tick]
        if expired:
            ids = {d.id for d in expired}
            self.duties = [d for d in self.duties if d.id not in ids]
        return expired

    def consume_now(self, task):
        # type: (Any) -> bool
        """「马上去」是一次性的：P2 已认下这个活（task 命中 now 作用域），
        就把命中的 now 降级成 do——马上去的「这就去」落实一次，剩下的交给
        do 的持续优先级，不再每拍触发 planner 硬指令闸（否则同一作用域里
        FetchToCooker↔FillCooker 逐拍换选、执行树逐拍重建，取料永远做不完）。

        返回是否有改动（True = 调用方需重新编译偏好）。
        """
        if task is None:
            return False
        changed = False
        for i, duty in enumerate(self.duties):
            if duty.mode != "now":
                continue
            if scope_matches(duty.scope, task):
                self.duties[i] = replace(duty, mode="do")
                changed = True
        return changed

    # ---- 编译 / 渲染

    def preferences(self):
        # type: () -> List[Preference]
        """编译成 planner 的偏好；重叠时新条目优先（旧条目 exclude 掉新的）。"""
        out = []  # type: List[Preference]
        newer = set()  # type: set
        for duty in reversed(self.duties):
            common = dict(scope=duty.scope, exclude=frozenset(newer),
                          duty_id=duty.id, expires_tick=duty.expires_tick)
            if duty.mode == "do":
                out.append(Preference(kind="direct", owner="P2",
                                      weight=WEIGHT_DO, **common))
            elif duty.mode == "now":
                out.append(Preference(kind="now", owner="P2",
                                      weight=WEIGHT_NOW, **common))
            elif duty.mode == "leave_to_p1":
                out.append(Preference(kind="claim", owner="P1",
                                      weight=WEIGHT_CLAIM, **common))
            else:
                out.append(Preference(kind="stop", owner="P2", weight=0.0,
                                      **common))
            newer |= duty.scope
        out.reverse()
        return out

    def snapshot(self, tick):
        # type: (int) -> List[Dict[str, Any]]
        """给上下文 / trace 的账本快照（秒为单位）。"""
        out = []
        for duty in self.duties:
            row = {"id": duty.id, "mode": duty.mode, "target": duty.target,
                   "source": duty.source}
            if duty.expires_tick:
                row["remaining_s"] = round(
                    max(0, duty.expires_tick - tick) / float(TICKS_PER_SECOND), 1)
            out.append(row)
        return out


def render_ledger(rows):
    # type: (Iterable[Dict[str, Any]]) -> str
    """账本快照 → 文本；每条带上主循环算好的「此刻影响」（有的话）。"""
    lines = []
    for row in rows or ():
        line = "[%s] %s %s" % (row.get("id"), _MODE_TEXT.get(row.get("mode"), row.get("mode")),
                               row.get("target"))
        if row.get("remaining_s") is not None:
            line += "（还剩 %s 秒）" % (row["remaining_s"],)
        if row.get("source"):
            line += "  来源「%s」" % (row["source"],)
        if row.get("impact"):
            line += "\n     此刻：%s" % (row["impact"],)
        lines.append(line)
    return "\n".join(lines) if lines else "（空）"


def tool_schemas(targets, duty_ids):
    # type: (List[str], List[str]) -> List[dict]
    """按本关数据生成 OpenAI tools；enum 让模型只能选真实存在的值。"""
    props = {
        "op": {"type": "string", "enum": list(OPS),
               "description": "add 新增（同一 target 已有约定时就是改写它）；edit 改已有条目（必须带 id，只写要改的字段）；remove 删除（必须带 id）"},
        "id": {"type": "string", "description": "约定条目 id，edit/remove 必填"},
        "mode": {"type": "string", "enum": list(MODES),
                 "description": "do=有空优先做；now=马上去；leave_to_p1=交给P1，P2避开；stop=先别做"},
        "target": {"type": "string", "enum": list(targets),
                   "description": "物料名（代表这种料的整条加工链）或共享动作"},
    }
    if duty_ids:
        props["id"]["enum"] = list(duty_ids)
    return [{
        "type": "function",
        "function": {
            "name": "update_duties",
            "description": "修改你（P2）的分工约定。玩家一句话里涉及几种物料/动作就调用几次；闲聊、提问不要调用。",
            "parameters": {"type": "object", "properties": props,
                           "required": ["op"]},
        },
    }]

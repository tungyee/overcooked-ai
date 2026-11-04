"""分工账本：动作级作用域、账本条目与 op 应用。

LLM 只通过 `update_duties` 工具改账本：`add` / `cancel` 两种 op，每条描述
「做什么**动作**」和「什么**性质**」（mode）：

- `now`    —— P2 马上去（一次性，做完即失效）；
- `assign` —— 分工（带 direction）：`P2`=你负责做，`P1`=交给 P1（P2 避开、
             订单收尾时系统可补位）；
- `stop`   —— P2 先别做，30 秒后自动解除。

「做什么」= `action`，6 个动作之一（切/下锅/拌/装盘/送餐/洗盘，对应砧板/
灶/搅拌站/盘/出餐口/洗碗池），定义见 `ai/dialog/actions.py`。**不按物料认领**
（物料链横跨多个动作、归多人）。取料/投掷/救援不开放认领：由 planner 按
可达性/物理证据决定。

同一 `(mode, action)` 再次 add 就是改写它；`cancel` 撤销这个动作的约定。
重叠时新条目优先（旧条目 `Preference.exclude` 掉新条目的作用域）。

本模块只依赖标准库、`ai.dialog.schema` 与 `ai.dialog.actions`；RecipeBook
按鸭子类型读，DAG 编译函数与 task 元数据由调用方注入（避免反向导入
`ai.query` / `ai.tasks`）。
"""
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple

from ai.dialog.actions import (ACTION_STATION_TYPES, ACTION_TASK_TYPES,
                               action_scope, task_action)
from ai.dialog.schema import Preference, scope_matches

MODES = ("now", "assign", "stop")
OPS = ("add", "cancel")

TICKS_PER_SECOND = 20
# stop 是「先别」：30 秒后自动解除。prompt / schema 文案从这里格式化。
STOP_TTL_S = 30
STOP_TTL_TICKS = STOP_TTL_S * TICKS_PER_SECOND
# help_request 瞬态 duty 的兜底 TTL（30 秒）；主清除路径是「该步已解决」。
HELP_REQUEST_TTL_TICKS = 30 * TICKS_PER_SECOND
# 同一 (task_type, item) 的求助冷却（45 秒）：duty 被剪枝/TTL 到期后也不立即
# 复读，避免分割图上「P1 更划算」的步骤被反复喊（trace-20261103-205951）。
HELP_REQUEST_COOLDOWN_TICKS = 45 * TICKS_PER_SECOND
# 约定挡住 P2 可做的活超过这么久，LLM 在回话时顺带提议调整（prompt 用）。
BLOCKED_NUDGE_S = 10

# 分工是软偏好：它应影响同等价值任务的选择，不能盖过订单紧迫度、完成度
# 与熟料压力。旧值 direct +4 / claim -8 会把 P2 锁死在自己的一种物料上，
# 即使另一张盘只差最后一味也继续填灶（trace-20261022-142224）。
WEIGHT_DO = 1.0
WEIGHT_CLAIM = -6.0
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
        """给 LLM 看的本关目录：菜 = 物料组合；本关有哪些动作（及缺哪些）。

        `shared` 参数保留签名兼容、不再使用：共享动作（洗盘/送餐/装盘）已
        并入动作枚举。
        """
        wanted = None if dishes is None else set(dishes)
        lines = ["本关菜："]
        for dish, mats in self.dish_materials.items():
            if wanted is not None and dish not in wanted:
                continue
            lines.append("  %s = %s" % (dish, " + ".join(mats)))
        lines.append("本关动作（分工用这些名字，对应一类站点）：")
        for action in ACTION_TASK_TYPES:
            st = ACTION_STATION_TYPES[action]
            if st in self.stations:
                lines.append("  %s（%s）" % (action, _ACTION_TEXT[action]))
            else:
                lines.append("  本关没有：%s" % (action,))
        return "\n".join(lines)


# ---------------------------------------------------------------- 账本


@dataclass(frozen=True)
class Duty(object):
    """账本里的一条约定（不可变；改动时整条替换）。"""
    id: str
    mode: str            # "now" | "assign" | "stop"
    action: str          # 动作名（assign/now/stop）；help_request 为 ""
    item: Optional[str]  # now/stop 的可选 item；其余为 None
    direction: str       # "P2" | "P1"（assign）；其余为 ""
    scope: Scope
    source: str = ""
    created_tick: int = 0
    expires_tick: int = 0       # 0 = 长期；只有 stop 有到期


_DIRECTION_TEXT = {"P2": "P2 负责", "P1": "P1 负责"}
_ACTION_TEXT = {
    "切": "砧板切",
    "下锅": "灶/炸锅/烤箱（炸煮烤）",
    "拌": "搅拌站拌",
    "装盘": "盘/出餐台装盘盛盘",
    "送餐": "出餐口送餐",
    "洗盘": "洗碗池洗盘",
}


def _duty_text(mode, action, direction=""):
    # type: (str, str, str) -> str
    if mode == "assign":
        return "%s%s" % (_DIRECTION_TEXT.get(direction, ""), action)
    if mode == "now":
        return "P2 马上去%s" % (action,)
    return "P2 先别做%s" % (action,)


def _mode_label(row):
    # type: (Dict[str, Any]) -> str
    mode = row.get("mode")
    if mode == "assign":
        return _DIRECTION_TEXT.get(row.get("direction"), "分工")
    if mode == "now":
        return "P2 马上去"
    if mode == "stop":
        return "P2 先别做"
    return str(mode)


class Ledger(object):
    """P2 的分工账本；主循环线程独占读写。

    `duties` 按「最后一次被声明」排序：越靠后越新，重叠时新条目优先。
    """

    def __init__(self):
        # type: () -> None
        self.duties = []        # type: List[Duty]
        self._seq = 0
        # (task_type, item 小写) → 最近一次求助的 tick，用于同一步冷却。
        self._help_request_last = {}  # type: Dict[Tuple[str, str], int]

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
        self._help_request_last = {}

    # ---- 修改

    def apply(self, args, kitchen, tick, source=""):
        # type: (Any, Kitchen, int, str) -> Dict[str, Any]
        """应用一条动作级 `update_duties` 调用，返回给 LLM 的结果 dict。

        args = {op: add/cancel, mode: now/assign/stop, action: 动作,
                direction?: P2/P1（assign 用）, item?: 可选（now/stop 用）}。
        失败时账本不变。`kitchen` 参数保留签名兼容，本方法不再使用。
        """
        if not isinstance(args, dict):
            return {"ok": False, "reason": "参数必须是对象"}
        op = args.get("op")
        if op not in OPS:
            return {"ok": False, "reason": "未知 op：%s" % (op,)}
        mode = args.get("mode")
        if mode not in MODES:
            return {"ok": False, "reason": "未知 mode：%s" % (mode,)}
        action = args.get("action")
        if action not in ACTION_TASK_TYPES:
            return {"ok": False, "reason": "未知 action：%s" % (action,)}
        action = str(action)
        item = args.get("item")
        item_key = str(item or "").lower()
        if op == "cancel":
            # 撤销这个动作的约定（不区分 mode/item：「算了别切了」）。
            for duty in self.duties:
                if duty.action == action:
                    self.duties = [d for d in self.duties if d.id != duty.id]
                    return {"ok": True, "id": duty.id, "removed": True}
            return {"ok": False, "reason": "没有这条约定"}
        # op == "add"
        direction = "P2"
        if mode == "assign":
            direction = args.get("direction")
            if direction not in ("P2", "P1"):
                return {"ok": False, "reason": "assign 需要 direction：P2 或 P1"}
        # 同 (mode, action, item) 再次 add = 覆盖（改方向/内容）
        same = next((d for d in self.duties
                     if d.mode == mode and d.action == action
                     and (d.item or "") == item_key), None)
        if same is not None and same.direction == direction:
            return {"ok": True, "id": same.id, "unchanged": True,
                    "text": _duty_text(mode, action, direction)}
        duty_id = same.id if same is not None else None
        if duty_id is None:
            self._seq += 1
            duty_id = "a%d" % (self._seq,)
        scope = action_scope(action, item if mode in ("now", "stop") else None)
        expires = tick + STOP_TTL_TICKS if mode == "stop" else 0
        duty = Duty(id=duty_id, mode=mode, action=action, item=item,
                    direction=direction, scope=scope,
                    source=source or (same.source if same else ""),
                    created_tick=tick, expires_tick=expires)
        # 重新声明的条目挪到末尾：它是最新的说法，重叠时优先
        self.duties = [d for d in self.duties if d.id != duty_id] + [duty]
        return {"ok": True, "id": duty_id,
                "text": _duty_text(mode, action, direction)}

    def add_help_request(self, task_type, item, tick,
                         ttl_ticks=HELP_REQUEST_TTL_TICKS):
        # type: (str, Optional[str], int, int) -> str
        """P2 自己发起的「挪走」：给某一步加一条瞬态 claim duty。

        可分工动作（task_type 在 ACTION_TASK_TYPES）→ `assign(P1, 动作)`
        动作级 scope；取料/交接/救援 → 保持节点级 `{(task_type, item)}`。
        同 scope 已有条目时返回原 id。
        """
        action = task_action(str(task_type))
        if action is not None:
            scope = action_scope(action)
            duty_action = action
            duty_item = None
        else:
            scope = frozenset({(str(task_type), str(item or "").lower())})
            duty_action = ""
            duty_item = str(item or "").lower()
        for duty in self.duties:
            if duty.source == "help_request" and duty.scope == scope:
                return duty.id
        # 只在真正新建一条求助（即真的会开口）时记冷却锚点；同一步已有
        # duty 的重复调用是去重空操作，不刷新冷却。
        self._help_request_last[(str(task_type), str(item or "").lower())] = tick
        self._seq += 1
        duty_id = "a%d" % (self._seq,)
        self.duties.append(Duty(
            id=duty_id, mode="assign", action=duty_action, item=duty_item,
            direction="P1", scope=scope, source="help_request",
            created_tick=tick, expires_tick=tick + ttl_ticks))
        return duty_id

    def help_request_ids(self):
        # type: () -> List[str]
        return [d.id for d in self.duties if d.source == "help_request"]

    def help_request_recent(self, task_type, item, tick,
                            cooldown=HELP_REQUEST_COOLDOWN_TICKS):
        # type: (str, Optional[str], int, int) -> bool
        """该 (task_type, item) 在冷却期内是否刚求助过（抑制同一步复读）。"""
        key = (str(task_type), str(item or "").lower())
        last = self._help_request_last.get(key)
        return last is not None and tick - last < cooldown

    def prune_help_requests(self, active_scopes):
        # type: (set) -> List[Duty]
        """删掉已不在 active_scopes 里的 help_request duty；返回被删的。

        - 动作级 duty（`action != ""`）：只要该动作还有任一 task_type 在前沿
          （active_scopes 里）就保留；
        - 节点级 duty（取料类，`action == ""`）：精确匹配 (task_type, item)。
        TTL 只是兜底，主清除路径走这里。
        """
        active_types = {t for t, _item in active_scopes}
        removed = []
        kept = []
        for d in self.duties:
            if d.source == "help_request":
                if d.action:
                    types = {t for t, _i in d.scope}
                    keep = bool(types & active_types)
                else:
                    keep = bool(d.scope and d.scope <= active_scopes)
                (kept if keep else removed).append(d)
            else:
                kept.append(d)
        self.duties = kept
        return removed

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
        """「做」（now）是一次性的：task 命中 now 作用域就删掉该 now（不持久）。

        与旧的「now→do 降级」不同：spec §5.2 说「执行完即失效，不持久」。
        返回是否有改动（True = 调用方需重新编译偏好）。
        """
        if task is None:
            return False
        matched = [d for d in self.duties
                   if d.mode == "now" and scope_matches(d.scope, task)]
        if not matched:
            return False
        ids = {d.id for d in matched}
        self.duties = [d for d in self.duties if d.id not in ids]
        return True

    # ---- 编译 / 渲染

    def preferences(self):
        # type: () -> List[Preference]
        """编译成 planner 的偏好；重叠时新条目优先（旧条目 exclude 掉新的）。"""
        out = []  # type: List[Preference]
        newer = set()  # type: set
        for duty in reversed(self.duties):
            common = dict(scope=duty.scope, exclude=frozenset(newer),
                          duty_id=duty.id, expires_tick=duty.expires_tick)
            if duty.mode == "now":
                out.append(Preference(kind="now", owner="P2",
                                      weight=WEIGHT_NOW, **common))
            elif duty.mode == "assign":
                if duty.direction == "P2":
                    out.append(Preference(kind="direct", owner="P2",
                                          weight=WEIGHT_DO, **common))
                else:
                    out.append(Preference(kind="claim", owner="P1",
                                          weight=WEIGHT_CLAIM, **common))
            else:  # stop
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
            row = {"id": duty.id, "mode": duty.mode, "action": duty.action,
                   "source": duty.source}
            if duty.item:
                row["item"] = duty.item
            if duty.direction:
                row["direction"] = duty.direction
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
        action = row.get("action") or ""
        if row.get("item"):
            action += ":%s" % row["item"]
        line = "[%s] %s %s" % (row.get("id"), _mode_label(row), action)
        if row.get("remaining_s") is not None:
            line += "（还剩 %s 秒）" % (row["remaining_s"],)
        if row.get("source"):
            line += "  来源「%s」" % (row["source"],)
        if row.get("impact"):
            line += "\n     此刻：%s" % (row["impact"],)
        lines.append(line)
    return "\n".join(lines) if lines else "（空）"


def tool_schemas(targets=None, duty_ids=None):
    # type: (Any, Any) -> List[dict]
    """动作级 OpenAI tool schema（替换旧物料/步骤/共享 target 枚举）。

    `targets`/`duty_ids` 参数保留签名兼容、不再使用：动作枚举是静态的 6 个
    动作，不随关卡/账本变化。
    """
    props = {
        "op": {"type": "string", "enum": list(OPS),
               "description": "add 新增（同一 action 已有约定时就是改写它）；cancel 撤销（按 mode+action+item 定位）"},
        "mode": {"type": "string", "enum": list(MODES),
                 "description": "now=马上去（一次性）；assign=分工（持续，带 direction）；stop=先别做（30 秒自动解除）"},
        "action": {"type": "string", "enum": list(ACTION_TASK_TYPES),
                   "description": "动作（绑定站点）：切/下锅/拌/装盘/送餐/洗盘"},
        "direction": {"type": "string", "enum": ["P2", "P1"],
                      "description": "assign 用：P2=你负责做，P1=交给 P1（你避开、收尾可补位）"},
    }
    return [{
        "type": "function",
        "function": {
            "name": "update_duties",
            "description": "修改你（P2）的分工约定。玩家一句话里涉及几个动作就调用几次；闲聊、提问不要调用。",
            "parameters": {"type": "object", "properties": props,
                           "required": ["op", "mode", "action"]},
        },
    }]

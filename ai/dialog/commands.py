"""临时命令队列：动作 → 持久命令 → 完成/等待/失败。

设计要点（与提示词里的工具协议对应）：

- 一条命令不是单个 task，而是一个**目标状态**（切好 / 煮熟 / 装盘 /
  送出 / 洗净 / 取到）。命令带 `scope`，只要没到 `done`，planner 就
  优先在这个 scope 里选任务；不会因为第一个子任务成功就消费掉。
- 队列最多 3 条待执行。满了丢最后一条待执行项，新指令照收。
- 当前命令 waiting（煎/煮/资源被占）时，下一条有可行任务的命令可以跑；
  依赖关系由 scope 的可行性天然表达，不额外建依赖图。
- 失败分类：临时不可行等；永久不可行/超时/连续任务失败才判 failed。

模块保持鸭子类型：不 import `ai.tasks` / `ai.state.world`，只读
`kitchen`、`book`、`world` 的属性，避免和 planner / state 形成循环。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

Pair = Tuple[Optional[str], Optional[str]]
Scope = FrozenSet[Pair]

ACTION_LABELS = {
    "chop": "切",
    "cook": "下锅",
    "mix": "拌",
    "plate": "装盘",
    "deliver": "送餐",
    "wash": "洗盘",
    "fetch": "取料",
    "prepare": "整道菜",
}

TICKS_PER_SECOND = 20
MAX_PENDING = 3
RESOURCE_WAIT_TIMEOUT_TICKS = 20 * TICKS_PER_SECOND
TASK_FAIL_LIMIT = 3

# 英文动作 → task 类型。这里只描述“做到什么状态”需要哪些 task；
# 具体命中哪个 item 由 build_scope 从本关 DAG 里取。
ACTION_TASK_TYPES = {
    "chop": ("FetchToBoard", "Chop"),
    "cook": ("FetchToBoard", "Chop", "FetchToCooker", "FillCooker",
             "PlaceCarrierInCooker", "TakeCookedToPlate"),
    "mix": ("FetchToBoard", "Chop", "Mix"),
    "plate": ("FetchToBoard", "Chop", "FetchToCooker", "FillCooker",
              "PlaceCarrierInCooker", "TakeCookedToPlate", "FetchToPlate",
              "PlaceCleanPlate"),
    "deliver": ("DeliverPlate",),
    "wash": ("WashPlate",),
    "fetch": ("FetchToBoard", "FetchToCooker", "FetchToPlate",
              "FetchToCarrier", "Prefetch"),
    "prepare": None,       # 整道菜：取该菜全部 task
}  # type: Dict[str, Optional[Tuple[str, ...]]]

# 中文口语 → DAG 英文名。只覆盖常见料；查不到时 check 会返回 valid_targets。
TARGET_ALIASES = {
    "黄瓜": "Cucumber", "海苔": "Seaweed", "紫菜": "Seaweed",
    "米饭": "BoiledSushiRice", "熟米饭": "BoiledSushiRice",
    "生米": "SushiRice", "米": "SushiRice",
    "鱼": "SushiFish", "寿司鱼": "SushiFish", "生鱼": "SushiFish",
    "虾": "SushiPrawn", "寿司虾": "SushiPrawn",
    "番茄": "Tomato", "西红柿": "Tomato",
    "意面": "Pasta", "面": "Pasta",
    "肉": "BurritoMeat", "牛肉": "BurritoMeat",
    "洋葱": "Onion", "生菜": "Lettuce", "面包": "Bread",
    "土豆": "Potato", "奶酪": "Cheese", "芝士": "Cheese",
}  # type: Dict[str, str]


def _norm(value):
    # type: (Any) -> str
    return str(value or "").strip().lower()


def _values(value):
    # type: (Any) -> List[Any]
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def scope_matches_task(scope, task):
    # type: (Scope, Any) -> bool
    """与 `schema.scope_matches` 同口径：动作级 / 物料级 / 精确节点。"""
    if not scope:
        return False
    task_type = getattr(task, "TYPE", None)
    if (task_type, None) in scope:
        return True
    item = getattr(task, "item", None)
    if item is None:
        return False
    item = _norm(item)
    return (None, item) in scope or (task_type, item) in scope


def _catalogs(kitchen):
    # type: (Any) -> Tuple[Dict[str, str], Dict[str, str]]
    materials = {}
    dishes = {}
    for name in (getattr(kitchen, "materials", {}) or {}):
        materials[_norm(name)] = str(name)
    for name in (getattr(kitchen, "dish_materials", {}) or {}):
        dishes[_norm(name)] = str(name)
    return materials, dishes


def valid_targets(kitchen):
    # type: (Any) -> List[str]
    materials, dishes = _catalogs(kitchen)
    return sorted(set(materials.values()) | set(dishes.values()))


def resolve_target(kitchen, action, raw):
    # type: (Any, str, Any) -> Tuple[Optional[str], Optional[str], Optional[str], str, List[str]]
    """target → (mode, resolved, display, reason, valid)。

    mode: "material" / "dish" / None（未指定）。target 用 DAG 英文名；
    用户说中文时先过 TARGET_ALIASES。
    """
    action = str(action or "")
    if action == "wash":
        return None, None, None, "", []
    text = str(raw or "").strip()
    if not text:
        return None, None, None, "", []
    materials, dishes = _catalogs(kitchen)
    candidates = [text, TARGET_ALIASES.get(text, ""), TARGET_ALIASES.get(_norm(text), "")]
    for candidate in candidates:
        key = _norm(candidate)
        if not key:
            continue
        if key in dishes:
            return "dish", dishes[key], text, "", []
        if key in materials:
            return "material", materials[key], text, "", []
    reason = "本关没有「%s」" % text
    return None, None, text, reason, valid_targets(kitchen)


def build_scope(kitchen, action, target, mode):
    # type: (Any, str, Optional[str], Optional[str]) -> Tuple[Optional[Scope], str]
    """动作 + target → task scope；做不到时返回 (None, reason)。"""
    action = str(action or "")
    task_types = ACTION_TASK_TYPES.get(action)
    if action not in ACTION_TASK_TYPES:
        return None, "未知动作：%s" % action
    if action == "wash":
        return frozenset({("WashPlate", None)}), ""
    if action == "deliver":
        return frozenset({("DeliverPlate", None)}), ""
    if not target:
        # 动作级：任意 item 的这个动作；由 planner 选具体料。
        if not task_types:
            return None, "这个动作需要指定目标"
        return frozenset((t, None) for t in task_types), ""

    wanted = set(task_types or ())
    scope = set()  # type: Set[Pair]

    def add_material(name):
        mat = (getattr(kitchen, "materials", {}) or {}).get(name)
        if mat is None:
            # 大小写不敏感兜底
            for key, value in (getattr(kitchen, "materials", {}) or {}).items():
                if _norm(key) == _norm(name):
                    mat = value
                    break
        if mat is None:
            return False
        for task_type, item in getattr(mat, "steps", ()) or ():
            if task_type in wanted and item:
                scope.add((task_type, _norm(item)))
        if action == "fetch":
            scope.add(("Prefetch", _norm(name)))
        return True

    if mode == "dish":
        names = (getattr(kitchen, "dish_materials", {}) or {}).get(target) or []
        for name in names:
            add_material(name)
        if action in ("plate", "prepare"):
            scope.add(("PlaceCleanPlate", None))
        if action == "prepare":
            scope.add(("DeliverPlate", None))
    else:
        add_material(target)

    if not scope:
        labels = {
            "chop": "这种料不用切",
            "cook": "这种料不用煮",
            "mix": "这种料不用拌",
            "plate": "这种料不用装盘",
        }
        return None, labels.get(action, "现在没有可做的这一步")
    return frozenset(scope), ""


@dataclass
class Command:
    id: str
    action: str
    target: Optional[str]
    resolved_target: Optional[str]
    display_target: Optional[str]
    scope: Scope
    mode: str = "material"
    status: str = "queued"          # queued / running / waiting / succeeded / failed / cancelled
    created_tick: int = 0
    started_tick: int = 0
    wait_since: int = 0
    wait_reason: str = ""
    order_id: Any = None
    start_deliveries: int = 0
    start_dirty: int = 0
    attempts: int = 0
    reason: str = ""

    @property
    def label(self):
        # type: () -> str
        return ACTION_LABELS.get(self.action, self.action)

    @property
    def key(self):
        # type: () -> Tuple[str, Any, Any]
        return (self.action, self.resolved_target, self.order_id)

    def terminal(self):
        # type: () -> bool
        return self.status in ("succeeded", "failed", "cancelled")


class CommandQueue:
    """临时命令队列；由主循环线程独占读写。"""

    def __init__(self):
        # type: () -> None
        self.commands = []  # type: List[Command]
        self._seq = 0
        # 有命令失败时暂停队列，等模型决定重试/跳过/解释。
        self.paused = False

    def active(self):
        # type: () -> Optional[Command]
        for command in self.commands:
            if command.status in ("running", "waiting"):
                return command
        return None

    def running(self):
        # type: () -> Optional[Command]
        for command in self.commands:
            if command.status == "running":
                return command
        return None

    def pending(self):
        # type: () -> List[Command]
        return [c for c in self.commands if c.status == "queued"]

    def get(self, command_id):
        # type: (Any) -> Optional[Command]
        target = str(command_id or "")
        for command in self.commands:
            if command.id == target:
                return command
        return None

    def clear(self):
        # type: () -> None
        self.commands = []
        self._seq = 0

    def add(self, action, resolved_target, scope, tick, order_id=None,
            display_target=None, start_deliveries=0, start_dirty=0,
            mode="material"):
        # type: (str, Optional[str], Scope, int, Any, Optional[str], int, int, str) -> Dict[str, Any]
        if self.paused:
            self.commands = [c for c in self.commands if not c.terminal()]
            self.paused = False
        key = (str(action), resolved_target, order_id)
        for command in self.commands:
            if command.key == key and not command.terminal():
                return {"ok": True, "status": command.status,
                        "command_id": command.id, "deduped": True}
        dropped = None
        pending = self.pending()
        if len(pending) >= MAX_PENDING and pending:
            victim = pending[-1]
            self.commands = [c for c in self.commands if c.id != victim.id]
            dropped = victim.id
        self._seq += 1
        command = Command(
            id="cmd%d" % self._seq, action=str(action),
            target=resolved_target, resolved_target=resolved_target,
            display_target=display_target or resolved_target,
            scope=scope, mode=mode, created_tick=tick,
            order_id=order_id, start_deliveries=start_deliveries,
            start_dirty=start_dirty,
        )
        if self.active() is None:
            command.status = "running"
            command.started_tick = tick
        self.commands.append(command)
        result = {"ok": True, "status": command.status,
                  "command_id": command.id, "action": command.action,
                  "target": command.resolved_target}
        if command.status == "queued":
            result["position"] = len(self.pending())
        if dropped:
            result["dropped"] = dropped
        return result

    def cancel(self, action=None, target=None):
        # type: (Optional[str], Optional[str]) -> Dict[str, Any]
        action = str(action or "").strip()
        target = str(target or "").strip()
        self.paused = False
        if not action and not target:
            ids = [c.id for c in self.commands if not c.terminal()]
            for command in self.commands:
                if not command.terminal():
                    command.status = "cancelled"
            self.commands = [c for c in self.commands if not c.terminal()]
            return {"ok": True, "cancelled": ids}
        matched = []
        for command in self.commands:
            if command.terminal():
                continue
            if action and command.action != action:
                continue
            if target and _norm(command.resolved_target) != _norm(target):
                continue
            matched.append(command)
        if not matched:
            return {"ok": False, "reason": "当前没有这条命令"}
        for command in matched:
            command.status = "cancelled"
        ids = [c.id for c in matched]
        self.commands = [c for c in self.commands if not c.terminal()]
        self._promote()
        return {"ok": True, "cancelled": ids}

    def mark_running(self, command, tick):
        # type: (Command, int) -> None
        if command.status == "queued":
            command.started_tick = tick
        command.status = "running"
        command.wait_reason = ""

    def mark_waiting(self, command, reason, tick):
        # type: (Command, str, int) -> None
        if command.status != "waiting":
            command.wait_since = tick
        command.status = "waiting"
        command.wait_reason = str(reason or "")

    def mark_succeeded(self, command):
        # type: (Command) -> None
        command.status = "succeeded"
        command.wait_reason = ""

    def mark_failed(self, command, reason):
        # type: (Command, str) -> None
        command.status = "failed"
        command.reason = str(reason or "")
        command.wait_reason = ""
        self.paused = True

    def note_task_failure(self, task, reason):
        # type: (Any, str) -> Optional[Command]
        """任务失败计入命中它的命令；连续失败到阈值返回该命令。"""
        for command in self.commands:
            if command.terminal():
                continue
            if not scope_matches_task(command.scope, task):
                continue
            command.attempts += 1
            if command.attempts >= TASK_FAIL_LIMIT:
                self.mark_failed(command, reason or "连续失败")
                return command
            return None
        return None

    def _promote(self):
        # type: () -> None
        if self.active() is not None:
            return
        for command in self.commands:
            if command.status == "queued":
                command.status = "running"
                return

    def expire(self, tick):
        # type: (int) -> List[Command]
        """waiting 超时的命令判失败；返回本次失败的命令。"""
        failed = []
        for command in self.commands:
            if command.status != "waiting":
                continue
            if command.wait_since and tick - command.wait_since > RESOURCE_WAIT_TIMEOUT_TICKS:
                self.mark_failed(command, command.wait_reason or "等太久了")
                failed.append(command)
        return failed

    def snapshot(self):
        # type: () -> List[Dict[str, Any]]
        return [{
            "id": c.id, "action": c.action, "label": c.label,
            "target": c.display_target,
            "status": c.status,
            "wait_reason": c.wait_reason,
            "reason": c.reason,
        } for c in self.commands if not c.terminal()]


# ---------------------------------------------------------------- done 谓词


def _world_list(world, name):
    # type: (Any, str) -> List[Any]
    return list(getattr(world, name, None) or ())


def item_present(world, form, include_cookers=True):
    # type: (Any, Any, bool) -> bool
    """世界里是否出现某个物品形态（手持 / 台面 / 盘 / 散落 / 锅）。"""
    wanted = _norm(form)
    if not wanted:
        return False
    for chef in _world_list(world, "chefs"):
        if isinstance(chef, dict) and _norm(chef.get("held")) == wanted:
            return True
    for station in _world_list(world, "workstations"):
        if not isinstance(station, dict):
            continue
        for value in _values(station.get("item")):
            if _norm(value) == wanted:
                return True
    for plate in _world_list(world, "plates"):
        if not isinstance(plate, dict):
            continue
        for key in ("contents", "detail"):
            for value in _values(plate.get(key)):
                if _norm(value) == wanted:
                    return True
                if isinstance(value, dict) and _norm(value.get("n")) == wanted:
                    return True
    for item in _world_list(world, "items"):
        if isinstance(item, dict) and _norm(item.get("n")) == wanted:
            return True
    if not include_cookers:
        return False
    for cooker in _world_list(world, "cookers"):
        if not isinstance(cooker, dict):
            continue
        for key in ("cooked", "pot_contents"):
            for value in _values(cooker.get(key)):
                if _norm(value) == wanted:
                    return True
                if isinstance(value, dict) and _norm(value.get("n")) == wanted:
                    return True
    return False


def on_plate(world, form):
    # type: (Any, Any) -> bool
    wanted = _norm(form)
    if not wanted:
        return False
    for plate in _world_list(world, "plates"):
        if not isinstance(plate, dict):
            continue
        for key in ("contents", "detail"):
            for value in _values(plate.get(key)):
                if _norm(value) == wanted:
                    return True
                if isinstance(value, dict) and _norm(value.get("n")) == wanted:
                    return True
    return False


def dirty_count(world):
    # type: (Any) -> int
    count = 0
    for station in _world_list(world, "stations"):
        if isinstance(station, dict) and station.get("t") == "washing":
            try:
                count += int(station.get("plate_count") or 0)
            except (TypeError, ValueError):
                pass
    for item in _world_list(world, "items"):
        if isinstance(item, dict) and "dirtyplate" in _norm(item.get("n")):
            count += 1
    return count


def deliveries(world):
    # type: (Any) -> int
    score = getattr(world, "score", None)
    if isinstance(score, dict):
        value = score.get("deliveries")
    else:
        value = getattr(score, "deliveries", None)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _plate_forms(book, target):
    # type: (Any, Any) -> List[str]
    """target 在装盘时应出现的形态：需切取切好产物，熟料取熟料名。"""
    target = str(target or "")
    if not book or not target:
        return [target] if target else []
    if book.needs_cooking(target):
        return [target]
    if book.needs_chopping(target):
        return [book.chop_target(target)]
    return [target]


def _dish_plate_forms(book, dish):
    # type: (Any, Any) -> List[str]
    forms = []
    if not book or not dish:
        return forms
    for ingredient in book.requirements({"dish": dish}) or ():
        if book.needs_cooking(ingredient):
            forms.append(str(ingredient))
        elif book.needs_chopping(ingredient):
            forms.append(str(book.chop_target(ingredient)))
        else:
            forms.append(str(ingredient))
    return [f for f in forms if f]


def _plate_has_all(world, forms):
    # type: (Any, Iterable[str]) -> bool
    wanted = {_norm(f) for f in forms or () if _norm(f)}
    if not wanted:
        return False
    for plate in _world_list(world, "plates"):
        if not isinstance(plate, dict):
            continue
        have = set()
        for key in ("contents", "detail"):
            for value in _values(plate.get(key)):
                have.add(_norm(value))
                if isinstance(value, dict):
                    have.add(_norm(value.get("n")))
        if wanted <= have:
            return True
    return False


def _target_forms(book, name):
    # type: (Any, Any) -> Set[str]
    forms = set()
    text = str(name or "")
    if not text:
        return forms
    forms.add(text.lower())
    if book is None:
        return forms
    if book.needs_chopping(text):
        target = book.chop_target(text)
        if target:
            forms.add(str(target).lower())
    for cooked in book.cooked_of_raw(text) or ():
        forms.add(str(cooked).lower())
    for raw in book.raws_of(text) or ():
        forms.add(str(raw).lower())
        for cooked in book.cooked_of_raw(raw) or ():
            forms.add(str(cooked).lower())
    return forms


def _target_needed(world, book, command):
    # type: (Any, Any, Command) -> bool
    target = command.resolved_target or command.target
    if not target:
        return True
    live = list(_world_list(world, "orders"))
    if not live:
        return False
    if getattr(command, "mode", "material") == "dish":
        return any(str(o.get("dish")) == str(target) for o in live)
    forms = _target_forms(book, target)
    for order in live:
        try:
            requirements = book.requirements(order) if book else (order.get("composition") or ())
        except Exception:                       # noqa: BLE001
            requirements = order.get("composition") or ()
        for ingredient in requirements or ():
            if str(ingredient).lower() in forms:
                return True
    return False


def command_failed(world, book, command):
    # type: (Any, Any, Command) -> str
    """永久不可行的原因；空字符串表示还没到失败条件。"""
    if command.action == "deliver":
        if command.order_id is None:
            return ""
        if deliveries(world) > command.start_deliveries:
            return ""
        live = {str(o.get("id")) for o in _world_list(world, "orders")}
        if str(command.order_id) not in live:
            return "订单已经过期"
        return ""
    if command.action == "wash":
        return ""
    if command.action in ("chop", "cook", "mix", "plate", "fetch", "prepare"):
        if not _target_needed(world, book, command):
            return "这单已经不需要了"
    return ""


def command_done(world, book, command):
    # type: (Any, Any, Command) -> bool
    """命令的目标状态是否已经成立（纯读）。"""
    action = command.action
    target = command.resolved_target or command.target
    if action == "wash":
        return command.start_dirty > 0 and dirty_count(world) < command.start_dirty
    if action == "deliver":
        if command.start_deliveries and deliveries(world) > command.start_deliveries:
            return True
        if command.order_id is not None:
            return all(str(o.get("id")) != str(command.order_id)
                       for o in _world_list(world, "orders"))
        return False
    if action == "chop":
        if not target or not book:
            return False
        return item_present(world, book.chop_target(target))
    if action == "cook":
        if not target:
            return False
        forms = [target] if (book and book.needs_cooking(target)) else []
        if book:
            forms.extend(book.cooked_of_raw(target))
            for raw in book.raws_of(target) or ():
                forms.extend(book.cooked_of_raw(raw))
        return any(item_present(world, form, include_cookers=False)
                   for form in forms if form)
    if action == "plate":
        if not book:
            return False
        if getattr(command, "mode", "material") == "dish":
            return _plate_has_all(world, _dish_plate_forms(book, target))
        return _plate_has_all(world, _plate_forms(book, target))
    if action in ("mix", "fetch", "prepare"):
        if not target:
            return False
        return item_present(world, target)
    return False

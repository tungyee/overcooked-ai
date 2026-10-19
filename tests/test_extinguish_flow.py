# -*- coding: utf-8 -*-
"""灭火流程离线剧本：先端锅离灶、放下，再拿灭火器喷。

需求（实机确认的机制）：**锅留在灶上就会一直烧**，灭火前必须先把锅从
灶台端下来、就近放到台面/地上，否则喷了也白喷。

这里用手搓的"游戏物理"驱动真实的 Extinguish 树：树按键 → 剧本按按键改
世界（端锅 / 放锅 / 拿灭火器 / 喷灭火）→ 树再按新世界走下一步。断言的是
**效果顺序**与**落点选择**，不是按键序列本身：

  1. 锅是在拿灭火器**之前**被端走的；
  2. 锅落在台面上，绝不落回灶台（那正是"继续烧"的成因）；
  3. 灶台本身还 onFire 时，喷到火灭才算完成。
"""
from ai import tasks as T
from ai.exec.bt import Status
from ai.state.blackboard import Blackboard
from ai.output.hands import Hands
from ai.space.navigation import distance_field
from ai.query.recipes import RecipeBook

CELL = 1.2
COOKER = (5, 0)          # 灶台格
COUNTER = (2, 3)         # 台面格（离灶台不邻，是可用的落点）
EXT = (9, 0)             # 灭火器格
SPRAY_TICKS_TO_OUT = 3   # 喷几拍火灭（剧本设定）
DIRV = {"up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0)}


class _Ctrl(object):
    def __init__(self):
        self.calls = []

    def tap(self, name, hold=0.12):
        self.calls.append(("tap", name))

    def press(self, name):
        self.calls.append(("press", name))

    def release(self, name):
        self.calls.append(("release", name))

    def release_all(self):
        self.calls.append(("release_all",))


def _inter():
    return {"interactable": None, "pickup": None, "placement": None,
            "grid": None, "gx": None, "gz": None, "pickup_gx": None,
            "pickup_gz": None, "placement_gx": None, "placement_gz": None}


class _World(object):
    """只提供原语/谓词要用的属性。锅着火、灶台也 onFire。"""

    def __init__(self):
        self.stations = [
            {"t": "cooking", "n": "workstation_cooker_01",
             "gx": COOKER[0], "gz": COOKER[1],
             "x": COOKER[0] * CELL, "z": COOKER[1] * CELL},
            {"t": "counter", "n": "countertop (1)",
             "gx": COUNTER[0], "gz": COUNTER[1],
             "x": COUNTER[0] * CELL, "z": COUNTER[1] * CELL},
        ]
        # 锅在灶上烧：cooked=True（灶上有锅）+ burning/onFire
        self.cookers = [{"n": "workstation_cooker_01", "cooked": True,
                         "burning": True, "onFire": True, "isCooking": False,
                         "x": COOKER[0] * CELL, "z": COOKER[1] * CELL}]
        self.extinguishers = [{"n": "extinguisher_01", "held": None,
                               "x": EXT[0] * CELL, "z": EXT[1] * CELL}]
        # 灭火器也占着那一格（occupancy 要看得见），锅被端起后才进 items
        self.items = [{"n": "extinguisher_01", "x": EXT[0] * CELL,
                       "z": EXT[1] * CELL, "has_contents": None,
                       "cooked": None}]
        self.plates = []
        self.workstations = []
        self.chefs = []
        self.orders = []
        self.recipes = []
        self.belt_cells = set()
        self.belt_dir = {}

    def walkable(self, gx, gz):
        return 0 <= gx <= 12 and 0 <= gz <= 6

    def to_grid(self, x, z):
        return (int(round(x / CELL)), int(round(z / CELL)))

    def to_world(self, gx, gz):
        return (gx * CELL, gz * CELL)


class _Game(object):
    """树 + 手搓物理。events 记录世界侧发生的事，供断言顺序。"""

    def __init__(self):
        self.w = _World()
        self.chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
                     "interactions": _inter()}
        self.ctrl = _Ctrl()
        self.hands = Hands(self.ctrl)
        self.book = RecipeBook(self.w.recipes, self.w.stations)
        self.tree = T.Extinguish().build()
        self.events = []          # type: list
        self.status = None
        self._spray = 0
        self._pot_dropped_at = None

    # ---------- 树 ----------
    def _sync_handles(self):
        """照插件的做法：把相邻目标的交互句柄填进 chef.interactions。"""
        it = _inter()
        cell = self.w.to_grid(self.chef["x"], self.chef["z"])
        for st in self.w.stations:
            n = (st["gx"], st["gz"])
            if abs(n[0] - cell[0]) + abs(n[1] - cell[1]) == 1:
                it["gx"], it["gz"] = n
                if st["t"] == "cooking":
                    it["pickup_gx"], it["pickup_gz"] = n
                else:
                    it["placement_gx"], it["placement_gz"] = n
        for e in self.w.extinguishers:
            n = self.w.to_grid(e["x"], e["z"])
            if abs(n[0] - cell[0]) + abs(n[1] - cell[1]) == 1:
                it["gx"], it["gz"] = n
                it["pickup_gx"], it["pickup_gz"] = n
        self.chef["interactions"] = it

    def step(self, tick):
        self.hands.tick()
        self._sync_handles()
        start = len(self.ctrl.calls)
        ctx = {"w": self.w, "chef": self.chef, "hands": self.hands,
               "bb": Blackboard(), "book": self.book, "tick": tick,
               "field": distance_field(
                   self.w.to_grid(self.chef["x"], self.chef["z"]),
                   self.w.walkable, self.w.belt_dir),
               "motion": {}, "motion_failure": {}}
        self.status = self.tree.tick(ctx)
        self._physics([k for a, k in self.ctrl.calls[start:] if a == "tap"])
        return self.status

    def run(self, limit=200):
        for tick in range(limit):
            if self.step(tick) != Status.RUNNING:
                break
        return self.status

    # ---------- 手搓物理 ----------
    def _physics(self, taps):
        cell = self.w.to_grid(self.chef["x"], self.chef["z"])
        if "pickup" in taps:
            self._on_press(cell)                           # 一次按键只做一件事
        for it in self.w.items:                            # 手持物跟着人走
            if it.get("n") == self.chef.get("held"):
                it["x"], it["z"] = self.chef["x"], self.chef["z"]

        if "chop" in self.hands.held_keys and self._adjacent(cell, COOKER):
            self._spray += 1
            if self._spray >= SPRAY_TICKS_TO_OUT:
                self.w.cookers[0]["onFire"] = False
                self.w.cookers[0]["burning"] = None
                self.events.append("fire_out")

        if self.hands.move_dir and not taps:               # 移动一步（一格/拍）
            d = DIRV[self.hands.move_dir]
            self.chef["x"] += d[0] * CELL
            self.chef["z"] += d[1] * CELL

    def _on_press(self, cell):
        """一次 pickup 按键按"站在谁旁边 + 手里有没有东西"落一个效果。"""
        if self._adjacent(cell, COOKER):
            c = self.w.cookers[0]
            if self.chef["held"] is None and c["cooked"] is not None:
                c["cooked"] = None                         # 端锅离灶
                c["burning"] = None
                self.chef["held"] = "utensil_pot_01"
                self.w.items.append(
                    {"n": "utensil_pot_01", "x": self.chef["x"],
                     "z": self.chef["z"], "has_contents": True,
                     "cooked": "Burnt"})                   # 糊了：有料但不是熟料
                self.events.append("pot_taken")
            elif self.chef["held"] is not None:            # 把锅放回灶台
                self.events.append("pot_placed_on_cooker")
                self.chef["held"] = None
        elif self._adjacent(cell, COUNTER):
            if self.chef["held"] is not None:
                self.events.append("pot_placed_on_counter")
                self._pot_dropped_at = cell
                self.chef["held"] = None
        elif self._adjacent(cell, EXT):
            if self.chef["held"] is None:
                self.chef["held"] = "extinguisher_01"      # 拿灭火器
                self.events.append("extinguisher_taken")
        else:
            if self.chef["held"] is not None:              # 地板兜底落点
                self.events.append("pot_placed_on_floor")
                self.chef["held"] = None

    @staticmethod
    def _adjacent(cell, target):
        return abs(cell[0] - target[0]) + abs(cell[1] - target[1]) == 1


def test_extinguish_takes_pot_off_stove_before_fetching_extinguisher():
    """锅必须先端走、放下，再去拿灭火器——顺序错了火灭不掉。"""
    game = _Game()
    assert game.run() == Status.SUCCESS

    assert "pot_taken" in game.events, "该把锅从灶上端下来"
    assert "extinguisher_taken" in game.events
    assert game.events.index("pot_taken") < \
        game.events.index("extinguisher_taken"), "端锅必须在拿灭火器之前"


def test_extinguish_never_puts_pot_back_on_cooker():
    """端下来的锅只许放台面/地上，回灶台＝继续烧。"""
    game = _Game()
    game.run()

    assert "pot_placed_on_cooker" not in game.events, \
        "刚清空的那口灶就是一张空灶，不许把锅放回去"
    assert "pot_placed_on_counter" in game.events


def test_extinguish_sprays_only_after_pot_is_gone_and_fire_dies():
    """灶台本身还 onFire 时，喷到火灭才算完；且喷之前锅已经在手外。"""
    game = _Game()
    assert game.run() == Status.SUCCESS

    assert "fire_out" in game.events, "灶台火要灭"
    assert game.events.index("fire_out") > game.events.index("pot_taken")
    assert game.w.cookers[0]["onFire"] is False
    assert game.chef["held"] is None or "extinguisher" in game.chef["held"]


def test_extinguish_flow_is_short():
    """整条流程要在树的兜底预算内跑完（两趟差事，别撞超时）。"""
    game = _Game()
    elapsed = 0
    for tick in range(T.FIREFIGHT_TIMEOUT_TICKS):
        elapsed = tick + 1
        if game.step(tick) != Status.RUNNING:
            break
    assert game.status == Status.SUCCESS
    assert elapsed < T.FIREFIGHT_TIMEOUT_TICKS

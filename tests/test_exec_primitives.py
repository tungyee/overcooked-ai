# -*- coding: utf-8 -*-
"""原语委托（EXEC_BACKEND=plugin）测试：叶子原语发 exec 命令而非按键。"""
from ai import primitives as PR
from ai.exec.bt import Status
from ai.space.navigation import distance_field
from ai.state.blackboard import Blackboard
from ai.primitives.base import handle_desc


class FakeWorld(object):
    def __init__(self, walkable=None, cell=1.2):
        self.stations = []
        self.belt_cells = set()
        self.belt_dir = {}
        self._walk = walkable or (lambda gx, gz: True)
        self._cell = cell

    def walkable(self, gx, gz):
        return self._walk(gx, gz)

    def to_grid(self, x, z):
        return (int(round(x / self._cell)), int(round(z / self._cell)))

    def to_world(self, gx, gz):
        return (gx * self._cell, gz * self._cell)


class FakeExec(object):
    """记录 issue 收到的命令；poll 结果可编程（按顺序弹出）。"""

    def __init__(self):
        self.issued = []
        self.results = []
        self._next_id = 0

    def issue(self, cmd_dict):
        self._next_id += 1
        cmd_dict["cmd_id"] = self._next_id
        self.issued.append(dict(cmd_dict))
        return self._next_id

    def poll(self):
        return self.results.pop(0) if self.results else None

    def ok_result(self, cmd_id):
        return {"cmd_id": cmd_id, "ok": True, "why": None,
                "held_after": None, "blocked_cell": None}


def make_ctx(w, chef, ex, start=(1, 0)):
    return {
        "w": w,
        "chef": chef,
        "bb": Blackboard(),
        "field": distance_field(start, w.walkable, w.belt_dir),
        "book": None,
        "tick": 0,
        "exec": ex,
    }


W = FakeWorld(walkable=lambda gx, gz: gz == 0 and -2 <= gx <= 8)
CHEF = {"name": "Player 2", "x": 1.2, "z": 0.0}


def test_approach_issues_move_and_polls():
    ex = FakeExec()
    ctx = make_ctx(W, CHEF, ex)
    target = {"gx": 5, "gz": 0, "x": 6.0, "z": 0.0, "n": "counter (3)"}
    ap = PR.Approach(lambda c: target, kind=None)
    assert ap.tick(ctx) == Status.RUNNING
    assert len(ex.issued) == 1
    cmd = ex.issued[0]
    assert cmd["cmd"] == "move"
    assert cmd["goal"] == [4.8, 0.0]           # 目标相邻可走格 (4,0)
    assert cmd["target"]["name"] == "counter (3)"
    ex.results.append(ex.ok_result(cmd["cmd_id"]))
    assert ap.tick(ctx) == Status.SUCCESS


def test_face_and_align_noop_under_plugin():
    ex = FakeExec()
    ctx = make_ctx(W, CHEF, ex)
    assert PR.Face(lambda c: {"gx": 1, "gz": 0}).tick(ctx) == Status.SUCCESS
    assert PR.AlignToCell(lambda c: {"gx": 1, "gz": 0}, "placement").tick(ctx) == Status.SUCCESS
    assert len(ex.issued) == 0


def test_tap_issues_tap():
    ex = FakeExec()
    ctx = make_ctx(W, CHEF, ex)
    target = {"gx": 2, "gz": 0, "x": 2.4, "z": 0.0, "n": "Rice"}
    tap = PR.Tap("pickup", lambda c: target, "pickup")
    assert tap.tick(ctx) == Status.RUNNING
    assert ex.issued[0]["cmd"] == "tap"
    assert ex.issued[0]["kind"] == "pickup"
    assert ex.issued[0]["target_world"] == [2.4, 0.0]
    ex.results.append(ex.ok_result(ex.issued[0]["cmd_id"]))
    assert tap.tick(ctx) == Status.SUCCESS


def test_primitive_returns_failure_when_target_vanishes():
    ex = FakeExec()
    ctx = make_ctx(W, CHEF, ex)
    ap = PR.Approach(lambda c: None, kind=None)
    assert ap.tick(ctx) == Status.FAILURE
    assert len(ex.issued) == 0


def test_move_cmd_sets_dash_edges(monkeypatch):
    """跨缝 move 要带上落点下标，插件才能只在跳边段按 Dash。"""
    from ai.primitives import move as M

    class GapWorld(FakeWorld):
        def __init__(self):
            FakeWorld.__init__(self, walkable=lambda gx, gz: (gx, gz) != (3, 1))
            self.terrain = {9: [(0.0, 2.4, 0.0, 2.4), (3.6, 6.0, 0.0, 2.4)]}
            self.raw = {}
            self.stations = []
            self.seam_gates = None
            self.portal_map = None
            self.lane_map = None

    w = GapWorld()
    monkeypatch.setattr(
        M.portals, "route",
        lambda w, path, field, bb: ([list(w.to_world(*c)) for c in path], [], [], 0.0))
    cmd = M._move_cmd(w, [(1, 1), (2, 1), (4, 1)], None, Blackboard(),
                      [4.8, 1.2], None, None, 8000)
    assert cmd["dash"] is True
    assert cmd["dash_edges"] == [2]


def test_move_cmd_without_gap_has_empty_dash_edges(monkeypatch):
    from ai.primitives import move as M

    w = FakeWorld(walkable=lambda gx, gz: True)
    w.terrain = {9: [(0.0, 6.0, 0.0, 2.4)]}
    w.raw = {}
    w.stations = []
    w.seam_gates = None
    w.portal_map = None
    w.lane_map = None
    monkeypatch.setattr(
        M.portals, "route",
        lambda w, path, field, bb: ([list(w.to_world(*c)) for c in path], [], [], 0.0))
    cmd = M._move_cmd(w, [(1, 1), (2, 1)], None, Blackboard(),
                      [2.4, 1.2], None, None, 8000)
    assert cmd["dash"] is False
    assert cmd["dash_edges"] == []


def test_hold_issues_hold():
    ex = FakeExec()
    ctx = make_ctx(W, CHEF, ex)
    hold = PR.HoldUntil("chop", lambda c: False, 100,
                        target=lambda c: {"gx": 1, "gz": 0, "x": 1.2, "z": 0.0,
                                          "n": "board"})
    assert hold.tick(ctx) == Status.RUNNING
    assert ex.issued[0]["cmd"] == "hold"
    assert ex.issued[0]["timeout_ms"] == 100 * 50
    assert ex.issued[0]["target_world"] == [1.2, 0.0]
    ex.results.append(ex.ok_result(ex.issued[0]["cmd_id"]))
    assert hold.tick(ctx) == Status.SUCCESS


def test_hold_spray_issues_spray_kind():
    """灭火喷雾的 hold 命令带 kind="spray"：插件据此改比朝向句柄。

    端着灭火器时 use 句柄（m_interactable）是灭火器自己，不是灶；spray 让
    HoldCommand 改走 HandleMatches(kind=null)（m_gridLocation，正对灶），
    否则 UseMatches 恒不中、按不出 WorkstationInteract（实机复现）。
    """
    ex = FakeExec()
    ctx = make_ctx(W, CHEF, ex)
    hold = PR.HoldUntil("chop", lambda c: False, 100,
                        kind="spray",
                        target=lambda c: {"gx": 1, "gz": 0, "x": 1.2, "z": 0.0,
                                          "n": "workstation_cooker_01 (3)"})
    assert hold.tick(ctx) == Status.RUNNING
    assert ex.issued[0]["cmd"] == "hold"
    assert ex.issued[0]["kind"] == "spray"
    assert ex.issued[0]["target"]["name"] == "workstation_cooker_01 (3)"


def test_hold_guard_fails_and_halts():
    """守卫每拍复检：手不空就松键 FAILURE，交给树首重新清手。"""
    ex = FakeExec()
    ctx = make_ctx(W, CHEF, ex)
    ok = {"v": True}
    hold = PR.HoldUntil(
        "chop", lambda c: False, 100,
        target=lambda c: {"gx": 1, "gz": 0, "n": "board"},
        guard=lambda c: ok["v"])
    assert hold.tick(ctx) == Status.RUNNING
    assert ex.issued[-1]["cmd"] == "hold"
    ok["v"] = False
    assert hold.tick(ctx) == Status.FAILURE
    assert ex.issued[-1]["cmd"] == "halt"


def test_hold_early_exits_on_pred():
    ex = FakeExec()
    ctx = make_ctx(W, CHEF, ex)
    ctx["now"] = 0.0
    done = {"v": False}
    hold = PR.HoldUntil("chop", lambda c: done["v"], 100,
                        target=lambda c: {"gx": 1, "gz": 0, "n": "board"})
    assert hold.tick(ctx) == Status.RUNNING       # 发 hold
    assert ex.issued[0]["cmd"] == "hold"
    done["v"] = True                              # 语义完成
    assert hold.tick(ctx) == Status.SUCCESS       # 早停
    assert ex.issued[-1]["cmd"] == "halt"         # 松键


def test_hold_times_out_when_no_result():
    ex = FakeExec()
    ctx = make_ctx(W, CHEF, ex)
    ctx["now"] = 0.0
    hold = PR.HoldUntil("chop", lambda c: False, 100,
                        target=lambda c: {"gx": 1, "gz": 0, "n": "board"})
    assert hold.tick(ctx) == Status.RUNNING       # deadline = 0 + 5 + 1 = 6
    ctx["now"] = 7.0                              # 超过客户端截止
    assert hold.tick(ctx) == Status.FAILURE       # 客户端超时 FAILURE


def test_handle_desc_unified_name():
    # 盘：name 是 GameObject 名
    d = handle_desc({"n": "-14452", "name": "equipment_plate_01 (2)",
                     "x": 12.0, "z": 1.2})
    assert d["name"] == "equipment_plate_01 (2)"
    # 散料：只有 n（n 就是 GameObject 名）
    d2 = handle_desc({"n": "Rice", "x": 3.0, "z": 3.0})
    assert d2["name"] == "Rice"
    # 无 n 无 name → name 为 None
    d3 = handle_desc({"x": 3.0, "z": 3.0})
    assert d3["name"] is None


def test_handle_desc_computes_grid_from_xz():
    # 盘只有 x/z（无 gx/gz），用 to_grid 从世界坐标算网格（放柜台上的盘靠这个匹配）
    d = handle_desc({"name": "equipment_plate_01 (2)", "x": 3.6, "z": -4.8},
                    to_grid=lambda x, z: (int(round(x / 1.2)), int(round(z / 1.2))))
    assert d["gx"] == 3 and d["gz"] == -4


def test_handle_desc_none_is_none():
    """无目标发 None：插件 tap/hold/move 对 target==null 走不校验句柄分支。
    旧实现发全 None 的字典，插件当成有目标校验、必然 handle mismatch。"""
    assert handle_desc(None) is None
    assert handle_desc({}) is None


def test_handle_desc_carries_stack():
    """盘堆里的盘把 stack 带给插件。

    盘在盘堆（沥水架/回盘台）上时 grid 为 null，世界坐标换算出的格心是
    DryingPart(-7,0)，但游戏的 placement 句柄可能被同一沥水架的洗碗池抢走
    （trace-20261108-174744 t=2117：句柄 WashingPart(-7,-1)）→ 旧插件
    HandleMatches 两个字段都对不上、tap 从不按键。带 stack 让插件改用
    朝向句柄 m_gridLocation 兜底。
    """
    d = handle_desc({"n": "-38364", "name": "equipment_plate_01",
                     "x": 6.0, "z": -6.0, "grid": None,
                     "stack": "CleanPlateStack", "stack_top": True})
    assert d["stack"] == "CleanPlateStack"
    # 台面上的盘不在堆里 → 不触发兜底
    d2 = handle_desc({"n": "-19312", "name": "equipment_plate_01",
                      "x": 21.6, "z": -2.4, "grid": "GridManager"})
    assert d2["stack"] is None


def test_tap_command_carries_stack_for_stack_plate():
    """堆顶盘 tap：命令里带 stack，插件才能放宽 placement 句柄校验。"""
    ex = FakeExec()
    ctx = make_ctx(W, CHEF, ex)
    plate = {"n": "-38364", "name": "equipment_plate_01",
             "x": 6.0, "z": -6.0, "grid": None, "count": 0,
             "contents": [], "stack": "CleanPlateStack", "stack_top": True}
    tap = PR.TapWhileHolding("pickup", None, lambda c: plate, "placement")
    assert tap.tick(ctx) == Status.RUNNING
    assert ex.issued[0]["cmd"] == "tap"
    assert ex.issued[0]["kind"] == "placement"
    assert ex.issued[0]["target"]["stack"] == "CleanPlateStack"
    assert ex.issued[0]["target"]["gx"] is not None


def test_station_interact_cells_uses_reachable_hatch_side():
    """4-1：墙外死区更深但不可达，执行层必须选厨房侧 (9,2)/(9,3)。"""
    st = {"t": "plate_station", "n": "hatch",
          "x": 36.8, "z": 7.8, "gx": 10, "gz": 2}

    class Mapping(object):
        origin_x, origin_z = 24.8, 4.8
        cell = cell_z = 1.2

        def to_world(self, gx, gz):
            return (24.8 + gx * 1.2, 4.8 + gz * 1.2)

    class HatchWorld(FakeWorld):
        def __init__(self):
            FakeWorld.__init__(self)
            self.terrain = {20: [(36.2, 37.4, 6.6, 9.0)]}
            self.mapping = Mapping()
            self.stations = [st]
            self._walk = lambda gx, gz: (gx, gz) in {
                (8, 2), (8, 3), (9, 2), (9, 3),
                (11, 2), (11, 3), (12, 2), (12, 3), (13, 2), (13, 3)}

        def to_grid(self, x, z):
            return (int(round((x - 24.8) / 1.2)), int(round((z - 4.8) / 1.2)))

        def to_world(self, gx, gz):
            return Mapping().to_world(gx, gz)

    from ai.primitives.move import _station_interact_cells
    from ai.space import reachability

    w = HatchWorld()
    field = {(8, 2): 21, (8, 3): 22, (9, 2): 22, (9, 3): 23}
    goals = _station_interact_cells(st, (10, 2), w, "placement", field=field)
    assert set(goals) == {(9, 2), (9, 3)}

    # 规划层必须与执行层同口径，否则 can_interact 为真、Approach 必失败。
    ctx = {"w": w, "chef": {"name": "Player 2"}, "field": field}
    assert set(reachability._interact_goals(ctx, st)) == {(9, 2), (9, 3)}


def test_approach_waits_at_curb_until_traffic_safe():
    """4-1 过街：不安全时先走到路沿，不整段过街；灯灭满 3 秒才走。"""
    class TrafficWorld(FakeWorld):
        traffic_road = {"minx": -3.6, "maxx": 7.2, "minz": -5.0, "maxz": 5.0}
        traffic_lights = [{"n": "l1", "on": False, "on_car_b": False}]
        vehicles = []

    target = {"t": "counter", "n": "target", "gx": 8, "gz": 1,
              "x": 9.6, "z": 1.2}
    w = TrafficWorld(walkable=lambda gx, gz: gz == 1 and -8 <= gx <= 8)
    chef = {"name": "Player 2", "x": -9.6, "z": 1.2, "held": None}

    ex = FakeExec()
    ctx = make_ctx(w, chef, ex, start=(-8, 1))
    bb = Blackboard()
    bb.tick = 0
    bb.traffic_off_since = None
    ctx["bb"] = bb

    ap = PR.Approach(lambda c: target, kind="pickup")
    assert ap.tick(ctx) == Status.RUNNING
    assert ex.issued[0]["cmd"] == "move"
    # 路沿是第一个马路格 (-3,1) 的前一格 (-4,1) → 世界 (-4.8,1.2)。
    assert ex.issued[0]["goal"] == [-4.8, 1.2]
    # 到路沿只是这一段走完，不能报 SUCCESS（否则 InteractWith 会原地开按）。
    ex.results.append(ex.ok_result(ex.issued[0]["cmd_id"]))
    assert ap.tick(ctx) == Status.RUNNING

    ex2 = FakeExec()
    ctx2 = make_ctx(w, chef, ex2, start=(-8, 1))
    ctx2["bb"] = bb
    bb.tick = 60
    bb.traffic_off_since = 0
    ap2 = PR.Approach(lambda c: target, kind="pickup")
    assert ap2.tick(ctx2) == Status.RUNNING
    assert ex2.issued[0]["goal"] != [-4.8, 1.2]

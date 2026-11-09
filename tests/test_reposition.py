# -*- coding: utf-8 -*-
"""Reposition：换布局时走到「两种布局都算对」的格。

3-3 实测：换布局时全图只连通 2.0–3.4 秒，走错边就要等 31 秒才有下一次
机会。这个 task 只做一件事——在窗口打开时把角色送进安全格。
"""
from ai import tasks as T
from ai.exec.bt import Status
from ai.exec.client import NullExec
from ai.space import zoning
from ai.space.navigation import distance_field
from ai.state.blackboard import Blackboard


class FakeWorld(object):
    def __init__(self, walkable):
        self.stations = []
        self.plates = []
        self.cookers = []
        self.items = []
        self.chefs = []
        self.orders = []
        self.recipes = []
        self.extinguishers = []
        self.level = "test"
        self.belt_cells = set()
        self.belt_dir = {}
        self._walk = walkable

    def walkable(self, gx, gz):
        return self._walk(gx, gz)

    def to_grid(self, x, z):
        return (int(round(x)), int(round(z)))

    def to_world(self, gx, gz):
        return (float(gx), float(gz))


def _ctx(cell, bb, walkable=None, tick=0):
    walkable = walkable or (lambda gx, gz: 0 <= gx <= 9 and 0 <= gz <= 5)
    world = FakeWorld(walkable)
    chef = {"name": "Player 2", "x": float(cell[0]), "z": float(cell[1]),
            "held": None, "interactions": {}}
    ex = NullExec()
    return {
        "w": world,
        "chef": chef,
        "exec": ex,
        "bb": bb,
        "field": distance_field(cell, world.walkable, world.belt_dir),
        "tick": tick,
        "frame_seq": tick,
    }, ex


def _intent(goal=(9, 5), safe=((8, 4), (9, 5)), until=10 ** 9):
    return zoning.RepositionGoal(goal=goal, safe=frozenset(safe),
                                until_tick=until)


def test_no_intent_is_not_feasible():
    task = T.Reposition()
    ctx, _ = _ctx((0, 0), Blackboard())
    assert task.feasible(ctx) is False
    assert task.done(ctx) is True


def test_feasible_when_outside_safe_and_goal_reachable():
    bb = Blackboard()
    bb.reposition = _intent()
    task = T.Reposition()
    ctx, _ = _ctx((0, 0), bb)
    assert task.feasible(ctx) is True
    assert task.done(ctx) is False


def test_not_feasible_when_already_inside_safe():
    bb = Blackboard()
    bb.reposition = _intent()
    task = T.Reposition()
    ctx, _ = _ctx((9, 5), bb)
    assert task.done(ctx) is True
    assert task.feasible(ctx) is False


def test_not_feasible_when_goal_unreachable():
    bb = Blackboard()
    bb.reposition = _intent(goal=(20, 20), safe=((20, 20),))
    task = T.Reposition()
    ctx, _ = _ctx((0, 0), bb)
    assert task.feasible(ctx) is False


def test_expired_intent_is_ignored():
    bb = Blackboard()
    bb.reposition = _intent(until=5)
    task = T.Reposition()
    ctx, _ = _ctx((0, 0), bb, tick=50)
    assert task.feasible(ctx) is False


def test_build_issues_move_towards_goal():
    bb = Blackboard()
    bb.reposition = _intent()
    ctx, ex = _ctx((0, 0), bb)
    tree = T.Reposition().build()
    status = tree.tick(ctx)
    assert status == Status.RUNNING
    assert ex.issued, "没有发出 move 命令"
    assert ex.issued[0]["cmd"] == "move"


def test_build_ends_immediately_when_inside_safe():
    bb = Blackboard()
    bb.reposition = _intent()
    ctx, ex = _ctx((9, 5), bb)
    tree = T.Reposition().build()
    assert tree.tick(ctx) == Status.SUCCESS
    assert not ex.issued


def test_claims_no_resource():
    assert T.Reposition().claims() == ()


# ---------- planner「马上去」硬闸 ----------

def _planner_world():
    return type("W", (), {
        "cookers": [], "plates": [], "orders": [], "stations": [],
        "items": [], "chefs": [], "level": "Sushi_1_4_2P",
    })()


def _cand(task):
    from ai.core.planner import Candidate
    return Candidate(task, [], 15, branch=None)


def test_reposition_gate_beats_delivery():
    """换边命中闸时压过送餐（送餐晚一拍只是少一次得分，错过窗口是 31 秒）。"""
    from ai.core import planner
    deliver = T.DeliverPlate("order1", ("plate1",))
    bb = Blackboard()
    bb.tick = 10
    bb.reposition = _intent()

    chosen = planner._select_imperative(
        [_cand(deliver), _cand(T.Reposition())], _planner_world(), bb)

    assert chosen is not None
    assert chosen.task.TYPE == "Reposition"


def test_reposition_gate_yields_to_rescue():
    """有火 / 快糊 / 刚熟救锅时让位（那口锅的倒计时不可压缩）。"""
    from ai.core import planner
    take = T.TakeCookedToPlate("order1", ("c1", "plate1"), "BoiledSushiRice")
    world = type("W", (), {
        "cookers": [{"n": "c1", "cooking_time": 12.0,
                     "cooking_progress": 10.0, "cooked": True}],
        "plates": [], "orders": [], "stations": [], "items": [], "chefs": [],
        "level": "Sushi_1_4_2P",
    })()
    bb = Blackboard()
    bb.tick = 10
    bb.reposition = _intent()

    assert planner._select_imperative(
        [_cand(take), _cand(T.Reposition())], world, bb) is None


def test_candidate_generated_when_window_open():
    """窗口打开时 planner 必须能看见 Reposition 候选。"""
    from ai.core import planner
    from ai.query.recipes import RecipeBook
    bb = Blackboard()
    bb.tick = 10
    bb.reposition = _intent()
    book = RecipeBook([], [])
    cands = planner.generate_candidates(_planner_world(), bb, None, book)
    assert any(c.task.TYPE == "Reposition" for c in cands)


def test_no_candidate_when_window_closed():
    from ai.core import planner
    from ai.query.recipes import RecipeBook
    bb = Blackboard()
    bb.tick = 10
    book = RecipeBook([], [])
    cands = planner.generate_candidates(_planner_world(), bb, None, book)
    assert not any(c.task.TYPE == "Reposition" for c in cands)

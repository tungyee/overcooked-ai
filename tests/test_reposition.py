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


def test_inside_safe_still_holds_until_window_closes():
    """已经站在安全区也要占住窗口：实测它会在开窗后两秒被普通任务拽出去，
    关窗时正好落在分界线上（trace-20261109-123544 t=220→224）。"""
    bb = Blackboard()
    bb.reposition = _intent()
    task = T.Reposition()
    ctx, ex = _ctx((9, 5), bb)
    assert task.done(ctx) is False
    assert task.feasible(ctx) is True
    tree = task.build()
    assert tree.tick(ctx) == Status.RUNNING
    assert not ex.issued, "已经在安全区就不该再发 move"
    bb.reposition = None          # 窗口关掉
    assert tree.tick(ctx) == Status.SUCCESS
    assert task.done(ctx) is True


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


def test_done_only_when_window_closes():
    bb = Blackboard()
    bb.reposition = _intent()
    task = T.Reposition()
    ctx, _ = _ctx((0, 0), bb)
    assert task.done(ctx) is False
    bb.reposition = None
    assert task.done(ctx) is True


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


def _cooker_world(cooking_progress):
    return type("W", (), {
        "cookers": [{"n": "c1", "cooking_time": 12.0,
                     "cooking_progress": cooking_progress, "cooked": True}],
        "plates": [], "orders": [], "stations": [], "items": [], "chefs": [],
        "level": "Sushi_1_4_2P",
    })()


def test_reposition_gate_still_runs_when_just_cooked():
    """刚熟（L2）不让位：那口锅还有十几秒，窗口只有 2~3 秒。

    实测 trace-20261109-123544 t=118~121：整段窗口都在 TakeCookedToPlate
    （pressure 刚过 1.0），换边一次都没被选中。
    """
    from ai.core import planner
    take = T.TakeCookedToPlate("order1", ("c1", "plate1"), "BoiledSushiRice")
    world = _cooker_world(10.0)
    assert planner._time_pressure(take, world) >= 1.0
    bb = Blackboard()
    bb.tick = 10
    bb.reposition = _intent()

    chosen = planner._select_imperative(
        [_cand(take), _cand(T.Reposition())], world, bb)

    assert chosen is not None and chosen.task.TYPE == "Reposition"


def test_reposition_gate_yields_to_fire():
    """火（L0）永远优先。"""
    from ai.core import planner
    bb = Blackboard()
    bb.tick = 10
    bb.reposition = _intent()
    assert planner._select_imperative(
        [_cand(T.Extinguish()), _cand(T.Reposition())], _planner_world(), bb) is None


def test_reposition_gate_yields_to_almost_burnt():
    """快糊（L1，pressure >= 1.5）让位：再晚就真烧了。"""
    from ai.core import planner
    take = T.TakeCookedToPlate("order1", ("c1", "plate1"), "BoiledSushiRice")
    world = _cooker_world(17.0)
    assert planner._time_pressure(take, world) >= 1.5
    bb = Blackboard()
    bb.tick = 10
    bb.reposition = _intent()

    assert planner._select_imperative(
        [_cand(take), _cand(T.Reposition())], world, bb) is None


def test_reposition_preempts_command_scope():
    """命令 scope 不能把换边整个屏蔽：窗口 2~3 秒，错过就被关 31 秒。

    实测 t=152/187/220 三个窗口都因为一条迟迟完不成的 plate 命令而没换边。
    """
    from ai.core import planner
    scope = frozenset({("Chop", None)})
    repo = _cand(T.Reposition())
    kept = planner._scope_with_preemption([repo], scope)
    assert repo in kept


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


def test_assembly_gets_a_small_constant_bonus():
    """组装（放料进饼底 / 送进烤箱）同分时要压过再取一份料。"""
    from ai.core import planner
    from ai.query.recipes import RecipeBook
    from ai.state.world import World

    world = World({
        "phase": "level", "level": "Wizard_3_2_2P", "paused": False,
        "orders": [{"id": "1", "dish": "MargheritaPizza", "composition": [],
                    "remaining": 90, "lifetime": 130}],
        "recipes": [], "stations": [], "chefs": [], "items": [],
        "plates": [], "workstations": [], "cookers": [], "extinguishers": [],
        "grid": {"size": [1.2, 1.0, 1.2], "nodeMap": None},
    })
    book = RecipeBook(world.recipes, world.stations)
    bb = Blackboard()
    bb.tick = 100

    carrier = T.FetchToCarrier("1", ("Pizza_Base_Raw",), "Tomato")
    chop = T.Chop("1", ("board",), "Tomato")
    _score, carrier_terms = planner._score_candidate(
        world, bb, _cand(carrier), book)
    _score, chop_terms = planner._score_candidate(world, bb, _cand(chop), book)

    assert carrier_terms["assembly"] == planner.W_ASSEMBLY
    assert chop_terms["assembly"] == 0.0

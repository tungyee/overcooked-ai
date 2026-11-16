# -*- coding: utf-8 -*-
"""命令 scope 的高优先级抢占：熟锅/救锅/灭火可越过用户命令。"""
from ai.core import planner


class _Task(object):
    def __init__(self, task_type, item=None):
        self.TYPE = task_type
        self.item = item


def _cand(task_type, item=None):
    return planner.Candidate(_Task(task_type, item), [], 1)


def test_preempt_types_are_decay_sensitive():
    assert planner._preempts_command(_Task("TakeCookedToPlate"))
    assert planner._preempts_command(_Task("RescueCooked"))
    assert planner._preempts_command(_Task("Extinguish"))
    assert not planner._preempts_command(_Task("FetchToBoard"))
    assert not planner._preempts_command(_Task("Chop"))


def test_command_scope_keeps_scoped_and_adds_urgent():
    scope = frozenset({("Chop", "cucumber")})
    scoped = _cand("Chop", "Cucumber")
    normal = _cand("WashPlate")
    take = _cand("TakeCookedToPlate")
    rescue = _cand("RescueCooked")
    got = planner._scope_with_preemption(
        [scoped, normal, take, rescue], scope)
    assert [c.task.TYPE for c in got] == [
        "Chop", "TakeCookedToPlate", "RescueCooked"]


def test_plan_preempts_command_with_cooked_pot():
    """命令 scope 是 wash 时，熟锅的 TakeCookedToPlate 仍被选中。"""
    from ai.query.recipes import RecipeBook
    from ai.state.blackboard import Blackboard
    from ai.state.world import World

    recipes = [{"name": "CookedFish", "comp": ["Fish"], "cook": "cooker"}]
    orders = [{"id": "order1", "dish": "FishSushi",
               "composition": ["CookedFish"], "remaining": 30}]
    stations = [
        {"t": "crate", "n": "crate_fish", "ing": "Fish", "gx": 3, "gz": 1},
        {"t": "counter", "n": "counter_01", "gx": 5, "gz": 5},
    ]
    cookers = [{"t": "cooking", "n": "cooker_01", "gx": 2, "gz": 1,
                "x": 16.8, "z": -4.8, "cooked": True, "isCooking": True,
                "pot_contents": ["ing:Fish"]}]
    world = World({"orders": orders, "stations": stations,
                   "recipes": recipes, "chefs": [], "plates": [],
                   "workstations": [], "cookers": cookers,
                   "grid": {"nodeMap": None}})
    px, pz = world.to_world(5, 5)
    world.plates = [{"n": "plate_A", "x": px, "z": pz,
                     "count": 0, "contents": []}]
    bb = Blackboard()
    bb.tick = 10
    bb.command_scope = frozenset({("WashPlate", None)})
    book = RecipeBook(recipes, stations)

    chosen, record = planner.plan(world, bb, None, book)

    assert chosen is not None and chosen.TYPE == "TakeCookedToPlate"
    assert not record.get("command_scope_empty")


def test_command_scope_still_empty_without_urgent():
    scope = frozenset({("Chop", "cucumber")})
    got = planner._scope_with_preemption(
        [_cand("WashPlate"), _cand("FetchToBoard")], scope)
    assert got == []

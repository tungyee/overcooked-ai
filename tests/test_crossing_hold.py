# -*- coding: utf-8 -*-
"""过街承诺：已经走到马路上时，不因“刚熟”换任务。"""
from ai.core import planner
from ai.state.blackboard import Blackboard, Commitment


class _Task(object):
    def __init__(self, task_type):
        self.TYPE = task_type
        self.item = None


class _World(object):
    traffic_road = {"minx": 21.2, "maxx": 32.0, "minz": -4.0, "maxz": 15.0}
    traffic_lights = [{"n": "l1", "on": False, "on_car_b": False}]
    cookers = []
    orders = []

    def __init__(self, x=24.8, z=6.0):
        self.chefs = [{"name": "Player 2", "x": x, "z": z, "held": None}]

    def to_grid(self, x, z):
        return (int(round((x - 24.8) / 1.2)), int(round((z - 4.8) / 1.2)))

    def to_world(self, gx, gz):
        return (24.8 + gx * 1.2, 4.8 + gz * 1.2)


def _bb(current):
    bb = Blackboard()
    bb.tick = 100
    bb.commitment = Commitment(active_task=current)
    return bb


def test_hold_current_task_while_on_road(monkeypatch):
    current = _Task("FetchToBoard")
    other = _Task("FillCooker")
    bb = _bb(current)
    feasible = [planner.Candidate(current, [], 10),
                planner.Candidate(other, [], 10)]
    monkeypatch.setattr(planner, "_time_pressure", lambda task, world: 1.0)
    assert planner._crossing_hold(_World(), bb, feasible, other) is current


def test_no_hold_when_not_on_road(monkeypatch):
    current = _Task("FetchToBoard")
    other = _Task("FillCooker")
    bb = _bb(current)
    feasible = [planner.Candidate(current, [], 10),
                planner.Candidate(other, [], 10)]
    monkeypatch.setattr(planner, "_time_pressure", lambda task, world: 1.0)
    assert planner._crossing_hold(_World(x=15.2, z=6.0), bb, feasible, other) is None


def test_emergency_can_break_crossing(monkeypatch):
    current = _Task("FetchToBoard")
    rescue = _Task("TakeCookedToPlate")
    bb = _bb(current)
    feasible = [planner.Candidate(current, [], 10),
                planner.Candidate(rescue, [], 10)]
    monkeypatch.setattr(planner, "_time_pressure", lambda task, world: 1.6)
    assert planner._crossing_hold(_World(), bb, feasible, rescue) is None


def test_fire_can_break_crossing(monkeypatch):
    current = _Task("FetchToBoard")
    fire = _Task("Extinguish")
    bb = _bb(current)
    feasible = [planner.Candidate(current, [], 10),
                planner.Candidate(fire, [], 10)]
    monkeypatch.setattr(planner, "_time_pressure", lambda task, world: 0.0)
    assert planner._crossing_hold(_World(), bb, feasible, fire) is None


def test_no_hold_when_current_no_longer_feasible(monkeypatch):
    current = _Task("FetchToBoard")
    other = _Task("FillCooker")
    bb = _bb(current)
    feasible = [planner.Candidate(other, [], 10)]
    monkeypatch.setattr(planner, "_time_pressure", lambda task, world: 1.0)
    assert planner._crossing_hold(_World(), bb, feasible, other) is None

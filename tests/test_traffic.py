# -*- coding: utf-8 -*-
"""4-1 过街：灯态/车 AABB 门控与路沿等待。"""
from ai.space import traffic
from ai.state.blackboard import Blackboard


class _World(object):
    def __init__(self, lights=None, cars=None, road=True):
        self.traffic_road = ({"minx": 21.2, "maxx": 32.0,
                              "minz": -4.0, "maxz": 15.0} if road else None)
        self.traffic_lights = list(lights if lights is not None else
                                   [{"n": "l1", "on": False, "on_car_b": False}])
        self.vehicles = list(cars or ())

    def to_world(self, gx, gz):
        return (24.8 + gx * 1.2, 4.8 + gz * 1.2)


LIGHT_OFF = [{"n": "l1", "on": False, "on_car_b": False},
             {"n": "l2", "on": False, "on_car_b": False}]
LIGHT_ON = [{"n": "l1", "on": True, "on_car_b": False},
            {"n": "l2", "on": True, "on_car_b": False}]
CAR_FAR = [{"n": "Blue_Car", "minx": 23.5, "maxx": 25.6,
            "minz": 30.0, "maxz": 34.0}]
CAR_ON_PATH = [{"n": "Blue_Car", "minx": 23.5, "maxx": 25.6,
                "minz": 4.0, "maxz": 6.0}]


def _bb(tick=60, off_since=0):
    bb = Blackboard()
    bb.tick = tick
    bb.traffic_off_since = off_since
    return bb


def test_no_traffic_data_is_safe():
    w = _World(lights=[], road=False)
    assert traffic.safe_to_cross(w, _bb(), [(-8, 1), (0, 1), (8, 1)]) is True


def test_path_not_crossing_road_is_safe():
    w = _World()
    assert traffic.safe_to_cross(w, _bb(), [(-8, 1), (-7, 1)]) is True


def test_lights_on_blocks_crossing():
    w = _World(lights=LIGHT_ON)
    assert traffic.safe_to_cross(w, _bb(), [(-8, 1), (0, 1), (8, 1)]) is False


def test_both_off_must_last_three_seconds():
    w = _World(lights=LIGHT_OFF)
    assert traffic.safe_to_cross(w, _bb(off_since=0, tick=30),
                                 [(-8, 1), (0, 1), (8, 1)]) is False
    assert traffic.safe_to_cross(w, _bb(off_since=0, tick=60),
                                 [(-8, 1), (0, 1), (8, 1)]) is True


def test_car_aabb_on_path_blocks_crossing():
    w = _World(lights=LIGHT_OFF, cars=CAR_ON_PATH)
    assert traffic.safe_to_cross(w, _bb(), [(-8, 1), (0, 1), (8, 1)]) is False


def test_car_far_away_does_not_block():
    w = _World(lights=LIGHT_OFF, cars=CAR_FAR)
    assert traffic.safe_to_cross(w, _bb(), [(-8, 1), (0, 1), (8, 1)]) is True


def test_already_on_road_keeps_going_even_if_light_on():
    w = _World(lights=LIGHT_ON)
    assert traffic.safe_to_cross(w, _bb(), [(-8, 1), (0, 1), (8, 1)],
                                 cur=(0, 1)) is True


def test_observe_tracks_both_off_since():
    bb = Blackboard()
    bb.tick = 10
    traffic.observe(_World(lights=LIGHT_OFF), bb)
    assert bb.traffic_off_since == 10
    traffic.observe(_World(lights=LIGHT_ON), bb)
    assert bb.traffic_off_since is None


def test_curb_index_is_cell_before_first_road_cell():
    w = _World()
    path = [(-8, 1), (-4, 1), (-3, 1), (0, 1), (6, 1), (8, 1)]
    assert traffic.curb_index(w, path) == 1
    assert traffic.curb_index(w, [(-3, 1), (0, 1)]) is None

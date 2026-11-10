# -*- coding: utf-8 -*-
"""4-1 过街：行人红灯/车 AABB 门控与路沿等待。"""
from ai.space import traffic
from ai.state.blackboard import Blackboard


class _World(object):
    def __init__(self, lights=None, cars=None, road=True):
        self.traffic_road = ({"minx": 21.2, "maxx": 32.0,
                              "minz": -4.0, "maxz": 15.0} if road else None)
        self.traffic_lights = list(lights if lights is not None else LIGHT_RED)
        self.vehicles = list(cars or ())

    def to_world(self, gx, gz):
        return (24.8 + gx * 1.2, 4.8 + gz * 1.2)


def _lamp(n, red=False, yellow=False, green=False, on=False, on_b=False):
    return {"n": n, "on": on, "on_car_b": on_b,
            "red": red, "yellow": yellow, "green": green,
            "has_lamps": True}


LIGHT_RED = [_lamp("l1", red=True), _lamp("l2", red=True)]
LIGHT_GREEN = [_lamp("l1", green=True, on=True), _lamp("l2", green=True, on=True)]
LIGHT_YELLOW = [_lamp("l1", yellow=True, on_b=True),
                _lamp("l2", yellow=True, on_b=True)]
# 旧插件帧：没有 red/yellow/green，只剩车灯 bool。
LIGHT_OFF_OLD = [{"n": "l1", "on": False, "on_car_b": False},
                 {"n": "l2", "on": False, "on_car_b": False}]
LIGHT_ON_OLD = [{"n": "l1", "on": True, "on_car_b": False},
                {"n": "l2", "on": True, "on_car_b": False}]
CAR_FAR = [{"n": "Blue_Car", "minx": 23.5, "maxx": 25.6,
            "minz": 30.0, "maxz": 34.0}]
CAR_ON_PATH = [{"n": "Blue_Car", "minx": 23.5, "maxx": 25.6,
                "minz": 4.0, "maxz": 6.0}]
PATH = [(-8, 1), (0, 1), (8, 1)]


def _bb(tick=60, red_since=None):
    bb = Blackboard()
    bb.tick = tick
    # 默认当作「这一拍刚看到红灯」；测红灯尾巴就传更早的拍号。
    bb.traffic_red_since = tick if red_since is None else red_since
    return bb


def test_no_traffic_data_is_safe():
    w = _World(lights=[], road=False)
    assert traffic.safe_to_cross(w, _bb(), PATH) is True


def test_path_not_crossing_road_is_safe():
    w = _World()
    assert traffic.safe_to_cross(w, _bb(), [(-8, 1), (-7, 1)]) is True


def test_red_lamp_is_safe_immediately():
    """红灯行：红灯刚亮就当拍可走。"""
    w = _World(lights=LIGHT_RED)
    assert traffic.safe_to_cross(w, _bb(tick=0), PATH) is True
    assert traffic.safe_to_cross(w, _bb(tick=1), PATH) is True


def test_long_red_is_safe_to_start():
    """红灯可能亮很久（4-1 开局长红灯实测 20~27s），长红灯照常起步。"""
    w = _World(lights=LIGHT_RED)
    bb = _bb(tick=1000, red_since=1000 - traffic.RED_LONG_TICKS)
    assert traffic.safe_to_cross(w, bb, PATH) is True


def test_short_red_tail_waits_for_next_cycle():
    """短红灯的尾巴（离绿灯太近）不起步，等下一轮。"""
    w = _World(lights=LIGHT_RED)
    bb = _bb(tick=100, red_since=100 - traffic.RED_FRESH_TICKS - 5)
    assert traffic.safe_to_cross(w, bb, PATH) is False


def test_observe_tracks_red_onset():
    w = _World(lights=LIGHT_GREEN)
    bb = _bb(tick=10)
    bb.traffic_red_since = None
    traffic.observe(w, bb)
    assert bb.traffic_red_since is None
    w.traffic_lights = LIGHT_RED
    traffic.observe(w, bb)
    assert bb.traffic_red_since == 10
    bb.tick = 12
    traffic.observe(w, bb)
    assert bb.traffic_red_since == 10       # 红灯持续期间起点不变
    w.traffic_lights = LIGHT_YELLOW
    traffic.observe(w, bb)
    assert bb.traffic_red_since is None


def test_curb_cell_uses_chef_position_not_cell_center():
    """路沿格心压在 road 边界上，不能因此当成「已在过街」绕开红绿灯。"""
    w = _World(lights=LIGHT_GREEN)
    curb = (-3, 1)          # 格心 (21.2, 6.0) == road.minx
    path = [(0, 1), (8, 1)]
    chef = {"name": "Player 2", "x": 21.0, "z": 6.0}   # 实际站在路沿外
    # 旧口径（只看格心）会短路成 True，这正是实机被撞的原因。
    assert traffic.safe_to_cross(w, _bb(), path, cur=curb) is True
    assert traffic.safe_to_cross(w, _bb(), path, cur=curb, chef=chef) is False


def test_chef_actually_on_road_keeps_going():
    w = _World(lights=LIGHT_GREEN)
    chef = {"name": "Player 2", "x": 24.8, "z": 6.0}
    assert traffic.safe_to_cross(w, _bb(), PATH, cur=(0, 1), chef=chef) is True


def test_green_lamp_blocks_crossing():
    w = _World(lights=LIGHT_GREEN)
    assert traffic.safe_to_cross(w, _bb(), PATH) is False


def test_yellow_lamp_blocks_crossing():
    w = _World(lights=LIGHT_YELLOW)
    assert traffic.safe_to_cross(w, _bb(), PATH) is False


def test_one_red_one_green_blocks_crossing():
    w = _World(lights=[_lamp("l1", red=True), _lamp("l2", green=True, on_b=True)])
    assert traffic.safe_to_cross(w, _bb(), PATH) is False


def test_car_aabb_on_path_blocks_crossing_even_on_red():
    w = _World(lights=LIGHT_RED, cars=CAR_ON_PATH)
    assert traffic.safe_to_cross(w, _bb(), PATH) is False


def test_car_far_away_does_not_block():
    w = _World(lights=LIGHT_RED, cars=CAR_FAR)
    assert traffic.safe_to_cross(w, _bb(), PATH) is True


def test_already_on_road_keeps_going_even_if_green():
    w = _World(lights=LIGHT_GREEN)
    assert traffic.safe_to_cross(w, _bb(), PATH, cur=(0, 1)) is True


def test_no_lamp_fields_blocks_crossing():
    """没有灯色字段（旧 DLL/子灯没找到）就不猜车灯，停在路沿。"""
    w = _World(lights=LIGHT_OFF_OLD)
    assert traffic.safe_to_cross(w, _bb(tick=0), PATH) is False
    assert traffic.safe_to_cross(_World(lights=LIGHT_ON_OLD), _bb(), PATH) is False


def test_has_lamps_false_blocks_crossing():
    lights = [{"n": "l1", "on": False, "on_car_b": False, "has_lamps": False}]
    assert traffic.safe_to_cross(_World(lights=lights), _bb(), PATH) is False


def test_pedestrian_red_reads_lamps():
    assert traffic.pedestrian_red(_World(lights=LIGHT_RED)) is True
    assert traffic.pedestrian_red(_World(lights=LIGHT_GREEN)) is False
    assert traffic.pedestrian_red(_World(lights=LIGHT_OFF_OLD)) is None
    assert traffic.pedestrian_red(_World(lights=[])) is None


def test_curb_index_is_cell_before_first_road_cell():
    w = _World()
    path = [(-8, 1), (-4, 1), (-3, 1), (0, 1), (6, 1), (8, 1)]
    assert traffic.curb_index(w, path) == 1
    assert traffic.curb_index(w, [(-3, 1), (0, 1)]) is None


def test_road_between_detects_opposite_sides():
    w = _World()
    assert traffic.road_between(w, (18.0, 6.0), (33.0, 6.0)) is True
    assert traffic.road_between(w, (33.0, 6.0), (18.0, 6.0)) is True
    assert traffic.road_between(w, (18.0, 6.0), (19.0, 6.0)) is False
    assert traffic.road_between(w, (33.0, 6.0), (34.0, 6.0)) is False
    assert traffic.road_between(_World(road=False), (18.0, 6.0), (33.0, 6.0)) is False


def test_median_cell_is_between_car_lanes():
    """中线等待格：两辆车 x 区间之间的缝隙里的路径格。"""
    w = _World(cars=[
        {"n": "A", "minx": 21.4, "maxx": 22.6, "minz": -10, "maxz": 40},
        {"n": "B", "minx": 27.0, "maxx": 28.6, "minz": -10, "maxz": 40},
    ])
    # 路径横穿：格心 21.2 .. 32.0（1.2 一格）。缝隙 (22.6, 27.0)，中点 24.8 → 格 (0,1)。
    path = [(gx, 1) for gx in range(-3, 7)]
    assert traffic.median_cell(w, path) == (0, 1)


def test_no_median_with_single_car_or_no_path():
    w = _World(cars=[{"n": "A", "minx": 21.4, "maxx": 22.6,
                      "minz": -10, "maxz": 40}])
    assert traffic.median_cell(w, [(0, 1)]) is None
    assert traffic.median_cell(_World(), [(-8, 1), (-7, 1)]) is None


def test_before_median_uses_travel_direction():
    w = _World(cars=[
        {"n": "A", "minx": 21.4, "maxx": 22.6, "minz": -10, "maxz": 40},
        {"n": "B", "minx": 27.0, "maxx": 28.6, "minz": -10, "maxz": 40},
    ])
    path = [(gx, 1) for gx in range(-3, 7)]
    spot = traffic.median_spot(w, path)
    assert spot[0] == (0, 1)
    assert abs(spot[1][0] - 24.8) < 1e-6      # 缝隙 (22.6, 27.0) 的中点
    assert traffic.before_median(w, {"x": 21.8, "z": 6.0}, spot, path) is True
    assert traffic.before_median(w, {"x": 25.2, "z": 6.0}, spot, path) is False
    # 反向走：从东往西，还没过中线的人 x 更大。
    west = list(reversed(path))
    assert traffic.before_median(w, {"x": 29.0, "z": 6.0}, spot, west) is True
    assert traffic.before_median(w, {"x": 24.0, "z": 6.0}, spot, west) is False




# ---------------------------------------------------------------- 按车道放行

LIGHT_BLUE_RUN = [_lamp("l1", green=True, on=True),
                  _lamp("l2", green=True, on=True)]
LIGHT_GREEN_RUN = [_lamp("l1", green=True, on_b=True),
                   _lamp("l2", green=True, on_b=True)]

BLUE_CAR = {"n": "Blue_Car", "minx": 23.55, "maxx": 25.63,
            "minz": -10, "maxz": 40}
GREEN_CAR = {"n": "Green_Car", "minx": 27.68, "maxx": 29.38,
             "minz": -10, "maxz": 40}


def test_car_running_reads_per_car_lamps():
    """每个周期只有一辆车跑：on 是 Blue_Car，on_car_b 是 Green_Car。"""
    w = _World(cars=[BLUE_CAR, GREEN_CAR], lights=LIGHT_BLUE_RUN)
    assert traffic.car_running(w, BLUE_CAR) is True
    assert traffic.car_running(w, GREEN_CAR) is False
    w = _World(cars=[BLUE_CAR, GREEN_CAR], lights=LIGHT_GREEN_RUN)
    assert traffic.car_running(w, BLUE_CAR) is False
    assert traffic.car_running(w, GREEN_CAR) is True
    assert traffic.car_running(w, {"n": "Unknown"}) is None


def test_split_at_median_by_direction():
    w = _World(cars=[BLUE_CAR, GREEN_CAR])
    path = [(gx, 1) for gx in range(-3, 7)]
    spot = traffic.median_spot(w, path)
    near, far = traffic.split_at_median(w, path, spot)
    assert all(w.to_world(*c)[0] <= spot[1][0] + 0.1 for c in near)
    assert all(w.to_world(*c)[0] > spot[1][0] + 0.1 for c in far)
    # 反向：近侧变成东半，远侧是西半。
    near_w, far_w = traffic.split_at_median(w, list(reversed(path)), spot)
    assert all(w.to_world(*c)[0] >= spot[1][0] - 0.1 for c in near_w)
    assert all(w.to_world(*c)[0] < spot[1][0] - 0.1 for c in far_w)


def test_leg_safe_only_needs_the_lane_car_stopped():
    """一个周期只有一辆车跑：另一条车道的腿照样能走（玩家也把料扔在路中间）。"""
    west_cells = [(-2, 1), (-1, 1), (0, 1)]      # 只压 Blue 车道
    east_cells = [(2, 1), (3, 1), (4, 1)]        # 只压 Green 车道
    w = _World(cars=[BLUE_CAR, GREEN_CAR], lights=LIGHT_GREEN_RUN)
    assert traffic.leg_safe(w, west_cells) is True    # Blue 停着
    assert traffic.leg_safe(w, east_cells) is False   # Green 在跑
    w = _World(cars=[BLUE_CAR, GREEN_CAR], lights=LIGHT_BLUE_RUN)
    assert traffic.leg_safe(w, west_cells) is False
    assert traffic.leg_safe(w, east_cells) is True
    # 红灯（两车都停）两条腿都安全。
    w = _World(cars=[BLUE_CAR, GREEN_CAR], lights=LIGHT_RED)
    assert traffic.leg_safe(w, west_cells + east_cells) is True

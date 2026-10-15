"""落点约束单测（spec §6.1 / §7.4）。

旧 _find_drop_cell 的筛选逻辑是功能性的不是优化：贴灶台会继续加热、
贴出餐口会被误上菜、贴拼装台会挡盘。这些断言就是那三条。
"""
import pytest
from ai import placement
from ai.navigation import distance_field


class FakeWorld(object):
    """只提供 placement 要用的几个属性。"""

    def __init__(self, stations=None, plates=None, belt_cells=None,
                 belt_dir=None, walkable=None, cookers=None):
        self.stations = stations or []
        self.plates = plates or []
        self.cookers = cookers or []
        self.belt_cells = set(belt_cells or ())
        self.belt_dir = belt_dir or {}
        self._walk = walkable or (lambda gx, gz: True)

    def walkable(self, gx, gz):
        return self._walk(gx, gz)

    def to_grid(self, x, z):
        return (int(round(x / 1.2)), int(round(z / 1.2)))

    def to_world(self, gx, gz):
        return (gx * 1.2, gz * 1.2)


def test_forbidden_includes_cooker_neighbors():
    """贴灶台的格不可落 —— 锅放那儿会继续加热。"""
    sts = [{"t": "cooking", "n": "cooker1", "gx": 5, "gz": 0}]
    bad = placement.forbidden_adjacent_cells(sts)
    assert (4, 0) in bad and (6, 0) in bad
    assert (5, 1) in bad and (5, -1) in bad
    assert (3, 0) not in bad


def test_forbidden_includes_plate_station_neighbors():
    """贴出餐口的格不可落 —— 东西放那儿会被误上菜。"""
    sts = [{"t": "plate_station", "n": "window", "gx": 9, "gz": 0}]
    bad = placement.forbidden_adjacent_cells(sts)
    assert (8, 0) in bad


def test_forbidden_skips_ordinary_counters():
    """普通台面不是禁区 —— 否则整张图都落不下。"""
    sts = [{"t": "counter", "n": "c", "gx": 2, "gz": 2}]
    assert placement.forbidden_adjacent_cells(sts) == set()


def test_forbidden_skips_stations_without_grid():
    """实测 71 个站点里 10 个没有 gx/gz，不许崩。"""
    sts = [{"t": "cooking", "n": "c"},
           {"t": "plate_station", "n": "w", "gx": None, "gz": None}]
    assert placement.forbidden_adjacent_cells(sts) == set()


def test_find_drop_station_prefers_empty_counter():
    """spec §7.4：优先放就近的合法空台面，不是地上。"""
    sts = [
        {"t": "counter", "n": "near", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0},
        {"t": "counter", "n": "far", "gx": 6, "gz": 0, "x": 7.2, "z": 0.0},
    ]
    w = FakeWorld(stations=sts, walkable=lambda gx, gz: gz == 0)
    field = distance_field((0, 0), w.walkable, {})
    got = placement.find_drop_station(w, field)
    assert got is not None and got["n"] == "near"


def test_find_drop_station_skips_occupied_counter():
    """台面上已有盘 → 不能往上放（会把盘顶掉或放不下）。"""
    sts = [
        {"t": "counter", "n": "busy", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0},
        {"t": "counter", "n": "free", "gx": 3, "gz": 0, "x": 3.6, "z": 0.0},
    ]
    plates = [{"n": "p1", "x": 1.2, "z": 0.0, "count": 2,
               "contents": ["Seaweed", "Cucumber"]}]
    w = FakeWorld(stations=sts, plates=plates,
                  walkable=lambda gx, gz: gz == 0)
    field = distance_field((0, 0), w.walkable, {})
    got = placement.find_drop_station(w, field)
    assert got is not None and got["n"] == "free"


def test_find_drop_station_skips_assembly_counter():
    """盘所在的那格就是在用的拼装位 → 它的四邻也是禁区（会挡盘）。

    PLATE_COUNTERS 硬编码表归 Plan 3 删/替（spec §9），所以这里只能从
    已就位的盘反推拼装位。
    """
    sts = [
        {"t": "counter", "n": "beside_plate", "gx": 1, "gz": 0,
         "x": 1.2, "z": 0.0},
        {"t": "counter", "n": "clear", "gx": 4, "gz": 0, "x": 4.8, "z": 0.0},
    ]
    plates = [{"n": "p1", "x": 2.4, "z": 0.0, "count": 1,
               "contents": ["Seaweed"]}]
    w = FakeWorld(stations=sts, plates=plates,
                  walkable=lambda gx, gz: gz == 0)
    field = distance_field((0, 0), w.walkable, {})
    got = placement.find_drop_station(w, field)
    assert got is not None and got["n"] == "clear"


def test_find_drop_station_unreachable_is_none():
    sts = [{"t": "counter", "n": "island", "gx": 50, "gz": 50,
            "x": 60.0, "z": 60.0}]
    w = FakeWorld(stations=sts, walkable=lambda gx, gz: gz == 0 and gx < 5)
    field = distance_field((0, 0), w.walkable, {})
    assert placement.find_drop_station(w, field) is None


def test_find_drop_station_honors_extra_forbidden():
    """调用方可追加禁区（如正在灭的那口灶周边）。"""
    sts = [{"t": "counter", "n": "c", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0}]
    w = FakeWorld(stations=sts, walkable=lambda gx, gz: gz == 0)
    field = distance_field((0, 0), w.walkable, {})
    assert placement.find_drop_station(w, field) is not None
    assert placement.find_drop_station(
        w, field, extra_forbidden={(1, 0)}) is None


def test_find_drop_cell_floor_fallback():
    """没有合法空台面时退到地板格。"""
    w = FakeWorld(walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 3)
    field = distance_field((0, 0), w.walkable, {})
    cell = placement.find_drop_cell(w, field)
    assert cell is not None
    assert w.walkable(cell[0], cell[1])


# ---------- 空灶台（手持空锅归位用）----------

def test_find_empty_cooker_skips_cooker_that_has_a_pot():
    """灶上有锅就不是空灶 —— 锅叠不上去。"""
    sts = [{"t": "cooking", "n": "ck1", "gx": 2, "gz": 0, "x": 2.4, "z": 0.0}]
    w = FakeWorld(stations=sts, cookers=[{"n": "ck1", "cooked": True}],
                  walkable=lambda gx, gz: gz == 0)
    field = distance_field((0, 0), w.walkable, {})
    assert placement.find_empty_cooker(w, field) is None


def test_find_empty_cooker_skips_unreadable_state():
    """状态读不到的灶台不当成"空的"——读不到就不能用。"""
    sts = [{"t": "cooking", "n": "ck1", "gx": 2, "gz": 0, "x": 2.4, "z": 0.0}]
    w = FakeWorld(stations=sts, cookers=[], walkable=lambda gx, gz: gz == 0)
    field = distance_field((0, 0), w.walkable, {})
    assert placement.find_empty_cooker(w, field) is None


def test_find_empty_cooker_picks_nearest_reachable():
    sts = [{"t": "cooking", "n": "far", "gx": 8, "gz": 0, "x": 9.6, "z": 0.0},
           {"t": "cooking", "n": "near", "gx": 2, "gz": 0, "x": 2.4, "z": 0.0}]
    w = FakeWorld(stations=sts,
                  cookers=[{"n": "near", "cooked": None},
                           {"n": "far", "cooked": None}],
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 9)
    field = distance_field((0, 0), w.walkable, {})
    got = placement.find_empty_cooker(w, field)
    assert got is not None and got["n"] == "near"


def test_find_empty_cooker_unreachable_is_none():
    sts = [{"t": "cooking", "n": "island", "gx": 50, "gz": 50,
            "x": 60.0, "z": 60.0}]
    w = FakeWorld(stations=sts, cookers=[{"n": "island", "cooked": None}],
                  walkable=lambda gx, gz: gz == 0 and gx < 5)
    field = distance_field((0, 0), w.walkable, {})
    assert placement.find_empty_cooker(w, field) is None


def test_find_drop_cell_avoids_belt():
    """传送带格不可落 —— 东西会被冲走（spec §7.4）。"""
    w = FakeWorld(belt_cells={(0, 0), (1, 0)},
                  belt_dir={(0, 0): (1, 0), (1, 0): (1, 0)},
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 3)
    field = distance_field((0, 0), w.walkable, {})
    cell = placement.find_drop_cell(w, field)
    assert cell not in ((0, 0), (1, 0))


def test_find_drop_cell_avoids_forbidden_adjacency():
    sts = [{"t": "cooking", "n": "cooker", "gx": 2, "gz": 0}]
    w = FakeWorld(stations=sts,
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 4)
    field = distance_field((0, 0), w.walkable, {})
    cell = placement.find_drop_cell(w, field)
    assert cell not in ((1, 0), (3, 0)), "不该落在贴灶台的格上"


def test_find_drop_cell_none_when_nothing_legal():
    """全图都是禁区 → None。调用方据此转 FAILURE，不是无限找。"""
    sts = [{"t": "cooking", "n": "c", "gx": 1, "gz": 0}]
    w = FakeWorld(stations=sts, walkable=lambda gx, gz: (gx, gz) in
                  ((0, 0), (2, 0)))
    field = distance_field((0, 0), w.walkable, {})
    assert placement.find_drop_cell(w, field) is None


def test_find_drop_on_real_frame(level_frame):
    """真实帧：既能找到空台面，也能找到地板落点。

    若两者都 None，说明禁区规则把整张图筛空了——那 EnsureHandsFree
    会永远 FAILURE，所有中断 task 都起不来。
    """
    from ai.world import World
    w = World(level_frame)
    chef = None
    for c in w.chefs:
        if c.get("x") is not None:
            chef = c
            break
    if chef is None:
        pytest.skip("样本里没有带世界坐标的厨师")
    field = distance_field(w.to_grid(chef["x"], chef["z"]),
                           w.walkable, w.belt_dir)

    st = placement.find_drop_station(w, field)
    cell = placement.find_drop_cell(w, field)
    assert st is not None or cell is not None, \
        "真实帧上一个落点都找不到，禁区规则过严"
    if cell is not None:
        assert w.walkable(cell[0], cell[1])
        assert cell not in w.belt_cells


def test_occupied_cells_includes_plates():
    w = FakeWorld(plates=[{"n": "p1", "x": 1.2, "z": 0.0, "count": 2}])
    occupied = placement.occupied_cells(w)
    assert (1, 0) in occupied
    assert (3, 0) not in occupied


def test_find_drop_at_returns_station_when_free():
    w = FakeWorld(stations=[], walkable=lambda gx, gz: gz == 0)
    field = distance_field((0, 0), w.walkable, {})
    st = placement.find_drop_at(w, field, (1, 0))
    assert st is not None
    assert st["gx"] == 1 and st["gz"] == 0
    assert st["t"] == "counter"


def test_find_drop_at_returns_none_when_occupied():
    w = FakeWorld(plates=[{"n": "p1", "x": 1.2, "z": 0.0, "count": 2}],
                  walkable=lambda gx, gz: gz == 0)
    field = distance_field((0, 0), w.walkable, {})
    assert placement.find_drop_at(w, field, (1, 0)) is None

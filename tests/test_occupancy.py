"""occupancy 纯函数单测。"""
import json
import os

import pytest

from ai.space import occupancy as O
from ai.space.geometry import GridMapping
from ai.exec.replay import load_frames


def test_parse_node_map_basic():
    """2x3 矩阵按行主序铺开：前 3 位是第 0 行。"""
    got = O.parse_node_map("2x3=101010")
    assert got is not None
    nm, rows, cols = got
    assert (rows, cols) == (2, 3)
    assert nm == [[True, False, True], [False, True, False]]


def test_parse_node_map_no_equals_returns_none():
    """没有 '=' 分隔符：插件没发 nodeMap，不是错误，退化为"全可走"由调用方决定。"""
    assert O.parse_node_map("") is None
    assert O.parse_node_map(None) is None
    assert O.parse_node_map("17x13") is None


def test_parse_node_map_length_mismatch_returns_none():
    """Review Focus 1：位串短于/长于声明维度时返回 None，绝不抛 IndexError。

    旧实现用 bits[r * d1 + c] 直接下标，截断的位串会在主循环里炸。
    数据源层的契约是"坏数据降级"，不是"坏数据传播"。
    """
    assert O.parse_node_map("2x3=1010") is None      # 短 2 位
    assert O.parse_node_map("2x3=10101010") is None  # 长 2 位


def test_parse_node_map_malformed_dims_returns_none():
    """维度段不是 int 时返回 None。"""
    assert O.parse_node_map("axb=101010") is None
    assert O.parse_node_map("2=1010") is None
    assert O.parse_node_map("0x0=") is None


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
REAL_FRAME = os.path.join(FIXTURES, "real_frame_sushi_1_4.json")


def _real_frame():
    if not os.path.exists(REAL_FRAME):
        pytest.skip("缺 tests/fixtures/real_frame_sushi_1_4.json")
    with open(REAL_FRAME, "r", encoding="utf-8") as f:
        return json.load(f)


def test_parse_node_map_real_frame_baseline():
    """真实帧基线：Sushi_1_4 的 nodeMap 是 17x13，141 格标可走。

    这三个数字是实测值。若插件改了 nodeMap 编码，这条会先红——
    这正是它存在的理由。
    """
    frame = _real_frame()
    got = O.parse_node_map((frame.get("grid") or {}).get("nodeMap"))
    assert got is not None
    nm, rows, cols = got
    assert (rows, cols) == (17, 13)
    assert sum(1 for row in nm for v in row if v) == 141


def test_playable_bounds_basic():
    stations = [
        {"gx": -2, "gz": 3},
        {"gx": 5, "gz": -1},
        {"gx": 0, "gz": 0},
    ]
    assert O.playable_bounds(stations) == (-2, 5, -1, 3)


def test_playable_bounds_empty_returns_none():
    """无站点时返回 None——调用方应跳过越界检查，而不是把 0 当边界。"""
    assert O.playable_bounds([]) is None
    assert O.playable_bounds(None) is None


def test_playable_bounds_skips_stations_without_grid():
    """Review Focus 2：实测 71 个站点里 10 个没有 gx/gz 键
    （8 个 plate_stack + washing + plate_station）。

    旧实现这里用 `"gx" in s` 判键存在，而 station_blocked_cells 用
    `.get() is not None`——同一份数据两种判法。本实现统一为后者：
    键不存在、或值为 None，都跳过。
    """
    stations = [
        {"gx": 1, "gz": 1},
        {"t": "plate_stack", "n": "equipment_plate_01", "x": 15.6, "z": -6.0},
        {"gx": None, "gz": None},
        {"gx": 4, "gz": -2},
        {"gx": 2},             # 只有一轴，不足以定位
    ]
    assert O.playable_bounds(stations) == (1, 4, -2, 1)


def test_playable_bounds_non_dict_entries_ignored():
    """快照里混入标量/None 时不崩。"""
    assert O.playable_bounds([None, "x", 7, {"gx": 3, "gz": 3}]) == (3, 3, 3, 3)


def test_playable_bounds_real_frame_baseline():
    """真实帧基线：gx -7..7、gz -5..4（实测）。"""
    frame = _real_frame()
    assert O.playable_bounds(frame["stations"]) == (-7, 7, -5, 4)


def _mapping(cell=1.2, origin_x=14.4, origin_z=-6.0, off_x=8, off_z=6):
    """造一个与 Sushi_1_4 同参数的映射，免得每个测试重复这五个数。"""
    return GridMapping(cell, off_x, off_z, origin_x, origin_z)


def test_station_blocked_single_cell_from_grid():
    """只有 gx/gz 没有 AABB 的站点：占用自己那一格。"""
    m = _mapping()
    got = O.station_blocked_cells([{"gx": 2, "gz": -1}], m)
    assert got == {(2, -1)}


def test_station_blocked_aabb_covers_multiple_cells():
    """AABB 跨多格的站点（如出餐口）：覆盖到的格全部占用。

    origin=(14.4,-6.0)、cell=1.2，故格 gx 占 x[14.4+1.2gx-0.6, +0.6]：
      gx=-1 → [12.60,13.80]  与 AABB 左边缘 13.8 仅贴合，不算重叠
      gx= 0 → [13.80,15.00]  与 AABB 重叠
      gx= 1 → [15.00,16.20]  与 AABB 重叠
    完整结果是 gx∈{0,1} × gz∈{-1,0,1} 六格，这里只断言 x 轴三种情形。
    """
    m = _mapping()
    st = {"gx": 0, "gz": 0,
          "minx": 13.8, "maxx": 16.2, "minz": -7.2, "maxz": -4.8}
    got = O.station_blocked_cells([st], m)
    assert (0, 0) in got      # 格心 (14.4, -6.0) 落在 AABB 内
    assert (1, 0) in got      # 格心 (15.6, -6.0) 落在 AABB 内
    assert (-1, 0) not in got  # 仅边界贴合


def test_station_blocked_edge_touch_not_overlap():
    """边界恰好贴合不算占用：AABB 右边缘 == 格左边缘。

    gx=0 的格占 x[13.80,15.00]。AABB x[12.6,13.8] 的右边缘正是 13.80,
    物理上只是挨着，厨师能走。旧实现用 eps=1e-3 容差处理浮点噪声，
    这里保持一致。该 AABB 真正占用的是 gx=-1 那格。
    """
    m = _mapping()
    st = {"minx": 12.6, "maxx": 13.8, "minz": -6.6, "maxz": -5.4}
    got = O.station_blocked_cells([st], m)
    assert (0, 0) not in got
    assert got == {(-1, 0)}


def test_station_blocked_non_dict_and_missing_fields():
    """AABB 四个字段缺任一就跳过 AABB 逻辑，但 gx/gz 兜底仍生效。"""
    m = _mapping()
    got = O.station_blocked_cells(
        [None, {"gx": 1, "gz": 1, "minx": 0.0}, {"gx": 3, "gz": 3}], m)
    assert got == {(1, 1), (3, 3)}


def test_station_blocked_real_frame_without_aabb():
    """Review Focus 3：real_frame fixture 早于 AABB 字段，退化为 gx/gz 单格。

    实测 61 格——恰是 71 站点里有 gx/gz 的那 61 个。退化结果可预测，
    不是崩溃也不是空集。
    """
    frame = _real_frame()
    m = GridMapping.from_grid(frame["grid"], frame["stations"])
    blocked = O.station_blocked_cells(frame["stations"], m)
    assert blocked == set(
        (s["gx"], s["gz"]) for s in frame["stations"]
        if s.get("gx") is not None and s.get("gz") is not None
    )
    assert len(blocked) == 61


def test_station_blocked_ndjson_frame_with_aabb():
    """带 AABB 的帧：65 格，比纯 gx/gz 多出出餐口的 4 格。

    多出的 (0,-5)(0,-6)(1,-5)(1,-6) 正是 workstation_plate_station
    的 AABB x[13.8,16.2] z[-13.2,-11.4] 覆盖区——这个多格功能站
    nodeMap 标为可走，只有 AABB 能发现它。
    """
    path = os.path.join(FIXTURES, "full_level.ndjson.gz")
    if not os.path.exists(path):
        pytest.skip("缺 tests/fixtures/full_level.ndjson.gz")
    frame = load_frames(path)[0]
    m = GridMapping.from_grid(frame["grid"], frame["stations"])
    blocked = O.station_blocked_cells(frame["stations"], m)
    assert len(blocked) == 65
    for cell in ((0, -5), (0, -6), (1, -5), (1, -6)):
        assert cell in blocked, cell


def test_make_walkable_priority_order():
    """四条规则的优先级：站点占用 > 越界 > 传送带 > nodeMap。

    这个顺序不是随意的：站点占用优先于 nodeMap，因为功能站 nodeMap
    标可走但撞不过去；传送带优先于 nodeMap，因为传送带 nodeMap 标
    不可走但能走上去。两条都是"nodeMap 不可信"的修正。
    """
    nm = [[True] * 13 for _ in range(17)]
    nm[8][6] = False          # (gx=0, gz=0) 在 nodeMap 里标不可走
    walk = O.make_walkable(
        node_map=nm, rows=17, cols=13,
        bounds=(-3, 3, -3, 3),
        blocked={(1, 1)},
        belt_cells={(0, 0)},
        off_x=8, off_z=6,
    )
    assert walk(0, 0) is True     # 传送带覆盖 nodeMap 的 '0'
    assert walk(1, 1) is False    # 站点占用，即便 nodeMap 标可走
    assert walk(9, 9) is False    # 越界
    assert walk(2, 2) is True     # nodeMap 可走且无其他规则


def test_make_walkable_blocked_beats_belt():
    """同一格既是传送带又被站点占用时，占用优先——撞不过去。"""
    nm = [[True] * 13 for _ in range(17)]
    walk = O.make_walkable(nm, 17, 13, (-3, 3, -3, 3),
                           blocked={(0, 0)}, belt_cells={(0, 0)},
                           off_x=8, off_z=6)
    assert walk(0, 0) is False


def test_make_walkable_no_node_map_falls_back_to_blocked_only():
    """nodeMap 为 None（插件没发）：只看站点占用，其余在 bounds 内一律可走。

    不能退化成"全不可走"——那样 AI 会完全瘫掉，比走错路更糟。但
    bounds 存在时仍要裁剪越界格：否则 nodeMap 缺失的帧会产出一个
    无边界的可走谓词，喂给 navigation.distance_field 会让 flood-fill
    失控增长（这正是本轮修的缺陷，见 navigation.distance_field 的
    max_cells 兜底）。
    """
    walk = O.make_walkable(None, 0, 0, (-3, 3, -3, 3),
                           blocked={(1, 1)}, belt_cells=set(),
                           off_x=8, off_z=6)
    assert walk(1, 1) is False
    assert walk(0, 0) is True
    assert walk(99, 99) is False  # bounds 存在时，无 nodeMap 也要越界裁剪


def test_make_walkable_no_bounds_skips_bounds_check():
    """bounds 为 None（无站点可定位边界）：跳过越界检查，仍查 nodeMap。"""
    nm = [[True] * 13 for _ in range(17)]
    walk = O.make_walkable(nm, 17, 13, None, set(), set(), 8, 6)
    assert walk(0, 0) is True
    assert walk(50, 50) is False  # nodeMap 下标越界 → 不可走


def test_make_walkable_real_frame_baseline():
    """真实帧基线：带 AABB 的帧在 -12..12 方框内 86 格可走。"""
    path = os.path.join(FIXTURES, "full_level.ndjson.gz")
    if not os.path.exists(path):
        pytest.skip("缺 tests/fixtures/full_level.ndjson.gz")
    frame = load_frames(path)[0]
    m = GridMapping.from_grid(frame["grid"], frame["stations"])
    parsed = O.parse_node_map(frame["grid"].get("nodeMap"))
    assert parsed is not None
    nm, rows, cols = parsed
    cells, _ = O.parse_belts(frame.get("belts"))
    walk = O.make_walkable(
        nm, rows, cols,
        O.playable_bounds(frame["stations"]),
        O.station_blocked_cells(frame["stations"], m),
        cells, m.off_x, m.off_z,
    )
    count = sum(1 for gx in range(-12, 13) for gz in range(-12, 13)
                if walk(gx, gz))
    assert count == 86


def test_dedupe_cookers_prefers_richer_entry():
    """同位置的 Client/Server 两份，保留字段更完整的那份。

    评分 = isCooking/cooked/burning/onFire 四个键里非 None 的个数。
    Client 那份通常只有位置，Server 那份才有烹饪状态——丢错了会让
    "锅好了吗"永远读不到答案。
    """
    raw = [
        {"x": 1.0, "z": 2.0, "n": "cooker_client"},
        {"x": 1.0, "z": 2.0, "n": "cooker_server",
         "isCooking": True, "cooked": False, "burning": False, "onFire": False},
    ]
    got = O.dedupe_cookers(raw)
    assert len(got) == 1
    assert got[0]["n"] == "cooker_server"


def test_dedupe_cookers_rounds_position_to_2dp():
    """位置按 2 位小数取键：浮点噪声不该制造重影。"""
    raw = [{"x": 1.0, "z": 2.0}, {"x": 1.001, "z": 2.004}]
    assert len(O.dedupe_cookers(raw)) == 1


def test_dedupe_cookers_keeps_distinct_positions():
    raw = [{"x": 1.0, "z": 2.0}, {"x": 5.0, "z": 2.0}]
    assert len(O.dedupe_cookers(raw)) == 2


def test_dedupe_cookers_drops_entries_without_position():
    """无 x/z 的条目直接丢弃——没位置的灶对决策毫无用处。"""
    raw = [{"n": "a"}, {"x": 1.0, "n": "b"}, {"z": 2.0, "n": "c"}, None, "junk"]
    assert O.dedupe_cookers(raw) == []


def test_dedupe_cookers_empty_input():
    assert O.dedupe_cookers([]) == []
    assert O.dedupe_cookers(None) == []


def test_dedupe_items_keys_on_name_and_position():
    """散落物按 名字+位置 去重：同位置的不同物品都要保留。

    与灶不同——两个不同食材可以叠在同一格，按位置去重会吞掉一个。
    """
    raw = [
        {"n": "Fish", "x": 1.0, "z": 2.0},
        {"n": "Fish", "x": 1.0, "z": 2.0},   # Client/Server 重复
        {"n": "Rice", "x": 1.0, "z": 2.0},   # 同位置不同物，保留
    ]
    got = O.dedupe_items(raw)
    assert len(got) == 2
    assert set(e["n"] for e in got) == {"Fish", "Rice"}


def test_dedupe_items_preserves_first_occurrence_order():
    """保留首次出现的顺序与条目本身。"""
    raw = [
        {"n": "A", "x": 0.0, "z": 0.0, "tag": "first"},
        {"n": "A", "x": 0.0, "z": 0.0, "tag": "second"},
        {"n": "B", "x": 1.0, "z": 0.0},
    ]
    got = O.dedupe_items(raw)
    assert [e["n"] for e in got] == ["A", "B"]
    assert got[0]["tag"] == "first"


def test_dedupe_items_missing_name_treated_as_empty():
    """无 n 字段按空名处理，仍参与去重，不崩。"""
    raw = [{"x": 1.0, "z": 1.0}, {"x": 1.0, "z": 1.0}]
    assert len(O.dedupe_items(raw)) == 1


def test_dedupe_items_drops_entries_without_position():
    assert O.dedupe_items([{"n": "A"}, None, "junk"]) == []


def test_dedupe_extinguishers_prefers_non_rigidbody():
    """灭火器一个对象上挂多个同名组件，优选非 Rigidbody 的那条。

    实测 8 条 → 1 个灭火器，其中 5 条名字带 _Rigidbody。Rigidbody
    组件的位置可能是物理中心而非交互点，取错了会站错位。
    """
    raw = [
        {"n": "utensil_fire_extinguisher_01_Rigidbody", "x": 1.0, "z": 2.0},
        {"n": "utensil_fire_extinguisher_01", "x": 1.0, "z": 2.0},
    ]
    got = O.dedupe_extinguishers(raw)
    assert len(got) == 1
    assert got[0]["n"] == "utensil_fire_extinguisher_01"


def test_dedupe_extinguishers_rigidbody_only_still_kept():
    """只有 Rigidbody 版本时仍要保留——有总比没有好。"""
    raw = [{"n": "utensil_fire_extinguisher_01_Rigidbody", "x": 1.0, "z": 2.0}]
    assert len(O.dedupe_extinguishers(raw)) == 1


def test_dedupe_extinguishers_name_match_is_case_insensitive():
    """名字判定不分大小写。"""
    raw = [
        {"n": "EXT_RIGIDBODY", "x": 1.0, "z": 2.0},
        {"n": "ext_plain", "x": 1.0, "z": 2.0},
    ]
    assert O.dedupe_extinguishers(raw)[0]["n"] == "ext_plain"


def test_dedupe_real_frame_baselines():
    """真实帧基线（实测）：灶 8→3、散落物 32→9、灭火器 8→1。"""
    path = os.path.join(FIXTURES, "full_level.ndjson.gz")
    if not os.path.exists(path):
        pytest.skip("缺 tests/fixtures/full_level.ndjson.gz")
    frame = load_frames(path)[0]
    assert len(frame["cookers"]) == 8
    assert len(O.dedupe_cookers(frame["cookers"])) == 3
    assert len(frame["items"]) == 32
    assert len(O.dedupe_items(frame["items"])) == 9
    assert len(frame["extinguishers"]) == 8
    assert len(O.dedupe_extinguishers(frame["extinguishers"])) == 1


def test_dedupe_cookers_first_wins_on_score_tie():
    """同位置同分时保留先出现的那条（strict >，不是 >=）。"""
    raw = [
        {"x": 1.0, "z": 2.0, "n": "first", "isCooking": True},
        {"x": 1.0, "z": 2.0, "n": "second", "cooked": True},
    ]
    assert O.dedupe_cookers(raw)[0]["n"] == "first"


def test_dedupe_extinguishers_keeps_first_non_rigidbody():
    """已选中非 Rigidbody 后，后来的条目不再顶替——与入参顺序无关。"""
    raw = [
        {"n": "ext_plain", "x": 1.0, "z": 2.0},
        {"n": "ext_RIGIDBODY", "x": 1.0, "z": 2.0},
        {"n": "ext_plain_2", "x": 1.0, "z": 2.0},
    ]
    got = O.dedupe_extinguishers(raw)
    assert len(got) == 1
    assert got[0]["n"] == "ext_plain"


MAPPING = GridMapping(cell=1.2, off_x=8, off_z=6, origin_x=9.6, origin_z=-2.4)


def test_parse_terrain_groups_by_layer():
    raw = [
        {"layer": 9, "boxes": [[3.6, 15.6, -8.6, 3.4]]},
        {"layer": 10, "boxes": [[0.0, 1.0, 0.0, 1.0], [2.0, 3.0, 2.0, 3.0]]},
    ]
    got = O.parse_terrain(raw)
    assert got[9] == [(3.6, 15.6, -8.6, 3.4)]
    assert got[10] == [(0.0, 1.0, 0.0, 1.0), (2.0, 3.0, 2.0, 3.0)]


def test_parse_terrain_skips_malformed_boxes():
    raw = [{"layer": 9, "boxes": [[1, 2, 3], None, "junk"]}]
    assert O.parse_terrain(raw) == {}


def test_walkable_blocks_outside_ground():
    """格中心不在任何 Ground AABB 内 -> 不可走（§5.3 第 1 步）。"""
    terrain = {9: [(0.0, 2.4, 0.0, 2.4)]}   # 覆盖 gx 0..2, gz 2..4（世界坐标）
    is_walkable = O.make_walkable_from_terrain(terrain, [], set(), MAPPING)
    # 格 (0,0) -> 世界 (9.6, -2.4)，在 ground box 外
    assert is_walkable(0, 0) is False


def test_walkable_permissive_when_no_terrain_data():
    """地形数据整体缺失（合成帧常见）-> 不拦，不能让现有测试全炸。"""
    is_walkable = O.make_walkable_from_terrain({}, [], set(), MAPPING)
    assert is_walkable(0, 0) is True
    assert is_walkable(100, 100) is True


def test_parse_level_bounds_basic():
    raw = [{"minx": 3.6, "maxx": 15.6, "minz": -8.6, "maxz": 3.4}]
    assert O.parse_level_bounds(raw) == [(3.6, 15.6, -8.6, 3.4)]


def test_parse_level_bounds_skips_malformed():
    """缺字段/非数值/非 dict 一律跳过，只保留完整的四条。"""
    raw = [
        None,
        {"minx": 1.0},                                  # 缺三字段
        {"minx": "a", "maxx": 2, "minz": 3, "maxz": 4},  # 非数值
        {"minx": 0.0, "maxx": 1.0, "minz": 0.0, "maxz": 1.0},
    ]
    assert O.parse_level_bounds(raw) == [(0.0, 1.0, 0.0, 1.0)]


def test_walkable_falls_back_to_level_bounds_when_no_ground():
    """Ground(9) 缺失时用 level_bounds 兜底裁剪（1-6 掉出范围的根因）。

    有 level_bounds 时不能再像旧实现那样整体放行——否则角色会被寻路
    引到地板外坠落。
    """
    level_bounds = [(9.0, 10.2, -3.0, -1.8)]   # 只覆盖 (0,0) 世界 (9.6,-2.4)
    is_walkable = O.make_walkable_from_terrain(
        {}, [], set(), MAPPING, level_bounds=level_bounds)
    assert is_walkable(0, 0) is True      # 在 bounds 内
    assert is_walkable(1, 0) is False     # 世界 (10.8,-2.4) 在 bounds 外
    assert is_walkable(100, 100) is False


def test_walkable_ground_wins_over_level_bounds():
    """Ground 与 level_bounds 同时存在时，Ground 优先（更细），level_bounds 不用。"""
    terrain = {9: [(9.0, 10.2, -3.0, -1.8)]}          # 只覆盖 (0,0)
    level_bounds = [(-100.0, 100.0, -100.0, 100.0)]   # 巨大：若生效则全放行
    is_walkable = O.make_walkable_from_terrain(
        terrain, [], set(), MAPPING, level_bounds=level_bounds)
    assert is_walkable(0, 0) is True     # ground 内
    assert is_walkable(1, 0) is False    # ground 外；level_bounds 不应放行


def test_walkable_blocks_killplane():
    terrain = {9: [(-100.0, 100.0, -100.0, 100.0)], 30: [(9.0, 10.2, -3.0, -1.8)]}
    is_walkable = O.make_walkable_from_terrain(terrain, [], set(), MAPPING)
    assert is_walkable(0, 0) is False   # 世界 (9.6,-2.4) 落在 killplane box 内


def test_walkable_blocks_wall_layers():
    terrain = {
        9: [(-100.0, 100.0, -100.0, 100.0)],
        10: [(9.0, 10.2, -3.0, -1.8)],
    }
    is_walkable = O.make_walkable_from_terrain(terrain, [], set(), MAPPING)
    assert is_walkable(0, 0) is False


def test_walkable_blocks_wall_within_chef_radius():
    """阻挡 box 距格心 < CHEF_RADIUS(0.4) 时，即使格心点在 box 外也应判不可走。

    厨师胶囊体半径 0.4；绿植/柜台 collider 常往邻格溢出 0.2~0.3，格心点判
    会漏掉，Dijkstra 引角色往「点判可走、身体却过不去」的格走（实机 1-3：
    绿植 (-3,-4) 的 collider 溢出到 (-3,-5)，角色卡在边界）。外扩 0.4 后
    (-3,-5) 会被正确判不可走、绕开。
    """
    terrain = {
        9: [(-100.0, 100.0, -100.0, 100.0)],
        10: [(9.9, 11.0, -3.0, -1.8)],   # minx=9.9，格心 x=9.6，相距 0.3 < 0.4
    }
    is_walkable = O.make_walkable_from_terrain(terrain, [], set(), MAPPING)
    assert is_walkable(0, 0) is False   # 格心 (9.6,-2.4) 距 box 0.3，被外扩挡住
    assert is_walkable(-1, 0) is True   # 格心 (8.4,-2.4) 距 box 1.5，可走


def test_walkable_blocks_wall_edge_touching_cell_center():
    """格中心恰好落在墙 AABB 边界上时，浮点误差不能把整行漏判成可走。

    Sushi_1_3 的 gz=-6：墙 box 的 maxz=0.0，而该格中心 z = 7.2 + 1.2*(-6)
    浮点算出来是 8.88e-16（略大于 0）。旧点判 `z <= maxz` 判定失败，整行
    被判可走，角色被寻路引到墙里顶墙卡住（no_progress）。修复后边界格
    算在墙内。
    """
    mapping = GridMapping(cell=1.2, off_x=8, off_z=6, origin_x=6.0, origin_z=7.2)
    terrain = {
        9: [(-2.4, 15.6, -0.6, 12.0)],     # ground 覆盖到 z=-0.6
        10: [(-1.8, 15.0, -0.6, 0.0)],     # 墙：maxz=0.0 恰在 gz=-6 格中心
    }
    is_walkable = O.make_walkable_from_terrain(terrain, [], set(), mapping)
    assert is_walkable(0, -6) is False     # 格中心 z≈0，在墙内 → 不可走
    assert is_walkable(0, -5) is True      # 格中心 z=1.2，在墙上方 → 可走


def test_walkable_does_not_block_when_wall_edge_overlaps_neighbor():
    """墙/站点 collider 只溢出到邻格边缘、盖不住邻格中心时，邻格仍可走。

    实机 trace-20261017-193620（Balloon_1_5）：顶部墙 collider 底边 z=2.8
    与 gz=4 地板格顶边 z=3.0 重叠 0.2，但这排是墙**前面**的可走地板。
    若按「格子区域相交」判，整排上层地板会被误判不可走，把上左走廊和
    上层全封死。格中心点判不会误伤：中心 z=2.4 不在墙 z[2.8,3.4] 内。
    """
    terrain = {
        9: [(-100.0, 100.0, -100.0, 100.0)],
        10: [(3.6, 15.61, 2.8, 3.4)],
    }
    is_walkable = O.make_walkable_from_terrain(terrain, [], set(), MAPPING)
    # gz=4 -> 世界 z = -2.4 + 4*1.2 = 2.4，格中心不在墙内 → 可走
    assert is_walkable(0, 4) is True
    assert is_walkable(3, 4) is True


def test_edge_blocker_blocks_thin_wall_between_cells():
    """薄墙夹在两格中心之间（点判漏掉），拦「跨过去」这条边，但不拦格子。"""
    terrain = {10: [(10.1, 10.3, -3.0, -1.8)]}   # 竖薄墙：宽 0.2，跨 gx=0/1 边界
    edge = O.make_edge_blocker(terrain, MAPPING)
    # (0,0) 中心 (9.6,-2.4)、(1,0) 中心 (10.8,-2.4)；墙夹在中间且盖住 z=-2.4
    assert edge((0, 0), (1, 0)) is True
    assert edge((1, 0), (0, 0)) is True
    # 垂直方向不拦
    assert edge((0, 0), (0, 1)) is False
    # 格中心不被墙覆盖：格子本身仍可走
    is_walkable = O.make_walkable_from_terrain(
        {9: [(-100.0, 100.0, -100.0, 100.0)], 10: [(10.1, 10.3, -3.0, -1.8)]},
        [], set(), MAPPING)
    assert is_walkable(0, 0) is True
    assert is_walkable(1, 0) is True


def test_edge_blocker_blocks_horizontal_wall():
    """横薄墙夹在上下两格中心之间，拦垂直移动那条边。"""
    terrain = {10: [(9.0, 10.2, -1.9, -1.7)]}   # 横薄墙：高 0.2，跨 gz=0/1 边界
    edge = O.make_edge_blocker(terrain, MAPPING)
    # (0,0) 中心 z=-2.4、(0,1) 中心 z=-1.2；墙夹在中间且盖住 x=9.6
    assert edge((0, 0), (0, 1)) is True
    assert edge((0, 1), (0, 0)) is True
    assert edge((0, 0), (1, 0)) is False


def test_edge_blocker_ignores_thick_station_block():
    """厚块盖住自己格中心（点判拦格），不额外拦相邻可走格之间的边。"""
    # 1.2×1.2 实心块盖住 (0,0) 中心 (9.6,-2.4)
    terrain = {10: [(9.0, 10.2, -3.0, -1.8)]}
    edge = O.make_edge_blocker(terrain, MAPPING)
    # 相邻可走格 (0,1)-(1,1) 之间不应被这块拦住（它不夹在这两格中心间）
    assert edge((0, 1), (1, 1)) is False
    # 进入该块的边由点判负责（格子不可走），edge 层可以不管，但也不误报
    is_walkable = O.make_walkable_from_terrain(
        {9: [(-100.0, 100.0, -100.0, 100.0)], 10: [(9.0, 10.2, -3.0, -1.8)]},
        [], set(), MAPPING)
    assert is_walkable(0, 0) is False
    assert is_walkable(0, 1) is True


def test_walkable_blocks_hazard_boxes():
    terrain = {9: [(-100.0, 100.0, -100.0, 100.0)]}
    hazard_boxes = [(9.0, 10.2, -3.0, -1.8)]
    is_walkable = O.make_walkable_from_terrain(terrain, hazard_boxes, set(), MAPPING)
    assert is_walkable(0, 0) is False


def test_walkable_blocks_occupancy_cells():
    terrain = {9: [(-100.0, 100.0, -100.0, 100.0)]}
    is_walkable = O.make_walkable_from_terrain(terrain, [], {(0, 0)}, MAPPING)
    assert is_walkable(0, 0) is False
    assert is_walkable(1, 0) is True


def test_walkable_belt_cells_exempt_from_occupancy():
    """传送带格即使在 m_gridOccupancy 里也必须可走（实机回归）。

    ConveyorStation 自身占据它所在的格，所以运行时占用表把全部 18 格
    传送带都标成占用。旧 `make_walkable` 有一条"传送带 → 可走"的放行
    （第 4 步），`make_walkable_from_terrain` 漏掉了它：寿司关两排传送带
    是左右两半唯一的通路，全标不可走后 P2 从出生点只够得到 6 格，
    **全部 task 的 Approach 第一拍就 FAILURE、整局零按键**
    （trace-20261016-175943，334 拍无一个按键）。
    """
    terrain = {9: [(-100.0, 100.0, -100.0, 100.0)]}
    is_walkable = O.make_walkable_from_terrain(
        terrain, [], {(0, 0), (1, 0)}, MAPPING, belt_cells={(0, 0)})
    assert is_walkable(0, 0) is True     # 传送带格：占用表不算
    assert is_walkable(1, 0) is False    # 普通占用格仍不可走


def test_walkable_belt_cells_still_blocked_by_terrain():
    """放行只豁免运行时占用，不豁免地形/墙/落火。"""
    terrain = {9: [(-100.0, 100.0, -100.0, 100.0)], 10: [(9.0, 10.2, -3.0, -1.8)]}
    is_walkable = O.make_walkable_from_terrain(
        terrain, [], set(), MAPPING, belt_cells={(0, 0)})
    assert is_walkable(0, 0) is False


# 气球关两块地板的真实 AABB（spec §4.3 实测，世界坐标）：
# 主地板 x[3.6,15.6]，篮子地板 x[16.0,23.2]，z 向在篮子三个停靠位置下平移
# ±4.9（§4.2）。为覆盖篮子移动的全部位置，Ground 层给两个并集框。
BALLOON_GROUND_BOXES = [
    (3.6, 15.6, -8.6, 3.4),                # 主地板（恒定）
    (16.0, 23.2, -8.6 - 4.9, 3.4 + 0.1),  # 篮子地板，覆盖 BOT..TOP 全部位置
]


def _load_p1_track():
    """加载 Balloon_1_5 P1 轨迹。"""
    path = os.path.join(os.path.dirname(__file__), "fixtures", "balloon_1_5_p1_track.json")
    if not os.path.exists(path):
        pytest.skip("缺 balloon_1_5_p1_track.json，先跑 Task 1 的提取脚本")
    return json.load(open(path))


def test_ground_truth_p1_track_fully_walkable():
    """spec §8 测试 1：P1 走过的每个真实位置必须可走。baseline 32.6% -> 目标 100%。

    用真实地形数据（spec §4.3 手工实测）验证 make_walkable_from_terrain
    在多网格关卡 Balloon_1_5 上的命中率。轨迹来自 Task 1 的提取。
    """
    track = _load_p1_track()
    mapping = GridMapping(cell=1.2, off_x=8, off_z=6, origin_x=9.6, origin_z=-2.4)
    terrain = {9: BALLOON_GROUND_BOXES}
    is_walkable = O.make_walkable_from_terrain(terrain, [], set(), mapping)

    hit, total = 0, 0
    misses = []
    for x, z in track:
        gx, gz = mapping.to_grid(x, z)
        total += 1
        if is_walkable(gx, gz):
            hit += 1
        else:
            misses.append((round(x, 1), round(z, 1), gx, gz))

    rate = hit / total
    assert rate == 1.0, "命中率 %.1f%%，未命中样本（前10个）：%s" % (
        rate * 100, misses[:10])


def test_parse_hazards_basic():
    """hazard 列表解析：提取 grid/x/z/ttl 四字段。"""
    raw = [{"t": "fire", "grid": "GridManager", "x": 13.2, "z": -2.4, "ttl": 11.0}]
    got = O.parse_hazards(raw)
    assert got == [{"grid": "GridManager", "x": 13.2, "z": -2.4, "ttl": 11.0}]


def test_parse_hazards_skips_expired_and_malformed():
    """ttl<=0 或缺坐标的 hazard 丢弃；非 dict 条目忽略。"""
    raw = [{"t": "fire", "x": 1.0, "z": 1.0, "ttl": 0.0},   # ttl<=0 已过期
           {"t": "fire", "x": None, "z": 1.0, "ttl": 5.0},   # 缺坐标
           "junk"]
    assert O.parse_hazards(raw) == []


def test_hazard_boxes_one_cell_square():
    """每个 hazard 给边长=cell 的正方形框（x±cell/2, z±cell/2）。"""
    hz = [{"grid": "G", "x": 10.0, "z": 10.0, "ttl": 5.0}]
    boxes = O.hazard_boxes(hz, cell=1.2)
    assert boxes == [(9.4, 10.6, 9.4, 10.6)]


def test_parse_wind_zones_basic():
    """风区解析：提取 AABB + 风向风速。"""
    raw = [{"minx": 3.0, "maxx": 5.0, "minz": -2.0, "maxz": 1.0,
            "vx": 0.0, "vz": 2.5}]
    assert O.parse_wind_zones(raw) == [
        {"minx": 3.0, "maxx": 5.0, "minz": -2.0, "maxz": 1.0,
         "vx": 0.0, "vz": 2.5}]


def test_parse_wind_zones_skips_malformed():
    """缺 AABB 字段或非 dict 的丢弃；vx/vz 缺失按 0。"""
    raw = [None,
           {"minx": 1.0},                                  # 缺三字段
           {"minx": 0.0, "maxx": 2.0, "minz": 0.0, "maxz": 1.0}]
    assert O.parse_wind_zones(raw) == [
        {"minx": 0.0, "maxx": 2.0, "minz": 0.0, "maxz": 1.0,
         "vx": 0.0, "vz": 0.0}]


def test_parse_belts_direction_from_velocity():
    """传送带方向从 vx/vz 归一化，不信任任何枚举字段。"""
    raw = [{"grid": "GridManager", "gx": -4, "gz": 2, "vx": 2.5, "vz": 0.0},
           {"grid": "GridManager", "gx": -4, "gz": -3, "vx": -2.5, "vz": 0.0}]
    cells, dirs = O.parse_belts(raw)
    assert cells == {(-4, 2), (-4, -3)}
    assert dirs[(-4, 2)] == (1, 0)
    assert dirs[(-4, -3)] == (-1, 0)


def test_parse_belts_empty_on_zero_velocity():
    """vx/vz 都是 0 时该格仍算传送带（belt_cells），但不记方向。"""
    raw = [{"grid": "G", "gx": 0, "gz": 0, "vx": 0.0, "vz": 0.0}]
    cells, dirs = O.parse_belts(raw)
    assert cells == {(0, 0)}
    assert dirs == {}   # 方向不可定义，但格仍可走


def test_station_block_cells_plate_station_uses_layer20():
    """出餐口用 PLATEBLOCK(20) box 反算占地格（1×2），不是交互格单格。

    Sushi_1_3 出餐口：交互格 (-6,-4)，layer20 box 覆盖 (-6,-3)/(-6,-4)
    两格（竖 1×2）。collider AABB 报 2×2 是溢出的，不能用。
    """
    mapping = GridMapping(cell=1.2, off_x=8, off_z=6, origin_x=6.0, origin_z=7.2)
    terrain = {20: [(-1.8, -0.6, 1.8, 4.2)]}
    st = {"t": "plate_station", "gx": -6, "gz": -4, "x": -1.2, "z": 3.0}

    assert O.station_block_cells(st, terrain, mapping) == {(-6, -4), (-6, -3)}


def test_station_block_cells_other_stations_single_cell():
    """非出餐口站点阻挡格 == gx/gz 单格，不查 terrain。"""
    mapping = GridMapping(cell=1.2, off_x=8, off_z=6, origin_x=6.0, origin_z=7.2)
    st = {"t": "cooking", "gx": 5, "gz": 3, "x": 12.0, "z": 10.8}

    assert O.station_block_cells(st, {}, mapping) == {(5, 3)}


def test_station_block_cells_plate_station_fallback_to_gx_gz():
    """出餐口缺 x/z、或匹配不到 layer20 box 时退回交互格单格。"""
    mapping = GridMapping(cell=1.2, off_x=8, off_z=6, origin_x=6.0, origin_z=7.2)
    st = {"t": "plate_station", "gx": -6, "gz": -4}

    assert O.station_block_cells(st, {}, mapping) == {(-6, -4)}


def test_station_block_cells_non_dict_returns_empty():
    assert O.station_block_cells(None, {}, None) == set()

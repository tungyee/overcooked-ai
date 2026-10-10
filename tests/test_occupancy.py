"""occupancy 纯函数单测。"""
import json
import os

import pytest

from ai import occupancy as O
from ai.geometry import GridMapping
from ai.replay import load_frames


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


def test_belt_cells_known_level():
    """已知关卡：返回格集合与每格的方向向量。实测 Sushi_1_4 有 18 格。

    方向是 (dx, dz) 向量而非方位字符串——navigation.step_cost 直接拿它
    与移动向量比对，判断是否逆向通行。
    """
    cells, dirs = O.belt_cells_and_dirs("Sushi_1_4_2P")
    assert len(cells) == 18
    assert len(dirs) == 18
    assert set(dirs.keys()) == cells
    assert all(isinstance(d, tuple) and len(d) == 2 for d in dirs.values())
    # K 排 (gz=2) 向右 (1,0)，Y 排 (gz=-3) 向左 (-1,0)
    assert dirs[(0, 2)] == (1, 0)
    assert dirs[(0, -3)] == (-1, 0)


def test_belt_cells_unknown_level_degrades_empty():
    """Review Focus 4：换关卡时 BELTS 查不到 → 空集，不抛异常。

    spec §9 的退化保证：BELTS 泛化需改插件（§10.1），本轮残留硬编码，
    但未知关卡必须安全退化——walkable 退回纯 nodeMap + 站点占用。
    """
    for level in ("Unknown_Level_9P", "", None):
        cells, dirs = O.belt_cells_and_dirs(level)
        assert cells == set()
        assert dirs == {}


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
    cells, _ = O.belt_cells_and_dirs(frame.get("level"))
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

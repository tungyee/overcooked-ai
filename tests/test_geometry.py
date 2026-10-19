import json
import pathlib

import pytest

from ai.space.geometry import GridMapping, GridSet, parse_vec


def test_parse_vec_parses_triple():
    assert parse_vec("(2.4, 0.0, 0.0)") == (2.4, 0.0, 0.0)


def test_parse_vec_returns_none_on_junk():
    assert parse_vec("null") is None
    assert parse_vec("") is None
    assert parse_vec(None) is None


# 实测帧（spec §9）：size=(1.2,1.0,1.2), mapOffset="8,6"
SUSHI_GRID = {"size": "(1.2, 1.0, 1.2)", "origin": "(2.4, 0.0, 0.0)", "mapOffset": "8,6"}


def test_from_grid_derives_sushi_origins():
    """公式必须复现原硬编码值 GRID_OX=14.4 / GRID_OZ=-6.0。"""
    m = GridMapping.from_grid(SUSHI_GRID)

    assert m.cell == pytest.approx(1.2)
    assert m.off_x == 8
    assert m.off_z == 6
    assert m.origin_x == pytest.approx(14.4)
    assert m.origin_z == pytest.approx(-6.0)


def test_to_world_matches_linear_formula():
    m = GridMapping.from_grid(SUSHI_GRID)

    assert m.to_world(0, 0) == (pytest.approx(14.4), pytest.approx(-6.0))
    assert m.to_world(1, 1) == (pytest.approx(15.6), pytest.approx(-4.8))
    assert m.to_world(-4, 2) == (pytest.approx(9.6), pytest.approx(-3.6))


def test_to_grid_is_inverse_of_to_world():
    m = GridMapping.from_grid(SUSHI_GRID)

    for gx in range(-6, 7):
        for gz in range(-5, 6):
            x, z = m.to_world(gx, gz)
            assert m.to_grid(x, z) == (gx, gz)


def test_to_grid_rounds_to_nearest_cell():
    m = GridMapping.from_grid(SUSHI_GRID)

    # 稍微偏离格心仍应归到同一格
    assert m.to_grid(14.4 + 0.3, -6.0 - 0.3) == (0, 0)


def test_node_index_applies_map_offset():
    """反编译确认 nodeMap[i,j] == 网格[gx+off_x, gz+off_z]（spec §9）。"""
    m = GridMapping.from_grid(SUSHI_GRID)

    assert m.node_index(0, 0) == (8, 6)
    assert m.node_index(-4, 2) == (4, 8)


def test_from_grid_falls_back_when_map_offset_missing():
    m = GridMapping.from_grid({"size": "(1.2, 1.0, 1.2)"})

    assert m.off_x == 8
    assert m.off_z == 6


def test_from_grid_falls_back_on_malformed_map_offset():
    m = GridMapping.from_grid({"size": "(1.2, 1.0, 1.2)", "mapOffset": "garbage"})

    assert (m.off_x, m.off_z) == (8, 6)


def test_from_grid_defaults_cell_when_size_missing():
    m = GridMapping.from_grid({})

    assert m.cell == pytest.approx(1.2)


# ---------- 世界原点自动校准（Option B / Ruling 8）----------
def test_calibrates_origin_from_stations():
    """给站点时，世界原点从 origin = coord - cell*g 反解，而非 mapOffset 推导。

    换关卡时原点随站点变——这是本任务"删硬编码、免改代码"的真正落点。
    """
    stations = [
        {"gx": 0, "gz": 0, "x": 20.0, "z": -3.0},
        {"gx": 2, "gz": 1, "x": 22.4, "z": -1.8},  # 同一原点、cell=1.2
    ]
    m = GridMapping.from_grid({"size": "(1.2, 1.0, 1.2)", "mapOffset": "8,6"}, stations)

    assert m.origin_x == pytest.approx(20.0)
    assert m.origin_z == pytest.approx(-3.0)
    assert m.to_world(0, 0) == (pytest.approx(20.0), pytest.approx(-3.0))


def test_calibration_ignores_stations_without_grid_coords():
    """只有部分站点带 gx/gz，缺的跳过，不污染校准。"""
    stations = [
        {"n": "no_grid", "x": 99.0, "z": 99.0},  # 无 gx/gz
        {"gx": 1, "gz": 1, "x": 15.6, "z": -4.8},
    ]
    m = GridMapping.from_grid({"size": "(1.2, 1.0, 1.2)"}, stations)

    assert m.origin_x == pytest.approx(14.4)
    assert m.origin_z == pytest.approx(-6.0)


def test_non_square_cell_uses_separate_z_extent():
    """grid.size 是 (x, y, z)：z 轴格宽必须取 size[2]，不能复用 x 轴。

    Sushi_1_4 恰为 (1.2, 1.0, 1.2) 方形，掩盖了这个问题；换关卡若非方形，
    沿用 x 轴格宽会让所有 z 换算错位。
    """
    stations = [{"gx": 0, "gz": 0, "x": 10.0, "z": -4.0}]
    m = GridMapping.from_grid({"size": "(1.2, 1.0, 1.6)"}, stations)

    assert m.cell_x == pytest.approx(1.2)
    assert m.cell_z == pytest.approx(1.6)
    assert m.to_world(2, 3) == (pytest.approx(10.0 + 2.4), pytest.approx(-4.0 + 4.8))
    assert m.to_grid(10.0 + 2.4, -4.0 + 4.8) == (2, 3)


def test_non_square_cell_round_trips():
    m = GridMapping.from_grid({"size": "(0.8, 1.0, 2.0)"})
    for gx in (-3, 0, 5):
        for gz in (-2, 0, 4):
            x, z = m.to_world(gx, gz)
            assert m.to_grid(x, z) == (gx, gz)


def test_square_cell_still_defaults_z_to_x():
    """size 只有两段或 z 缺失时，退回 x 轴格宽，保持旧行为。"""
    m = GridMapping.from_grid({"size": "(1.2, 1.0, 1.2)"})
    assert m.cell_z == pytest.approx(1.2)
    m2 = GridMapping.from_grid(None)
    assert m2.cell_z == pytest.approx(m2.cell)


def test_falls_back_to_default_origin_without_stations():
    """无站点可校准时回落到兜底常量（Sushi_1_4 实测值）。"""
    m = GridMapping.from_grid({"size": "(1.2, 1.0, 1.2)", "mapOffset": "8,6"})

    assert m.origin_x == pytest.approx(14.4)
    assert m.origin_z == pytest.approx(-6.0)


def test_calibration_tolerates_single_dirty_station():
    """一个脏站点不该带偏原点——取众数。"""
    stations = [
        {"gx": 0, "gz": 0, "x": 14.4, "z": -6.0},
        {"gx": 1, "gz": 0, "x": 15.6, "z": -6.0},
        {"gx": 5, "gz": 5, "x": 999.0, "z": 999.0},  # 脏
    ]
    m = GridMapping.from_grid({"size": "(1.2, 1.0, 1.2)"}, stations)

    assert m.origin_x == pytest.approx(14.4)
    assert m.origin_z == pytest.approx(-6.0)


REAL_FRAME = pathlib.Path(__file__).parent / "fixtures" / "real_frame_sushi_1_4.json"


def test_real_frame_all_stations_round_trip():
    """spec §9 的 61/61 零违例结论的回归测试：用实测帧的站点逐个校验。

    不写死 GRID_OX/OZ——世界原点由站点自动校准（Option B / Ruling 8）。
    """
    if not REAL_FRAME.exists():
        pytest.skip("缺 tests/fixtures/real_frame_sushi_1_4.json")
    state = json.loads(REAL_FRAME.read_text())
    m = GridMapping.from_grid(state["grid"], state["stations"])

    checked = 0
    for st in state["stations"]:
        if "gx" not in st or "gz" not in st:
            continue
        gx, gz = st["gx"], st["gz"]
        x, z = m.to_world(gx, gz)
        assert x == pytest.approx(st["x"], abs=1e-6), st
        assert z == pytest.approx(st["z"], abs=1e-6), st
        assert m.to_grid(st["x"], st["z"]) == (gx, gz), st
        checked += 1

    assert checked == 61


# ---------- GridSet（多网格坐标模型）----------
BALLOON_GRIDS = [
    {"id": "GridManager", "ox": 9.6, "oz": -2.4, "cell": 1.2},
    {"id": "Balloon Basket", "ox": 19.6, "oz": -2.4, "cell": 1.2},
]


def test_gridset_from_frame_builds_both_grids():
    gs = GridSet.from_frame(BALLOON_GRIDS)
    assert set(gs.ids()) == {"GridManager", "Balloon Basket"}


def test_gridset_to_world_uses_correct_origin_per_grid():
    gs = GridSet.from_frame(BALLOON_GRIDS)
    # 料箱 DispenserCrate 3 (1) 局部 g(2,-1) -> 世界 (22.0, -3.6)（§4.2 实测）
    assert gs.to_world("Balloon Basket", 2, -1) == pytest.approx((22.0, -3.6))
    # 同样的局部坐标在主网格是另一个世界点——证明两网格不共享坐标
    assert gs.to_world("GridManager", 2, -1) == pytest.approx((12.0, -3.6))


def test_gridset_to_grid_is_inverse_per_grid():
    gs = GridSet.from_frame(BALLOON_GRIDS)
    x, z = gs.to_world("Balloon Basket", 2, -1)
    assert gs.to_grid("Balloon Basket", x, z) == (2, -1)


def test_gridset_unknown_grid_returns_none():
    gs = GridSet.from_frame(BALLOON_GRIDS)
    assert gs.to_world("NoSuchGrid", 0, 0) is None
    assert gs.to_grid("NoSuchGrid", 0.0, 0.0) is None


def test_gridset_from_empty_frame_is_empty():
    gs = GridSet.from_frame(None)
    assert gs.ids() == []

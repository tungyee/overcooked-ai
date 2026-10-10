"""用真实录制帧验证地基各层。

样本帧不存在时整个模块跳过——这样没录帧的环境仍能跑完其余测试，
但一旦录了帧就自动获得真实数据的回归保护。
"""
import pytest

from ai import predicates as P
from ai import world as W
from ai.geometry import GridMapping
from ai.navigation import distance_field, interact_cells

# Import fixture_files from conftest to check if sample frames exist
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from conftest import fixture_files

pytestmark = pytest.mark.skipif(
    not fixture_files(),
    reason="缺 tests/fixtures/*.ndjson，先运行 tools/record_frames.py",
)


def test_grid_formula_matches_all_stations(level_frame):
    """spec §9 的核心断言：公式对每个站点都成立，零违例。"""
    m = GridMapping.from_grid(level_frame.get("grid") or {},
                              level_frame.get("stations"))
    checked = 0

    for st in level_frame.get("stations") or []:
        gx, gz = st.get("gx"), st.get("gz")
        x, z = st.get("x"), st.get("z")
        if gx is None or gz is None or x is None or z is None:
            continue
        assert m.to_grid(x, z) == (gx, gz), st.get("n")
        checked += 1

    assert checked > 0, "样本里没有带 gx/gz 的站点"


def test_derived_origins_match_legacy_constants(level_frame):
    """公式推出的值应等于原硬编码 GRID_OX=14.4 / GRID_OZ=-6.0。

    若换了关卡，这条会失败——那说明样本不是 Sushi_1_4，跳过即可。
    """
    if level_frame.get("level") != "Sushi_1_4_2P":
        pytest.skip("样本不是 Sushi_1_4_2P")

    m = GridMapping.from_grid(level_frame.get("grid") or {},
                              level_frame.get("stations"))

    assert m.origin_x == pytest.approx(14.4)
    assert m.origin_z == pytest.approx(-6.0)


def test_predicates_run_on_every_frame_without_error(frames):
    """谓词对真实数据的所有形态都不崩（null、缺字段、异常值）。"""
    for f in frames:
        chefs = f.get("chefs") or []
        for chef in chefs:
            P.held_name(chef)
            P.held_is_pot(chef)
            P.held_is_plate(chef)
            P.hands_free(chef)
        for plate in f.get("plates") or []:
            P.plate_count(plate)
            P.plate_contents(plate)
        for cooker in f.get("cookers") or []:
            P.cooker_has_pot(cooker)
            P.cooker_cooked(cooker)
            P.cooker_burning(cooker)
            P.cooker_started(cooker)
        P.p1_holds_plate(chefs)


def test_distance_field_reaches_stations_from_chef(level_frame):
    """从厨师位置 flood-fill 应能到达大部分站点的交互位。

    可走性走 World.walkable。这里曾内联一份只看"站点单格 + 矩形边界"
    的手搓 is_walkable（Plan 1 时还没有 World），它不看 nodeMap、能
    穿墙，比真实可走性宽松（实测距离场 70 格 vs 61 格）。内联版与
    生产代码各走一路，生产代码退化了它照旧绿，故删除。
    """
    w = W.World(level_frame)
    stations = w.stations
    if not any(s.get("gx") is not None for s in stations):
        pytest.skip("样本站点无网格坐标")

    # 真实帧里 chefs[].slot 全为 null（见 ledger 发现 A），chef_by_slot 取不到；
    # 距离场只需一个有世界坐标的起点，取任一有坐标的厨师即可。
    chef = None
    for c in w.chefs:
        if c.get("x") is not None and c.get("z") is not None:
            chef = c
            break
    if chef is None:
        pytest.skip("样本里没有带世界坐标的厨师")
    start = w.to_grid(chef["x"], chef["z"])

    field = distance_field(start, w.walkable, w.belt_dir)

    assert field[start] == 0
    reachable = 0
    for st in stations:
        gx, gz = st.get("gx"), st.get("gz")
        if gx is None or gz is None:
            continue
        cells = interact_cells(gx, gz, w.walkable)
        if any(c in field for c in cells):
            reachable += 1

    assert reachable > 0, "没有任何站点可达，距离场或可走判定有问题"

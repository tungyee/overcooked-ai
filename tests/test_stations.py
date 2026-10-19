"""站点检索单测。"""
import os

import pytest

from ai.query import stations as S
from ai.state import world as W

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def test_nearest_station_picks_closest_of_type():
    sts = [
        {"t": "counter", "n": "far", "x": 10.0, "z": 0.0},
        {"t": "counter", "n": "near", "x": 1.0, "z": 0.0},
        {"t": "bin", "n": "closest_but_wrong_type", "x": 0.1, "z": 0.0},
    ]
    assert S.nearest_station(sts, "counter", 0.0, 0.0)["n"] == "near"


def test_nearest_station_none_type_matches_any():
    """t=None 时不限类型——"离我最近的任何站点"。"""
    sts = [{"t": "counter", "x": 9.0, "z": 0.0},
           {"t": "bin", "x": 1.0, "z": 0.0}]
    assert S.nearest_station(sts, None, 0.0, 0.0)["t"] == "bin"


def test_nearest_station_no_match_returns_none():
    assert S.nearest_station([{"t": "bin", "x": 0.0, "z": 0.0}],
                             "counter", 0.0, 0.0) is None
    assert S.nearest_station([], "counter", 0.0, 0.0) is None
    assert S.nearest_station(None, "counter", 0.0, 0.0) is None


def test_nearest_station_skips_entries_without_position():
    """缺 x/z 的站点跳过而非崩。

    实测有站点缺 gx/gz，x/z 同样可能缺——旧实现 s["x"] 直接下标会抛
    KeyError。
    """
    sts = [{"t": "counter", "n": "no_pos"},
           {"t": "counter", "n": "ok", "x": 5.0, "z": 0.0}]
    assert S.nearest_station(sts, "counter", 0.0, 0.0)["n"] == "ok"


def test_find_station_without_near_returns_first():
    """不给 near 时返回第一个匹配——调用方不关心选哪个。"""
    sts = [{"t": "counter", "n": "a", "x": 9.0, "z": 0.0},
           {"t": "counter", "n": "b", "x": 1.0, "z": 0.0}]
    assert S.find_station(sts, "counter")["n"] == "a"


def test_find_station_with_near_returns_closest():
    sts = [{"t": "counter", "n": "a", "x": 9.0, "z": 0.0},
           {"t": "counter", "n": "b", "x": 1.0, "z": 0.0}]
    assert S.find_station(sts, "counter", near=(0.0, 0.0))["n"] == "b"


def test_find_station_no_match_returns_none():
    assert S.find_station([], "counter") is None


def test_find_crate_matches_ingredient_case_insensitive():
    """箱子按 ing 字段匹配食材，不分大小写。

    配方里写 "SushiRice"，快照里也是 "SushiRice"，但大小写不一致的
    关卡存在过——匹配不上会让"去拿米"整条链静默失败。
    """
    sts = [{"t": "crate", "n": "c1", "ing": "SushiRice", "x": 1.0, "z": 0.0},
           {"t": "crate", "n": "c2", "ing": "Cucumber", "x": 2.0, "z": 0.0}]
    assert S.find_crate(sts, "sushirice")["n"] == "c1"
    assert S.find_crate(sts, "SushiRice")["n"] == "c1"
    assert S.find_crate(sts, "CUCUMBER")["n"] == "c2"


def test_find_crate_unknown_ingredient_returns_none():
    sts = [{"t": "crate", "ing": "SushiRice", "x": 1.0, "z": 0.0}]
    assert S.find_crate(sts, "Tomato") is None
    assert S.find_crate(sts, "") is None
    assert S.find_crate(sts, None) is None


def test_find_crate_ignores_non_crate_stations():
    """同名 ing 出现在非箱子站点上时不误取。"""
    sts = [{"t": "counter", "ing": "SushiRice", "x": 1.0, "z": 0.0},
           {"t": "crate", "n": "real", "ing": "SushiRice", "x": 2.0, "z": 0.0}]
    assert S.find_crate(sts, "SushiRice")["n"] == "real"


def test_find_crate_real_frame():
    """真实帧：4 个箱子，四种食材都能找到。"""
    import json
    path = os.path.join(FIXTURES, "real_frame_sushi_1_4.json")
    if not os.path.exists(path):
        pytest.skip("缺 tests/fixtures/real_frame_sushi_1_4.json")
    with open(path, "r", encoding="utf-8") as f:
        frame = json.load(f)
    for ing in ("Seaweed", "SushiFish", "SushiRice", "Cucumber"):
        got = S.find_crate(frame["stations"], ing)
        assert got is not None, ing
        assert got["ing"] == ing


def test_find_plate_stack_prefers_stocked_spawn():
    """只取当前真有干净盘的盘堆。

    盘子被端走后出生点仍在快照里但已空。按距离盲选会选中空位，厨师
    走过去拿不到盘、整条做菜链卡死。干净盘的判据是 plates 里
    count == 0 的条目（count 是盘上食材数）。
    """
    w = W.World({
        "level": "Sushi_1_4_2P",
        "stations": [
            {"t": "plate_stack", "n": "empty", "x": 1.0, "z": 0.0},
            {"t": "plate_stack", "n": "stocked", "x": 9.0, "z": 0.0},
        ],
        "plates": [{"count": 0, "x": 9.0, "z": 0.0}],
    })
    # near 指向 empty，但它没盘——必须选远处有盘的那个
    assert S.find_plate_stack(w, near=(1.0, 0.0))["n"] == "stocked"


def test_find_plate_stack_includes_drying_rack():
    """晾干架（plate_return 且名字含 drying）也是可拿盘处。"""
    w = W.World({
        "level": "Sushi_1_4_2P",
        "stations": [
            {"t": "plate_return", "n": "DryingPart", "x": 2.0, "z": 0.0},
            {"t": "plate_return", "n": "workstation_plate_return",
             "x": 1.0, "z": 0.0},
        ],
        "plates": [],
    })
    got = S.find_plate_stack(w, near=(0.0, 0.0))
    assert got["n"] == "DryingPart"


def test_find_plate_stack_keeps_unfiltered_when_no_stack_matches_clean():
    """有干净盘但没有一个盘堆落在干净盘位置：保留未过滤列表，不坠落。

    守住两道卫兵（clean_pos 非空、stocked 却为空）合并成一道时会退化的
    分支：若 clean 盘的坐标与所有盘堆都对不上，仍应返回盘堆而非兜底/None。
    """
    w = W.World({
        "level": "Sushi_1_4_2P",
        "stations": [
            {"t": "plate_stack", "n": "s1", "x": 1.0, "z": 0.0},
            {"t": "plate_stack", "n": "s2", "x": 2.0, "z": 0.0},
        ],
        # 干净盘在 (7, 7)——没有任何盘堆在那里
        "plates": [{"count": 0, "x": 7.0, "z": 7.0}],
    })
    got = S.find_plate_stack(w, near=(1.0, 0.0))
    assert got is not None
    assert got["n"] in ("s1", "s2")


def test_find_plate_stack_dedupes_by_position():
    """同位置重复上报只算一个。"""
    w = W.World({
        "level": "Sushi_1_4_2P",
        "stations": [
            {"t": "plate_stack", "n": "a", "x": 1.0, "z": 0.0},
            {"t": "plate_stack", "n": "b", "x": 1.0, "z": 0.0},
        ],
        "plates": [],
    })
    assert S.find_plate_stack(w) is not None


def test_find_plate_stack_unknown_level_falls_back_to_plate_station():
    """换关卡且无盘堆：退到出餐口，仍不返回 None 就崩。"""
    w = W.World({
        "level": "Unknown_9P",
        "stations": [{"t": "plate_station", "n": "ps", "x": 3.0, "z": 0.0}],
        "plates": [],
    })
    assert S.find_plate_stack(w)["n"] == "ps"


def test_find_plate_stack_nothing_available_returns_none():
    """什么都没有时返回 None，由调用方决定怎么办。"""
    w = W.World({"level": "Unknown_9P", "stations": [], "plates": []})
    assert S.find_plate_stack(w) is None


def test_find_plate_stack_real_frame():
    """真实帧：8 个盘堆上报，3 个有干净盘，应从后者里选。"""
    import json
    path = os.path.join(FIXTURES, "real_frame_sushi_1_4.json")
    if not os.path.exists(path):
        pytest.skip("缺 tests/fixtures/real_frame_sushi_1_4.json")
    with open(path, "r", encoding="utf-8") as f:
        frame = json.load(f)
    w = W.World(frame)
    got = S.find_plate_stack(w, near=(14.4, -6.0))
    assert got is not None
    clean = set((round(p["x"], 2), round(p["z"], 2)) for p in w.plates
                if (p.get("count") or 0) == 0)
    assert (round(got["x"], 2), round(got["z"], 2)) in clean


def _grid13():
    """Sushi_1_3 主网格映射：origin (6.0, 7.2), cell 1.2。"""
    from ai.space.geometry import GridMapping
    m = GridMapping(cell=1.2, off_x=8, off_z=6, origin_x=6.0, origin_z=7.2)
    return m.to_grid


def test_sink_place_cells_merges_adjacent_drying_rack():
    """放脏盘占地 = 洗碗池 + 相邻晾干架（合并 1×2）。

    washing 的 gz 字段故意报错（2，实为 -4）：占地必须从 x/z 投影，不能信
    gx/gz（实机 WashingPart 的 gz 是局部网格坐标，与主网格不一致）。
    """
    to_grid = _grid13()
    st = {"t": "washing", "n": "WashingPart", "gx": 4, "gz": 2,
          "x": 10.8, "z": 2.4}
    stations = [
        st,
        {"t": "plate_return", "n": "DryingPart", "gx": 3, "gz": -4,
         "x": 9.6, "z": 2.4},
        {"t": "plate_return", "n": "workstation_plate_return", "gx": 5,
         "gz": -4, "x": 12.0, "z": 2.4},
    ]
    assert S.sink_place_cells(st, stations, to_grid) == {(4, -4), (3, -4)}


def test_sink_place_cells_ignores_dirty_return_and_non_adjacent():
    """只合并相邻的晾干架（drying）；脏盘回收口、不相邻的 drying 不并。"""
    to_grid = _grid13()
    st = {"t": "washing", "n": "WashingPart", "x": 10.8, "z": 2.4}
    stations = [
        st,
        {"t": "plate_return", "n": "workstation_plate_return",
         "x": 12.0, "z": 2.4},
        {"t": "plate_return", "n": "DryingPart", "x": 22.8, "z": 2.4},  # 不相邻
    ]
    assert S.sink_place_cells(st, stations, to_grid) == {(4, -4)}


def test_sink_place_cells_no_world_pos_returns_empty():
    to_grid = _grid13()
    st = {"t": "washing", "n": "WashingPart", "gx": 4, "gz": -4}
    assert S.sink_place_cells(st, [], to_grid) == set()


def test_sink_place_cells_non_dict_returns_empty():
    assert S.sink_place_cells(None, [], None) == set()

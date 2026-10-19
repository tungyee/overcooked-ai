"""state_dump 状态快照单测。

验证 snapshot() 输出可 JSON 序列化，且覆盖 trace 复盘要看的各类实体：
厨师 / 盘 / 灶 / 散落锅 / 箱子（crate 站点）/ 地图。
"""
import json

from ai.output import state_dump
from ai.output.state_dump import snapshot
from ai.state.world import World


def _world():
    # type: () -> World
    state = {
        "phase": "level",
        "level": "Sushi_1_4_2P",
        "chefs": [
            {"name": "Player 2", "x": 1.2, "z": 2.4, "held": None},
            {"name": "Player 1", "x": 0.0, "z": 0.0, "held": "Plate"},
        ],
        "plates": [
            {"n": "Plate_1", "x": 1.0, "z": 1.0, "count": 0,
             "contents": [], "detail": []},
        ],
        "cookers": [
            {"n": "workstation_cooker_01", "x": 3.0, "z": 3.0,
             "cooked": True, "pot_contents": []},
        ],
        "items": [
            {"n": "utensil_pot_01", "x": 4.0, "z": 4.0,
             "detail": ["cooked:SushiRice:pot"]},
        ],
        "stations": [
            {"t": "crate", "n": "crate_01", "gx": 0, "gz": 0,
             "x": 0.0, "z": 0.0, "ing": "SushiFish"},
        ],
        "recipes": [],
        "grid": {"size": "(1.2, 1.0, 1.2)", "nodeMap": None},
        # 传送带改用新协议 belts[]（§5.4，Task 10 起 World 不再读
        # occupancy.BELTS 关卡硬编码表），这里手工构造等价的两排、
        # 共 18 格，与旧 Sushi_1_4_2P 硬编码表数量一致。
        "belts": (
            [{"grid": "GridManager", "gx": gx, "gz": 2, "vx": 2.5, "vz": 0.0}
             for gx in range(-4, 5)]
            + [{"grid": "GridManager", "gx": gx, "gz": -3, "vx": -2.5, "vz": 0.0}
               for gx in range(-4, 5)]
        ),
    }
    return World(state)


def test_snapshot_is_json_serializable():
    """snapshot 输出可直接 json.dumps（无 tuple/set 泄漏）。"""
    snap = state_dump.snapshot(_world())
    json.dumps(snap, ensure_ascii=False)


def test_snapshot_covers_all_entity_sections():
    """各类实体都在：盘 / 灶 / 散落锅 / 箱子 / 地图。"""
    snap = state_dump.snapshot(_world())

    for key in ("chefs", "orders", "plates", "workstations", "cookers",
                "items", "extinguishers", "stations", "recipes", "map"):
        assert key in snap

    # 锅（散落 item）detail 原样透传，复盘"锅里有熟料"就靠它
    assert snap["items"][0]["detail"] == ["cooked:SushiRice:pot"]
    # 箱子（crate）落在 stations 里
    assert any(s.get("t") == "crate" for s in snap["stations"])
    # 厨师补算网格格
    assert snap["chefs"][0]["cell"] == [1, 2]


def test_snapshot_map_includes_cells_and_belts():
    """map 段含可玩区、传送带、P1/P2 所在格。"""
    snap = state_dump.snapshot(_world())
    m = snap["map"]
    assert set(("grids", "grid", "hazards", "belt_cells", "belt_dir",
                "p2_cell", "p1_cell")) <= set(m.keys())
    assert "bounds" not in m
    assert m["p2_cell"] == [1, 2]
    # Sushi_1_4_2P 有硬编码传送带，应被落盘；每项都是 [gx, gz] 列表
    assert m["belt_cells"]
    assert all(isinstance(c, list) and len(c) == 2 for c in m["belt_cells"])
    assert isinstance(m["belt_dir"], dict)


def test_snapshot_map_has_no_bounds_key():
    w = World({"grid": {"nodeMap": None}})
    snap = snapshot(w)
    assert "bounds" not in snap["map"]


def test_snapshot_map_includes_grids_and_hazards():
    w = World({
        "grid": {"nodeMap": None},
        "grids": [{"id": "GridManager", "ox": 9.6, "oz": -2.4, "cell": 1.2}],
        "hazards": [{"t": "fire", "grid": "GridManager", "x": 1.0, "z": 1.0, "ttl": 5.0}],
    })
    snap = snapshot(w)
    assert snap["map"]["grids"] == [{"id": "GridManager", "ox": 9.6, "oz": -2.4, "cell": 1.2}]
    assert len(snap["map"]["hazards"]) == 1


def test_snapshot_map_includes_terrain_and_occupancy():
    """map 段含可走性判定依据 terrain / occupancy（§5.3）。

    缺这两段时「为什么这格被判成可走/不可走」无法从 trace 复盘——
    trace-20261017-191650 的 no_progress 排查就卡在这里。
    """
    w = World({
        "grid": {"nodeMap": None},
        "terrain": [
            {"layer": 9, "boxes": [[3.6, 15.6, -8.6, 3.4]]},
            {"layer": 10, "boxes": [[9.0, 10.2, -3.0, -1.8]]},
        ],
        "occupancy": [
            {"grid": "GridManager", "cells": [[0, 0], [1, 0]]},
        ],
    })
    snap = snapshot(w)
    assert snap["map"]["terrain"] == [
        {"layer": 9, "boxes": [[3.6, 15.6, -8.6, 3.4]]},
        {"layer": 10, "boxes": [[9.0, 10.2, -3.0, -1.8]]},
    ]
    assert snap["map"]["occupancy"] == [
        {"grid": "GridManager", "cells": [[0, 0], [1, 0]]},
    ]

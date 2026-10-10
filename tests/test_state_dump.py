"""state_dump 状态快照单测。

验证 snapshot() 输出可 JSON 序列化，且覆盖 trace 复盘要看的各类实体：
厨师 / 盘 / 灶 / 散落锅 / 箱子（crate 站点）/ 地图。
"""
import json

from ai import state_dump
from ai.world import World


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
    assert set(("bounds", "grid", "belt_cells", "belt_dir",
                "p2_cell", "p1_cell")) <= set(m.keys())
    assert m["p2_cell"] == [1, 2]
    # Sushi_1_4_2P 有硬编码传送带，应被落盘；每项都是 [gx, gz] 列表
    assert m["belt_cells"]
    assert all(isinstance(c, list) and len(c) == 2 for c in m["belt_cells"])
    assert isinstance(m["belt_dir"], dict)

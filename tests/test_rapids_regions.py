"""Rapids_2_1 不连通地图：主网格判定与分区（trace-20261028-234917）。

fixture 5 帧：开局 / P1 投鸡飞行中 / 木筏漂移 / 切好待传 / 全候选 unreachable。
"""
import gzip
import json
import os

import pytest

from ai.query import predicates as P
from ai.space import regions as RG
from ai.state import world as W

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                       "rapids21-20261028.ndjson.gz")


@pytest.fixture(scope="module")
def frames():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _station(world, name):
    return next(s for s in world.stations if s.get("n") == name)


def test_main_grid_is_static_kitchen_not_raft(frames):
    """木筏站点 15 > 厨房 13，按众数会选中漂流的 Raft_Top。"""
    for f in frames:
        w = W.World(f)
        assert w._majority_grid() == "GridManager"


def test_mapping_origin_stable_while_raft_drifts(frames):
    """旧实现 origin_x 随木筏 15.6→22.1 漂，静止 P1 的格坐标每帧跳。"""
    origins = {(W.World(f).mapping.origin_x, W.World(f).mapping.origin_z)
               for f in frames}
    assert len(origins) == 1
    w = W.World(frames[0])
    # 厨房炸锅 FryingStation (1) 恒在主网格 (-3,-1)
    assert w.to_grid(18.0, -1.2) == (-3, -1)


def test_grids_main_flag_wins_over_name():
    """插件 0.21+ 的 grids[].main 优先于名字约定与众数。"""
    frame = {
        "grids": [{"id": "GridManager", "ox": 0, "oz": 0, "cell": 1.2},
                  {"id": "Kitchen", "ox": 0, "oz": 0, "cell": 1.2, "main": True}],
        "stations": [{"t": "counter", "n": "a", "grid": "GridManager",
                      "x": 0.0, "z": 0.0, "gx": 0, "gz": 0},
                     {"t": "counter", "n": "b", "grid": "Kitchen",
                      "x": 1.2, "z": 0.0, "gx": 1, "gz": 0}],
    }
    assert W.World(frame)._majority_grid() == "Kitchen"


def test_majority_skips_moving_grid_without_main_flag():
    frame = {
        "grids": [{"id": "Raft", "ox": 0, "oz": 0, "cell": 1.2, "moving": True},
                  {"id": "Floor", "ox": 0, "oz": 0, "cell": 1.2}],
        "stations": [dict(t="counter", n="r%d" % i, grid="Raft",
                          x=1.2 * i, z=0.0, gx=i, gz=0) for i in range(3)]
                    + [dict(t="counter", n="f", grid="Floor",
                            x=0.0, z=6.0, gx=0, gz=5)],
    }
    assert W.World(frame)._majority_grid() == "Floor"


def test_map_is_split_between_raft_and_kitchen(frames):
    for f in frames:
        assert RG.split(W.World(f)) is True


def test_far_side_kitchen_vs_raft(frames):
    w = W.World(frames[3])   # 切好待传
    fryer = _station(w, "FryingStation (1)")
    crate = _station(w, "DispenserCrate 3 (3)")
    board = _station(w, "countertop_01_chopping_board_old")
    assert RG.on_far_side(w, fryer) is True
    assert RG.on_far_side(w, crate) is True
    assert RG.reachable_by(w, board) is True
    assert RG.on_far_side(w, board) is False


def test_split_false_without_topology():
    """无边界的旧合成帧：拓扑未知，按连通处理，不凭空发投掷候选。"""
    w = W.World({"chefs": [{"name": P.P2_NAME, "x": 0, "z": 0},
                           {"name": P.P1_NAME, "x": 9, "z": 9}]})
    assert RG.split(w) is False
    assert RG.on_far_side(w, {"x": 9, "z": 9}) is False

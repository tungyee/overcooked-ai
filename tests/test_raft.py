# -*- coding: utf-8 -*-
"""舢板（ai/space/raft.py）、RAFT 通道（ai/space/channels.py）、RideRaft task 单测。

合成帧：左右两岛 + 中间一块下沉地板（舢板），柜台世界坐标随船位变化。
"""
import json

import pytest

from ai import tasks as T
from ai.exec.client import NullExec
from ai.exec.bt import Status
from ai.primitives import Raft
from ai.space import channels as CH
from ai.space import raft as RF
from ai.state import world as W


def _state(raft_minx, raft_maxx, p2_x=19.0, p1_x=0.0):
    """两块岛 + 一块下沉舢板地板；舢板柜台 x 取船中心。"""
    cx = (raft_minx + raft_maxx) / 2.0
    return {
        "phase": "level", "level": "synthetic", "paused": False,
        "chefs": [
            {"name": "Player 1", "x": p1_x, "z": 6.0},
            {"name": "Player 2", "x": p2_x, "z": 6.0},
        ],
        "stations": [
            # 舢板柜台（Counters 网格，x 随船动）
            {"t": "counter", "n": "raft_c", "grid": "Counters",
             "x": cx, "z": 4.8},
            # 右岛普通柜台（不在船上）
            {"t": "counter", "n": "island_c", "grid": "GridManager",
             "x": 18.0, "z": 2.4},
        ],
        "grids": [
            {"id": "GridManager", "main": True, "moving": False,
             "ox": 10.8, "oz": 6.0, "cell": 1.2},
            {"id": "Counters", "main": False, "moving": False,
             "ox": cx, "oz": 6.0, "cell": 1.2},
        ],
        "grid": {"size": 1.2},
        "terrain": [{"layer": 9, "boxes": [
            [-3, 4.2, -5, 11, -0.5, 0],                       # 左岛
            [16.2, 23.4, -5, 11, -0.5, 0],                    # 右岛
            [raft_minx, raft_maxx, 1.83, 10.23, -2.37, 0.03],  # 舢板
        ]}],
        "occupancy": [], "items": [], "plates": [], "held_plates": [],
        "cookers": [], "workstations": [], "orders": [], "recipes": [],
        "extinguishers": [], "held_items": [], "held_extinguishers": [],
    }


def test_raft_box_identifies_lowered_floor():
    w = W.World(_state(10.17, 16.17))
    assert RF.raft_box(w) == (10.17, 16.17, 1.83, 10.23)


def test_raft_box_none_without_lowered_floor():
    st = _state(10.17, 16.17)
    # 去掉舢板地板，只剩两块同高岛面
    st["terrain"][0]["boxes"] = st["terrain"][0]["boxes"][:2]
    assert RF.raft_box(W.World(st)) is None


def test_docked_side_right_and_left_and_middle():
    assert RF.docked_side(W.World(_state(10.17, 16.17))) == "right"   # 贴右岛
    assert RF.docked_side(W.World(_state(4.2, 10.2))) == "left"       # 贴左岛
    assert RF.docked_side(W.World(_state(7.0, 13.0))) is None         # 中间


def test_countertop_slots_only_raft_counters():
    slots = RF.countertop_slots(W.World(_state(10.17, 16.17)))
    assert [s["n"] for s in slots] == ["raft_c"]


def test_is_raft_counter_true_and_false():
    w = W.World(_state(10.17, 16.17))
    by_name = {s["n"]: s for s in w.stations}
    assert RF.is_raft_counter(w, by_name["raft_c"]) is True
    assert RF.is_raft_counter(w, by_name["island_c"]) is False


def test_on_my_side_follows_docked_side():
    # 船停右岸、P2 在右 → 我这一侧；P2 在左 → 不是
    assert RF.on_my_side(W.World(_state(10.17, 16.17, p2_x=19.0))) is True
    assert RF.on_my_side(W.World(_state(10.17, 16.17, p2_x=0.0))) is False


def test_channel_of_slot_maps_raft_counter_to_raft():
    w = W.World(_state(10.17, 16.17))
    raft_c = next(s for s in w.stations if s["n"] == "raft_c")
    island_c = next(s for s in w.stations if s["n"] == "island_c")
    assert CH.channel_of_slot(w, raft_c) == CH.RAFT
    assert CH.channel_of_slot(w, island_c) is None


def test_raft_slots_empty_when_not_split():
    # 两厨师都在右半（P1 也在右岛），可达场不相交 → 无 RAFT 通道
    w = W.World(_state(10.17, 16.17, p1_x=19.0))
    assert CH.raft_slots(w, None) == []
    assert CH.RAFT not in CH.channel_keys(w, None)


# ---------------------------------------------------------------- RideRaft task


def _ctx(raft_minx, raft_maxx, p2_x=19.0, p1_x=0.0):
    return {"w": W.World(_state(raft_minx, raft_maxx, p2_x=p2_x, p1_x=p1_x))}


def test_ride_raft_done_and_feasible():
    # 船停右岸、P2 在右岛：过河到左可行，到右不可行（已经在右）
    ctx = _ctx(10.17, 16.17, p2_x=19.0)
    left = T.RideRaft(item="left")
    right = T.RideRaft(item="right")
    assert left.done(ctx) is False
    assert left.feasible(ctx) is True
    assert right.feasible(ctx) is False

    # 船停左岸：到左 done
    ctx = _ctx(4.2, 10.2, p2_x=0.0)
    assert T.RideRaft(item="left").done(ctx) is True


def test_ride_raft_infeasible_when_moving_or_wrong_side():
    # 船在中间（未停靠）：不可行
    assert T.RideRaft(item="left").feasible(_ctx(7.0, 13.0)) is False
    # 船停右岸但 P2 在左岛（船不在 P2 这一侧）：不可行
    assert T.RideRaft(item="right").feasible(_ctx(10.17, 16.17, p2_x=0.0)) is False


def test_ride_raft_build_returns_tree():
    node = T.RideRaft(item="left").build()
    assert node is not None


# ---------------------------------------------------------------- Raft 原语


def test_raft_primitive_sends_command_and_stops_on_pred():
    w = W.World(_state(10.17, 16.17))
    chef = next(c for c in w.chefs if c["name"] == "Player 2")
    ex = NullExec()
    state = {"arrived": False}
    pred = lambda c: state["arrived"]  # noqa: E731
    prim = Raft("left", pred, timeout=200)
    ctx = {"w": w, "chef": chef, "exec": ex, "now": 0.0, "tick": 0}

    # 第一拍：发 raft 命令（RUNNING）
    assert prim.tick(ctx) == Status.RUNNING
    assert ex.issued and ex.issued[-1]["cmd"] == "raft"
    assert ex.issued[-1]["direction"] == "left"

    # 第二拍：命令已回 ok，仍在等 pred（RUNNING）
    assert prim.tick(ctx) == Status.RUNNING

    # 船到岸：SUCCESS
    state["arrived"] = True
    assert prim.tick(ctx) == Status.SUCCESS


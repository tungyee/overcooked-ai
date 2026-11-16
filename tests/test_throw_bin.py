# -*- coding: utf-8 -*-
"""投掷不能落进垃圾桶（4-2 Bin 1）。

真实帧 tests/fixtures/rapids/rapids42-throw-bin.ndjson.gz（trace-20261110-224136
t=141）：P2 在 Raft Two 拿 ChoppedLettuce，目标是对面 Raft One 的砧板；直线
正好从 Raft One 的 `Bin 1` 上方过。旧实现只看落点、不把垃圾桶当障碍，料被
桶接住（t=171 `ChoppedLettuce ... attach:Bin 1`），玩家拿不到。
"""
import gzip
import json
import math
import os

from ai import tasks as T
from ai.core import planner
from ai.query import predicates as P
from ai.query.recipes import RecipeBook
from ai.revenue import throw_supply as TS
from ai.state.bb_factory import make_bb
from ai.state.world import World

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                   "rapids42-throw-bin.ndjson.gz")
BOARD = "countertop_01_chopping_board_swamp (1)"


def _frame():
    with gzip.open(FIX, "rt", encoding="utf8") as fh:
        return json.loads(next(line for line in fh if line.strip()))


def _ctx():
    world = World(json.loads(json.dumps(_frame())))
    book = RecipeBook(world.recipes, world.stations, world.items)
    bb = make_bb(141)
    return world, book, bb, planner._make_context(world, bb, book)


def test_bin_is_a_throw_obstacle():
    world, book, bb, _ctx_ = _ctx()
    board = next(st for st in world.stations if st.get("n") == BOARD)
    # 当前站位的直线被 Bin 1 截断，不能“原地投”
    assert TS.aim_here(world, board, TS.MODE_SPLIT, bb) is None
    plan = TS.launch_plan(world, board, TS.MODE_SPLIT, bb)
    assert plan is not None
    bin_ = next(st for st in world.stations if st.get("t") == "bin"
                and st.get("grid") == "Raft One")
    lx, lz = plan["landing"]
    assert math.hypot(lx - bin_["x"], lz - bin_["z"]) > 1.2, plan


def test_throw_batch_walks_to_a_clear_launch_cell():
    world, book, bb, ctx = _ctx()
    board = next(st for st in world.stations if st.get("n") == BOARD)
    task = T.ThrowBatch(None, (P.resource_id(board), "board",
                              "Lettuce", "Lettuce"), "ChoppedLettuce")
    assert task.feasible(ctx)
    plan = TS.launch_plan(world, board, TS.MODE_SPLIT, bb)
    assert plan is not None and plan["cell"] is not None
    # 出手格不是 P2 当前所在格：说明它会走过去避开垃圾桶
    chef = P.chef_by_name(world.chefs, P.P2_NAME)
    assert plan["cell"] != P.chef_cell(chef, world.to_grid)

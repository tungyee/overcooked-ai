# -*- coding: utf-8 -*-
"""同一块盘不能被 SendAcross 和 TakeCookedToPlate 同时抢。

真实帧 tests/fixtures/rapids/rapids42-plate-conflict.ndjson.gz
（trace-20261110-231906 t=512/541）：盘 -20214 只装了薯条，1:1 配给
`ChickenNuggetsAndChips_All`（order#1，还缺鸡块）；同一帧它又满足
`ChipsOnly`（order#2），于是 SendAcross 想把它当成品送走，
TakeCookedToPlate 又想拿它加鸡块，两个任务每几拍交替，盘被拿起又放下。
"""
import gzip
import json
import os

from ai.core import planner
from ai.query.recipes import RecipeBook
from ai.state.bb_factory import make_bb
from ai.state.world import World

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                   "rapids42-plate-conflict.ndjson.gz")
PID = "-20214"


def _frames():
    with gzip.open(FIX, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _plan(frame):
    world = World(json.loads(json.dumps(frame)))
    book = RecipeBook(world.recipes, world.stations, world.items)
    bb = make_bb(frame["_trace_t"])
    task, record = planner.plan(world, bb, None, book)
    return task, record


def test_reserved_plate_is_not_sent_as_a_simpler_dish():
    """盘配给 All 单且还缺料：不要生成「按 ChipsOnly 送走」的 SendAcross。"""
    for t in (512,):
        frame = next(f for f in _frames() if f["_trace_t"] == t)
        task, record = _plan(frame)
        sends = [c[0] for c in record["candidates"]
                 if c[0].TYPE == "SendAcross" and PID in str(c[0])]
        assert not sends, (t, sends)
        assert task.TYPE == "TakeCookedToPlate"
        assert PID in str(task)
        assert record["plate_match"][PID] == "1"


def test_send_across_candidate_is_gone_while_plate_is_held_for_all_order():
    frame = next(f for f in _frames() if f["_trace_t"] == 512)
    task, _record = _plan(frame)
    assert "order#1" in str(task)


def test_unreachable_plate_is_not_matched_to_the_head_order():
    """队首单的盘漂到对面时，别占住 P2 这侧唯一能用的盘，导致整局没任务。

    trace-20261110-233159 t=1726：-19556 在 Raft One（P2 够不着），
    -19560 在 Raft Two；旧匹配把队首 All 单绑到 -19556，TakeCookedToPlate
    恒 unreachable，P2 静止到结束。
    """
    frame = json.loads(gzip.open(
        os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                     "rapids42-static.ndjson.gz"), "rt").readline())
    task, record = _plan(frame)
    assert "-19556" not in record["plate_match"]
    assert task is not None and task.TYPE == "TakeCookedToPlate"
    assert "-19560" in str(task)

"""砧板被产物占着时先腾板，再放料（通用，不按物料特判）+ 载体匹配优先可达。

真实帧 tests/fixtures/wizard/wizard32-20261101-173006-board-base.ndjson.gz
（trace-20261101-173006，Wizard_3_2_2P，`_trace_t` 为 trace 拍号）：
  [0] t=176：右板 board (1) 上是刚切好的饼，左板空着、P2 够得着；
  [1] t=400：中线动墙合上，右板（饼上已有番茄）P2 够不着，左边台面 (2) 上有
      一张空饼 P2 够得着；
  [2] t=1000：三张饼分处两边；
  [3] t=2100：P2 在右半区。右板仍被那张只放了番茄的饼占着，左板被切好的
      意式香肠占着且够不着——芝士没有板可切，饼补不齐料，当局一直卡到结束。
"""
import gzip
import json
import os

import pytest

from ai import tasks as T
from ai.core import planner
from ai.exec.bt import Status
from ai.query import carriers as CR
from ai.query.recipes import RecipeBook
from ai.state import world as W
from ai.state.bb_factory import make_bb

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "wizard",
                       "wizard32-20261101-173006-board-base.ndjson.gz")
RIGHT = "countertop_01_chopping_board_gold_02 (1)"
LEFT = "countertop_01_chopping_board_gold_02"


@pytest.fixture(scope="module")
def frames():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _ctx(frame):
    w = W.World(frame)
    book = RecipeBook(w.recipes, w.stations)
    bb = make_bb(100)
    return w, book, bb, planner._make_context(w, bb, book)


def _plan(frame):
    w, book, bb, ctx = _ctx(frame)
    task, rec = planner.plan(w, bb, None, book)
    return w, book, bb, ctx, task, rec


def _set_board(frame, board, item):
    f = json.loads(json.dumps(frame))
    next(ws for ws in f["workstations"] if ws["n"] == board)["item"] = item
    return f


# ---------------------------------------------------------------- 腾板判据


def test_carrier_on_board_is_clearable(frames):
    w, book, bb, _ctx_ = _ctx(frames[3])
    assert T.board_clearable(w, book, bb, RIGHT)


def test_unreachable_board_is_not_clearable(frames):
    """左板上切好的香肠是产物，但 P2 此刻在右半区够不着。"""
    w, book, bb, _ctx_ = _ctx(frames[3])
    assert not T.board_clearable(w, book, bb, LEFT)


@pytest.mark.parametrize("item,ok", [
    ("ChoppedCheese", True),      # 切好的料：产物，可挪
    ("Cheese", False),            # 还没切的生料：Chop 的输入，不动
    (None, False),                # 空板：没东西可腾
])
def test_clearable_is_generic_by_chop_state(frames, item, ok):
    w, book, bb, _ctx_ = _ctx(_set_board(frames[3], RIGHT, item))
    assert T.board_clearable(w, book, bb, RIGHT) is ok


def test_board_being_chopped_is_not_clearable(frames):
    f = json.loads(json.dumps(frames[3]))
    ws = next(ws for ws in f["workstations"] if ws["n"] == RIGHT)
    ws.update(item="ChoppedCheese", interacting=True)
    w, book, bb, _ctx_ = _ctx(f)
    assert not T.board_clearable(w, book, bb, RIGHT)


# ---------------------------------------------------------------- 候选与执行


def test_free_reachable_board_is_used_without_clearing(frames):
    """有空板就不腾：t=176 左板空着，只往左板放料。"""
    _w, _b, _bb, _c, task, rec = _plan(frames[0])
    boards = {c[0].board_id for c in rec["candidates"] if c[0].TYPE == "FetchToBoard"}
    assert boards == {LEFT}
    assert task.TYPE == "FetchToBoard" and task.board_id == LEFT


def test_occupied_board_gets_fetch_candidates_when_no_free_board(frames):
    """t=2100：没有够得着的空板 → 给被饼占着的右板发切料候选（当局恒无）。"""
    _w, _b, _bb, _c, task, rec = _plan(frames[3])
    ok = [c[0] for c in rec["candidates"]
          if c[0].TYPE == "FetchToBoard" and c[0].board_id == RIGHT and c[3]]
    assert {t.item for t in ok} >= {"Cheese", "Tomato"}
    assert task.TYPE == "FetchToBoard" and task.board_id == RIGHT


def _empty_handed(frame):
    f = json.loads(json.dumps(frame))
    next(c for c in f["chefs"] if c["name"] == "Player 2")["held"] = None
    f["items"] = [i for i in f["items"] if i.get("held_by") != "Player 2"]
    f["held_items"] = [i for i in f.get("held_items") or ()
                       if i.get("held_by") != "Player 2"]
    return f


def test_fetch_to_board_puts_down_held_raw_before_clearing(frames):
    """t=2100 P2 手里拿着番茄：对着有饼的板交互会把番茄放上饼，必须先放下。"""
    from tests.test_exec_primitives import FakeExec
    _w, _b, _bb, ctx = _ctx(frames[3])
    assert ctx["chef"]["held"] == "Tomato"
    task = T.FetchToBoard("4", (RIGHT,), "Cheese")
    assert task.feasible(ctx)
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert task.build().tick(ctx) == Status.RUNNING
    assert ctx["exec"].issued[-1]["target"]["name"] != RIGHT


def test_fetch_to_board_clears_board_first(frames):
    """空手：先去板上拿走饼，而不是先去箱子取料。"""
    from tests.test_exec_primitives import FakeExec
    _w, _b, _bb, ctx = _ctx(_empty_handed(frames[3]))
    task = T.FetchToBoard("4", (RIGHT,), "Cheese")
    assert task.feasible(ctx)
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert task.build().tick(ctx) == Status.RUNNING
    assert ctx["interaction"]["kind"] == "pickup"
    assert ctx["exec"].issued[-1]["target"]["name"] == RIGHT


def test_raw_on_board_blocks_fetch(frames):
    """板上是没切的生料：不腾、不可行（那是 Chop 的事）。"""
    _w, _b, _bb, ctx = _ctx(_set_board(frames[3], RIGHT, "Tomato"))
    assert not T.FetchToBoard("4", (RIGHT,), "Cheese").feasible(ctx)


# ---------------------------------------------------------------- 载体匹配优先可达


def test_match_prefers_reachable_carrier(frames):
    """t=400：放了番茄的饼够不着，空饼够得着 → 最早的单配够得着的那张。"""
    w, book, _bb, _ctx_ = _ctx(frames[1])
    m = CR.match_prep_carriers(w, book)
    first = w.orders[0]["id"]
    assert m[first]["item"]["on_station"] == "attach:countertop_01_standard_gold (2)"
    assert m[first]["contents"] == frozenset()

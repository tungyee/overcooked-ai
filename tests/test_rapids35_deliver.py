# -*- coding: utf-8 -*-
"""Rapids_3_5 最后 13 秒：成品盘在手却不送（trace-20261101-180609）。

真实帧 tests/fixtures/rapids/rapids35-20261101-180609-deliver.ndjson.gz
（`_trace_t` 为 trace 拍号；trace 的 state 已还原成插件原始帧形状）：
  [0] t=12480：P2 手里端着装好鸡块的盘 -107804（#2 只要鸡块）。出餐船上下晃，
      出餐口占地量化到紧邻水面的那一行，格内交互位为空 → 当局判
      unreachable_destination，P2 把成品盘放到木筏上去干别的；
  [1] t=12521：盘已在木筏上，出餐口量化回可达那一行；
  [2] t=12549：同上。
三帧台面上都躺着一篮已离火的熟薯条 FrierBasket。

两处根因：
  1. 熟散锅压力 1.0 → L2 救料层，结构性压过 L3 送餐（[1][2] 当局都选了盛薯条）；
  2. 漂移出餐口格内交互位为空时没有兜底站位（[0] 判不可达）。
"""
import gzip
import json
import os

import pytest

from ai import primitives as PR
from ai import tasks as T
from ai.core import planner
from ai.core.planner_layers import L_PREP, L_REDEEM
from ai.exec.bt import Status
from ai.query.recipes import RecipeBook
from ai.space import reachability as R
from ai.space import shuttle as SH
from ai.state import world as W
from ai.state.bb_factory import make_bb

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                       "rapids35-20261101-180609-deliver.ndjson.gz")
DISH = "-107804"


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
    w, book, bb, _ = _ctx(frame)
    return planner.plan(w, bb, None, book)


def _hatch(w):
    return next(s for s in w.stations if s.get("t") == "plate_station")


# ---------------------------------------------------------------- 散锅分层


@pytest.mark.parametrize("idx", [0, 1, 2])
def test_loose_cooked_basket_not_in_rescue_layer(frames, idx):
    _, rec = _plan(frames[idx])
    layers = [c[6] for c in rec["candidates"] if c[0].TYPE == "TakeCookedToPlate"]
    assert layers and all(layer == L_PREP for layer in layers)


# ---------------------------------------------------------------- 漂移出餐口


def test_hatch_has_no_grid_interact_cell_at_12480(frames):
    """根因复现：[0] 的出餐口常规交互位为空，只能靠格外站位。"""
    w = W.World(frames[0])
    stances = SH.hatch_fallback(w, _hatch(w))
    assert stances
    field = R.field_for(w, None)
    assert any(s["cell"] in field for s in stances)


@pytest.mark.parametrize("idx", [1, 2])
def test_hatch_fallback_off_when_grid_cells_exist(frames, idx):
    """有格内交互位时不启用兜底：静态出餐口的站位选择不变。"""
    w = W.World(frames[idx])
    assert SH.hatch_fallback(w, _hatch(w)) == []


def test_deliver_not_vetoed_while_hatch_bobs(frames):
    w, book, bb, ctx = _ctx(frames[0])
    assert R.task_veto(T.DeliverPlate("2", (DISH,)), ctx) == ""


@pytest.mark.parametrize("idx", [0, 1, 2])
def test_finished_plate_is_delivered(frames, idx):
    task, rec = _plan(frames[idx])
    assert rec["top_layer"] == L_REDEEM
    assert task.TYPE == "DeliverPlate"
    assert task.plate_id == DISH


def test_approach_hatch_moves_to_stance_point(frames):
    """执行层同一判据：走到格外站位，move 终点朝出餐口推 GOAL_PUSH。"""
    from tests.test_exec_primitives import FakeExec
    w, book, bb, ctx = _ctx(frames[0])
    ex = FakeExec()
    ctx = dict(ctx, exec=ex, tick=0)
    ap = PR.Approach(lambda c: _hatch(c["w"]), kind="placement")
    assert ap.tick(ctx) == Status.RUNNING
    cmd = ex.issued[0]
    assert cmd["cmd"] == "move"
    stance = [s for s in SH.hatch_fallback(w, _hatch(w)) if s["cell"] in ctx["field"]][0]
    assert cmd["goal"] == SH.goal_point(stance)
    assert cmd["path"][-1] == cmd["goal"]
    assert cmd["target"] is not None

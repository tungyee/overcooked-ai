# -*- coding: utf-8 -*-
"""Mine_2_4_2P 墨西哥卷：折叠盘识别与快糊锅兜底。

真实帧 tests/fixtures/mine/mine24-20261031-230344-burrito.ndjson.gz
（trace-20261031-230344）：
  [0] t=1400 盘 -15532 放上卷饼后折叠成单个 CompositeAssembledNode，已齐
  [1] t=1870 同一盘仍齐；灶 (2) 上一锅多煎的蘑菇已熟、在烧糊计时
旧行为：解析器只认出折叠节点里第一个熟料 → 盘永远「缺料」、从不送餐，
还多煎一锅蘑菇；往满盘加料被拒后灶前站位被拉黑，RescueCooked 判
unreachable，锅在拉黑 TTL 内烧穿起火。
"""
import gzip
import json
import os

import pytest

from ai.core import planner
from ai.primitives.stuck import _ensure_stuck_scratch
from ai.query.plate_tokens import parse_plate_detail
from ai.query.recipes import RecipeBook
from ai.space import reachability as R
from ai.state import world as W
from ai.state.bb_factory import make_bb
from ai import tasks as T

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mine",
                       "mine24-20261031-230344-burrito.ndjson.gz")
PLATE = "-15532"
COOKER = ("GridManager", "workstation_cooker_01 (2)")
FOLDED = ("CompositeAssembledNode{ CookedCompositeAssembledNode(Pot){ ing:Rice }, "
          "CookedCompositeAssembledNode(FryingPan){ ing:Mushroom }, ing:Tortilla }")


@pytest.fixture(scope="module")
def frames():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _plan(frame, bb=None):
    w = W.World(frame)
    book = RecipeBook(w.recipes, w.stations)
    bb = bb or make_bb(100)
    task, rec = planner.plan(w, bb, None, book)
    return w, book, bb, task, rec


def _cand(rec, text):
    return [c for c in rec["candidates"] if str(c[0]).startswith(text)]


def test_folded_composite_plate_expands_all_children():
    assert parse_plate_detail([FOLDED]) == {
        "cooked:rice:pot", "cooked:mushroom:fryingpan", "tortilla"}


def test_nested_composite_inside_composite():
    s = "CompositeAssembledNode{ ing:A, CompositeAssembledNode{ ing:B, Cooked(Pot){ ing:C, ing:D } } }"
    assert parse_plate_detail([s]) == {"a", "b", "cooked:c+d:pot"}


def test_folded_plate_matches_order(frames):
    w = W.World(frames[0])
    book = RecipeBook(w.recipes, w.stations)
    plate = next(p for p in frames[0]["plates"] if p["n"] == PLATE)
    order = next(o for o in frames[0]["orders"] if o["id"] == "1")
    assert book.plate_tokens(plate) <= book.order_want_tokens(order)


def test_complete_folded_plate_gets_delivered(frames):
    _, _, _, task, rec = _plan(frames[0])
    assert str(task) == "DeliverPlate(%s, order#1)" % PLATE
    # 不再给已齐的盘补卷饼/补蘑菇
    assert not [c for c in rec["candidates"]
                if PLATE in str(c[0]) and c[0].TYPE in ("FetchToPlate", "TakeCookedToPlate")]


def _blacklist_all_stances(w, bb, book):
    ctx = planner._make_context(w, bb, book)
    goals = R._interact_goals(ctx, R.resource(w, COOKER))
    assert goals
    scratch = _ensure_stuck_scratch({"bb": bb})
    for g in goals:
        scratch.bad_pairs[(COOKER, tuple(g))] = bb.tick + 200


def test_rescue_not_vetoed_when_all_stances_blacklisted(frames):
    w = W.World(frames[1])
    book = RecipeBook(w.recipes, w.stations)
    bb = make_bb(100)
    _blacklist_all_stances(w, bb, book)
    ctx = planner._make_context(w, bb, book)
    rescue = T.RescueCooked(resources=(COOKER,))
    assert rescue.feasible(ctx)
    assert R.task_veto(rescue, ctx) == ""
    # 共享 ctx 没被原地改：其它 task 仍尊重拉黑（防单站位死循环）
    assert "stance_last_resort" not in ctx
    assert not R.can_interact(ctx, R.resource(w, COOKER))


def test_planner_keeps_rescue_feasible_under_blacklist(frames):
    w = W.World(frames[1])
    book = RecipeBook(w.recipes, w.stations)
    bb = make_bb(100)
    _blacklist_all_stances(w, bb, book)
    _, _, _, task, rec = _plan(frames[1], bb)
    rescue = _cand(rec, "RescueCooked(%s" % (COOKER,))
    assert rescue and rescue[0][3] is True, rescue
    take = [c for c in rec["candidates"]
            if c[0].TYPE == "TakeCookedToPlate" and COOKER in c[0].key[2]]
    assert take and all(c[3] is False for c in take)


def test_executor_scopes_last_resort_flag_to_task_tick():
    from ai.core.executor import Executor
    from ai.exec.bt import Node, Status

    seen = []

    class _Probe(Node):
        def tick(self, ctx):
            seen.append(ctx.get("stance_last_resort"))
            return Status.RUNNING

    class _Task(object):
        def __init__(self, flag):
            self.key = ("Probe", None, (flag,), None)
            if flag:
                self.STANCE_LAST_RESORT = True

        def build(self):
            return _Probe()

    ex, ctx = Executor(), {"tick": 0}
    ex.tick(_Task(True), ctx)
    assert seen[-1] is True and "stance_last_resort" not in ctx
    ex.tick(_Task(False), ctx)
    assert seen[-1] is None

"""Wizard_3_2（游戏内 3-2）：同名生饼抢走熟披萨的可达性判定。

真实帧 tests/fixtures/wizard/wizard32-20261101-170754-cooked-pizza.ndjson.gz
（trace-20261101-170754 t=8005 的 state 快照还原）：地图左右分割，P2 在右半区。
回盘处左边台面 (5,0) 上有一份出炉的熟玛格丽特（Pizza_Base_Raw, cooked=Cooked），
旁边 (4,0) 有空盘 -59512（订单 #1）。场上还有两件同名 Pizza_Base_Raw：P1 区砧板
上的生饼和左烤箱里的饼。可达性按名字只取第一件 → 生饼 → 判 unreachable，
P2 干站着不装盘。
"""
import gzip
import json
import os

import pytest

from ai.core import planner
from ai.query.recipes import RecipeBook
from ai.space import reachability as R
from ai.state import world as W
from ai.state.bb_factory import make_bb

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "wizard",
                       "wizard32-20261101-170754-cooked-pizza.ndjson.gz")


@pytest.fixture(scope="module")
def frame():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return json.loads(fh.readline())


def _plan(frame):
    w = W.World(frame)
    book = RecipeBook(w.recipes, w.stations)
    bb = make_bb(100)
    return w, book, bb, planner.plan(w, bb, None, book)


def _loose_take(rec):
    return [c for c in rec["candidates"]
            if c[0].TYPE == "TakeCookedToPlate" and c[0].cooker_id == "Pizza_Base_Raw"]


def test_loose_cooked_pizza_is_reachable_despite_same_name_raw(frame):
    w, book, bb, (task, rec) = _plan(frame)
    cands = _loose_take(rec)
    assert cands, "熟披萨没生成端盘候选"
    assert all(c[3] for c in cands), [(str(c[0]), c[4]) for c in cands]
    # 按名字查确实会撞上 P1 区的生饼（本用例要覆盖的前提）
    ctx = planner._make_context(w, bb, book)
    assert not R.can_interact(ctx, R.resource(w, "Pizza_Base_Raw"))


def test_p2_plates_the_loose_pizza_instead_of_idling(frame):
    _w, _book, _bb, (task, _rec) = _plan(frame)
    assert task is not None
    assert task.TYPE == "TakeCookedToPlate"
    assert task.cooker_id == "Pizza_Base_Raw"

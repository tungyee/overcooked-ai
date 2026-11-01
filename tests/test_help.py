"""P1 求助触发：P1/P2 成本比较、阈值、订单 DAG 前沿枚举与排名（spec §5.1–§5.2）。"""
import gzip
import json
import os

from ai.dialog.duties import Ledger
from ai.dialog.help import (HELP_THRESHOLD_STEPS, FrontierStep, _rescue_for_help,
                            cost_to, frontier_steps, p1_saves_enough,
                            top_help_request)
from ai.query import predicates as P
from ai.query.recipes import RecipeBook
from ai.state import blackboard as BB
from ai.state import world as W

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                       "rapids21-20261028.ndjson.gz")


def _rapids_frame():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        for line in fh:
            if line.strip():
                return json.loads(line)
    raise AssertionError("empty fixture")


def _rapids_world_book_ledger():
    w = W.World(_rapids_frame())
    book = RecipeBook(w.recipes, w.stations)
    return w, book, BB.Blackboard(), Ledger()


# ---------- Task 1 ----------

def test_p1_saves_enough_p1_unreachable():
    assert p1_saves_enough(None, 5) is False


def test_p1_saves_enough_p2_unreachable():
    assert p1_saves_enough(3, None) is True


def test_p1_saves_enough_at_threshold():
    assert HELP_THRESHOLD_STEPS == 8
    assert p1_saves_enough(2, 10) is True      # 10 - 2 == 8
    assert p1_saves_enough(2, 9) is False      # 7 < 8


def test_cost_to_rapids_p2_cannot_reach_potato():
    w = W.World(_rapids_frame())
    crate = next(s for s in w.stations if s.get("ing") == "Potato")
    assert cost_to(w, None, P.P2_NAME, crate) is None
    assert isinstance(cost_to(w, None, P.P1_NAME, crate), int)


# ---------- Task 2 ----------

def _rapids_frontier():
    w, book, bb, _ = _rapids_world_book_ledger()
    return w, book, bb, frontier_steps(w, bb, book, w.orders, {}, {})


def test_frontier_rapids_includes_unreachable_potato_fetch():
    _, _, _, steps = _rapids_frontier()
    by = {(s.task_type, s.item): s for s in steps}
    assert ("FetchToBoard", "potato") in by
    assert by[("FetchToBoard", "potato")].p2_cost is None
    assert isinstance(by[("FetchToBoard", "potato")].p1_cost, int)


def test_frontier_only_upstream_ready():
    # 土豆还没上板时，Chop(Potato) 不进前沿；取料是前沿。
    _, _, _, steps = _rapids_frontier()
    assert not any(s.task_type == "Chop" and s.item == "potato" for s in steps)


def test_rescue_for_help_skips_rescue_types():
    class _T(object):
        TYPE = "RescueCooked"
    assert _rescue_for_help(_T(), {}) is True
    class _E(object):
        TYPE = "Extinguish"
    assert _rescue_for_help(_E(), {}) is True


def test_rescue_for_help_take_cooked_only_when_burning():
    class _T(object):
        TYPE = "TakeCookedToPlate"
    assert _rescue_for_help(_T(), {"burning": True}) is True
    assert _rescue_for_help(_T(), {"burning": False, "onFire": False}) is False


# ---------- Task 4 ----------

def test_top_help_request_returns_blocking_step():
    w, book, bb, ledger = _rapids_world_book_ledger()
    got = top_help_request(w, bb, book, w.orders, {}, {}, ledger)
    assert got is not None and got.p2_cost is None


def test_top_help_request_skips_already_requested():
    w, book, bb, ledger = _rapids_world_book_ledger()
    ledger.add_help_request("FetchToBoard", "Potato", tick=0)
    got = top_help_request(w, bb, book, w.orders, {}, {}, ledger)
    assert got is None or (got.task_type, got.item) != ("FetchToBoard", "potato")


def test_source_for_take_cooked_uses_cooker():
    from ai.dialog.help import _source_for
    class _T(object):
        TYPE = "TakeCookedToPlate"
        def _source(self, ctx):
            return {"kind": "cooker", "target": {"t": "cooking", "n": "cooker_x"}}
    assert _source_for(None, None, _T(), {}) == {"t": "cooking", "n": "cooker_x"}


def test_source_for_fetch_to_cooker_uses_raws():
    from ai.dialog.help import _source_for
    class _T(object):
        TYPE = "FetchToCooker"
        def _raws(self, ctx):
            return ("Potato",)
    w = W.World(_rapids_frame())
    src = _source_for(w, None, _T(), {})
    assert src is not None and src.get("t") == "crate"


def test_top_help_request_prunes_resolved_duty():
    w, book, bb, ledger = _rapids_world_book_ledger()
    # Chop 不是当前 P1 更划算的前沿步骤（板空、不上前沿）→ 该被清掉
    ledger.add_help_request("Chop", "Potato", tick=0)
    assert ledger.help_request_ids()
    top_help_request(w, bb, book, w.orders, {}, {}, ledger)
    assert ledger.help_request_ids() == []


def test_top_help_request_silent_in_mine24_spawn():
    # Mine_2_4 出生帧：肉/饭 P1 只近 3~5 步（<8），连通图不喊
    fp = os.path.join(os.path.dirname(__file__), "fixtures", "mine",
                      "mine24-20261031-230344-burrito.ndjson.gz")
    with gzip.open(fp, "rt", encoding="utf8") as fh:
        frame = json.loads(next(l for l in fh if l.strip()))
    w = W.World(frame)
    book = RecipeBook(w.recipes, w.stations)
    assert top_help_request(w, BB.Blackboard(), book, w.orders, {}, {},
                            Ledger()) is None

"""P1 求助触发：P1/P2 成本比较与阈值（spec §5.1）。"""
import gzip
import json
import os

from ai.dialog.help import HELP_THRESHOLD_STEPS, cost_to, p1_saves_enough
from ai.query import predicates as P
from ai.state import world as W

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                       "rapids21-20261028.ndjson.gz")


def _rapids_frame():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        for line in fh:
            if line.strip():
                return json.loads(line)
    raise AssertionError("empty fixture")


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

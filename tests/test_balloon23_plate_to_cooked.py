"""Balloon_2_3（游戏内 2-2）：熟锅等盘时，盘别被用不上的单占着。

真实帧 tests/fixtures/balloon/balloon23-20261030-224658-plate-to-cooked.ndjson.gz
（trace-20261030-224658）：
  [0] t=660  鱼锅熟，Marinara #2 没盘；意面盘 -16230 绑给缺肉、没肉锅的 MeatOnly #3
  [1] t=1901 鱼锅熟，Marinara #4 没盘；意面盘 -16228 绑给缺肉、没肉锅的 MeatOnly #3

修复前两帧都选 RescueCooked（端锅离火），修复后应端盘盛鱼。
"""
import gzip
import json
import os

import pytest

from ai.core import planner
from ai.query.recipes import RecipeBook
from ai.state import world as W
from ai.state.bb_factory import make_bb

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "balloon",
                       "balloon23-20261030-224658-plate-to-cooked.ndjson.gz")


@pytest.fixture(scope="module")
def frames():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _matches(frame):
    w = W.World(frame)
    book = RecipeBook(w.recipes, w.stations)
    bb = make_bb(100)
    pm = planner._match_plates_to_orders(w, bb, book, orders=w.orders)
    cm = planner._match_cookers_to_orders(w, bb, book, pm, orders=w.orders)
    return w, book, bb, pm, cm


def test_t660_pasta_plate_goes_to_marinara_with_cooked_fish(frames):
    w, book, bb, pm, cm = _matches(frames[0])
    assert pm.get("-16230") == "2", pm
    fish_pan = ("RightBasket", "workstation_cooker_01 (5)")
    assert cm.get(fish_pan) == "2", cm


def test_t660_only_one_marinara_jumps_ahead_per_fish_pan(frames):
    """一口熟鱼锅只够一块盘提前：空盘 -16228 不该再给另一单 Marinara #4。"""
    w, book, bb, pm, cm = _matches(frames[0])
    assert pm.get("-16228") != "4", pm


def test_t1901_pasta_plate_goes_to_marinara_with_cooked_fish(frames):
    w, book, bb, pm, cm = _matches(frames[1])
    assert pm.get("-16228") == "2", pm
    fish_pan = ("RightBasket", "workstation_cooker_01 (3)")
    assert cm.get(fish_pan) == "2", cm


@pytest.mark.parametrize("idx", [0, 1])
def test_plan_takes_cooked_fish_to_plate_instead_of_rescue(frames, idx):
    w = W.World(frames[idx])
    book = RecipeBook(w.recipes, w.stations)
    task, _rec = planner.plan(w, make_bb(100), None, book)
    assert task.TYPE == "TakeCookedToPlate", task
    assert task.item == "PanFriedFish", task

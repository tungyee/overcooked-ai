"""移动网格台面的拼装位身份：(grid, 站名)，不是一次性投影出来的主网格格。

真实帧 tests/fixtures/balloon/balloon23-20261030-224658-basket-counter.ndjson.gz
（trace-20261030-224658，Balloon_2_3_2P）：
  [0] t=2991 选中 PlaceCleanPlate(-37174, (6, 0))——FoodBasket 台面 (13) 此刻投影在 (6,0)
  [1] t=3127 篮子下移，台面 (13) 投影到 (6,-2)；(6,0) 只剩 P2 和手里的盘，
             旧实现把手里的盘当成放盘目标，P2 站在空地上反复 tap 直到 stage_failed
"""
import gzip
import json
import os

import pytest

from ai import tasks as T
from ai.query.placement import counter_cell_now, counter_key, counter_key_at
from ai.query.recipe_graph import _emit_plate
from ai.query.recipes import RecipeBook
from ai.state import world as W
from ai.state.bb_factory import make_bb
from ai.tasks.base import station_at_cell

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "balloon",
                       "balloon23-20261030-224658-basket-counter.ndjson.gz")
BASKET_COUNTER = ("FoodBasket", "countertop_01_standard_dark_wood (13)")


@pytest.fixture(scope="module")
def worlds():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [W.World(json.loads(line)) for line in fh if line.strip()]


def _ctx(w):
    p2 = [c for c in w.chefs if c.get("name") == "Player 2"][0]
    return {"w": w, "chef": p2, "book": RecipeBook(w.recipes, w.stations)}


def test_basket_counter_key_is_station_name(worlds):
    w = worlds[0]
    st = [s for s in w.stations
          if s.get("n") == BASKET_COUNTER[1] and s.get("grid") == "FoodBasket"][0]
    assert counter_key(w, st) == BASKET_COUNTER
    assert counter_key_at(w, (6, 0)) == BASKET_COUNTER


def test_main_grid_counter_key_stays_cell(worlds):
    w = worlds[0]
    st = [s for s in w.stations
          if s.get("t") == "counter" and s.get("grid") == "GridManager"][0]
    key = counter_key(w, st)
    assert isinstance(key, tuple) and all(isinstance(v, int) for v in key)


def test_emit_plate_binds_basket_counter_by_name(worlds):
    """候选生成时就用站名绑定，不存当前投影格。"""
    w = worlds[0]
    tasks = _emit_plate(w, make_bb(100), "6")
    cells = {t.counter_cell for t in tasks}
    assert BASKET_COUNTER in cells
    assert (6, 0) not in cells


def test_target_follows_basket(worlds):
    """篮子移动后，拼装位仍解析到同一块台面、格随之更新。"""
    w0, w1 = worlds
    assert counter_cell_now(w0, BASKET_COUNTER) == (6, 0)
    assert counter_cell_now(w1, BASKET_COUNTER) == (6, -2)
    st = station_at_cell(_ctx(w1), BASKET_COUNTER)
    assert st is not None and st.get("n") == BASKET_COUNTER[1]


def test_old_cell_identity_resolves_to_held_plate(worlds):
    """回归说明：旧身份 (6,0) 在 t=3127 解析成 P2 手里的盘（plate_stack 条目）。"""
    st = station_at_cell(_ctx(worlds[1]), (6, 0))
    assert st is not None and st.get("t") != "counter"


def test_place_clean_plate_feasible_after_basket_moves(worlds):
    w = worlds[1]
    t = T.PlaceCleanPlate("6", ("-37174", BASKET_COUNTER))
    ctx = _ctx(w)
    assert t.done(ctx) is False
    assert t.feasible(ctx) is True       # 端着盘、台面还在 → 继续放
    assert set(t.claims()) == {("-37174", "place"), (BASKET_COUNTER, "assemble")}


def test_missing_basket_counter_infeasible(worlds):
    w = worlds[0]
    pid = [p for p in w.plates if p.get("count") == 0][0]["n"]
    t = T.PlaceCleanPlate("6", (pid, ("FoodBasket", "no_such_counter")))
    assert t.feasible(_ctx(w)) is False

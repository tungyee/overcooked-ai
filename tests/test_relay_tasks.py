# -*- coding: utf-8 -*-
"""中转交接 task 的契约测试（feasible / done / claims / build）。

复用 test_tasks.py 的 FakeWorld / make_ctx / plate 等 fixture 风格。
"""
import pytest

from ai import tasks as T
from tests.test_tasks import ORDER, RECIPES, FakeWorld, make_ctx, plate


COOKER_ST = {"t": "cooking", "n": "cooker1", "gx": 4, "gz": 3,
             "x": 4.8, "z": 3.6}


# ---------- PlaceToRelay ----------

def test_place_to_relay_claims_nothing():
    t = T.PlaceToRelay(item="p1")
    assert t.claims() == ()


def test_place_to_relay_infeasible_after_relay_disabled():
    # 中转台已停用：即使 1-3 关卡也不再生成/可行（feasible 依赖 relay_cells）
    w = FakeWorld(stations=[COOKER_ST], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", -3, 0)], level="Sushi_1_3_2P")
    ctx, _ = make_ctx(w)
    t = T.PlaceToRelay(item="p1")
    assert t.feasible(ctx) is False


def test_place_to_relay_infeasible_without_relay_cells():
    # 非 1-3 关卡：无中转台
    w = FakeWorld(stations=[COOKER_ST], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", -3, 0)], level="Sushi_1_4_2P")
    ctx, _ = make_ctx(w)
    t = T.PlaceToRelay(item="p1")
    assert t.feasible(ctx) is False


def test_place_to_relay_infeasible_when_plate_dirty():
    w = FakeWorld(stations=[COOKER_ST], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", -3, 0, ["Seaweed"])],
                  level="Sushi_1_3_2P")
    ctx, _ = make_ctx(w)
    t = T.PlaceToRelay(item="p1")
    assert t.feasible(ctx) is False


def test_place_to_relay_done_false_after_relay_disabled():
    # 中转台已停用：盘即使在 (0,0) 也不再算"已放中转台"
    w = FakeWorld(recipes=RECIPES, plates=[plate("p1", 0, 0)],
                  level="Sushi_1_3_2P")
    ctx, _ = make_ctx(w)
    t = T.PlaceToRelay(item="p1")
    assert t.done(ctx) is False

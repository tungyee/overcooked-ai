# -*- coding: utf-8 -*-
"""终局不开无效工 + Prefetch 受 slack 约束（第 4 组 4.1/4.2）。

实机 trace-20261014-120406 的 t>4300（剩不到 15 秒）仍在跑 `Prefetch(Seaweed)`
并连续 failure，45 拍纯浪费；`Prefetch` 全局 287 拍里 45 拍 failure。时限内
变不成送达的事就不该开工。
"""
import pytest

from ai import planner, tasks as T
from ai.blackboard import Blackboard, StuckScratch
from ai.recipes import RecipeBook
from tests.test_eta import RECIPES, STATIONS, order, plate, world


def _bb(tick=0):
    bb = Blackboard()
    bb.scratch = StuckScratch()
    bb.tick = tick
    return bb


def _w(orders=None, plates=(), time_left=None):
    w = world(orders=orders, plates=plates)
    if time_left is not None:
        w.raw = dict(getattr(w, "raw", None) or {})
        w.raw["timer"] = {"server": {"left": time_left, "limit": 210},
                          "client": {"left": time_left, "limit": 210}}
    return w


# ---------- 剩余时间读取 ----------

def test_time_left_reads_timer():
    w = _w(time_left=12)
    assert planner._time_left_ticks(w) == 12 * 20


def test_time_left_none_without_timer():
    """没有计时器信息（单测/残帧）→ None，表示"不知道"，不要据此否决。"""
    assert planner._time_left_ticks(_w()) is None


def test_time_left_zero_is_not_a_deadline():
    """`left == 0` 是「未开局 / 已结束」，不是「剩 0 秒」。

    实机 trace-20261014-204822：开局 t=8~155 共 147 拍，插件报 left=0（关卡
    尚未开始计时），于是 20 个候选全被判 endgame，AI 站着不动 7 秒——正好够
    填一口灶（12 秒米）。终场 t=4091 之后同样恒为 0。
    `0` 是个合法数字，所以旧实现的 `left is not None` 放它过去了。
    """
    assert planner._time_left_ticks(_w(time_left=0)) is None


def test_time_left_negative_is_not_a_deadline():
    """负数同理（计时器溢出 / 残帧），按「不知道」处理。"""
    assert planner._time_left_ticks(_w(time_left=-3)) is None


def test_prep_allowed_before_the_clock_starts():
    """未开局那几拍必须照常备料，不能因 left=0 罢工。"""
    w = _w(orders=[order("1", "Sushi_Cucumber", 100.0)], plates=[plate("p1")],
           time_left=0)
    bb, book = _bb(), RecipeBook(w.recipes, w.stations)
    cand = planner.Candidate(T.FetchToPlate("1", ("p1",), "Seaweed"), [], 10)
    ok, veto = planner._feasibility_of(cand, planner._make_context(w, bb, book),
                                       bb, time_left=planner._time_left_ticks(w))
    assert veto != "endgame"


# ---------- 终局否决 ----------

def test_prep_vetoed_when_no_time_to_finish():
    """剩 8 秒还去取料：这一单无论如何变不成送达。"""
    w = _w(orders=[order("1", "Sushi_Cucumber", 100.0)], plates=[plate("p1")],
           time_left=8)
    bb, book = _bb(), RecipeBook(w.recipes, w.stations)
    cand = planner.Candidate(T.FetchToPlate("1", ("p1",), "Seaweed"), [], 10)
    ok, veto = planner._feasibility_of(cand, planner._make_context(w, bb, book),
                                       bb, time_left=planner._time_left_ticks(w))
    assert ok is False
    assert veto == "endgame"


def test_delivery_still_allowed_at_endgame():
    """送餐例外：料齐的盘在最后几秒仍要送出去，那是 60 分 base。"""
    p = plate("p1", ["ing:Seaweed",
                     "CookedCompositeAssembledNode(Pot){ ing:SushiRice }",
                     "ing:Cucumber"])
    w = _w(orders=[order("1", "Sushi_Cucumber", 100.0)], plates=[p], time_left=5)
    bb, book = _bb(), RecipeBook(w.recipes, w.stations)
    cand = planner.Candidate(T.DeliverPlate("1", ("p1",)), [], 5)
    ok, veto = planner._feasibility_of(cand, planner._make_context(w, bb, book),
                                       bb, time_left=planner._time_left_ticks(w))
    assert ok is True, veto


def test_prep_allowed_when_plenty_of_time():
    w = _w(orders=[order("1", "Sushi_Cucumber", 100.0)], plates=[plate("p1")],
           time_left=180)
    bb, book = _bb(), RecipeBook(w.recipes, w.stations)
    cand = planner.Candidate(T.FetchToPlate("1", ("p1",), "Seaweed"), [], 10)
    ok, veto = planner._feasibility_of(cand, planner._make_context(w, bb, book),
                                       bb, time_left=planner._time_left_ticks(w))
    assert veto != "endgame"


def test_endgame_veto_skipped_without_timer_info():
    """不知道剩多少时间就别否决——宁可白干，也不要在正常局面下罢工。"""
    w = _w(orders=[order("1", "Sushi_Cucumber", 100.0)], plates=[plate("p1")])
    bb, book = _bb(), RecipeBook(w.recipes, w.stations)
    cand = planner.Candidate(T.FetchToPlate("1", ("p1",), "Seaweed"), [], 10)
    ok, veto = planner._feasibility_of(cand, planner._make_context(w, bb, book),
                                       bb, time_left=planner._time_left_ticks(w))
    assert veto != "endgame"


def test_firefighting_is_never_endgame_vetoed():
    """灭火不走 plan() 的终局闸：火会一直烧，最后几秒也得灭。

    `plan_firefighting` 自己调 `_feasibility_of` 且不传 time_left，所以终局
    否决对它天然不生效。这条断言把这个性质钉住——真实帧 108 个着火采样点
    全部仍能发出 Extinguish。
    """
    from ai.world import World
    st = {
        "orders": [], "stations": list(STATIONS), "recipes": list(RECIPES),
        "chefs": [{"name": "Player 2", "x": 1.0, "z": 1.0, "held": None}],
        "plates": [], "workstations": [],
        "cookers": [{"n": "c1", "x": 5.0, "z": 5.0, "cooked": True,
                     "isCooking": False, "burning": True, "onFire": True,
                     "pot_contents": []}],
        "items": [], "extinguishers": [], "grid": {"nodeMap": None},
        "timer": {"server": {"left": 1, "limit": 210},
                  "client": {"left": 1, "limit": 210}},
    }
    w = World(st)
    bb, book = _bb(), RecipeBook(w.recipes, w.stations)
    task, record = planner.plan_firefighting(w, bb, book)
    vetoes = [v for _t, _s, _tm, ok, v, _c in record["candidates"] if not ok]
    assert "endgame" not in vetoes


def test_housekeeping_vetoed_at_endgame():
    """洗盘/填灶这类纯投资在终局也该停——它们的产出来不及兑现。"""
    w = _w(orders=[order("1", "Sushi_Cucumber", 100.0)], plates=[], time_left=6)
    bb, book = _bb(), RecipeBook(w.recipes, w.stations)
    for task in (T.WashPlate(resources=("sink",)),
                 T.FillCooker(None, ("c1", ("SushiRice",)), "BoiledSushiRice"),
                 T.Prefetch(item="Seaweed")):
        cand = planner.Candidate(task, [], 10)
        ok, veto = planner._feasibility_of(
            cand, planner._make_context(w, bb, book), bb,
            time_left=planner._time_left_ticks(w))
        assert veto == "endgame", (task.TYPE, veto)


# ---------- Prefetch 的 slack 约束（4.2）----------

def test_prefetch_infeasible_when_all_wanting_orders_are_doomed():
    """给必死单备料是纯损失。"""
    w = world(orders=[order("1", "Sushi_Cucumber", 2.0),
                      order("2", "Sushi_Cucumber", 3.0)], plates=[])
    bb, book = _bb(), RecipeBook(w.recipes, w.stations)
    # 判死有入口迟滞（连续 DOOM_CONFIRM_TICKS 拍才生效），喂满才进表
    for i in range(planner.DOOM_CONFIRM_TICKS):
        bb.tick = i
        planner.mark_doomed_orders(w, bb, book, plate_match={})
    bb.tick = 0
    ctx = planner._make_context(w, bb, book)
    assert T.Prefetch(item="Seaweed").feasible(ctx) is False


def test_prefetch_feasible_when_some_wanting_order_survives():
    w = world(orders=[order("1", "Sushi_Cucumber", 2.0),
                      order("2", "Sushi_Cucumber", 125.0),
                      order("3", "Sushi_Cucumber", 128.0)], plates=[])
    bb, book = _bb(), RecipeBook(w.recipes, w.stations)
    planner.mark_doomed_orders(w, bb, book, plate_match={})
    ctx = planner._make_context(w, bb, book)
    # 至少两单仍要海苔且未判死 → 仍值得提前取
    assert T.Prefetch(item="Seaweed").feasible(ctx) is True

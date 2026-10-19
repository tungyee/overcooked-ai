# -*- coding: utf-8 -*-
"""必死单的硬否决与迟滞（第 2 组 2.3）。

必死单不该靠「分数低一点」来躲——它应该根本不在候选里。本局 order#3 吞掉
1214 拍（全局 26% 注意力）仍然过期，而 #4/#5 全程饿死，就是因为没有这道闸。

两个工程约束一起钉住：
  1. `slack` 每拍重算会在 0 附近抖 → 判死要有 TTL 迟滞（沿用 abandoned 机制）；
  2. 放弃只否决**新投入**；手里已端着的东西仍要能收尾，否则卡手。
"""
import pytest

from ai.core import planner
from ai import tasks as T
from ai.state.blackboard import Blackboard, Commitment, StuckScratch
from ai.query.recipes import RecipeBook
from tests.test_eta import RECIPES, STATIONS, order, plate, world


def _ctx(w, bb, book):
    return planner._make_context(w, bb, book)


def _bb(tick=0):
    bb = Blackboard()
    bb.scratch = StuckScratch()
    bb.tick = tick
    return bb


def _mark_until_doomed(w, bb, book, **kw):
    """连喂 DOOM_CONFIRM_TICKS 拍，走完判死的入口迟滞。

    入口迟滞是为了让一次 slack 抖动不足以翻转候选集（见 planner 的常数
    注释）；关心「判死之后怎样」的用例用这个helper 跳过攒拍数的过程。
    """
    base = bb.tick
    for i in range(planner.DOOM_CONFIRM_TICKS):
        bb.tick = base + i
        planner.mark_doomed_orders(w, bb, book, **kw)
    bb.tick = base


def test_doomed_order_tasks_are_vetoed():
    """只剩 2 秒、还差三样料 → 这单的备料候选应被否决，不是打低分。"""
    w = world(orders=[order("1", "Sushi_Cucumber", 2.0)], plates=[])
    bb, book = _bb(), RecipeBook(w.recipes, w.stations)
    _mark_until_doomed(w, bb, book, plate_match={})
    cand = planner.Candidate(T.FetchToPlate("1", ("p1",), "Seaweed"), [], 10)
    ok, veto = planner._feasibility_of(cand, _ctx(w, bb, book), bb)
    assert ok is False
    assert veto == "doomed"


def test_survivable_order_is_not_vetoed():
    w = world(orders=[order("1", "Sushi_Cucumber", 130.0)], plates=[plate("p1")])
    bb, book = _bb(), RecipeBook(w.recipes, w.stations)
    planner.mark_doomed_orders(w, bb, book, plate_match={"p1": "1"})
    assert bb.doomed_orders == {}


def test_delivery_of_doomed_order_still_allowed():
    """盘已料齐只差送餐——即便 slack 判死也要送：沉没成本已付，送出去还有分。

    否决只针对**新投入**。连送餐都挡住会让料齐的盘烂在手里。
    """
    p = plate("p1", ["ing:Seaweed",
                     "CookedCompositeAssembledNode(Pot){ ing:SushiRice }",
                     "ing:Cucumber"])
    w = world(orders=[order("1", "Sushi_Cucumber", 2.0)], plates=[p])
    bb, book = _bb(), RecipeBook(w.recipes, w.stations)
    _mark_until_doomed(w, bb, book, plate_match={"p1": "1"})
    cand = planner.Candidate(T.DeliverPlate("1", ("p1",)), [], 5)
    ok, veto = planner._feasibility_of(cand, _ctx(w, bb, book), bb)
    assert ok is True, veto


def test_order_independent_tasks_are_never_vetoed():
    """填灶/洗盘/灭火不绑订单，不该被任何单的判死牵连。"""
    w = world(orders=[order("1", "Sushi_Cucumber", 2.0)], plates=[])
    bb, book = _bb(), RecipeBook(w.recipes, w.stations)
    _mark_until_doomed(w, bb, book, plate_match={})
    fill = planner.Candidate(
        T.FillCooker(None, ("c1", ("SushiRice",)), "BoiledSushiRice"), [], 15)
    ok, _ = planner._feasibility_of(fill, _ctx(w, bb, book), bb)
    # 可能因别的谓词不可行，但否决原因绝不能是 doomed
    _, veto = planner._feasibility_of(fill, _ctx(w, bb, book), bb)
    assert veto != "doomed"


def test_doom_has_hysteresis_ttl():
    """判死后要维持一段时间：slack 在 0 附近每拍抖动，否则候选会 flap。"""
    w = world(orders=[order("1", "Sushi_Cucumber", 2.0)], plates=[])
    bb, book = _bb(tick=100), RecipeBook(w.recipes, w.stations)
    _mark_until_doomed(w, bb, book, plate_match={})
    assert bb.doomed_orders["1"] > 100

    # 下一拍订单看起来又「有救」了（时间回弹 = 换了新单），判死仍在 TTL 内
    w2 = world(orders=[order("1", "Sushi_Cucumber", 130.0)], plates=[plate("p1")])
    bb.tick = 101
    planner.mark_doomed_orders(w2, bb, book, plate_match={"p1": "1"})
    cand = planner.Candidate(T.FetchToPlate("1", ("p1",), "Seaweed"), [], 10)
    ok, veto = planner._feasibility_of(cand, _ctx(w2, bb, book), bb)
    assert ok is False and veto == "doomed"


def test_doom_expires_after_ttl():
    w = world(orders=[order("1", "Sushi_Cucumber", 2.0)], plates=[])
    bb, book = _bb(tick=100), RecipeBook(w.recipes, w.stations)
    planner.mark_doomed_orders(w, bb, book, plate_match={})
    bb.tick = 100 + planner.DOOM_TTL_TICKS + 1
    w2 = world(orders=[order("1", "Sushi_Cucumber", 130.0)], plates=[plate("p1")])
    planner.mark_doomed_orders(w2, bb, book, plate_match={"p1": "1"})
    cand = planner.Candidate(T.FetchToPlate("1", ("p1",), "Seaweed"), [], 10)
    ok, _ = planner._feasibility_of(cand, _ctx(w2, bb, book), bb)
    assert ok is True


def test_doom_survives_mode_switches():
    """判死必须跨模式存活（放在 Blackboard 而不是 StuckScratch）。

    scratch 是「模式进入时构造、退出即丢弃」的局部状态，arbiter / agent /
    stuck 原语四处都会整体 new 一个。若判死存在 scratch 里，灭个火回来所有
    单又都"有救"了，这道闸等于没有。
    """
    from ai.state.blackboard import StuckScratch
    w = world(orders=[order("1", "Sushi_Cucumber", 2.0)], plates=[])
    bb, book = _bb(tick=50), RecipeBook(w.recipes, w.stations)
    _mark_until_doomed(w, bb, book, plate_match={})
    assert bb.doomed_orders

    bb.scratch = StuckScratch()      # 模拟进出灭火/脱困模式
    cand = planner.Candidate(T.FetchToPlate("1", ("p1",), "Seaweed"), [], 10)
    ok, veto = planner._feasibility_of(cand, _ctx(w, bb, book), bb)
    assert ok is False and veto == "doomed"


def test_no_investment_into_an_order_with_seconds_left():
    """真实帧 t=2755 的病灶：order#3 只剩 0.9 秒，旧版还在为它放黄瓜。

    旧 trace 那一拍选的是 `FetchToPlate(Cucumber, -12510, order#3)`，得分
    4.67（urgency 旧曲线在 remaining=0.9 处爆到 5.0 把人拽过去）——那一单
    必死，这 100 多拍全是白费，而 #4/#5 正在饿死。离线复验：加了判死闸之后
    同一帧改选 order#6 的 PlaceCleanPlate。
    """
    w = world(orders=[order("3", "Sushi_Cucumber", 0.9),
                      order("6", "Sushi_Cucumber", 60.0)],
              plates=[plate("p3", ["ing:Seaweed"])])
    bb, book = _bb(), RecipeBook(w.recipes, w.stations)
    _mark_until_doomed(w, bb, book, plate_match={"p3": "3"})
    assert "3" in bb.doomed_orders

    cand = planner.Candidate(T.FetchToPlate("3", ("p3",), "Cucumber"), [], 10)
    ok, veto = planner._feasibility_of(cand, _ctx(w, bb, book), bb)
    assert ok is False and veto == "doomed"


def test_doom_needs_consecutive_confirmations():
    """判死要连续 N 拍成立才生效：单拍即入 + 200 拍才出是不对称的。

    实机 trace-20261014-195652：order#3 的 doomed 在 t=9098 解禁（TTL 到期）、
    t=9120 被选中、t=9171 又判死 —— AI 在 50 拍的路上被抽掉目标，白走一个
    来回。入口加迟滞让一次 slack 抖动不足以翻转候选集。
    """
    w = world(orders=[order("1", "Sushi_Cucumber", 2.0)], plates=[])
    bb, book = _bb(tick=100), RecipeBook(w.recipes, w.stations)
    planner.mark_doomed_orders(w, bb, book, plate_match={})
    assert bb.doomed_orders == {}, "第一拍不该立刻判死"

    # 第一拍已经计 1，再补到 CONFIRM-1 拍仍不该判死
    for i in range(1, planner.DOOM_CONFIRM_TICKS - 1):
        bb.tick = 100 + i
        planner.mark_doomed_orders(w, bb, book, plate_match={})
    assert bb.doomed_orders == {}, "未达确认拍数前不判死"

    bb.tick = 100 + planner.DOOM_CONFIRM_TICKS
    planner.mark_doomed_orders(w, bb, book, plate_match={})
    assert "1" in bb.doomed_orders


def test_doom_confirmation_streak_resets_when_survivable():
    """中间只要有一拍「有救」，确认计数归零，不许攒够拍数。"""
    doomed_w = world(orders=[order("1", "Sushi_Cucumber", 2.0)], plates=[])
    alive_w = world(orders=[order("1", "Sushi_Cucumber", 130.0)],
                    plates=[plate("p1")])
    bb, book = _bb(tick=0), RecipeBook(doomed_w.recipes, doomed_w.stations)

    for i in range(planner.DOOM_CONFIRM_TICKS - 1):
        bb.tick = i
        planner.mark_doomed_orders(doomed_w, bb, book, plate_match={})
    bb.tick = planner.DOOM_CONFIRM_TICKS - 1
    planner.mark_doomed_orders(alive_w, bb, book, plate_match={"p1": "1"})

    bb.tick = planner.DOOM_CONFIRM_TICKS
    planner.mark_doomed_orders(doomed_w, bb, book, plate_match={})
    assert bb.doomed_orders == {}, "一拍有救应清零确认计数"


def test_in_flight_task_is_not_vetoed_by_doom():
    """已承诺、正在执行的那个 task 不被判死抽走（它的沉没成本已付）。

    planner 永不**选中**已判死单的 task，所以承诺只会建立在当时未判死的单
    上 —— 豁免只覆盖这一个 task 的时长，它做完后同单的下一个 task 照样被
    否决，不会退回「order#3 吞 1214 拍」。
    """
    w = world(orders=[order("1", "Sushi_Cucumber", 2.0)], plates=[])
    bb, book = _bb(tick=0), RecipeBook(w.recipes, w.stations)
    in_flight = T.FetchToPlate("1", ("p1",), "Seaweed")
    bb.commitment = Commitment(active_task=in_flight)
    for i in range(planner.DOOM_CONFIRM_TICKS + 1):
        bb.tick = i
        planner.mark_doomed_orders(w, bb, book, plate_match={})
    assert "1" in bb.doomed_orders

    # 可能因别的谓词不可行，但否决原因绝不能是 doomed
    _, veto = planner._feasibility_of(
        planner.Candidate(in_flight, [], 10), _ctx(w, bb, book), bb)
    assert veto != "doomed"

    # 同一单的**另一个** task 仍然被否决：豁免不扩散
    other = planner.Candidate(T.FetchToPlate("1", ("p1",), "Cucumber"), [], 10)
    ok2, veto2 = planner._feasibility_of(other, _ctx(w, bb, book), bb)
    assert ok2 is False and veto2 == "doomed"


def test_plan_does_not_crash_when_everything_doomed():
    """全员必死时 planner 不能停摆：仍要能选到订单无关的活。"""
    orders = [order(str(i), "Sushi_Cucumber", 1.0) for i in range(1, 6)]
    w = world(orders=orders, plates=[])
    bb, book = _bb(), RecipeBook(w.recipes, w.stations)
    task, record = planner.plan(w, bb, None, book)
    assert isinstance(record.get("candidates"), list)

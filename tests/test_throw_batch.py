"""批量投掷：落点几何 / 缺口计数 / ThrowBatch 树结构 / planner 选择。

真实帧：tests/fixtures/rapids/rapids21-20261028.ndjson.gz（Rapids_2_1，见
test_rapids_regions.py 的帧说明）。
"""
import gzip
import json
import math
import os

import pytest

from ai import primitives as PR
from ai import tasks as T
from ai.core import planner
from ai.exec.bt import Selector, Sequence, Status
from ai.query import predicates as P
from ai.query.recipes import RecipeBook
from ai.revenue import throw_supply as TS
from ai.space import throw as TH
from ai.state import world as W
from ai.state.bb_factory import make_bb

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                       "rapids21-20261028.ndjson.gz")
FRYER2 = ("GridManager", "FryingStation (2)")
# 「切好待传」帧（frames[3]）可见订单里的薯条份数。
CHIPS = 3


@pytest.fixture(scope="module")
def frames():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _ctx(frame, tick=100):
    w = W.World(frame)
    book = RecipeBook(w.recipes, w.stations)
    bb = make_bb(tick)
    return w, book, bb, planner._make_context(w, bb, book)


def _throw_potato_to_fryer():
    return T.ThrowBatch(None, (FRYER2, "cooker", "Potato", "FriedChips"),
                        "Chopped_Potato")


# ---------------------------------------------------------------- 几何


@pytest.mark.parametrize("start,land", [
    ((19.73, 2.6), (19.8, 10.2)),    # trace t=311 NuggetChicken
    ((20.68, 2.6), (20.7, 10.0)),    # t=415 Potato
    ((23.8, 2.44), (23.8, 9.7)),     # t=673 Potato
])
def test_landing_matches_measured_throws(start, land):
    """实测落点（落地后 2~3 拍、木筏带走前）距名义落点在射程容差内。"""
    pt = TH.landing(start[0], start[1], 0.0, 1.0)
    assert math.hypot(pt[0] - land[0], pt[1] - land[1]) <= TH.THROW_RANGE_TOL + 0.2


def test_effective_landing_retracts_when_target_is_short():
    """目标比射程近：名义落点越过目标，沿出手方向回收到第一个安全点。"""
    ok = lambda x, z: 4.0 <= z <= 6.0
    pt = TH.effective_landing((0.0, 0.0), (0.0, 5.0), ok)
    assert pt is not None and 4.0 <= pt[1] <= 6.0


def test_effective_landing_none_when_never_safe():
    assert TH.effective_landing((0.0, 0.0), (0.0, 7.5), lambda x, z: False) is None


def test_plan_launch_prefers_cheap_cell_within_range():
    target = (0.0, 7.5)
    ok = lambda x, z: abs(z - 7.5) < 1.5 and abs(x) < 1.5
    cells = [(0, 0), (0, -3), (5, 0)]
    cost = {(0, 0): 4, (0, -3): 1, (5, 0): 0}.get
    plan = TH.plan_launch(cells, cost, lambda gx, gz: (gx * 1.2, gz * 1.2), target, ok)
    # (0,-3) 离目标 11.1m 太远，(5,0) 斜投落点偏到 x≈4 不安全，只剩 (0,0)
    assert plan is not None and plan["cell"] == (0, 0)


# ---------------------------------------------------------------- 供需


def test_stages_for_chopped_then_fried(frames):
    """薯条 = 土豆 --砧板--> 切好的土豆 --炸锅--> 薯条，两站都可能要投。"""
    w, book, bb, ctx = _ctx(frames[0])
    assert TS.stages(book, "FriedChips") == [
        ("Potato", TS.CONSUMER_BOARD, "Potato"),
        ("Chopped_Potato", TS.CONSUMER_COOKER, "Potato"),
    ]


@pytest.fixture
def uncapped(monkeypatch):
    """缺口计数本身（throw_supply.gap）的口径测试：解除 MAX_BATCH_UNITS 封顶。"""
    from ai.tasks import relay
    monkeypatch.setattr(relay, "MAX_BATCH_UNITS", 99)


def test_gap_counts_orders_plus_extra_minus_placed(frames, uncapped):
    """切好待传帧：4 单里 3 份薯条，炸锅空、对面无料 → 缺 3 + 预备 1。

    板上那份 Chopped_Potato 还在 P2 这边，不算已就位。
    """
    w, book, bb, ctx = _ctx(frames[3])
    task = _throw_potato_to_fryer()
    assert task.batch_size(ctx) == CHIPS + TS.THROW_EXTRA_UNITS


def test_gap_not_capped_by_receiving_slots(frames, uncapped):
    """N 不按接料空位封顶：炸锅篮一次只装一份，缺口照样是全部需求。"""
    w, book, bb, ctx = _ctx(frames[3])
    assert _throw_potato_to_fryer().batch_size(ctx) > 2


def test_item_on_far_side_counts_as_placed(frames, uncapped):
    """投到厨房地上的料算已就位（落地也是进度）。"""
    frame = json.loads(json.dumps(frames[3]))
    frame["items"].append({"n": "Chopped_Potato", "x": 24.0, "z": 0.8,
                           "throwable": True, "on_station": None})
    w, book, bb, ctx = _ctx(frame)
    assert _throw_potato_to_fryer().batch_size(ctx) == CHIPS + TS.THROW_EXTRA_UNITS - 1


def test_flying_item_counts_as_placed(frames, uncapped):
    frame = json.loads(json.dumps(frames[3]))
    frame["items"].append({"n": "Chopped_Potato", "x": 24.0, "z": 5.0,
                           "throwable": True, "flying": True})
    w, book, bb, ctx = _ctx(frame)
    assert _throw_potato_to_fryer().batch_size(ctx) == CHIPS + TS.THROW_EXTRA_UNITS - 1


# ---------------------------------------------------------------- task


def test_throw_batch_feasible_when_chopped_on_raft(frames):
    w, book, bb, ctx = _ctx(frames[3])
    assert _throw_potato_to_fryer().feasible(ctx) is True


def test_throw_batch_infeasible_without_source(frames):
    """开局：木筏上没有切好的土豆 → 无料可投。"""
    w, book, bb, ctx = _ctx(frames[0])
    assert _throw_potato_to_fryer().feasible(ctx) is False


def test_batch_size_capped_to_one_unit(frames):
    """暂不批量：缺口再大，一次也只投一份（relay.MAX_BATCH_UNITS=1）。"""
    from ai.tasks import relay
    w, book, bb, ctx = _ctx(frames[3])
    assert relay.MAX_BATCH_UNITS == 1
    assert _throw_potato_to_fryer().batch_size(ctx) == 1


def test_throw_batch_tree_is_one_round_ending_with_throw():
    """单份：一棵树只投一轮（取 → 走到出手位 → 投），没有 RepeatUntil 循环。

    以 ThrowTo 收尾（没有尾部 drop）——与历史上坏掉的「投满 N 份才 drop」
    的结构差别，钉住它。
    """
    from ai.tasks import relay
    tree = _throw_potato_to_fryer().build()
    assert isinstance(tree, PR.Timeout)
    seq = tree.child
    assert isinstance(seq, Sequence)
    assert isinstance(seq.children[-1], PR.ThrowTo)
    assert isinstance(seq.children[-2], PR.GoToCell)
    assert isinstance(seq.children[1], Selector)
    assert _throw_potato_to_fryer().TIMEOUT_TICKS == relay.THROW_ROUND_TICKS


# ---------------------------------------------------------------- planner


def test_planner_throws_chopped_potato_to_kitchen(frames):
    """切好待传帧：原来全候选 unreachable、P2 干等；现在选批量投回厨房。"""
    w, book, bb, ctx = _ctx(frames[3])
    task, rec = planner.plan(w, bb, None, book)
    assert task is not None and task.TYPE == "ThrowBatch"
    assert task.item == "Chopped_Potato"
    terms = next(c[2] for c in rec["candidates"] if c[0] == task)
    # 单份：N=1，不吃批量加分
    assert terms["batch_bonus"] == 0.0


def test_batch_bonus_decays_per_extra_unit():
    """后面几份边际加分递减：服务的单更从容、锅位不够要等、批次常被打断。"""
    b = [planner._batch_bonus(n) for n in range(0, 9)]
    assert b[0] == b[1] == 0.0
    assert b[2] == pytest.approx(planner.W_BATCH)
    steps = [b[i + 1] - b[i] for i in range(1, 8)]
    assert all(s2 < s1 for s1, s2 in zip(steps, steps[1:]))
    cap = planner.W_BATCH / (1.0 - planner.BATCH_DECAY)
    assert b[-1] < cap and b[6] > 0.9 * cap


def test_score_throw_batch_inherits_proxy_terms(frames):
    w, book, bb, ctx = _ctx(frames[3])
    cand = planner.Candidate(_throw_potato_to_fryer(), [], planner.EST_FETCH)
    score, terms = planner._score_candidate(w, bb, cand, book)
    assert "urgency" in terms and "completion_ratio" in terms
    assert terms["batch_bonus"] == 0.0     # 单份：N=1 不吃批量加分


# ---------------------------------------------------------------- GoToCell


def _goto_ctx(frames, ex):
    w, book, bb, ctx = _ctx(frames[3])
    ctx["exec"] = ex
    return w, ctx


def test_goto_cell_issues_move_to_launch_cell(frames):
    from ai.exec.client import NullExec
    ex = NullExec()
    w, ctx = _goto_ctx(frames, ex)
    plan = TS.launch_plan(w, next(s for s in w.stations if s.get("n") == "FryingStation (2)"),
                          TS.MODE_SPLIT)
    prim = PR.GoToCell(lambda c: plan["cell"])
    assert prim.tick(ctx) == Status.RUNNING
    cmd = ex.issued[-1]
    assert cmd["cmd"] == "move" and cmd["target"] is None
    assert cmd["goal"] == list(w.to_world(*plan["cell"]))
    assert prim.tick(ctx) == Status.SUCCESS


def test_goto_cell_success_when_already_there(frames):
    from ai.exec.client import NullExec
    ex = NullExec()
    w, ctx = _goto_ctx(frames, ex)
    here = P.chef_cell(ctx["chef"], w.to_grid)
    assert PR.GoToCell(lambda c: here).tick(ctx) == Status.SUCCESS
    assert ex.issued == []


def test_goto_cell_fails_on_unreachable_cell(frames):
    from ai.exec.client import NullExec
    w, ctx = _goto_ctx(frames, NullExec())
    kitchen = w.to_grid(20.0, 0.0)   # 对面厨房：木筏上走不过去
    assert PR.GoToCell(lambda c: kitchen).tick(ctx) == Status.FAILURE


# ---------------------------------------------------------------- 实机回归（trace-20261029-122831）

FIXTURE_0129 = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                            "rapids21-20261029.ndjson.gz")


@pytest.fixture(scope="module")
def frames0129():
    with gzip.open(FIXTURE_0129, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _fryer2(w):
    return next(s for s in w.stations if s.get("n") == "FryingStation (2)")


@pytest.mark.parametrize("idx", [0, 1])
def test_throw_ok_from_current_position_on_drifting_raft(frames0129, idx):
    """t=351 刚拿起切好的土豆 / t=450 已经干耗 5 秒：当下位置都投得过去。

    旧实现只认「走到出手格」，木筏把格心带走，move 追不上，拿着料耗到
    t=798 才投出第一份土豆。"""
    w = W.World(frames0129[idx])
    assert P.chef_by_name(w.chefs, P.P2_NAME)["held"] == "Chopped_Potato"
    assert TS.throw_ok_here(w, _fryer2(w), TS.MODE_SPLIT) is True


def test_goto_cell_stops_when_arrived_predicate_holds(frames0129):
    from ai.exec.client import NullExec
    ex = NullExec()
    w, book, bb, ctx = _ctx(frames0129[1])
    ctx["exec"] = ex
    prim = PR.GoToCell(lambda c: (9, 9),
                       arrived=lambda c: TS.throw_ok_here(c["w"], _fryer2(c["w"]),
                                                          TS.MODE_SPLIT))
    assert prim.tick(ctx) == Status.SUCCESS
    assert ex.issued == []


def test_goto_cell_retries_after_failed_move(frames):
    """一段 move 失败（木筏带走格心 → 超时）不直接判败，按新帧重算再发。"""
    class FailOnce(object):
        def __init__(self):
            self.issued, self._n = [], 0

        def issue(self, cmd):
            self._n += 1
            self.issued.append(dict(cmd, cmd_id=self._n))
            return self._n

        def poll(self):
            n = self.issued[-1]["cmd_id"]
            return {"cmd_id": n, "ok": n > 1, "why": None if n > 1 else "timeout",
                    "held_after": None, "blocked_cell": None}

        def halt(self):
            pass

    ex = FailOnce()
    w, book, bb, ctx = _ctx(frames[3])
    ctx["exec"] = ex
    plan = TS.launch_plan(w, _fryer2(w), TS.MODE_SPLIT)
    prim = PR.GoToCell(lambda c: plan["cell"])
    assert prim.tick(ctx) == Status.RUNNING       # 发第 1 段
    assert prim.tick(ctx) == Status.RUNNING       # 第 1 段失败 → 不判败
    assert prim.tick(ctx) == Status.RUNNING       # 重算后发第 2 段
    assert prim.tick(ctx) == Status.SUCCESS
    assert len(ex.issued) == 2


@pytest.mark.parametrize("idx", [2, 3])
def test_same_name_counter_resolves_to_raft_not_kitchen(frames0129, idx):
    """厨房和木筏各有一张 countertop_01_standard_old (4)：鸡块在木筏那张上。

    旧实现取第一个同名站点 = 厨房 (16.8,2.4)，P2 走不过去，FetchToBoard
    连败被拉黑，t=1273~1469 / t=1794~1990 两段各干等 10 秒。插件给的
    item.grid 也是按名字反查的，同样报错（GridManager），不能用它判。"""
    from ai.tasks.base import _station_for_item
    w = W.World(frames0129[idx])
    chicken = next(i for i in w.items if i["n"] == "NuggetChicken"
                   and "standard_old (4)" in str(i.get("on_station")))
    st = _station_for_item(w, chicken)
    assert st["grid"] == "Raft_Top"
    assert math.hypot(st["x"] - chicken["x"], st["z"] - chicken["z"]) < 0.5


# ---------------------------------------------------------------- 份数不够时补发切菜链（trace-20261029-125348）

FIXTURE_0129B = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                             "rapids21-20261029b.ndjson.gz")


@pytest.fixture(scope="module")
def frame_second_chips():
    with gzip.open(FIXTURE_0129B, "rt", encoding="utf8") as fh:
        return json.loads(fh.readline())


def test_second_chips_order_still_gets_chop_chain(frame_second_chips):
    """两单要薯条，场上只有一份切好的土豆、P2 手里接住了第二个土豆。

    旧口径「有一份切好的就算全切完」：第二单不发 FetchToBoard，P2 拿着土豆
    干站 7.5 秒直到暂停。现在第二单要补发取料上板。"""
    from ai.query import recipe_graph
    w, book, bb, ctx = _ctx(frame_second_chips)
    assert P.chef_by_name(w.chefs, P.P2_NAME)["held"] == "Potato"
    pm = planner._match_plates_to_orders(w, bb, book)
    cm = planner._match_cookers_to_orders(w, bb, book, pm)
    assert recipe_graph.chopped_count(w, book, "Potato") == 1
    assert len(recipe_graph.chop_waiters(w, book, "Potato", pm, cm)) == 2
    third = next(o for o in w.orders if o["id"] == "3")
    emitted = recipe_graph.emit(w, bb, book, third, pm, cm)
    assert any(t.TYPE == "FetchToBoard" and t.item == "Potato" for t in emitted)
    task, _ = planner.plan(w, bb, None, book)
    assert task.TYPE == "FetchToBoard" and task.item == "Potato"


def test_first_chips_order_still_uses_existing_chopped(frame_second_chips):
    """补发切菜链不能挡住排在前面那单用现成的切好料下锅。"""
    from ai.query import recipe_graph
    w, book, bb, ctx = _ctx(frame_second_chips)
    pm = planner._match_plates_to_orders(w, bb, book)
    cm = planner._match_cookers_to_orders(w, bb, book, pm)
    second = next(o for o in w.orders if o["id"] == "2")
    types = {t.TYPE for t in recipe_graph.emit(w, bb, book, second, pm, cm)}
    assert "FetchToCooker" in types
    assert "FetchToBoard" not in types


def test_stash_held_agrees_potato_is_not_redundant(frame_second_chips):
    """StashHeld 与 recipe_graph 同口径：还有单在等，手里的土豆不算多余。"""
    from ai.tasks.cleanup import _chopped_form_available
    w, book, bb, ctx = _ctx(frame_second_chips)
    pm = planner._match_plates_to_orders(w, bb, book)
    cm = planner._match_cookers_to_orders(w, bb, book, pm)
    assert _chopped_form_available(w, book, "Potato", pm, cm) is False


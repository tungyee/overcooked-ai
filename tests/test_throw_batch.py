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
    # 走到出手位 → 等新帧 → 按真实位置定瞄准（定不下不投）→ 投
    assert isinstance(seq.children[-4], PR.GoToCell)
    assert isinstance(seq.children[-3], PR.WaitNewFrame)
    assert isinstance(seq.children[-2], PR.WaitUntil)
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



# ---------------------------------------------------------------- 撞墙（trace-20261031-123418，Balloon_2_2）

FIXTURE_WALL = os.path.join(os.path.dirname(__file__), "fixtures", "balloon",
                            "balloon22-20261031-123418-throw-wall.ndjson.gz")
# 右篮左墙：门洞在 z 8.03~12.44，往上这段墙挡住了投向炸锅的料。
RIGHT_BASKET_WALL = (13.21, 13.81, 12.44, 15.03)


@pytest.fixture(scope="module")
def frames_wall():
    with gzip.open(FIXTURE_WALL, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_wall_hit_segment_vs_box():
    walls = [RIGHT_BASKET_WALL]
    d = TH.wall_hit((7.2, 12.0), 1.0, 0.0, 7.5, [(13.21, 13.81, 11.0, 13.0)])
    assert d == pytest.approx(6.01)
    # 从门洞下方平飞过去：不撞
    assert TH.wall_hit((7.2, 10.0), 1.0, 0.0, 7.5, walls) is None
    # 出手点在墙盒里：跳过这面墙
    assert TH.wall_hit((13.5, 13.0), 1.0, 0.0, 7.5, walls) is None


def test_effective_landing_stops_before_wall():
    """名义落点在墙后、目标区域也在墙后：撞墙掉在墙前，判不安全。"""
    ok = lambda x, z: x > 13.81
    assert TH.effective_landing((7.2, 12.0), (16.8, 15.6), ok) is not None
    assert TH.effective_landing((7.2, 12.0), (16.8, 15.6), ok,
                                walls=[RIGHT_BASKET_WALL]) is None


def test_throw_walls_keeps_thin_walls_with_height():
    thick = (13.18, 23.9, 0.03, 6.03)
    sunk = (13.21, 23.92, 0.03, 6.03)
    terrain_y = {10: [(RIGHT_BASKET_WALL, 0.0, 1.32), (thick, 0.0, 3.0), (sunk, -2.6, -0.5)],
                 26: [((13.23, 13.63, 8.03, 12.44), 0.49, 1.33)]}
    # TableBlock(26) 门洞盒不挡料（边框中间是空的）
    assert TH.throw_walls(terrain_y) == [RIGHT_BASKET_WALL + (0.0, 1.32)]
    # 高度相对投掷者脚下
    lifted = TH.throw_walls({10: [(RIGHT_BASKET_WALL, 0.0, 1.32)]}, floor_y=0.5)
    assert lifted == [pytest.approx(RIGHT_BASKET_WALL + (-0.5, 0.82))]
    # 旧插件帧没有高度：不拦
    assert TH.throw_walls({}) == []


def test_wall_hit_uses_arc_height():
    """弧线：平飞 ≈1m 到 5.4m 再落地（trace-20261031-150043 items[].y 标定）。
    1.32m 的边墙任何距离都挡；矮墙只在末段料落低了才挡。"""
    assert TH.arc_height(3.8) == pytest.approx(1.0)
    assert 0.6 < TH.arc_height(6.5) < 0.8
    tall_near = (7.86, 8.26, 8.0, 14.0, 0.0, 1.32)
    tall_far = (13.21, 13.81, 8.0, 14.0, 0.0, 1.32)
    low_near = (9.0, 9.4, 8.0, 14.0, 0.0, 0.6)
    low_far = (14.2, 14.6, 8.0, 14.0, 0.0, 0.6)
    assert TH.wall_hit((7.4, 11.6), 1.0, 0.0, 7.9, [tall_near]) == pytest.approx(0.46)
    assert TH.wall_hit((7.4, 11.6), 1.0, 0.0, 7.9, [tall_far]) == pytest.approx(5.81)
    assert TH.wall_hit((7.4, 11.6), 1.0, 0.0, 7.9, [low_near]) is None
    assert TH.wall_hit((7.4, 11.6), 1.0, 0.0, 7.9, [low_far]) == pytest.approx(6.8)
    # 悬空栏杆（下沿高于弧线）：从下面穿过
    assert TH.wall_hit((7.4, 11.6), 1.0, 0.0, 7.9, [tall_far[:4] + (1.33, 2.49)]) is None


def test_parse_terrain_accepts_boxes_with_height():
    from ai.space import occupancy as O
    raw = [{"layer": 10, "boxes": [[1, 2, 3, 4], [5, 6, 7, 8, -0.5, 2.5], [1, 2, 3]]}]
    assert O.parse_terrain(raw) == {10: [(1.0, 2.0, 3.0, 4.0), (5.0, 6.0, 7.0, 8.0)]}
    assert O.parse_terrain_y(raw) == {10: [((5.0, 6.0, 7.0, 8.0), -0.5, 2.5)]}


def _with_wall_height(frame, maxy):
    """给帧里 Walls 层每个盒子补上高度（模拟插件 0.23.5 帧）。"""
    f = json.loads(json.dumps(frame))
    for entry in f["terrain"]:
        if entry["layer"] == 10:
            entry["boxes"] = [list(b[:4]) + [0.0, maxy] for b in entry["boxes"]]
    return f


def _pan3(w):
    return next(s for s in w.stations if s.get("t") == "cooking"
                and s.get("n") == "workstation_cooker_01 (3)")


@pytest.mark.parametrize("idx", range(5))
def test_no_straight_throw_into_basket_wall(frames_wall, idx):
    """P2 在左地板拿着切好的蘑菇：旧实现选 (7.2,12) 正对炸锅出手，直线穿右篮
    左墙，两份蘑菇都撞墙掉进缝里（t=4571/4671）。墙高 1.32 时正对投不过去，
    方案（若有）必须偏开瞄准点。"""
    w = W.World(_with_wall_height(frames_wall[idx], 1.32))
    assert P.chef_by_name(w.chefs, P.P2_NAME)["held"] == "ChoppedMushroom"
    pan = _pan3(w)
    plan = TS.launch_plan(w, pan, TS.MODE_SPLIT)
    if plan is not None:
        assert plan["aim"] != pytest.approx((pan["x"], pan["z"]))
    walls = TS.walls_of(w)
    assert TH.effective_landing((7.2, 12.0), (pan["x"], pan["z"]),
                                lambda x, z: x > 13.81, walls=walls) is None


def test_low_walls_or_old_frames_keep_old_plan(frames_wall):
    """墙矮到弧线飞得过、或旧帧没有墙高：出手方案与旧实现一致。"""
    for f in (frames_wall[2], _with_wall_height(frames_wall[2], 0.1)):
        w = W.World(f)
        assert TS.launch_plan(w, _pan3(w), TS.MODE_SPLIT) is not None


# trace-20261031-144101（插件 0.23.5，真实墙高）：P2 三次把切好的肉投向
# 右篮炸锅，t=157/350 撞右篮左墙（顶 1.32）、t=251 撞门洞栏杆，都掉进缝里。
FIXTURE_RIM = os.path.join(os.path.dirname(__file__), "fixtures", "balloon",
                           "balloon22-20261031-144101-throw-rim.ndjson.gz")


@pytest.fixture(scope="module")
def frames_rim():
    with gzip.open(FIXTURE_RIM, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_rim_fixture_has_wall_heights(frames_rim):
    w = W.World(frames_rim[0])
    assert (RIGHT_BASKET_WALL, 0.0, 1.32) in w.terrain_y[10]
    assert P.chef_by_name(w.chefs, P.P2_NAME).get("y") == 0.0


# 两道门洞（左地板右边框 x≈8、右篮左边框 x≈13.2）都在 z 8.03~12.44。
GAP_Z = (8.03, 12.44)


@pytest.mark.parametrize("idx", range(9))
def test_throw_through_basket_gap(frames_rim, idx):
    """正对炸锅撞框；改成斜着从两道门洞中间穿过去，落在右篮地板上。"""
    w = W.World(frames_rim[idx])
    assert P.chef_by_name(w.chefs, P.P2_NAME)["held"] == "DicedMeat"
    pan = _pan3(w)
    plan = TS.launch_plan(w, pan, TS.MODE_SPLIT)
    assert plan is not None
    sx, sz = plan["world"]
    ax, az = plan["aim"]
    # 瞄准线穿过右篮左边框（x=13.21）时在门洞里，离门框留出 5° 横偏
    f = (13.21 - sx) / (ax - sx)
    z_at = sz + (az - sz) * f
    lateral = (13.21 - sx) * math.tan(math.radians(TH.AIM_TOL_DEG))
    assert GAP_Z[0] + lateral < z_at < GAP_Z[1] - lateral
    lx, lz = plan["landing"]
    assert lx > 13.81 and plan["land_dist"] <= TS.SPLIT_MAX_LAND
    # 正对炸锅投不过去
    assert TH.effective_landing((sx, sz), (pan["x"], pan["z"]),
                                lambda x, z: x > 13.81, walls=TS.walls_of(w)) is None


def test_gap_throw_command_is_precise(frames_rim):
    """t=150 站在能斜穿的位置：ThrowBatch 发的 throw 瞄门洞方向、精准对准。"""
    from ai.exec.client import NullExec
    w = W.World(frames_rim[1])
    pan = _pan3(w)
    got = TS.aim_here(w, pan, TS.MODE_SPLIT)
    assert got is not None and got["aim"] != pytest.approx((pan["x"], pan["z"]))
    aim = got["aim_far"]

    class Ex(NullExec):
        def __init__(self):
            NullExec.__init__(self)
            self.issued = []

        def issue(self, cmd):
            self.issued.append(cmd)
            return NullExec.issue(self, cmd)

    ex = Ex()
    tk = T.ThrowBatch(None, (("GridManager", "workstation_cooker_01 (3)"), "cooker",
                             "BurritoMeat", "FriedBurritoMeat"), "DicedMeat")
    book = RecipeBook(w.recipes, w.stations)
    bb = make_bb(100)
    bb.commitment.active_task = tk
    ctx = planner._make_context(w, bb, book)
    ctx["exec"] = ex
    tree = tk.build()
    for _ in range(5):
        tree.tick(ctx)
        if any(c.get("cmd") == "throw" for c in ex.issued):
            break
    cmd = next(c for c in ex.issued if c.get("cmd") == "throw")
    assert cmd["target_world"] == [pytest.approx(aim[0]), pytest.approx(aim[1])]
    assert cmd["aim_dot"] == TH.WALL_AIM_DOT and cmd["strict"] is True


# ---------------------------------------------------------------- 旧快照瞄准（trace-20261031-150043）

# t=149/153 P2 在走向出手格途中，t=157 到位（插件已到、Python 快照滞后一帧）；
# t=257 途中、t=261 到位。旧实现 move 一到位就按旧位置算瞄准，算不出就退回
# 瞄炸锅本身，两次都撞右篮左框掉进缝里（t=160/265，朝向 61°/58°）。
FIXTURE_STALE = os.path.join(os.path.dirname(__file__), "fixtures", "balloon",
                             "balloon22-20261031-150043-stale-aim.ndjson.gz")


@pytest.fixture(scope="module")
def frames_stale():
    with gzip.open(FIXTURE_STALE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class _IssueLog(object):
    """记录发出的命令；move 立即回 ok（模拟插件到位即回）。"""

    def __init__(self):
        from ai.exec.client import NullExec
        self._inner = NullExec()
        self.issued = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def issue(self, cmd):
        self.issued.append(cmd)
        return self._inner.issue(cmd)


def _run_mushroom_round(frames, ticks_per_frame=4):
    tk = T.ThrowBatch(None, (("GridManager", "workstation_cooker_01 (3)"), "cooker",
                             "Mushroom", "FriedMushrooms"), "ChoppedMushroom")
    tree, ex, bb = tk.build(), _IssueLog(), make_bb(100)
    bb.commitment.active_task = tk
    tick, thrown = 100, None
    for seq, f in enumerate(frames):
        w = W.World(f)
        book = RecipeBook(w.recipes, w.stations)
        for _ in range(ticks_per_frame):
            ctx = planner._make_context(w, bb, book)
            ctx.update(exec=ex, frame_seq=seq, tick=tick, now=tick * 0.05)
            tick += 1
            tree.tick(ctx)
            if thrown is None and "throw" in ctx:
                thrown = ctx["throw"]
    return ex, thrown


def _gap_z_at(rec, x):
    (fx, fz), (tx, tz) = rec["from_world"], rec["target_world"]
    return fz + (tz - fz) * (x - fx) / (tx - fx)


@pytest.mark.parametrize("frames_idx", [(0, 1, 2), (3, 4)])
def test_throw_aims_through_both_gaps_from_fresh_position(frames_stale, frames_idx):
    ex, rec = _run_mushroom_round([frames_stale[i] for i in frames_idx])
    assert rec is not None, [c["cmd"] for c in ex.issued]
    assert rec["aim_dot"] == TH.WALL_AIM_DOT and rec["strict"] is True
    lat = 6.0 * math.tan(math.radians(TH.AIM_TOL_DEG))
    for x in (8.1, 13.21):     # 左地板右边框 / 右篮左边框
        assert GAP_Z[0] + lat < _gap_z_at(rec, x) < GAP_Z[1] - lat
    # 瞄的是远点（抗站位偏差），不是炸锅
    assert math.hypot(rec["target_world"][0] - rec["from_world"][0],
                      rec["target_world"][1] - rec["from_world"][1]) > 20


def test_no_fallback_to_cooker_when_walls(frames_stale, monkeypatch):
    """按真实位置算不出能投通的方向：不投（以前退回瞄炸锅本身，正好撞框）。"""
    monkeypatch.setattr(TS, "aim_still_ok", lambda *a, **k: None)
    monkeypatch.setattr(TS, "aim_here", lambda *a, **k: None)
    ex, rec = _run_mushroom_round([frames_stale[0], frames_stale[1], frames_stale[2]] * 3)
    assert rec is None
    assert not any(c["cmd"] == "throw" for c in ex.issued)


def test_throw_to_without_target_does_not_throw():
    from ai.primitives.throw import ThrowTo
    ex = _IssueLog()
    chef = {"name": P.P2_NAME, "x": 0.0, "z": 0.0, "held": "Pasta"}
    assert ThrowTo(lambda c: None).tick({"chef": chef, "exec": ex, "tick": 0}) == Status.FAILURE
    assert ex.issued == []


def test_wait_new_frame():
    node = PR.WaitNewFrame(3)
    assert node.tick({"frame_seq": 5}) == Status.RUNNING
    assert node.tick({"frame_seq": 5}) == Status.RUNNING
    assert node.tick({"frame_seq": 6}) == Status.SUCCESS
    assert PR.WaitNewFrame(3).tick({}) == Status.SUCCESS      # 离线：不等
    node = PR.WaitNewFrame(2)
    got = [node.tick({"frame_seq": 1}) for _ in range(3)]
    assert got == [Status.RUNNING, Status.RUNNING, Status.FAILURE]


def test_split_relaxed_land_only_when_needed(frames0129):
    """Rapids 木筏→炸锅仍按 4.5m 那档出方案（老场景不变）。"""
    w = W.World(frames0129[0])
    plan = TS.launch_plan(w, _fryer2(w), TS.MODE_SPLIT)
    assert plan is not None and plan["max_land"] == TS.SPLIT_MAX_LAND


# ---------------------------------------------------------------- 意面投煮锅（trace-20261031-152300）

# t=177 / t=407：P2 空手在左地板。两口煮锅在右篮深处，最近落点离锅 7.15 /
# 8.74m，旧的 4.5/6m 两档都判投不了，ThrowBatch(Pasta) 全局 infeasible。
FIXTURE_PASTA = os.path.join(os.path.dirname(__file__), "fixtures", "balloon",
                             "balloon22-20261031-152300-pasta.ndjson.gz")
PASTA_POTS = ("workstation_cooker_01 (2)", "workstation_cooker_01 (1)")


@pytest.fixture(scope="module")
def frames_pasta():
    with gzip.open(FIXTURE_PASTA, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _pasta_throw(pot):
    return T.ThrowBatch(None, (("GridManager", pot), "cooker", "Pasta", "BoiledPasta"),
                        "Pasta")


@pytest.mark.parametrize("idx", [0, 1])
@pytest.mark.parametrize("pot", PASTA_POTS)
def test_pasta_throw_uses_far_split_tier(frames_pasta, idx, pot):
    w, book, bb, ctx = _ctx(frames_pasta[idx])
    assert _pasta_throw(pot).feasible(ctx)
    dest = next(s for s in w.stations if s.get("t") == "cooking" and s.get("n") == pot)
    plan = TS.launch_plan(w, dest, TS.MODE_SPLIT)
    assert plan["max_land"] == TS.SPLIT_MAX_LAND_FAR
    assert TS.SPLIT_MAX_LAND_RELAXED < plan["land_dist"] <= TS.SPLIT_MAX_LAND_FAR
    assert plan["landing"][0] > 13.81           # 落在右篮地板上


def test_planner_picks_pasta_throw(frames_pasta):
    w, book, bb, _ctx0 = _ctx(frames_pasta[0])
    task, _rec = planner.plan(w, bb, None, book)
    assert task.TYPE == "ThrowBatch" and task.item == "Pasta"


def test_landed_pasta_counts_as_delivered(frames_pasta):
    """落在右篮入口的意面（P2 走不到、P1 走得到）算已送到，不重复投。"""
    f = json.loads(json.dumps(frames_pasta[0]))
    w = W.World(f)
    book = RecipeBook(w.recipes, w.stations)
    pot = next(s for s in w.stations if s.get("t") == "cooking" and s.get("n") == PASTA_POTS[0])
    before = TS.gap(w, book, "BoiledPasta", "Pasta", "cooker", "Pasta", pot, TS.MODE_SPLIT)
    f["items"] = list(f["items"]) + [dict(n="Pasta", x=14.39, z=11.72, flying=False,
                                          throwable=True, on_station=None, held_by=None,
                                          grid=None, detail=[])]
    w = W.World(f)
    after = TS.gap(w, book, "BoiledPasta", "Pasta", "cooker", "Pasta", pot, TS.MODE_SPLIT)
    assert after == before - 1


# ---------------------------------------------------------------- 出手站位离格心太远（trace-20261031-201659）

FIXTURE_OFFCENTER = os.path.join(os.path.dirname(__file__), "fixtures", "balloon",
                                 "balloon22-20261031-201659-throw-offcenter.ndjson.gz")
SHROOM_BOARD = ("GridManager", "countertop_01_chopping_dark_wood (1)")


@pytest.fixture(scope="module")
def frame_offcenter():
    with gzip.open(FIXTURE_OFFCENTER, "rt", encoding="utf8") as fh:
        return json.loads(fh.readline())


def _throw_shroom():
    return T.ThrowBatch(None, (SHROOM_BOARD, "board", "Mushroom", "FriedMushrooms"),
                        "Mushroom")


def _offcenter_ctx(frame, x=None):
    f = json.loads(json.dumps(frame))
    if x is not None:
        next(c for c in f["chefs"] if c["name"] == P.P2_NAME)["x"] = x
    return _ctx(f)


def test_offcenter_stance_cannot_throw_but_center_can(frame_offcenter):
    """t=39.2：P2 拿着蘑菇站出手格 (-5,0)、x=6.72（格心 7.2）——投不过门洞框；
    规划按格心算可行，于是 planner 一直选它、P2 原地不动 56 秒。挪到 x≥6.8 就能投。"""
    w, book, bb, ctx = _offcenter_ctx(frame_offcenter)
    st = _throw_shroom()._state(ctx)
    assert TS.launch_plan(w, st[0], st[1], bb)["cell"] == P.chef_cell(ctx["chef"], w.to_grid)
    assert TS.aim_here(w, st[0], st[1], bb) is None
    w2, _b, bb2, ctx2 = _offcenter_ctx(frame_offcenter, x=7.0)
    assert TS.aim_here(w2, st[0], st[1], bb2) is not None


def test_precise_goto_nudges_toward_cell_center_once(frame_offcenter):
    """同格但离格心 0.48m、此刻投不出去：再发一次 move，终点越过格心；挪完就 SUCCESS。"""
    from ai.exec.client import NullExec
    from ai.primitives import move as M
    w, book, bb, ctx = _offcenter_ctx(frame_offcenter)
    ex = NullExec()
    ctx["exec"] = ex
    st = _throw_shroom()._state(ctx)
    cell = TS.launch_plan(w, st[0], st[1], bb)["cell"]
    prim = PR.GoToCell(lambda c: cell, arrived=lambda c: False, precise=True)
    assert prim.tick(ctx) == Status.RUNNING
    cmd = ex.issued[-1]
    cx, cz = w.to_world(*cell)
    assert cmd["cmd"] == "move" and cmd["target"] is None
    assert cmd["goal"][0] == pytest.approx(cx + M.GOTO_NUDGE_OVERSHOOT)
    assert cmd["goal"][1] == pytest.approx(cz)
    assert prim.tick(ctx) == Status.SUCCESS      # 只挪一次，结果交给后续节点
    assert len(ex.issued) == 1


def test_precise_goto_no_nudge_near_center_or_when_throwable(frame_offcenter):
    from ai.exec.client import NullExec
    w, book, bb, ctx = _offcenter_ctx(frame_offcenter, x=7.1)
    ex = NullExec()
    ctx["exec"] = ex
    cell = P.chef_cell(ctx["chef"], w.to_grid)
    assert PR.GoToCell(lambda c: cell, arrived=lambda c: False,
                       precise=True).tick(ctx) == Status.SUCCESS
    w, book, bb, ctx = _offcenter_ctx(frame_offcenter)
    ctx["exec"] = ex
    assert PR.GoToCell(lambda c: cell, arrived=lambda c: True,
                       precise=True).tick(ctx) == Status.SUCCESS
    # 默认（非精确）行为不变：同格即到
    assert PR.GoToCell(lambda c: cell).tick(ctx) == Status.SUCCESS
    assert ex.issued == []


def test_throw_trees_use_precise_goto():
    from ai.primitives.throw import ThrowOntoStation
    seq = _throw_shroom().build().child
    goto = next(ch for ch in seq.children if isinstance(ch, PR.GoToCell))
    assert goto.precise is True
    pot = ThrowOntoStation(lambda c: None, lambda c: True, lambda c: False, "k")._build_child()
    assert next(ch for ch in pot.children if isinstance(ch, PR.GoToCell)).precise is True


# ---------------------------------------------------------------- 高度差（Wizard_5_5）

# trace-20261101-142950（Wizard_5_5，插件 0.24.1）：P2 在一楼楼梯口 (14,-6) y=0
# 往二楼（地板顶 y=2.0）左侧砧板投肉，料撞楼梯台阶弹回一楼，连投 5 次。
# 帧取 P2 拿着肉准备出手的 7 拍（t=240~338）。
FIXTURE_UPSTAIRS = os.path.join(os.path.dirname(__file__), "fixtures", "wizard",
                                "wizard55-20261101-142950-throw-upstairs.ndjson.gz")
# 楼梯台阶（SlopedGround 28）顶面：x 越小越高
STAIRS = [((13.49, 13.82, -6.73, -4.21), 0.01, 0.39), ((12.69, 13.02, -6.73, -4.21), 0.51, 0.89),
          ((11.89, 12.22, -6.73, -4.21), 1.01, 1.39), ((11.09, 11.42, -6.73, -4.21), 1.51, 1.89)]
UPPER = ((5.4, 10.2, -9.6, -1.2), 1.5, 2.0)
LOWER = ((13.8, 23.4, -10.2, -0.6), -1.0, 0.0)


@pytest.fixture(scope="module")
def frames_upstairs():
    with gzip.open(FIXTURE_UPSTAIRS, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_raised_ground_blocks_throw_into_stairs():
    """一楼 (14.04,-5.96) 朝二楼投：弧线 1.0m 平飞，撞上顶 1.39 的台阶立面。"""
    terrain_y = {9: [UPPER, LOWER], 28: STAIRS}
    obs = TH.raised_ground(terrain_y, floor_y=0.0)
    assert all(b[4] == float("-inf") for b in obs)
    assert not any(b[:4] == LOWER[0] for b in obs)       # 脚下这层不算
    d = TH.wall_hit((14.04, -5.96), -1.0, 0.0, 7.9, obs)
    assert d == pytest.approx(14.04 - 12.22)
    # 站在楼梯顶 (10.8,-6) y=1.88：二楼地板只高 0.12，不当挡板
    top = TH.raised_ground(terrain_y, floor_y=1.88)
    assert TH.wall_hit((10.8, -6.0), -1.0, 0.0, 7.9, top) is None


def test_floor_reachable_by_height():
    terrain_y = {9: [UPPER, LOWER], 28: STAIRS}
    assert TH.ground_top(terrain_y, 6.0, -3.6) == 2.0
    assert TH.ground_top(terrain_y, 30.0, -3.6) is None
    assert not TH.floor_reachable(terrain_y, 0.0)(6.0, -3.6)
    assert TH.floor_reachable(terrain_y, 1.88)(6.0, -3.6)
    assert TH.floor_reachable(terrain_y, 0.0)(16.0, -3.6)
    # 查不到地面高度（旧帧 / 盒缝）不拦
    assert TH.floor_reachable({}, 0.0)(6.0, -3.6)


@pytest.mark.parametrize("idx", range(7))
def test_no_throw_upstairs_from_ground_floor(frames_upstairs, idx):
    """一楼任何格都投不上二楼；方案（若有）的出手格必须在楼梯顶/二楼。"""
    w = W.World(frames_upstairs[idx])
    p2 = P.chef_by_name(w.chefs, P.P2_NAME)
    assert p2["held"] == "Meat" and p2["y"] < 0.1
    for board in (s for s in w.stations if s.get("t") == "chopping"):
        assert TS.aim_here(w, board, TS.MODE_FAR) is None
        plan = TS.launch_plan(w, board, TS.MODE_FAR)
        if plan is not None:
            top = TH.ground_top(w.terrain_y, *plan["world"])
            assert top is not None and top > 1.4

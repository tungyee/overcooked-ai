"""渡运台面（ai/space/shuttle.py）：Balloon_2_2（游戏内 2-3）下方 Server_Balloon 篮子。

真实帧 tests/fixtures/balloon/balloon22-20261031-185032-shuttle.ndjson.gz
（trace-20261031-185032 level_time 38~101s，约 1 帧/秒；`_key` 帧带完整配方）：
  - t≈45.3：篮子停右口（bx≈18），P1 刚把成品 -16272 放上台面 (3)；
  - t≈50.3：篮子在路上（bx≈13），两盘成品 -16272 / -16276 在台面上；
  - t≈57.9 / 61.9：篮子停左口（bx≈3.6），P2 当局全程 DeliverPlate unreachable；
  - t≈66.1：篮子离开左口，(3)/(6) 还够得着、(4)/(5) 已出口。
台面正前一排格心 z=6.0 落在固定地板外（Ground minz=6.03），常规交互位为空。
"""
import copy
import gzip
import json
import os

import pytest

from ai import primitives as PR
from ai import tasks as T
from ai.core import planner
from ai.exec.bt import Status
from ai.query.recipe_graph import _assembly_cells, _counter_cells
from ai.query.recipes import RecipeBook
from ai.space import reachability as R
from ai.space import seam_clock as SC
from ai.space import shuttle as SH
from ai.state import world as W
from ai.state.bb_factory import make_bb

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "balloon")
FIXTURE = os.path.join(FIX, "balloon22-20261031-185032-shuttle.ndjson.gz")
GRID = "Server_Balloon"
MEAT_DISH, SHROOM_DISH = "-16276", "-16272"
PERIOD = 36.2


@pytest.fixture(scope="module")
def frames():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.fixture(scope="module")
def keyframes(frames):
    return {round(f["level_time"], 1): f for f in frames if f.get("_key")}


@pytest.fixture(scope="module")
def learned(frames):
    """整段帧喂一遍学出来的时间表（左口两次开门沿 → 周期）。"""
    clock = SC.SeamClock()
    for f in frames:
        w = W.World(f)
        SH.observe(clock, w, R.field_for(w, None), f["level_time"])
    return clock


def _clock_at(learned, t):
    """把学好的时间表挪到 t 的同相位（往后整两圈），当作「已学会时」的此刻。"""
    clock = copy.deepcopy(learned)
    clock.now = t + 2 * PERIOD
    return clock


def _ctx(frame, clock=None):
    w = W.World(frame)
    book = RecipeBook(w.recipes, w.stations)
    bb = make_bb(100)
    bb.shuttle_clock = clock
    return w, book, bb, planner._make_context(w, bb, book)


def _plate(w, pid):
    return next(p for p in w.plates if str(p["n"]) == pid)


# ---------------------------------------------------------------- 识别


def test_only_balloon22_has_shuttle():
    """人能走上去的移动平台（Balloon_2_3 气球、Rapids 木筏）不算渡运。"""
    import glob
    for path in sorted(glob.glob(os.path.join(os.path.dirname(__file__),
                                              "fixtures", "*", "*.ndjson.gz"))):
        with gzip.open(path, "rt", encoding="utf8") as fh:
            frame = next((json.loads(l) for l in fh
                          if '"phase": "level"' in l and '"stations"' in l), None)
        if frame is None:
            continue
        got = SH.shuttle_grids(W.World(frame))
        want = {GRID} if os.path.basename(path).startswith("balloon22") else set()
        assert got == want, path


def test_counter_front_row_not_walkable(keyframes):
    """根因复现：台面 (gz=-6) 正前一排 gz=-5 全不可走，常规交互位为空。"""
    w = W.World(keyframes[57.9])
    for st in SH.shuttle_stations(w):
        cell = w.to_grid(st["x"], st["z"])
        assert not w.walkable(cell[0], cell[1] + 1)


# ---------------------------------------------------------------- 站位


def test_left_dock_stances(keyframes):
    """篮子停左口：4 格台面都有站位，站在固定地板 z∈[6.4,6.7]，朝 -z，落脚格 P2 走得到。"""
    w = W.World(keyframes[57.9])
    field = R.field_for(w, None)
    stations = SH.shuttle_stations(w)
    assert len(stations) == 4
    for st in stations:
        got = SH.stances(w, st)
        assert len(got) == 1, st["n"]
        s = got[0]
        assert 6.4 <= s["z"] <= 6.7
        assert s["face"] == (0.0, -1.0)
        assert s["cell"] in field
        assert abs(s["x"] - st["x"]) <= 0.35


def test_no_stance_in_transit(keyframes):
    w = W.World(keyframes[50.3])
    assert all(SH.stances(w, st) == [] for st in SH.shuttle_stations(w))


def test_right_dock_stances_not_in_p2_field(keyframes):
    """篮子停右口：站位在 P1 那侧，P2 可达场里没有。"""
    w = W.World(keyframes[45.3])
    field = R.field_for(w, None)
    stations = SH.shuttle_stations(w)
    assert any(SH.stances(w, st) for st in stations)
    assert all(SH.reachable_stances(w, st, field) == [] for st in stations)


def test_dish_on_shuttle_reachable_at_left_dock(keyframes):
    """t≈57.9 当局判 unreachable 的两盘菜：现在够得着、DeliverPlate 不被否决。"""
    w, book, bb, ctx = _ctx(keyframes[57.9])
    for pid in (MEAT_DISH, SHROOM_DISH):
        p = _plate(w, pid)
        assert SH.station_of(w, p) is not None
        assert R.can_interact(ctx, p)
    assert R.task_veto(T.DeliverPlate("1", (MEAT_DISH,)), ctx) == ""


def test_planner_delivers_at_left_dock(keyframes):
    w, book, bb, _ = _ctx(keyframes[57.9])
    task, _rec = planner.plan(w, bb, None, book)
    assert task.TYPE == "DeliverPlate" and task.plate_id == MEAT_DISH


def test_approach_moves_to_stance_point(keyframes):
    """move 终点 = 站位朝台面推 GOAL_PUSH；路径最后一点就是它；带盘的交互句柄。"""
    from tests.test_exec_primitives import FakeExec
    w, book, bb, ctx = _ctx(keyframes[57.9])
    ex = FakeExec()
    ctx = dict(ctx, exec=ex, tick=0)
    p = _plate(w, MEAT_DISH)
    ap = PR.Approach(lambda c: _plate(c["w"], MEAT_DISH), kind="pickup")
    assert ap.tick(ctx) == Status.RUNNING
    cmd = ex.issued[0]
    assert cmd["cmd"] == "move"
    st = SH.station_of(w, p)
    stance = SH.reachable_stances(w, st, ctx["field"])[0]
    assert cmd["goal"] == SH.goal_point(stance)
    assert cmd["path"][-1] == cmd["goal"]
    assert cmd["goal"][1] == pytest.approx(stance["z"] - SH.GOAL_PUSH)
    assert cmd["target"] is not None


def test_approach_fails_in_transit(keyframes):
    from tests.test_exec_primitives import FakeExec
    w, book, bb, ctx = _ctx(keyframes[50.3])
    ex = FakeExec()
    ctx = dict(ctx, exec=ex, tick=0)
    ap = PR.Approach(lambda c: _plate(c["w"], MEAT_DISH), kind="pickup")
    assert ap.tick(ctx) == Status.FAILURE
    assert ex.issued == []


def test_shuttle_counters_not_assembly_cells(keyframes):
    """摆空盘不选渡运台面：摆上去会被篮子运走。"""
    w = W.World(keyframes[57.9])
    moving = {w.to_grid(st["x"], st["z"]) for st in SH.shuttle_stations(w)}
    counters = _counter_cells(w)
    assert moving & set(counters)
    assert not moving & set(_assembly_cells(w, counters))


# ---------------------------------------------------------------- 时间表


def test_clock_learns_period(learned):
    """左口两次「够得着」上升沿（t≈55 / 91）→ 周期 ≈36.2s（实测停靠间隔）。"""
    known = [k for k in learned.tracks if learned.known(k)]
    assert len(known) >= 3
    for key in known:
        assert learned.period(key) == pytest.approx(PERIOD, abs=1.0)


def test_unknown_clock_vetoes_in_transit(keyframes):
    """时间表还没学会（第一圈）：路上的菜照旧判 unreachable，不瞎等。"""
    w, book, bb, ctx = _ctx(keyframes[50.3], clock=None)
    assert R.task_veto(T.DeliverPlate("1", (MEAT_DISH,)), ctx) == "unreachable"


def test_learned_clock_predicts_wait_in_transit(keyframes, learned):
    """学会后 t≈50.3：不否决，等待 ≈ 篮子到左口(≈55~56) − 走过去，几秒量级。"""
    w, book, bb, ctx = _ctx(keyframes[50.3], clock=_clock_at(learned, 50.3))
    task = T.DeliverPlate("1", (MEAT_DISH,))
    assert R.task_veto(task, ctx) == ""
    wait = R.task_wait_seconds(task, ctx)
    assert 2.0 <= wait <= 8.0
    assert R.task_shuttle_wait_seconds(task, ctx) == pytest.approx(wait)


def test_planner_defers_long_shuttle_wait(keyframes, learned):
    """篮子还要等 > GATE_DEFER_S：同层有别的就先做别的（这一帧选的是切/投肉）。"""
    w, book, bb, _ = _ctx(keyframes[50.3], clock=_clock_at(learned, 50.3))
    task, rec = planner.plan(w, bb, None, book)
    assert task.TYPE != "DeliverPlate"
    assert rec.get("gate_waits")


def test_staging_cell_is_last_round_stance(keyframes, learned):
    w, book, bb, ctx = _ctx(keyframes[50.3], clock=_clock_at(learned, 50.3))
    cell = SH.staging_cell(w, _plate(w, MEAT_DISH), ctx["field"], bb.shuttle_clock)
    assert cell is not None and cell in ctx["field"]
    assert cell[1] == -4      # 左口围栏前那一排


# ---------------------------------------------------------------- 等篮子（原语层，所有 task 通用）


def _approach_ctx(frame, learned, t):
    from tests.test_exec_primitives import FakeExec
    w, book, bb, ctx = _ctx(frame, clock=_clock_at(learned, t))
    ex = FakeExec()
    return w, dict(ctx, exec=ex, tick=0), ex


def test_approach_waits_for_shuttle_instead_of_failing(keyframes, learned):
    """篮子在路上：Approach 原地等（RUNNING、不发 move），并告诉外层这是外部等待。"""
    w, ctx, ex = _approach_ctx(keyframes[50.3], learned, 50.3)
    ap = PR.Approach(lambda c: _plate(c["w"], MEAT_DISH), kind="pickup")
    assert ap.tick(ctx) == Status.RUNNING
    assert ex.issued == []
    assert ctx["external_wait"] == 1
    assert 2.0 <= ctx["shuttle_wait"] <= 8.0


def test_approach_stages_when_shuttle_close(keyframes, learned):
    """离到口 ≤ SHUTTLE_STAGE_S：先走到上一轮的落脚格（围栏前那一排）。"""
    from ai.primitives import move as M
    w, ctx, ex = _approach_ctx(keyframes[50.3], learned, 50.3)
    wait = R.shuttle_wait(ctx, _plate(w, MEAT_DISH))
    clock = ctx["bb"].shuttle_clock
    clock.now += wait - M.SHUTTLE_STAGE_S + 0.5      # 挪到「快到了」
    ap = PR.Approach(lambda c: _plate(c["w"], MEAT_DISH), kind="pickup")
    assert ap.tick(ctx) == Status.RUNNING
    cmd = ex.issued[-1]
    assert cmd["cmd"] == "move" and cmd["target"] is None
    assert w.to_grid(*cmd["goal"])[1] == -4


def test_approach_unpredictable_shuttle_fails(keyframes):
    """时间表没学会：照旧 FAILURE 交还 Planner。"""
    from tests.test_exec_primitives import FakeExec
    w, book, bb, ctx = _ctx(keyframes[50.3], clock=None)
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    ap = PR.Approach(lambda c: _plate(c["w"], MEAT_DISH), kind="pickup")
    assert ap.tick(ctx) == Status.FAILURE


def test_any_task_waits_for_shuttle_in_timeout(keyframes, learned):
    """等篮子在原语层：DeliverPlate 树结构不变，整树 Timeout 不因等待超时。"""
    tree = T.DeliverPlate("1", (MEAT_DISH,)).build()
    assert isinstance(tree, PR.Timeout)
    for k in range(tree.max_ticks + 50):
        w, ctx, ex = _approach_ctx(keyframes[50.3], learned, 50.3)
        assert tree.tick(ctx) == Status.RUNNING, k
    assert tree._ticks <= 2


def test_timeout_still_counts_without_external_wait():
    from ai.exec.bt import Node

    class Spin(Node):
        def tick(self, ctx):
            return Status.RUNNING

        def halt(self):
            pass
    t = PR.Timeout(Spin(), 3)
    assert [t.tick({}) for _ in range(4)][-1] == Status.FAILURE


def test_active_task_yields_on_long_shuttle_wait(keyframes, learned):
    """当前承诺要等篮子 > ACTIVE_SHUTTLE_DEFER_S 也让位；短等待照旧不让（迟滞带）。"""
    import types
    w, book, bb, ctx = _ctx(keyframes[66.1], clock=_clock_at(learned, 66.1))
    task = T.DeliverPlate("1", (MEAT_DISH,))
    bb.commitment.active_task = task
    cand = types.SimpleNamespace(task=task)
    waits = planner._gate_waits([cand], ctx, bb)
    assert waits.get(task.key, 0) > planner.ACTIVE_SHUTTLE_DEFER_S

    w, book, bb, ctx = _ctx(keyframes[50.3], clock=_clock_at(learned, 50.3))
    bb.commitment.active_task = task
    assert R.task_shuttle_wait_seconds(task, ctx) < planner.ACTIVE_SHUTTLE_DEFER_S
    assert task.key not in planner._gate_waits([cand], ctx, bb)


# ---------------------------------------------------------------- 跨区送件 SendAcross（trace-20261031-204801）
# 帧：[0] t=9.6 开局（右边 4 块干净盘）；[1] t=133.4 篮子停左口、右边 0 块干净盘、
# 左台面 (-10,0) 一块洗好的盘；[2] t=172.6 篮子停左口、左边 2 块（台面 + 沥碗架顶）。

SEND_FIXTURE = os.path.join(FIX, "balloon22-20261031-204801-send-plates.ndjson.gz")


@pytest.fixture(scope="module")
def send_frames():
    with gzip.open(SEND_FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _hold_plate(frame, pid):
    """改帧：P2 端着 pid 那块盘。"""
    f = json.loads(json.dumps(frame))
    p = next(x for x in f["plates"] if x["n"] == pid)
    p["held_by"] = "Player 2"
    next(c for c in f["chefs"] if c["name"] == "Player 2")["held"] = p["name"]
    return f


def test_far_plate_deficit(send_frames):
    from ai.revenue import cross_supply as XS
    w0, book, _bb, _c = _ctx(send_frames[0])
    assert XS.send_deficit(w0, XS.KIND_CLEAN_PLATE, book) == 0      # 右边有盘
    w1, book, _bb, _c = _ctx(send_frames[1])
    acc = XS.far_plate_need(w1, book)
    assert acc["far"] == 0 and acc["near_spare"] == 1
    assert XS.send_deficit(w1, XS.KIND_CLEAN_PLATE, book) == 1
    w2, book, _bb, _c = _ctx(send_frames[2])
    assert XS.send_deficit(w2, XS.KIND_CLEAN_PLATE, book) == 2


def test_plate_on_shuttle_counts_as_in_transit(send_frames):
    """放上篮子的干净盘算对面的（在途），缺口随之减一，也不再生成它的送件候选。"""
    from ai.revenue import cross_supply as XS
    f = json.loads(json.dumps(send_frames[2]))
    w = W.World(f)
    slot = next(s for s in SH.shuttle_stations(w) if s["n"].endswith("(5)"))
    p = next(x for x in f["plates"] if x["n"] == "-38226")
    p.update(x=slot["x"], z=slot["z"], grid=GRID)
    w, book, bb, ctx = _ctx(f)
    assert XS.far_plate_need(w, book)["far"] == 1
    assert XS.send_deficit(w, XS.KIND_CLEAN_PLATE, book) == 1
    task, rec = planner.plan(w, bb, None, book)
    sends = [c[0] for c in rec["candidates"] if c[0].TYPE == "SendAcross"]
    assert sends and all(t.source_id != "-38226" for t in sends)


def test_no_send_without_shuttle(frame_any_balloon23):
    from ai.revenue import cross_supply as XS
    w = W.World(frame_any_balloon23)
    book = RecipeBook(w.recipes, w.stations)
    assert XS.send_deficit(w, XS.KIND_CLEAN_PLATE, book) == 0


@pytest.fixture(scope="module")
def frame_any_balloon23():
    path = os.path.join(FIX, "balloon23-20261029.ndjson.gz")
    with gzip.open(path, "rt", encoding="utf8") as fh:
        return next(json.loads(l) for l in fh if '"phase": "level"' in l and '"stations"' in l)


@pytest.mark.parametrize("idx,pid", [(1, "-38226"), (2, None)])
def test_planner_sends_clean_plate(send_frames, idx, pid):
    """右边没盘、篮子停左口：P2 选 SendAcross，先去拿干净盘（台面上或沥碗架顶）。"""
    from tests.test_exec_primitives import FakeExec
    w, book, bb, ctx = _ctx(send_frames[idx])
    task, _rec = planner.plan(w, bb, None, book)
    assert task.TYPE == "SendAcross" and task.grid == GRID
    if pid:
        assert task.source_id == pid
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    tree = task.build()
    assert tree.tick(ctx) == Status.RUNNING
    cmd = ctx["exec"].issued[-1]
    assert cmd["cmd"] == "move" and ctx["interaction"]["kind"] == "pickup"


def test_send_holding_plate_goes_to_shuttle_slot(send_frames):
    """端着干净盘：直接走到篮子台面的格外站位（围栏口前），放置句柄指向台面。"""
    from tests.test_exec_primitives import FakeExec
    f = _hold_plate(send_frames[1], "-38226")
    w, book, bb, ctx = _ctx(f)
    task = T.SendAcross(item="clean_plate", resources=("-38226", GRID))
    assert task.feasible(ctx) and not task.done(ctx)
    ctx = dict(ctx, exec=FakeExec(), tick=0)
    assert task.build().tick(ctx) == Status.RUNNING
    cmd = ctx["exec"].issued[-1]
    assert cmd["cmd"] == "move" and cmd["target"]["name"].startswith("countertop_01_standard")
    assert 6.0 <= cmd["goal"][1] <= 6.6           # 站位 z≈6.5 朝台面推一点
    assert ctx["interaction"]["kind"] == "placement"


def test_send_done_when_plate_on_shuttle(send_frames):
    f = json.loads(json.dumps(send_frames[1]))
    w = W.World(f)
    slot = next(s for s in SH.shuttle_stations(w) if s["n"].endswith("(6)"))
    p = next(x for x in f["plates"] if x["n"] == "-38226")
    p.update(x=slot["x"], z=slot["z"], grid=GRID)
    w, book, bb, ctx = _ctx(f)
    assert T.SendAcross(item="clean_plate", resources=("-38226", GRID)).done(ctx)


def test_send_waits_for_shuttle_counts_toward_deferral(send_frames, learned):
    """篮子不在口上：放哪一格的等待时间进 task_wait_seconds（让位 / 迟滞同送餐）。"""
    f = json.loads(json.dumps(send_frames[1]))
    for g in f["grids"]:
        if g["id"] == GRID:
            g["ox"] = 13.0            # 篮子挪到路上
    for st in f["stations"]:
        if st.get("grid") == GRID:
            for k in ("x", "minx", "maxx"):
                st[k] += 13.0 - 3.6
    w, book, bb, ctx = _ctx(f, clock=_clock_at(learned, 50.3))
    task = T.SendAcross(item="clean_plate", resources=("-38226", GRID))
    assert R.task_wait_seconds(task, ctx) > 0

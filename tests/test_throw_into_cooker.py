"""下锅最后几步改成投进锅（ThrowOntoStation）：几何、门槛、真实帧行为、命令字段。

真实帧：tests/fixtures/balloon/balloon23-20261029-213506.ndjson.gz（Balloon_2_3）
  [0] t=105 P2 拿着意面在 (17.4,-13.2)，离 2 号锅 7.8m —— 该先走到 3~5m 出手位
  [1] t=550 P2 拿着意面在 (27.3,-8.8)，离 2 号锅 4.9m、直线无遮挡 —— 原地就能投；
      这一份在 t=558 投出、t=561 进锅（全局唯一一次投进锅的实测样本）
"""
import gzip
import json
import os

import pytest

from ai import primitives as PR
from ai.core import planner
from ai.exec.bt import Status
from ai.primitives import throw as PT
from ai.query import predicates as P
from ai.query.recipes import RecipeBook
from ai.space import throw as TH
from ai.state import world as W
from ai.state.bb_factory import make_bb
from ai.tasks.base import cooker_state, cooker_station

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "balloon",
                       "balloon23-20261029-213506.ndjson.gz")
POT2 = ("RightBasket", "workstation_cooker_01 (2)")


@pytest.fixture(scope="module")
def frames():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class FakeExec(object):
    def __init__(self):
        self.issued = []
        self.results = []
        self._next_id = 0

    def issue(self, cmd):
        self._next_id += 1
        cmd["cmd_id"] = self._next_id
        self.issued.append(dict(cmd))
        return self._next_id

    def poll(self):
        return self.results.pop(0) if self.results else None

    def result(self, ok=True, why=None):
        return {"cmd_id": self._next_id, "ok": ok, "why": why,
                "held_after": None, "blocked_cell": None}


def _ctx(frame, tick=100, ex=None):
    w = W.World(frame)
    book = RecipeBook(w.recipes, w.stations)
    bb = make_bb(tick)
    ctx = planner._make_context(w, bb, book)
    ctx["exec"] = ex or FakeExec()
    return w, bb, ctx


def _node():
    def holding(c):
        return P.held_matches(c["chef"], "Pasta")

    def placed(c):
        cc = cooker_state(c, POT2)
        return "pasta" in str((cc or {}).get("pot_contents")).lower()

    return PR.ThrowOntoStation(lambda c: cooker_station(c, POT2), holding, placed, POT2)


# ---------------------------------------------------------------- 几何


def _grid(x, z):
    return (int(round(x / 1.2)), int(round(z / 1.2)))


def test_pot_throw_distance_band():
    """出手距离带外不投：太近直接放更快，太远料已越过锅的高度。"""
    open_floor = lambda cell: True  # noqa: E731
    assert TH.pot_throw_ok((0.0, 0.0), (4.8, 0.0), _grid, open_floor)
    assert not TH.pot_throw_ok((0.0, 0.0), (1.2, 0.0), _grid, open_floor)
    assert not TH.pot_throw_ok((0.0, 0.0), (7.2, 0.0), _grid, open_floor)


def test_pot_line_blocked_by_counter_or_chef():
    """路径上有台面/别的灶（会截料）或别的厨师（会接住）就不投。"""
    blocked = {(2, 0)}
    passable = lambda cell: cell not in blocked  # noqa: E731
    assert not TH.pot_throw_ok((0.0, 0.0), (4.8, 0.0), _grid, passable)
    # 换一条不经过 (2,0) 的线
    assert TH.pot_throw_ok((0.0, 1.2), (4.8, 1.2), _grid, passable)


def test_plan_pot_throw_picks_cheapest_valid_cell():
    cells = [(0, 0), (1, 0), (4, 0)]
    cost = {(0, 0): 5, (1, 0): 2, (4, 0): 1}.get
    to_world = lambda gx, gz: (gx * 1.2, gz * 1.2)  # noqa: E731
    plan = TH.plan_pot_throw(cells, cost, to_world, _grid, (6.0, 0.0), lambda c: True)
    # (4,0) 离锅 1.2m 太近不算；(1,0) 4.8m 比 (0,0) 6.0m 近（也更便宜）
    assert plan is not None and plan["cell"] == (1, 0)
    assert TH.POT_THROW_MIN <= plan["dist"] <= TH.POT_THROW_MAX


# ---------------------------------------------------------------- 真实帧


def test_in_range_throws_with_strict_aim(frames):
    """t=550：4.9m、直线无遮挡 → 原地投，命令带精准对准字段。"""
    ex = FakeExec()
    w, bb, ctx = _ctx(frames[1], ex=ex)
    node = _node()
    assert node.ok_here(ctx)
    assert node.tick(ctx) == Status.RUNNING
    cmd = ex.issued[-1]
    assert cmd["cmd"] == "throw"
    assert cmd["aim_dot"] == PT.POT_AIM_DOT and cmd["strict"] is True
    assert cmd["face_timeout_ms"] == PT.POT_FACE_TIMEOUT_MS
    assert cmd["timeout_ms"] >= PT.POT_FACE_TIMEOUT_MS + 1400
    pot = cooker_station(ctx, POT2)
    assert cmd["target_world"] == [pot["x"], pot["z"]]
    assert not any(c["cmd"] == "move" for c in ex.issued)


def test_far_walks_to_stance_first(frames):
    """t=105：离锅 7.8m → 先走到 3~5m 的出手位，不在原地投。"""
    ex = FakeExec()
    w, bb, ctx = _ctx(frames[0], ex=ex)
    node = _node()
    assert not node.ok_here(ctx)
    plan = node.plan(ctx)
    assert plan is not None and TH.POT_THROW_MIN <= plan["dist"] <= TH.POT_THROW_MAX
    assert node.tick(ctx) == Status.RUNNING
    cmd = ex.issued[-1]
    assert cmd["cmd"] == "move"
    assert cmd["goal"] == list(w.to_world(*plan["cell"]))


def test_failed_throw_sets_cooldown_then_walks(frames):
    """没对准（插件 strict 拒投）→ 写冷却、FAILURE；冷却期内不再投。"""
    ex = FakeExec()
    w, bb, ctx = _ctx(frames[1], ex=ex)
    node = _node()
    assert node.tick(ctx) == Status.RUNNING
    ex.results.append(ex.result(ok=False, why="aim timeout"))
    assert node.tick(ctx) == Status.FAILURE
    assert bb.pot_throw_cooldown.get(POT2, 0) > ctx["tick"]
    assert node.plan(ctx) is None
    # 冷却过期后恢复
    ctx["tick"] = bb.pot_throw_cooldown[POT2] + 1
    assert node.plan(ctx) is not None


def test_no_throw_when_pot_already_has_it_or_hands_empty(frames):
    """锅已收到这份 / 手上没料：门槛不过，FAILURE 且不写冷却（不算投偏）。"""
    frame = json.loads(json.dumps(frames[1]))
    for c in frame["cookers"]:
        if c["n"] == POT2[1]:
            c["pot_contents"] = ["ing:Pasta"]
    w, bb, ctx = _ctx(frame)
    node = _node()
    assert node.plan(ctx) is None
    assert node.tick(ctx) == Status.FAILURE
    assert not bb.pot_throw_cooldown

    frame = json.loads(json.dumps(frames[1]))
    for c in frame["chefs"]:
        if c["name"] == P.P2_NAME:
            c["held"] = None
    w, bb, ctx = _ctx(frame)
    assert _node().plan(ctx) is None


def test_skip_throw_when_it_saves_little(frames, monkeypatch):
    """走过去也就几步时直接放：省不到 POT_THROW_SAVE_CELLS 格不投。"""
    w, bb, ctx = _ctx(frames[0])
    assert _node().plan(ctx) is not None
    monkeypatch.setattr(PT, "POT_THROW_SAVE_CELLS", 99)
    assert _node().plan(ctx) is None


def test_plain_throw_command_unchanged():
    """不传精准字段的 ThrowTo（ThrowBatch 用）命令形状不变。"""
    ex = FakeExec()
    chef = {"name": P.P2_NAME, "x": 0.0, "z": 0.0, "held": "Pasta"}
    ctx = {"chef": chef, "exec": ex, "tick": 0}
    PR.ThrowTo(lambda c: {"x": 3.0, "z": 0.0}).tick(ctx)
    cmd = ex.issued[-1]
    assert cmd["cmd"] == "throw" and cmd["timeout_ms"] == PT.THROW_TIMEOUT_MS
    assert "aim_dot" not in cmd and "strict" not in cmd and "face_timeout_ms" not in cmd


@pytest.mark.parametrize("idx,expect", [(0, "move"), (1, "throw")])
def test_planner_fill_cooker_tree_throws_into_pot(frames, idx, expect):
    """端到端：planner 选 FillCooker(2 号锅) → 建树 tick。

    t=550 拿着意面在出手距离内 → 直接发精准 throw；t=105 还远 → 先 move 到出手位
    （旧树会一路走到锅旁 placement）。
    """
    ex = FakeExec()
    w, bb, ctx = _ctx(frames[idx], ex=ex)
    task, _rec = planner.plan(w, bb, None, RecipeBook(w.recipes, w.stations))
    assert task is not None and task.TYPE == "FillCooker"
    assert task.resources[0] == POT2
    tree = task.build()
    for _ in range(3):
        tree.tick(ctx)
    first = ex.issued[0]
    assert first["cmd"] == expect
    if expect == "throw":
        assert first["aim_dot"] == PT.POT_AIM_DOT

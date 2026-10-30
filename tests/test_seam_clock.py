"""门洞时间表（ai/space/seam_clock.py）+ 按到达时刻等门的寻路 + 等门让位。

周期取 trace-20261030-001857 实测：x=13.2 那道门每 27s 开一次、开 9s。
"""
from types import SimpleNamespace

from ai.core import planner
from ai.primitives.move import GoToCell
from ai.space import navigation, reachability, seam_clock as SC, seams
from ai.state.blackboard import Blackboard
from ai.state.world import World
from tests.test_seams import _frame

KEY = ("x", 13.2)
T, D = 27.0, 9.0


class _Seam(object):
    axis, line = "x", 13.2


def _gates(is_open, mid=-11.2):
    seam = _Seam()
    band = (mid - 0.2, mid + 0.2) if is_open else None
    return SimpleNamespace(seams=[seam], gates={(4, 1): (seam, band)})


def _feed(c, edges, until, step=0.2, start=0.0, gates=None):
    """按 0.2s 采样喂观测：edges 是 [(时刻, 开/关)]，两沿之间保持上一个状态。"""
    gates = gates or _gates
    t, i, state = start, 0, edges[0][1]
    while t <= until + 1e-9:
        while i < len(edges) and edges[i][0] <= t + 1e-9:
            state = edges[i][1]
            i += 1
        c.observe(round(t, 3), gates(state) if not callable(state) else state())
        t += step
    return c


def _learned(until=80.0, first_rise=20.0, step=0.2):
    """按 T/D 周期喂观测到 until 秒，返回学好的时钟。"""
    c = SC.SeamClock()
    t = 0.0
    while t <= until:
        phase = (t - first_rise) % T
        c.observe(t, _gates(t >= first_rise and phase < D))
        t = round(t + step, 3)
    return c


# ---------- 时间表 ----------
def test_learns_period_and_duration_from_edges():
    c = _learned()
    assert c.known(KEY)
    assert abs(c.period(KEY) - T) < 0.3
    assert abs(c.tracks[KEY].duration - D) < 0.3


def test_first_frame_open_is_not_a_rise():
    c = SC.SeamClock()
    c.observe(0.0, _gates(True))
    c.observe(0.2, _gates(True))
    assert c.tracks[KEY].rises == []


def test_single_rise_needs_anim_period():
    c = _feed(SC.SeamClock(), [(0.0, False), (1.0, True), (10.0, False)], 10.0)
    assert not c.known(KEY)
    c.observe(10.2, _gates(False), anim_period=27.0)
    assert c.known(KEY) and c.period(KEY) == 27.0


def test_flicker_is_not_an_edge():
    seq = [(0.0, False), (1.0, True), (1.4, False), (1.6, True), (10.0, False)]
    c = _feed(SC.SeamClock(), seq, 10.4)
    tr = c.tracks[KEY]
    assert tr.rises == [1.0]
    assert abs(tr.duration - 9.0) < 1e-6


def test_wait_predicts_next_opening():
    c = _learned(until=80.0)          # 开门沿 20, 47, 74
    assert c.wait(KEY, 75.0, need=0.6) == 0.0                     # 正开着
    assert abs(c.wait(KEY, 90.0, need=0.6) - (101.5 - 90.0)) < 0.3  # 下一圈 101 开（+0.5 余量）
    # 到达时只剩不到 need+余量就关：等下一圈
    assert c.wait(KEY, 82.5, need=0.6) > 15.0


def test_unknown_schedule_falls_back_to_now():
    c = SC.SeamClock()
    assert c.wait(KEY, 5.0, open_now=True) == 0.0
    assert c.wait(KEY, 5.0, open_now=False) is None


def test_slip_relearns():
    c = _learned(until=80.0)
    # 下一次开门比预测（101）晚 12s：离上次开门（74）32s 不是整数圈，丢掉重学
    _feed(c, [(0.0, True), (83.0, False), (113.0, True)], 113.0, start=80.2)
    assert c.tracks[KEY].rises == [113.0]
    assert not c.known(KEY)


def test_phase_slip_keeps_period_and_duration():
    """trace-20261030-125344：掉帧让开门沿晚看到 1.5s，旧实现整表清空、
    再等一整圈才可用，期间跨缝 task 全判 unreachable。"""
    c = _learned(until=80.0)                 # 74 开，按表 83 关
    _feed(c, [(0.0, True), (83.0, False), (102.6, True)], 102.6, start=80.2)
    tr = c.tracks[KEY]
    assert tr.rises == [102.6]
    assert c.known(KEY) and abs(c.period(KEY) - T) < 0.3
    assert abs(tr.duration - D) < 0.3


def test_anim_period_requires_loop_and_agreement():
    g = [{"id": "A", "anim": {"len": 27.0, "speed": 1.0, "loop": True}},
         {"id": "B", "anim": {"len": 54.0, "speed": 2.0, "loop": True}}]
    assert SC.anim_period(g) == 27.0
    assert SC.anim_period([{"anim": {"len": 27.0, "speed": 1.0, "loop": False}}]) is None
    g[1]["anim"]["len"] = 40.0
    assert SC.anim_period(g) is None


# ---------- 按到达时刻等门的寻路 ----------
def _closed_world_with_clock(next_open_in=6.0):
    """门洞此刻关着（篮子上移 2.4m），时间表已知、next_open_in 秒后开。"""
    w_open = World(_frame())
    w = World(_frame(basket_dz=2.4))
    # 两圈完整开关（周期 T、开 D），最后一次开门在 now+next_open_in-T
    now = 100.0
    r1 = now + next_open_in - T
    r0 = r1 - T
    closed, opened = (lambda: w.seam_gates), (lambda: w_open.seam_gates)
    c = _feed(SC.SeamClock(), [(0.0, closed), (r0, opened), (r0 + D, closed),
                               (r1, opened), (r1 + D, closed)], now, start=r0 - 2.0)
    bb = Blackboard()
    bb.seam_clock = c
    return w, bb


def test_closed_door_reachable_with_wait_when_schedule_known():
    w, bb = _closed_world_with_clock(next_open_in=6.0)
    assert (6, 1) not in reachability.field_for(w)          # 没时间表：不通
    field = reachability.field_for(w, bb)
    assert (6, 1) in field
    wait_s = field.waits[(6, 1)] * seams.SEC_PER_STEP
    # 走到缝口约 2 格（0.5s），门 6s 后开（+0.5 余量）→ 等 ≈6s
    assert 5.0 <= wait_s <= 6.5


def test_trace_back_matches_waited_field():
    w, bb = _closed_world_with_clock()
    field = reachability.field_for(w, bb)
    path = navigation.trace_back(field, (6, 1), w.belt_dir, None,
                                 seams.nav_predicates(w, bb)[1])
    assert path and path[0] == (2, 1) and path[-1] == (6, 1) and (4, 1) in path


def test_goto_cell_sends_predicted_gate_with_max_wait():
    w, bb = _closed_world_with_clock(next_open_in=6.0)

    class Ex(object):
        def __init__(self):
            self.issued = []

        def issue(self, cmd):
            self.issued.append(cmd)
            return len(self.issued)

        def poll(self):
            return None

    ex = Ex()
    ctx = {"w": w, "bb": bb, "chef": w.chefs[0], "field": reachability.field_for(w, bb),
           "exec": ex, "tick": 0}
    GoToCell(lambda c: (6, 1)).tick(ctx)
    (cmd,) = ex.issued
    (g,) = cmd["gates"]
    # 中线取上次开门时这一格的中线；等门上限 = 预测等待 + 3s 余量
    assert abs(g["mid"] + 11.18) < 1e-6
    assert 8.0 <= g["max_wait"] <= 9.5
    assert cmd["timeout_ms"] >= 2500 + 8000


# ---------- 等门让位 ----------
def _cand(name):
    return SimpleNamespace(task=SimpleNamespace(key=name))


def test_long_wait_candidate_yields_to_lower_layer():
    a, b = _cand("deliver"), _cand("chop")
    layers = {"deliver": 3, "chop": 6}
    got = planner._select_layer([a, b], None, layers, Blackboard(), {}, 0,
                                gate_waits={"deliver": 8.0})
    assert got == (6, [b])
    # 等待缩到阈值内：回到本层
    got = planner._select_layer([a, b], None, layers, Blackboard(), {}, 0,
                                gate_waits={"deliver": 2.0})
    assert got == (3, [a])


def test_rescue_layers_never_yield():
    a, b = _cand("take_cooked"), _cand("chop")
    got = planner._select_layer([a, b], None, {"take_cooked": 2, "chop": 6},
                                Blackboard(), {}, 0, gate_waits={"take_cooked": 8.0})
    assert got == (2, [a])


def test_all_waiting_still_selects():
    a = _cand("deliver")
    got = planner._select_layer([a], None, {"deliver": 3}, Blackboard(), {}, 0,
                                gate_waits={"deliver": 8.0})
    assert got == (3, [a])


def test_gate_waits_exempts_active_task_and_holding():
    field = navigation.Field({(0, 0): 0})
    field.waits = {(0, 0): 4}
    bb = Blackboard()
    task = SimpleNamespace(key="k", TYPE="Chop", claims=lambda: [])
    bb.commitment.active_task = task
    ctx = {"field": field, "chef": {"name": "Player 2", "held": ""}}
    assert planner._gate_waits([SimpleNamespace(task=task)], ctx, bb) == {}
    bb.commitment.active_task = None
    ctx["chef"]["held"] = "Plate"
    assert planner._gate_waits([SimpleNamespace(task=task)], ctx, bb) == {}


def test_observation_gap_does_not_fake_an_edge():
    """trace-20261029-234145：t=2007~6221 观测断档，首尾两帧被连成一次 210s 的开门。"""
    c = _feed(SC.SeamClock(), [(0.0, False), (1.0, True), (10.0, False)], 10.2)
    _feed(c, [(0.0, True), (310.0, False)], 310.0, start=300.0)   # 断档后第一帧开着：只定初值
    assert c.tracks[KEY].rises == [1.0]
    assert abs(c.tracks[KEY].duration - 9.0) < 1e-6


def test_implausible_period_not_trusted():
    # 关着 190s 后再开：周期 199s 不可信
    c = _feed(SC.SeamClock(), [(0.0, False), (1.0, True), (10.0, False), (200.0, True)], 200.0)
    assert not c.known(KEY)


# ---------- 缝口卡死兜底（trace-20261030-123850）----------
def test_timeout_always_covers_plugin_gate_wait():
    """GoToCell 2.5s 比插件缺省等门 4s 短：插件回不了 gate closed，P2 在缝口反复超时。"""
    assert seams.gates_wait_seconds([{"axis": "x"}]) >= seams.GATE_WAIT_DEFAULT + 1.0
    assert seams.gates_wait_seconds([{"max_wait": 9.0}]) >= 10.0
    assert seams.gates_wait_seconds([]) == 0


def test_gate_closed_result_marks_gate_cell_bad_without_abandoning():
    from ai.primitives.base import Primitive
    from ai.primitives.stuck import note_motion_failure

    w = World(_frame())

    class Ex(object):
        def poll(self):
            return {"cmd_id": 1, "ok": False, "why": "gate closed",
                    "blocked_cell": [13.2, -10.8], "held_after": None}

    p = Primitive()
    p._pending_cmd = 1
    ctx = {"w": w}
    from ai.exec.bt import Status
    assert p._poll_exec(ctx, Ex()) == Status.FAILURE
    assert ctx["motion_failure"] == {"reason": "gate_closed", "blocked_cell": (4, 1)}

    bb = Blackboard()
    task = SimpleNamespace(key="k", claims=lambda: [("res", "use")])
    note_motion_failure(bb, task, 10, ctx["motion_failure"], world=w)
    assert (4, 1) in bb.scratch.bad_cells
    assert not bb.blacklisted_resources


# ---------- 门关着时对面那格按开门时的篮子位置算（trace-20261030-125344）----------
def test_closed_gate_lands_where_basket_will_be_when_it_opens():
    """t=1665~1830：门关着时门洞格对面那格此刻已不是开门时的那块地板，可达场止于
    门洞格，跨缝 task 全判 unreachable，P2 空站 8s。开门时贴着门洞的篮子地板记在
    时间表里，按篮子的平移挪回现在的位置连过去。"""
    w_open = World(_frame())                 # 门开（t=1486 门洞在两排格中心之间）
    w = World(_frame(basket_dz=2.4))         # 门关：篮子往 +z 挪了 2 格
    assert not any(b for (s, b) in w.seam_gates.gates.values())
    now = 100.0
    r1 = now + 4.0 - T
    r0 = r1 - T
    closed, opened = (lambda: w.seam_gates), (lambda: w_open.seam_gates)
    c = _feed(SC.SeamClock(), [(0.0, closed), (r0, opened), (r0 + D, closed),
                               (r1, opened), (r1 + D, closed)], now, start=r0 - 2.0)
    bb = Blackboard()
    bb.seam_clock = c
    jumps, stale = seams.nav_jumps(w, bb)
    # 静止侧照常相邻；开门时贴着门洞的篮子地板（z=-10.8）此刻在 z=-8.4
    assert ((4, 1), (5, 1)) in stale
    assert (5, 3) in jumps[(4, 1)]
    field = reachability.field_for(w, bb)
    assert (6, 3) in field
    assert field.waits.get((6, 3), 0) * seams.SEC_PER_STEP > 1.0
    path = navigation.trace_back(field, (6, 3), w.belt_dir)
    assert path[path.index((4, 1)) + 1] == (5, 3)
    points, gates = seams.path_points(w, path, field, bb)
    (g,) = gates
    assert points[g["exit"] + 1] == list(w.to_world(5, 3))
    assert g["max_wait"] >= seams.GATE_WAIT_DEFAULT


def test_summary_reports_next_open_and_close():
    c = _learned(until=60.0)                 # 开门沿 20/47，下一次 74
    row = SC.summary(c)["x=13.2"]
    assert row["known"] and row["open"] is False
    assert abs(row["next_open"] - 14.0) < 0.5 and abs(row["next_close"] - 23.0) < 0.5
    assert SC.summary(None) is None

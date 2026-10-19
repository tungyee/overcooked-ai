"""带 5 Hz 位置反馈与惯性的移动回归；全程不注入真实按键。"""
import pytest
from pathlib import Path

from ai import primitives as PR
from ai.exec.bt import Status
from ai.space.navigation import distance_field
from tests.test_primitives import FakeWorld, make_ctx, _inter


@pytest.mark.skip(reason="FetchToCooker 改成批量取+投，此移动控制回归需按新 build 重写")
def test_latest_trace_stall_moves_toward_actual_cooker():
    """固定真实 tick 1032：柜台前持米、离交互位不到一格也必须持续移动。"""
    from ai import tasks as T
    from ai.query import predicates as P
    from ai.exec.replay import load_frames
    from ai.state.world import World

    path = Path(__file__).parent / "fixtures/motion/trace-stall-20261010.ndjson.gz"
    frame = load_frames(str(path))[0]
    assert frame["_trace_source"]["tick"] == 1032
    w = World(frame)
    chef = w.chef("Player 2")
    assert chef["held"] == "SushiRice"
    assert P.chef_cell(chef, w.to_grid) == (6, 2)
    ctx, ctrl = make_ctx(w=w, chef=chef, start=(6, 2))
    observe(ctx, 0.0, 1)
    tree = T.FetchToCooker(
        "2", ("workstation_cooker_01 (2)", ("SushiRice",)), "BoiledSushiRice").build()
    assert tree.tick(ctx) == Status.RUNNING
    assert ctx["motion"]["goal"] == [6, 1]
    assert ctx["hands"].move_dir == "down"
    observe(ctx, 0.05, 1)
    assert tree.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "down"   # 巡航持续按住，位置未变也不再松键
    assert ctrl.calls.count(("press", "down")) == 1


def observe(ctx, now, seq, x=None, z=None):
    ctx.update(now=now, frame_seq=seq, frame_received_at=now)
    if x is not None:
        ctx["chef"]["x"] = x
    if z is not None:
        ctx["chef"]["z"] = z
    w = ctx["w"]
    cur = w.to_grid(ctx["chef"]["x"], ctx["chef"]["z"])
    ctx["field"] = distance_field(cur, w.walkable, w.belt_dir)


def test_motion_tracks_relative_position_when_reported():
    """插件上报 rel_x/rel_z 时，进度追踪用相对坐标而非世界坐标。

    根因：角色站在移动平台（篮子）上，世界 x/z 被平台带着漂，settle /
    no_progress 判据会误判「没停稳」→ observation_timeout。
    """
    from ai.primitives.motion import MotionControl
    ctx, _ = make_ctx(start=(0, 0))
    chef = ctx["chef"]
    chef["x"] = 1.0
    chef["z"] = 2.0
    chef["rel_x"] = 0.3
    chef["rel_z"] = 0.4
    mc = MotionControl()
    ctx.update(now=0.0, frame_seq=1, frame_received_at=0.0)
    mc.tick(ctx, "up", None, False, False, final=False)
    # 用了相对坐标 (0.3,0.4)，不是世界坐标 (1.0,2.0)
    assert mc._progress_pos == (0.3, 0.4)


def test_motion_falls_back_to_world_position_without_relative():
    """插件没报 rel_x/rel_z 时（旧协议/合成帧）退回世界坐标，行为不变。"""
    from ai.primitives.motion import MotionControl
    ctx, _ = make_ctx(start=(0, 0))
    ctx["chef"]["x"] = 1.0
    ctx["chef"]["z"] = 2.0
    mc = MotionControl()
    ctx.update(now=0.0, frame_seq=1, frame_received_at=0.0)
    mc.tick(ctx, "up", None, False, False, final=False)
    assert mc._progress_pos == (1.0, 2.0)


def near_cooker():
    # 最新 trace 的柜台前停住状态平移到零原点；下面一格才是交互位。
    w = FakeWorld(walkable=lambda x, z: x == 6 and 0 <= z <= 4)
    ctx, ctrl = make_ctx(w=w, start=(6, 2))
    observe(ctx, 0.0, 1, x=7.2, z=2.2)
    return ctx, ctrl, PR.Approach({"n": "cooker", "gx": 7, "gz": 1})


def test_near_stopped_chef_gets_a_step_toward_cooker():
    ctx, ctrl, node = near_cooker()
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "down"
    assert ("press", "down") in ctrl.calls


def test_approach_can_follow_field_with_p1_soft_cost():
    ctx, _, node = near_cooker()
    ctx["soft_cost"] = {(6, 1): 4}
    ctx["field"] = distance_field((6, 2), ctx["w"].walkable, {},
                                  soft_cost=ctx["soft_cost"])
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "down"


def test_cruise_holds_on_old_frames_without_repressing():
    # 长路径中间步走巡航（最后一步才切短按，故用长路径）。
    ctx, ctrl, node = long_approach()
    node.tick(ctx)
    for now in (0.05, 0.10, 0.15):
        observe(ctx, now, 1)
        assert node.tick(ctx) == Status.RUNNING
        assert ctx["hands"].move_dir == "left"
    assert ctrl.calls.count(("press", "left")) == 1


def long_approach():
    # 一条长直线：从 (5,0) 走到 (-5,0) 交互位，路径 > 3 格，触发巡航冲刺。
    w = FakeWorld(walkable=lambda gx, gz: gz == 0 and -8 <= gx <= 8)
    ctx, ctrl = make_ctx(w=w, start=(5, 0))
    observe(ctx, 0.0, 1)
    return ctx, ctrl, PR.Approach({"n": "cooker", "gx": -5, "gz": 1})


def test_long_distance_cruise_holds_dash():
    ctx, ctrl, node = long_approach()
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "left"
    assert ctx["hands"].move_dash is True
    assert ("press", "dash") in ctrl.calls


def test_near_target_does_not_dash():
    """近处（≤3 格）精确站位，不按冲刺避免冲过头。"""
    ctx, ctrl, node = near_cooker()
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "down"
    assert ctx["hands"].move_dash is False
    assert ("press", "dash") not in ctrl.calls


def test_dash_stops_before_final_approach():
    """冲刺要**提前**退出，给惯性留刹车距离。

    旧判据是 `len(path) > 3`（只看路径长短），于是一路冲到倒数第二格才
    松冲刺——冲刺的惯性已经把人带过了交互格。实机：横穿传送带去中间柜台
    时反复冲过头、错过好几次。

    现在在「距目标 ≤ DASH_BRAKE_CELLS 格」就停冲：方向键仍按住（继续走，
    不慢），只是不再加速。
    """
    w = FakeWorld(walkable=lambda x, z: z == 0 and -10 <= x <= 10)
    ctx, ctrl = make_ctx(w=w, start=(8, 0))
    observe(ctx, 0.0, 1)
    node = PR.Approach({"n": "cooker", "gx": -8, "gz": 0})
    # 远处：冲
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dash is True

    # 走到距目标交互位 3 步（刹车区内）：仍在走，但不再冲。
    # 旧判据 len(path) > 3 在这里**仍会冲**（path=[-4,-5,-6,-7] 长度 4），
    # 所以这一格正是新旧行为的分界。
    observe(ctx, 1.0, 20, x=-4 * 1.2)
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "left", "刹车区仍要持续移动，不是停下"
    assert ctx["hands"].move_dash is False, "距目标 3 步要停冲，给惯性留余量"


def test_motion_has_three_tiers_dash_cruise_pulse():
    """移动分三档：冲刺巡航 / 普通巡航（不冲） / 短按精确。

    旧实现只有两档——「冲刺巡航」与「短按」，分界是 `len(path) > 3`。
    两头都不对：远处一路冲到倒数第二格才松冲刺，惯性把人带过交互格
    （横穿传送带去中间柜台反复错过）；而最后一格无条件短按+等停稳，
    每次接近目标要 10~15 拍（trace-20261014-011202：pulse 51 拍 vs
    observe 635 拍）。

    中间档「按住方向但不冲刺」就是补上的那一档：进入刹车区后继续全速
    走路、只是不再加速，到最后一格才精确站位。
    """
    w = FakeWorld(walkable=lambda x, z: z == 0 and -10 <= x <= 10)
    ctx, ctrl = make_ctx(w=w, start=(8, 0))
    observe(ctx, 0.0, 1)
    node = PR.Approach({"n": "cooker", "gx": -8, "gz": 0})

    # 第一档：远距离巡航 + 冲刺
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["motion"]["phase"] == "cruise"
    assert ctx["hands"].move_dash is True

    # 第二档：进入刹车区——仍巡航（按住方向），但不再冲刺
    observe(ctx, 1.0, 20, x=-4 * 1.2)
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["motion"]["phase"] == "cruise", "刹车区仍是巡航，不是短按"
    assert ctx["hands"].move_dir == "left"
    assert ctx["hands"].move_dash is False

    # 第三档：最后一步短按精确站位（保留上一轮「盘旁不冲过头」的修复）
    observe(ctx, 2.0, 40, x=-6 * 1.2)
    node.tick(ctx)
    assert ctx["hands"].move_dash is False


@pytest.mark.skip(reason="震荡检测暂时注释掉（实机卡顿恼火），后续优化再启用")
def test_oscillation_detected_switches_to_pulse():
    """越过目标又掉头（a—c—b 震荡）→ 检到震荡，切到短按+停稳。"""
    w = FakeWorld(walkable=lambda x, z: z == 0 and -8 <= x <= 8)
    ctx, ctrl = make_ctx(w=w, start=(1, 0))
    observe(ctx, 0.0, 1)  # x=1.2 → 格 (1,0)，目标 (0,0)
    node = PR.Approach({"n": "cooker", "gx": 0, "gz": 1})
    assert node.tick(ctx) == Status.RUNNING          # 默认巡航向左
    assert ctx["hands"].move_dir == "left"
    assert not node._oscillating
    observe(ctx, 0.20, 2, x=-0.7)                    # 越过了目标，落在格 (-1,0)
    assert node.tick(ctx) == Status.RUNNING
    assert node._oscillating is True                 # 检出反向折返
    assert ctx["hands"].move_dir is None             # 进入停稳观察，不再持续按住


@pytest.mark.skip(reason="震荡检测暂时注释掉（实机卡顿恼火），后续优化再启用")
@pytest.mark.parametrize("start", [-6.3, -0.8, 0.8, 6.3])
@pytest.mark.parametrize("speed", [4.0, 6.0, 8.0])
@pytest.mark.parametrize("coast", [0.0, 0.35, 0.5])
def test_step_control_converges_with_delayed_feedback_and_coasting(start, speed, coast):
    """a—c—b：长按会跳过 c；短按必须进入 c 并停住，不靠理想位置跳变。"""
    w = FakeWorld(walkable=lambda x, z: z == 0 and -8 <= x <= 8)
    ctx, _ = make_ctx(w=w)
    node = PR.Approach({"n": "cooker", "gx": 0, "gz": 1})
    x, velocity, seq = start, 0.0, 0
    status = Status.RUNNING
    for tick in range(200):
        now = tick * 0.05
        if tick % 4 == 0:
            seq += 1
            observe(ctx, now, seq, x=x)
        else:
            ctx["now"] = now
        status = node.tick(ctx)
        direction = ctx["hands"].move_dir
        velocity = (speed if direction == "right" else
                    -speed if direction == "left" else velocity * coast)
        x += velocity * 0.05
        if status != Status.RUNNING:
            break
    assert status == Status.SUCCESS
    for _ in range(8):
        velocity *= coast
        x += velocity * 0.05
    assert w.to_grid(x, 0) == (0, 0), "宣告到位后不能继续滑出交互位"
    assert ctx["hands"].move_dir is None


def test_arrival_is_rechecked_after_coasting_out_of_goal():
    ctx, _, node = near_cooker()
    node.tick(ctx)
    observe(ctx, 0.05, 1)
    node.tick(ctx)
    observe(ctx, 0.20, 2, z=1.5)
    assert node.tick(ctx) == Status.RUNNING
    observe(ctx, 0.40, 3, z=0.5)
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "up", "滑出目标格后应提前纠偏，不再等满停稳"
    observe(ctx, 0.60, 4)
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir is None, "纠偏短按必须按时释放"


def test_real_obstruction_fails_before_task_timeout():
    ctx, _, node = near_cooker()
    for tick in range(60):
        observe(ctx, tick * 0.05, tick // 4 + 1)
        status = node.tick(ctx)
        if status == Status.FAILURE:
            break
    assert status == Status.FAILURE
    assert ctx["hands"].move_dir is None
    assert ctx["motion_failure"]["reason"] == "no_progress"


def test_frame_loss_stops_continuous_movement():
    ctx, _ = make_ctx()
    observe(ctx, 0.0, 1)
    node = PR.Approach({"n": "cooker", "gx": 7, "gz": 0})
    node.tick(ctx)
    assert ctx["hands"].move_dir == "right"
    ctx["now"] = 1.0
    assert node.tick(ctx) == Status.FAILURE
    assert ctx["hands"].move_dir is None


def test_corner_turns_instantly_without_settling():
    # 90° 拐弯即时换向（中间步）。用长路径：拐弯发生在非最后一步，不应 settle；
    # 最后一步才会切短按（见 test_final_step_settles）。
    ctx, ctrl, node = long_approach()
    ctx["hands"].set_move("up")
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "left"   # 垂直换向，即时 release 旧键 press 新键
    assert ("release", "up") in ctrl.calls
    assert ("press", "left") in ctrl.calls


def test_final_step_settles_before_pulse():
    """最后一步（下一步即目标格）先松键消惯性，不巡航满速冲过头。

    根因：米熟了端饭时，盘旁最后一步仍满速巡航，冲过交互格来回振荡、米烧糊。
    """
    ctx, ctrl, node = near_cooker()
    ctx["hands"].set_move("down")   # 模拟正朝目标方向巡航
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir is None   # 先松键消惯性
    assert ("release", "down") in ctrl.calls


def test_final_step_with_wall_ahead_cruises_instead_of_pulse():
    """最后一步前方有阻碍物时直接长按巡航，不切短按。

    墙/站点会挡住角色，长按不会冲过目标格，省去「短按 + 停稳」的精确档，
    加快正对灶台/柜台的最后一步收敛。
    """
    # 交互位 (6,1)，站 (6,2) 往下走；正下方 (6,0) 不可走 = 前方有阻碍物。
    w = FakeWorld(walkable=lambda gx, gz: gx == 6 and 1 <= gz <= 4)
    ctx, ctrl = make_ctx(w=w, start=(6, 2))
    observe(ctx, 0.0, 1, x=7.2, z=2.2)
    node = PR.Approach({"n": "cooker", "gx": 7, "gz": 1})
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "down"          # 长按，而非短按松键
    assert ctx["motion"]["phase"] == "cruise"
    assert ("press", "down") in ctrl.calls


def test_belt_crossing_final_step_holds_not_pulses():
    """最后一步垂直穿越传送带时按住方向+顶带键，不切 50ms 短按。

    实机 trace（trace-20261017-172221 t=2191~2366）：端盘去取盘，最后一步要
    从 z=2 传送带下到 z=1，却走短按——50ms 跨不过一格、松键又被带推回，在带上
    左右兜圈 175 拍（~8.7s），熟饭在锅里烧糊。
    """
    # (0,0) 是向右推的传送带；站 (0,0) 向上穿越到 (0,1)，(0,2) 是空地（无墙）。
    w = FakeWorld(walkable=lambda gx, gz: (gx, gz) in {(0, 0), (0, 1), (0, 2)},
                  belt_cells={(0, 0)}, belt_dir={(0, 0): (1, 0)})
    ctx, ctrl = make_ctx(w=w, start=(0, 0))
    observe(ctx, 0.0, 1)
    node = PR.Approach({"n": "c", "gx": 1, "gz": 1}, "placement")
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "up"       # 长按，不是短按
    assert ctx["hands"].move_extra == "left"   # 顶住向右推的带
    assert ctx["motion"]["phase"] == "cruise"
    assert ("press", "up") in ctrl.calls
    assert ("press", "left") in ctrl.calls


def test_reverse_belt_walk_holds_dash_and_cruises_on_final_step():
    """逆向行走（顶传送带）必须冲刺+长按，普通步速会被带抵消、卡住不动。

    实机 trace（trace-20261017-171202 t≈3690~3740）：P2 端盘在 z=-3 传送带
    上向右逆行（带向左推），最后一步却走短按——一松键就被带推回，一格打转
    约 50 拍。
    """
    # (0,0) 是向左推的传送带；目标站 (1,-1) 的交互位是 (1,0)，站 (0,0) 向右逆行。
    w = FakeWorld(walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 4,
                  belt_cells={(0, 0)}, belt_dir={(0, 0): (-1, 0)})
    ctx, ctrl = make_ctx(w=w, start=(0, 0))
    observe(ctx, 0.0, 1)
    node = PR.Approach({"n": "c", "gx": 1, "gz": -1}, "placement")
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "right"      # 长按，不是短按
    assert ctx["hands"].move_dash is True        # 逆行必须冲刺
    assert ctx["motion"]["phase"] == "cruise"
    assert ("press", "dash") in ctrl.calls


def test_face_step_is_released_before_new_facing_signal():
    ctx, ctrl = make_ctx()
    observe(ctx, 0.0, 1)
    node = PR.Face({"n": "cooker", "gx": 1, "gz": 0})
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "right"
    observe(ctx, 0.05, 1)
    node.tick(ctx)
    assert ctx["hands"].move_dir is None
    observe(ctx, 0.10, 1)
    node.tick(ctx)
    assert ctrl.calls.count(("press", "right")) == 1


def test_confirmation_does_not_restart_approach_when_pushed_away():
    ctx, ctrl = make_ctx()
    ctx["chef"]["interactions"] = _inter(gx=1, gz=0, pickup_gx=1, pickup_gz=0)
    node = PR.InteractWith({"n": "crate", "gx": 1, "gz": 0},
                           "pickup", lambda c: False)
    assert node.tick(ctx) == Status.RUNNING
    assert ("tap", "pickup") in ctrl.calls
    ctx["chef"]["x"] = 6.0
    ctx["chef"]["interactions"] = _inter()
    ctx["field"] = distance_field((5, 0), ctx["w"].walkable, {})
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir is None
    assert ctrl.calls.count(("tap", "pickup")) == 1


def test_disappearing_pickup_target_does_not_interrupt_confirmation():
    ctx, _ = make_ctx()
    target = {"n": "crate", "gx": 1, "gz": 0}
    ctx["chef"]["interactions"] = _inter(gx=1, gz=0, pickup_gx=1, pickup_gz=0)
    node = PR.InteractWith(lambda c: target, "pickup", lambda c: False)
    node.tick(ctx)
    target = None
    assert node.tick(ctx) == Status.RUNNING


def test_rebuilding_task_cannot_repeat_a_step_on_the_same_frame():
    ctx, ctrl, node = near_cooker()
    node.tick(ctx)
    node.halt()
    observe(ctx, 0.10, 1)
    replacement = PR.Approach({"n": "cooker", "gx": 7, "gz": 1})
    replacement.tick(ctx)
    assert ctrl.calls.count(("press", "down")) == 1
    assert ctx["hands"].move_dir is None


def test_rebuilding_task_cannot_change_cruise_direction_on_old_frame():
    ctx, ctrl = make_ctx()
    observe(ctx, 0.0, 1)
    first = PR.Approach({"n": "c1", "gx": 7, "gz": 0})
    first.tick(ctx)
    first.halt()
    observe(ctx, 0.10, 1)
    PR.Approach({"n": "c2", "gx": -7, "gz": 0}).tick(ctx)
    assert ("press", "left") not in ctrl.calls


def test_confirm_does_not_spend_budget_on_reused_frames():
    ctx, _ = make_ctx()
    node = PR.Confirm(lambda c: False, retries=1)
    for tick in range(5):
        observe(ctx, tick * 0.05, 1)
        assert node.tick(ctx) == Status.RUNNING
    observe(ctx, 0.25, 2)
    assert node.tick(ctx) == Status.RUNNING
    observe(ctx, 0.45, 3)
    assert node.tick(ctx) == Status.FAILURE


def test_confirm_has_deadline_when_no_new_frames_arrive():
    ctx, _ = make_ctx()
    observe(ctx, 0.0, 1)
    node = PR.Confirm(lambda c: False)
    node.tick(ctx)
    ctx["now"] = 1.5
    assert node.tick(ctx) == Status.FAILURE


@pytest.mark.skip(reason="FetchToCooker 改成批量取+投，此移动控制回归需按新 build 重写")
def test_fetch_to_cooker_finishes_with_delayed_position_and_turning():
    """柜台前持米→补步到灶→转向→下米：验证整棵树及真实后置条件。"""
    from ai import tasks as T
    from ai.query.recipes import RecipeBook

    ctx, ctrl, _ = near_cooker()
    w = ctx["w"]
    w.stations = [{"t": "cooking", "n": "cooker", "gx": 7, "gz": 1,
                   "x": 8.4, "z": 1.2}]
    w.cookers = [{"n": "cooker", "cooked": False, "isCooking": False,
                  "pot_contents": []}]
    ctx["book"] = RecipeBook([
        {"name": "BoiledSushiRice", "comp": ["SushiRice"], "cook": "Pot"}], w.stations)
    ctx["chef"]["held"] = "SushiRice"
    tree = T.FetchToCooker("1", ("cooker", ("SushiRice",)), "BoiledSushiRice").build()
    x, z, vx, vz, facing, placed = 7.2, 2.2, 0.0, 0.0, "down", False
    status = Status.RUNNING
    for tick in range(160):
        now = tick * 0.05
        if tick % 4 == 0:
            observe(ctx, now, tick // 4 + 1, x=x, z=z)
            ready = w.to_grid(x, z) == (6, 1) and facing == "right"
            ctx["chef"]["interactions"] = (_inter(gx=7, gz=1, placement_gx=7, placement_gz=1)
                                              if ready else _inter())
            if placed:
                ctx["chef"]["held"] = None
                w.cookers[0].update(isCooking=True, pot_contents=["ing:SushiRice"])
        else:
            ctx["now"] = now
        status = tree.tick(ctx)
        if ("tap", "pickup") in ctrl.calls:
            placed = True
        d = ctx["hands"].move_dir
        if d:
            facing = d
        vx = 6.0 if d == "right" else -6.0 if d == "left" else vx * 0.35
        vz = 6.0 if d == "up" else -6.0 if d == "down" else vz * 0.35
        # 灶的物理碰撞，转向时只允许靠近灶台前沿。
        x, z = min(7.7, x + vx * 0.05), z + vz * 0.05
        if status != Status.RUNNING:
            break
    assert status == Status.SUCCESS
    assert ctx["chef"]["held"] is None
    assert w.cookers[0]["isCooking"]
    assert ctrl.calls.count(("tap", "pickup")) == 1

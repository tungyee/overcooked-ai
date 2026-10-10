"""带 5 Hz 位置反馈与惯性的移动回归；全程不注入真实按键。"""
import pytest
from pathlib import Path

from ai import primitives as PR
from ai.bt import Status
from ai.navigation import distance_field
from tests.test_primitives import FakeWorld, make_ctx, _inter


def test_latest_trace_stall_moves_toward_actual_cooker():
    """固定真实 tick 1032：柜台前持米、离交互位不到一格也必须持续移动。"""
    from ai import tasks as T
    from ai import predicates as P
    from ai.replay import load_frames
    from ai.world import World

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
    ctx, ctrl, node = near_cooker()
    node.tick(ctx)
    for now in (0.05, 0.10, 0.15):
        observe(ctx, now, 1)
        assert node.tick(ctx) == Status.RUNNING
        assert ctx["hands"].move_dir == "down"
    assert ctrl.calls.count(("press", "down")) == 1


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
    observe(ctx, 0.60, 4)
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "up"


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
    ctx, ctrl, node = near_cooker()
    ctx["hands"].set_move("right")
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "down"   # 90° 拐弯即时换向，不再等停稳
    assert ("release", "right") in ctrl.calls
    assert ("press", "down") in ctrl.calls


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


def test_fetch_to_cooker_finishes_with_delayed_position_and_turning():
    """柜台前持米→补步到灶→转向→下米：验证整棵树及真实后置条件。"""
    from ai import tasks as T
    from ai.recipes import RecipeBook

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

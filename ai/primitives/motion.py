"""低频快照下的连续移动 / 短按 / 停稳观察。

松键按真实截止时间执行，补步只消费新帧。同坐标的新帧仍能触发补步，
否则站住后就会永远等惯性；同一旧帧不能连续触发四次方向输入。
"""
import math

from ai.bt import Status

STEP_SECONDS = 0.05
OBSERVE_SECONDS = 0.15
FRAME_MAX_AGE = 0.75
WAIT_MAX_SECONDS = 1.0
PROGRESS_MAX_SECONDS = 1.6
POSITION_EPSILON = 0.08


class MotionControl:
    def __init__(self):
        # type: () -> None
        self.stage = "idle"
        self._calls = 0
        self._seq = None
        self._until = None
        self._wait_since = None
        self._wait_seq = None
        self._wait_pos = None
        self._progress_since = None
        self._progress_pos = None
        self._best_error = None
        self._near = False
        self._steps = 0
        self._settle = True   # 当前/上一次 observe 是否要求停稳确认
        self._final = True    # 当前这一步是否落到目标格（决定中间步是否可免停稳）

    def _wait(self, ctx, now, seq, pos, settle=True):
        # type: (...) -> None
        ctx["hands"].set_move(None)
        self.stage = "observe"
        ctx["motion"]["phase"] = self.stage
        self._wait_since, self._wait_seq, self._wait_pos = now, seq, pos
        self._settle = settle

    def _fail(self, ctx, reason):
        # type: (dict, str) -> str
        ctx["hands"].set_move(None)
        ctx["motion_failure"] = {"reason": reason}
        self.stage = "failed"
        ctx["motion"].update(phase=self.stage, reason=reason)
        return Status.FAILURE

    def tick(self, ctx, direction, extra, near, arrived, error=None, belt=False,
             final=True):
        # type: (...) -> str
        self._calls += 1
        self._final = final
        # 未携带收帧信息的离线原语调用按理想的逐拍新观测处理；正式入口
        # 明确传 frame_seq / now，绝不拿循环 tick 当游戏帧编号。
        timed = ctx.get("frame_seq") is not None
        now = ctx.get("now", self._calls * STEP_SECONDS)
        seq = ctx.get("frame_seq") if timed else self._calls
        fresh = seq != self._seq
        self._seq = seq
        chef, hands = ctx["chef"], ctx["hands"]
        pos = (chef.get("x", 0.0), chef.get("z", 0.0))
        hands.service(now)
        ctx["motion"] = {"phase": self.stage, "frame_seq": seq,
                         "fresh": fresh, "direction": direction}
        received = ctx.get("frame_received_at")
        if received is not None and now - received > FRAME_MAX_AGE:
            return self._fail(ctx, "stale_frame")

        if self._progress_since is None:
            self._progress_since, self._progress_pos = now, pos
        if fresh:
            improved = False
            if near and error is not None:
                if not self._near or self._best_error is None or error < self._best_error - POSITION_EPSILON:
                    self._best_error = error
                    improved = True
            elif math.hypot(pos[0] - self._progress_pos[0], pos[1] - self._progress_pos[1]) > POSITION_EPSILON:
                improved = True
            if improved:
                self._progress_since, self._progress_pos = now, pos
            self._near = near

        # 离线调用给出的到位观测视为已落定，不要求测试凭空模拟物理时钟。
        if not timed and arrived:
            hands.set_move(None)
            ctx["motion"]["phase"] = "arrived"
            return Status.SUCCESS

        if self.stage == "pulse":
            if now + 1e-9 < self._until:
                return Status.RUNNING
            self._wait(ctx, now, seq, pos, settle=self._final)
            return Status.RUNNING

        if self.stage == "observe":
            hands.set_move(None)
            if now - self._wait_since > WAIT_MAX_SECONDS:
                return self._fail(ctx, "observation_timeout")
            if not fresh or seq == self._wait_seq or now - self._wait_since + 1e-9 < OBSERVE_SECONDS:
                return Status.RUNNING
            moved = math.hypot(pos[0] - self._wait_pos[0], pos[1] - self._wait_pos[1])
            self._wait_pos = pos
            # 只有「落到目标格」的最后一步才要求停稳确认（普通地面惯性消退）。
            # 中间格只是路过，exact 停点无所谓，等一个新鲜帧就继续走——
            # 每格都等停稳是近处「一顿一顿」变慢的根源（实机复现）。传送带
            # 持续有外力，不能要求绝对静止，到位后由交互按键的反向顶带兜底。
            if self._settle and moved > POSITION_EPSILON and not belt:
                return Status.RUNNING
            self.stage = "idle"

        if not fresh:
            return Status.RUNNING
        if arrived:
            if hands.move_dir is not None or self.stage == "cruise" or not self._settle:
                self._wait(ctx, now, seq, pos)
                return Status.RUNNING
            ctx["motion"]["phase"] = "arrived"
            return Status.SUCCESS

        if now - self._progress_since >= PROGRESS_MAX_SECONDS and (self._steps >= 3 or self.stage == "cruise"):
            return self._fail(ctx, "no_progress")

        if hands.move_dir is not None and near:
            # 只有震荡中的近处才先消惯性再短按。90° 拐弯即时换向，不再
            # 每拐一次就等停稳——实机：每次拐弯都停顿，legacy 即时拐弯
            # 没有这个卡顿（set_move 本身就会按差集 release 旧键 press 新键）。
            self._wait(ctx, now, seq, pos)
            return Status.RUNNING
        if near:
            if not hands.pulse_move(direction, STEP_SECONDS, extra, now,
                                    seq if timed else None):
                return Status.RUNNING
            self._until = now + STEP_SECONDS
            self._steps += 1
            self.stage = "pulse"
        else:
            if not hands.move_on_frame(direction, extra, seq if timed else None):
                return Status.RUNNING
            self.stage = "cruise"
        ctx["motion"]["phase"] = self.stage
        return Status.RUNNING

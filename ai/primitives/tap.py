"""Tap、Confirm、Settle 原语。

Tap：单击一次，边沿触发（spec §7.2）。
Confirm：后置校验，有限重试（spec §7.2、§7.5）。
Settle：等状态刷新（spec §7.5）。
"""
from typing import Any, Callable, Dict, Optional

from ai.query import predicates as P
from ai.exec.bt import Status
from ai.primitives.base import Primitive, resolve


class Tap(Primitive):
    """单击一次，边沿触发。

    为什么必须边沿触发（spec §7.2，三重原因叠加）：
      1. bt.Sequence 每拍从第 0 个子节点重新 tick，已 SUCCESS 的子节点
         照旧被 tick（见 tests/test_bt.py 的 retick 用例）。
      2. 游戏里拾取是 toggle——oc/ai/agent.py:921 注释原话"拾取是
         toggle，连点会拿起又放下"。
      3. Python 循环 20 Hz 而插件推 5 Hz，同一帧会被决策 3-4 次
         （spec §3.4），相邻拍输入完全相同。

    三者相乘的表现就是"反复拿起放下"。所以本节点自己记"这次激活期已
    按过"，标记由 reset() 清除（halt 或 task 身份变化时）。

    重试不靠反复 tick 自然重按，由 Confirm(retries) 显式控制
    （spec §7.2 规则 2）。

    带 target 时按键前做一次 interact_ready 校验：信号不对就 FAILURE，
    不瞎按——瞎按会把手里的东西放到错误的格子上。
    """

    def __init__(self, key, target=None, kind=None):
        # type: (str, Any, Optional[str]) -> None
        Primitive.__init__(self)
        self.key = key
        self.target = target
        self.kind = kind
        self._fired = False

    def reset(self):
        # type: () -> None
        if self._hands is not None and self._fired:
            self._hands.cancel_tap(self.key)
        Primitive.reset(self)
        self._fired = False

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        hands = ctx["hands"]
        if self._fired:
            return Status.RUNNING if hands.tap_active(self.key) else Status.SUCCESS

        if self.target is not None:
            st = resolve(self.target, ctx)
            if st is None:
                return Status.FAILURE
            if not P.interact_ready(ctx["chef"], st, self.kind,
                                    ctx["w"].to_grid):
                return Status.FAILURE

        # 先停移动：边滑边按会落空或串位（旧 _grab_burst 同样先停）
        hands.set_move(None)
        if not hands.tap(self.key):
            # 冷却未到（另一个节点刚按过同键）。返回 RUNNING 而不是
            # SUCCESS——谎称按成功会让 Sequence 直接走到 Confirm，
            # 然后校验失败，多绕一圈且日志误导。
            return Status.RUNNING
        self._fired = True
        return Status.RUNNING if hands.tap_active(self.key) else Status.SUCCESS


class Confirm(Primitive):
    """后置校验：确认世界真的变了（spec §7.2 / §7.5）。

    pred 是 ctx -> bool。不满足时先 RUNNING 等状态刷新（插件 5 Hz 推帧，
    刚按下那拍世界还没更新），retries 用尽才 FAILURE。

    **双条件由调用方写进 pred**。spec §7.5 第二类故障原话："只查'手空了'
    不够（可能掉地上）。今天 held_before 字段名存实亡，是漏洞。"正确的
    pred 形如 `hands_free(chef) and item in plate_contents(plate)`。
    """

    def __init__(self, pred, retries=5):
        # type: (Callable[[Dict], bool], int) -> None
        # 正式运行重试按新帧计，避免同一旧快照在 20Hz 下提前耗尽预算。
        # 未携带帧信息的离线调用保留按调用次数计重试的契约。
        Primitive.__init__(self)
        self.pred = pred
        self.retries = retries
        self._tries = 0
        self._frame_seq = None
        self._started = None

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._tries = 0
        self._frame_seq = None
        self._started = None

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        if self.pred(ctx):
            self.reset()
            return Status.SUCCESS
        if ctx.get("frame_seq") is not None:
            seq, now = ctx["frame_seq"], ctx["now"]
            if self._started is None:
                self._started, self._frame_seq = now, seq
                return Status.RUNNING
            # 重试预算按真实新观测消耗；无帧也有真实时间上限。
            if now - self._started > max(0.75, (self.retries + 1) * 0.2):
                self.reset()
                return Status.FAILURE
            if seq == self._frame_seq:
                return Status.RUNNING
            self._frame_seq = seq
        if self._tries >= self.retries:
            self.reset()
            return Status.FAILURE
        self._tries += 1
        return Status.RUNNING


class Settle(Primitive):
    """等 ticks 拍，让状态刷新。

    spec §7.5 定界：**只用于等状态刷新**（如倒料后等 contents 更新），
    不再用于等物理稳定——后者由 Approach 的松键 / 新帧观察处理。
    """

    def __init__(self, ticks):
        # type: (int) -> None
        Primitive.__init__(self)
        self.ticks = ticks
        self._left = ticks

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._left = self.ticks

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        ctx["hands"].set_move(None)
        if self._left <= 0:
            self.reset()
            return Status.SUCCESS
        self._left -= 1
        return Status.RUNNING

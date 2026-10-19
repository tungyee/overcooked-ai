"""HoldUntil + TapWhileHolding 原语。

HoldUntil：按住到条件成立。timeout 无默认值，必须显式传（spec §7.2）。
TapWhileHolding：按住同时单击——传送带顶位专用（spec §7.2）。
"""
from typing import Any, Callable, Dict, Optional

from ai.query import predicates as P
from ai.exec.bt import Status
from ai.primitives.base import Primitive, resolve


class HoldUntil(Primitive):
    """按住 key 到 pred 成立：切菜 / 喷灭火器 / 洗盘。

    timeout **无默认值，必须显式传**（spec §7.2）。给默认值等于把"这个
    动作该等多久"的领域知识藏进原语里，而切菜与喷灭火器的合理时长差一个
    量级。

    halt 必须 release（spec §7.6 纪律 2 + §11.5 "零残留按键"硬标准）：
    中断（火情、脱困）来时若不松键，按键会卡死到进程退出。这是本项目已
    踩过的坑——AI_REVIEW.md Bug#2 加 halt() 就是为了它。
    """

    def __init__(self, key, pred, timeout, target=None, kind=None):
        # type: (str, Callable[[Dict], bool], int, Any, Optional[str]) -> None
        Primitive.__init__(self)
        self.key = key
        self.pred = pred
        self.timeout = timeout
        self.target = target
        self.kind = kind
        self._held = False

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._held = False

    def _release(self, ctx=None):
        # type: (Optional[Dict]) -> None
        hands = (ctx or {}).get("hands") if ctx else self._hands
        if hands is not None and self._held:
            hands.release(self.key)
        self._held = False

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        hands = ctx["hands"]
        if self.pred(ctx):
            self._release(ctx)
            self.reset()
            return Status.SUCCESS
        # 位置校验（可选）：人被推离目标 → 松键 FAILURE，让 Sequence 重置
        # 重新 Approach。否则会空按着 key 在错误位置死等 done（实机复现：
        # 切菜时被传送带推走，HoldUntil 不松键、done 永不成立 → 卡死）。
        if self.target is not None:
            st = resolve(self.target, ctx)
            if st is None or not P.interact_ready(
                    ctx["chef"], st, self.kind, ctx["w"].to_grid):
                self._release(ctx)
                self.reset()
                return Status.FAILURE
        if self._ticks > self.timeout:
            self._release(ctx)
            self.reset()
            return Status.FAILURE
        hands.set_move(None)      # 按住干活时不该还在移动
        hands.hold(self.key)
        self._held = True
        return Status.RUNNING

    def halt(self):
        # type: () -> None
        self._release()
        Primitive.halt(self)


class TapWhileHolding(Primitive):
    """按住 hold_key 的同时单击 tap_key —— 传送带顶位专用。

    spec §7.2 原话："在传送带格上拾取时，必须**同时按住反向键顶住**，
    否则角色会被传送带推走。现有 _grab_burst（oc/ai/agent.py:920）+
    _belt_counter（:947，计算反向键）就是干这个的，而本关 Sushi_1_4_2P
    有传送带——这是实跑踩出来的坑，不是理论问题。"

    Tap(key, hold) 的两参数表达不了"按住 A 的同时点 B"，故单列一个原语。

    hold_key 可以是字符串或 ctx -> Optional[str] 的可调用（从
    hands.counter_dir 算）。解析为 None 时退化为普通单击——不在传送带上
    就不需要顶。

    与 Tap 一样边沿触发（同样的 toggle 问题），halt 松开顶带键。
    """

    def __init__(self, tap_key, hold_key, target=None, kind=None):
        # type: (str, Any, Any, Optional[str]) -> None
        Primitive.__init__(self)
        self.tap_key = tap_key
        self.hold_key = hold_key
        self.target = target
        self.kind = kind
        self._fired = False
        self._holding = None   # type: Optional[str]

    def reset(self):
        # type: () -> None
        if self._hands is not None and self._fired:
            self._hands.cancel_tap(self.tap_key)
        if self._hands is not None and self._holding:
            self._hands.release(self._holding)
        Primitive.reset(self)
        self._fired = False
        self._holding = None

    def _resolve_hold(self, ctx):
        # type: (Dict) -> Optional[str]
        hk = self.hold_key(ctx) if callable(self.hold_key) else self.hold_key
        return hk or None

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        hands = ctx["hands"]
        if self._fired:
            if hands.tap_active(self.tap_key):
                return Status.RUNNING
            if self._holding:
                hands.release(self._holding)
                self._holding = None
            return Status.SUCCESS

        if self.target is not None:
            st = resolve(self.target, ctx)
            if st is None:
                return Status.FAILURE
            if not P.interact_ready(ctx["chef"], st, self.kind,
                                    ctx["w"].to_grid):
                return Status.FAILURE

        hands.set_move(None)      # 先停移动，专心顶带 + 拿（同 _grab_burst）
        hk = self._resolve_hold(ctx)
        if hk:
            hands.hold(hk)
            self._holding = hk
        try:
            pressed = hands.tap(self.tap_key)
        except BaseException:
            self.reset()
            raise
        if not pressed:
            self.reset()
            return Status.RUNNING
        self._fired = True
        if hands.tap_active(self.tap_key):
            # 真实按键现在非阻塞，顶带必须覆盖完整的按下时段。
            if self._holding:
                hands.schedule_hold_release(self._holding)
            return Status.RUNNING
        if self._holding:
            hands.release(self._holding)
            self._holding = None
        return Status.SUCCESS

    def halt(self):
        # type: () -> None
        if self._hands is not None and self._holding:
            self._hands.release(self._holding)
            self._holding = None
        Primitive.halt(self)


class WaitUntil(Primitive):
    """等到世界谓词成立才 SUCCESS，期间不按任何键（timeout 拍数兜底）。

    与 HoldUntil 的区别：**不按键**，纯等待。用于「端盘在锅前等饭熟」这类
    需要等一个可观测状态、但此刻不该有任何按键输出的场景（等熟期间乱按
    interact 会把手里的盘/锅弄丢或触发无关动作）。

    timeout **无默认值，必须显式传**——等待时长是领域知识（等熟 vs 等状态
    刷新差一个量级）。超时交还 Planner，而不是无限等一个可能永远不成立的状态。
    """

    def __init__(self, pred, timeout):
        # type: (Callable[[Dict], bool], int) -> None
        Primitive.__init__(self)
        self.pred = pred
        self.timeout = timeout

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        if self.pred(ctx):
            self.reset()
            return Status.SUCCESS
        if self._ticks > self.timeout:
            self.reset()
            return Status.FAILURE
        ctx["hands"].set_move(None)
        return Status.RUNNING

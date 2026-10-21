"""HoldUntil + TapWhileHolding + WaitUntil 原语。

HoldUntil：按住到条件成立（委托后端：发 hold 命令，语义早停）。timeout 无默认值。
TapWhileHolding：按住的同时单击——传送带顶位专用（委托后端：hold_dir 并入 tap 命令）。
WaitUntil：等到世界谓词成立才 SUCCESS，期间不发命令。
"""
from typing import Any, Callable, Dict, Optional

from ai.exec.bt import Status
from ai.exec.commands import build_command
from ai.primitives.base import Primitive, resolve, handle_desc, target_world, target_grid_callbacks
from ai.primitives.tap import TAP_TIMEOUT_MS


class HoldUntil(Primitive):
    """按住 key 到 pred 成立：切菜 / 喷灭火器 / 洗盘。

    timeout **无默认值，必须显式传**（spec §7.2）。给默认值等于把"这个
    动作该等多久"的领域知识藏进原语里，而切菜与喷灭火器的合理时长差一个
    量级。

    委托后端下 pred 成立即语义早停：C# HoldCommand 不做 pred，只负责按住 +
    位置重验 + timeout FAILURE，所以语义完成由 Python 侧判 pred 后发 halt。
    """

    def __init__(self, key, pred, timeout, target=None, kind=None):
        # type: (str, Callable[[Dict], bool], int, Any, Optional[str]) -> None
        Primitive.__init__(self)
        self.key = key
        self.pred = pred
        self.timeout = timeout
        self.target = target
        self.kind = kind

    def reset(self):
        # type: () -> None
        Primitive.reset(self)

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        return self._tick_delegated(ctx, ctx["exec"])

    def _tick_delegated(self, ctx, ex):
        # type: (Dict, Any) -> str
        # 语义早停：切/洗/喷的 done 就写在 self.pred 里，pred 成立即松键 SUCCESS
        #（C# HoldCommand 不做 pred，只负责按住 + 位置重验 + timeout FAILURE）。
        if self.pred(ctx):
            if self._pending_cmd is not None:
                self._exec_halt()
            return Status.SUCCESS
        if self._pending_cmd is None:
            st = resolve(self.target, ctx) if self.target is not None else None
            if self.target is not None and st is None:
                return Status.FAILURE
            cmd = build_command(
                "hold",
                target=handle_desc(st, target_grid_callbacks(ctx["w"], st)[0]),
                kind=self.kind,
                target_world=target_world(st),
                timeout_ms=self.timeout * 50,
            )
            return self._begin_exec(ctx, ex, cmd)
        return self._poll_exec(ctx, ex)


class TapWhileHolding(Primitive):
    """按住 hold_key 的同时单击 tap_key —— 传送带顶位专用。

    spec §7.2 原话："在传送带格上拾取时，必须**同时按住反向键顶住**，
    否则角色会被传送带推走。"

    hold_key 可以是字符串或 ctx -> Optional[str] 的可调用（从 counter_dir
    算）。解析为 None 时退化为普通单击——不在传送带上就不需要顶。

    委托后端下 hold_dir 并入 tap 命令，由插件同时按住顶带键并单击。
    """

    def __init__(self, tap_key, hold_key, target=None, kind=None):
        # type: (str, Any, Any, Optional[str]) -> None
        Primitive.__init__(self)
        self.tap_key = tap_key
        self.hold_key = hold_key
        self.target = target
        self.kind = kind
        self._fired = False

    def reset(self):
        # type: () -> None
        Primitive.reset(self)
        self._fired = False

    def _resolve_hold(self, ctx):
        # type: (Dict) -> Optional[str]
        hk = self.hold_key(ctx) if callable(self.hold_key) else self.hold_key
        return hk or None

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        return self._tick_delegated(ctx, ctx["exec"])

    def _tick_delegated(self, ctx, ex):
        # type: (Dict, Any) -> str
        if self._pending_cmd is None:
            st = resolve(self.target, ctx) if self.target is not None else None
            if self.target is not None and st is None:
                return Status.FAILURE
            cmd = build_command(
                "tap",
                target=handle_desc(st, target_grid_callbacks(ctx["w"], st)[0]),
                kind=self.kind,
                hold_dir=self._resolve_hold(ctx),
                target_world=target_world(st),
                timeout_ms=TAP_TIMEOUT_MS,
            )
            s = self._begin_exec(ctx, ex, cmd)
            if s == Status.RUNNING:
                self._fired = True   # 已发出按键命令，之后手部变化是预期的
            return s
        return self._poll_exec(ctx, ex)


class WaitUntil(Primitive):
    """等到世界谓词成立才 SUCCESS，期间不发任何命令（timeout 拍数兜底）。

    与 HoldUntil 的区别：**不按键**，纯等待。用于「端盘在锅前等饭熟」这类
    需要等一个可观测状态、但此刻不该有任何输出的场景。

    timeout **无默认值，必须显式传**。
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
        return Status.RUNNING

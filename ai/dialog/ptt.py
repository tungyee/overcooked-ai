"""全局按键 PTT（push-to-talk）监听：按住录音、松开发送。

用 Quartz 的 listen-only event tap 监听指定键的 keyDown/keyUp，不拦截
事件，所以游戏/Steam Input 仍能收到同一个按键。事件只放进线程安全队列，
由 DialogWorker 在自己的线程里消费；本模块不碰音频。

macOS 需要给启动终端/IDE「辅助功能」权限（与按键注入相同）；如果系统
额外要求「输入监控」，授权后需重启进程。
"""
import queue
import threading


# macOS 虚拟键码（Carbon/HIToolbox）。
KEYCODES = {
    "f1": 122, "f2": 120, "f3": 99, "f4": 118,
    "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "right_option": 61, "right_shift": 60, "right_control": 62,
}


def keycode_for(name):
    # type: (str) -> int
    """把 `f8` / `right_option` / 数字键码转成 macOS keycode。"""
    key = str(name or "").strip().lower()
    if not key:
        raise ValueError("PTT 键名为空")
    if key in KEYCODES:
        return KEYCODES[key]
    try:
        return int(key)
    except ValueError:
        raise ValueError(
            "未知 PTT 键 %r；可用：%s 或数字 keycode"
            % (name, ", ".join(sorted(KEYCODES))))


class PushToTalk(object):
    """监听一个全局按键，`poll()` 返回 "down"/"up" 事件。

    `tap_factory` 仅用于测试注入；默认使用 Quartz.CGEventTapCreate。
    `start()` 建立 event tap 并启动 run loop；失败时抛 RuntimeError，
    调用方应退回 VAD。
    """

    def __init__(self, keycode, tap_factory=None, ready_timeout=3.0):
        # type: (int, object, float) -> None
        self.keycode = int(keycode)
        self._tap_factory = tap_factory
        self._ready_timeout = float(ready_timeout)
        self._events = queue.Queue()
        self._ready = threading.Event()
        self._thread = None  # type: object
        self._loop = None
        self._tap = None
        self._error = None
        self._closed = False

    def start(self):
        # type: () -> None
        """启动监听；event tap 建不起来时抛 RuntimeError。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._closed = False
        self._error = None
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run, name="dialog-ptt", daemon=True)
        self._thread.start()
        if not self._ready.wait(self._ready_timeout):
            raise RuntimeError(
                "PTT 按键监听启动超时（检查辅助功能/输入监控权限）")
        if self._error:
            raise RuntimeError(self._error)

    def poll(self):
        # type: () -> list
        """取出自上次调用以来的按键事件（"down"/"up"）。"""
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    def stop(self):
        # type: () -> None
        """停止 run loop 并释放 event tap（可重复调用）。"""
        self._closed = True
        loop = self._loop
        tap = self._tap
        if loop is not None:
            try:
                import Quartz
                Quartz.CFRunLoopStop(loop)
            except Exception:
                pass
        if tap is not None:
            try:
                import Quartz
                Quartz.CGEventTapEnable(tap, False)
            except Exception:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(1.0)
        self._thread = None
        self._loop = None
        self._tap = None

    def _run(self):
        # type: () -> None
        try:
            import Quartz
        except Exception as exc:                # pragma: no cover - 缺 pyobjc
            self._error = "PTT 需要 pyobjc/Quartz：%s" % exc
            self._ready.set()
            return

        def callback(proxy, event_type, event, refcon):
            if event_type == Quartz.kCGEventTapDisabledByTimeout:
                if self._tap is not None:
                    Quartz.CGEventTapEnable(self._tap, True)
                return event
            try:
                keycode = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventKeycode)
            except Exception:
                return event
            if keycode != self.keycode:
                return event
            if event_type == Quartz.kCGEventKeyDown:
                repeat = Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventAutorepeat)
                if not repeat:
                    self._events.put("down")
            elif event_type == Quartz.kCGEventKeyUp:
                self._events.put("up")
            return event

        try:
            mask = ((1 << Quartz.kCGEventKeyDown)
                    | (1 << Quartz.kCGEventKeyUp))
            factory = self._tap_factory or Quartz.CGEventTapCreate
            self._tap = factory(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly,
                mask,
                callback,
                None,
            )
            if self._tap is None:
                self._error = (
                    "无法创建 PTT 按键监听：需要在系统设置里给终端/IDE "
                    "「辅助功能」（必要时还有「输入监控」）权限")
                self._ready.set()
                return
            source = Quartz.CFMachPortCreateRunLoopSource(
                None, self._tap, 0)
            self._loop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(
                self._loop, source, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(self._tap, True)
            self._ready.set()
            Quartz.CFRunLoopRun()
        except Exception as exc:                # noqa: BLE001
            self._error = "PTT 按键监听失败：%s" % (exc,)
            self._ready.set()

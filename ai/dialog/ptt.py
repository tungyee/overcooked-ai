"""全局按键 PTT（push-to-talk）监听：按住录音、松开发送。

macOS 用 Quartz listen-only event tap，Windows 用 GetAsyncKeyState
只读轮询指定键，不拦截事件，游戏/Steam Input 仍能收到同一个按键。事件只放进线程安全队列，
由 DialogWorker 在自己的线程里消费；本模块不碰音频。

macOS 的语音热键监听需要给启动终端/IDE「辅助功能」权限；如果系统
额外要求「输入监控」，授权后需重启进程。
"""
import queue
import threading
import sys


# macOS 虚拟键码（Carbon/HIToolbox）。
KEYCODES = {
    "f1": 122, "f2": 120, "f3": 99, "f4": 118,
    "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "right_option": 61, "right_shift": 60, "right_control": 62,
}
WINDOWS_KEYCODES = dict(('f%d' % n, 0x6f+n) for n in range(1, 13))
WINDOWS_KEYCODES.update(right_option=0xa5, right_shift=0xa1, right_control=0xa3)
WINDOWS_KEYCODES['right_alt'] = 0xa5
# 本机 PTT 使用普通键，避免 Alt/Ctrl/Shift 与游戏输入组合成系统快捷键。
WINDOWS_KEYCODES['caps_lock'] = 0x14


def keycode_for(name, platform=None):
    # type: (str, object) -> int
    """按当前平台解析键名；Windows 使用虚拟键码，macOS 使用 Carbon。"""
    codes = WINDOWS_KEYCODES if (platform or sys.platform) == 'win32' else KEYCODES
    key = str(name or "").strip().lower()
    if not key:
        raise ValueError("PTT 键名为空")
    if key in codes:
        return codes[key]
    try:
        return int(key)
    except ValueError:
        raise ValueError(
            "未知 PTT 键 %r；可用：%s 或数字 keycode"
            % (name, ", ".join(sorted(codes))))


class PushToTalk(object):
    """监听一个全局按键，`poll()` 返回 "down"/"up" 事件。

    `tap_factory` / `state_reader` 仅用于测试注入。
    `start()` 启动平台监听线程；失败时抛 RuntimeError，
    调用方应退回 VAD。
    """

    def __init__(self, keycode, tap_factory=None, ready_timeout=3.0,
                 platform=None, state_reader=None):
        # type: (int, object, float, object, object) -> None
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
        self._platform = platform or sys.platform
        self._state_reader = state_reader
        self._stop_event = threading.Event()

    def start(self):
        # type: () -> None
        """启动监听；event tap 建不起来时抛 RuntimeError。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._closed = False
        self._error = None
        self._ready.clear()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="dialog-ptt", daemon=True)
        self._thread.start()
        if not self._ready.wait(self._ready_timeout):
            self.stop()
            raise RuntimeError(
                "PTT 按键监听启动超时；macOS 请检查辅助功能/输入监控权限")
        if self._error:
            error = self._error
            self.stop()
            raise RuntimeError(error)

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
        self._stop_event.set()
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
        if self._platform == 'win32':
            self._run_windows()
            return
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

    def _run_windows(self):
        # type: () -> None
        """只读按键高位，去除重复 down；不拦截、不注入任何游戏按键。"""
        try:
            read = self._state_reader
            if read is None:
                import ctypes
                from ctypes import wintypes
                read = ctypes.WinDLL('user32', use_last_error=True).GetAsyncKeyState
                read.argtypes = [wintypes.INT]
                read.restype = wintypes.SHORT
            pressed = False
            while not self._stop_event.is_set():
                down = bool(read(self.keycode) & 0x8000)
                if down != pressed:
                    self._events.put('down' if down else 'up')
                    pressed = down
                self._ready.set()
                self._stop_event.wait(.01)
        except Exception as exc:
            self._error = 'PTT Windows 按键监听失败：%s' % exc
            self._ready.set()

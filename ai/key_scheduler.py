"""按键截止时间：定时松键独立于规划耗时，不把 50ms 短按拖成一整格。

send / timer 注入，测试无需 Quartz。锁与代际令牌防止旧定时器在抢占后
松掉新的按键；所有真实事件仍由 controller 这一出口发送。
"""
import threading


class DeadlineKeys:
    def __init__(self, send, timer=threading.Timer):
        # type: (...) -> None
        self._send = send
        self._timer = timer
        self._lock = threading.RLock()
        self._pressed = set()
        self._pending = {}

    def _cancel(self, key):
        # type: (str) -> None
        pending = self._pending.pop(key, None)
        if pending is not None:
            pending[1].cancel()

    def press(self, key):
        # type: (str) -> None
        with self._lock:
            self._cancel(key)
            if key not in self._pressed:
                self._send(key, True)
                self._pressed.add(key)

    def release(self, key):
        # type: (str) -> None
        with self._lock:
            self._cancel(key)
            if key in self._pressed:
                self._send(key, False)
                self._pressed.discard(key)

    def schedule_release(self, key, seconds):
        # type: (str, float) -> None
        with self._lock:
            self._cancel(key)
            token = object()

            def finish():
                with self._lock:
                    pending = self._pending.get(key)
                    if pending is None or pending[0] is not token:
                        return
                    del self._pending[key]
                    self._send(key, False)
                    self._pressed.discard(key)

            timer = self._timer(seconds, finish)
            timer.daemon = True
            self._pending[key] = (token, timer)
            timer.start()

    def tap(self, key, seconds=0.12):
        # type: (str, float) -> None
        with self._lock:
            self.press(key)
            self.schedule_release(key, seconds)

    def tapping(self, key):
        # type: (str) -> bool
        with self._lock:
            return key in self._pending

    def release_all(self, keys):
        # type: (...) -> None
        with self._lock:
            for key in list(self._pending):
                self._cancel(key)
            # 连启动时未知的游戏按键状态也清掉，沿袭 controller 的契约。
            for key in keys:
                self._send(key, False)
            self._pressed.clear()

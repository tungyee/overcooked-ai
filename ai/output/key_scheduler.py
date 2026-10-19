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

    def release_all(self, keys=None):
        # type: (...) -> None
        """松开本对象实际按下的键并取消未完成的定时松键（幂等）。

        主循环在暂停 / 非关卡 / 断流的空闲守卫里每拍都会调
        Hands.release_all → 这里。旧实现会对 keys 里的每个键都补发一次
        key-up，于是选关页人类玩家按住的同一批方向键被反复「抬起」，
        移动车一顿一顿（实机）。现在只松自己真正按下的键（_pressed），
        重复调用是空操作，不再打扰人类输入。
        """
        with self._lock:
            for key in list(self._pending):
                self._cancel(key)
            for key in list(self._pressed):
                self._send(key, False)
            self._pressed.clear()

    def release_all_hard(self, keys):
        # type: (...) -> None
        """一次性清掉可能残留的按键状态（上一进程崩溃时留住的键）。

        只应在 Controller.__init__ 启动时调一次：这里对 keys 里的每个键都
        补发 key-up，是给「上个进程没来得及松键就死了」的兜底，不能进每拍
        路径（否则又会把人类玩家按住的键反复抬起，见 release_all）。
        """
        with self._lock:
            for key in list(self._pending):
                self._cancel(key)
            for key in keys:
                self._send(key, False)
            self._pressed.clear()

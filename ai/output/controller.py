# -*- coding: utf-8 -*-
"""P2 控制注入：macOS CGEvent 键盘事件（方案 A）。

从 oc/ai/controller.py 移植。spec §8.2 把 controller.py 记为"不动"，但
它此前只存在于 legacy `oc/` 目录，从未进入重构后的 `oc2/ai/`——新架构的
`ai/hands.py` 需要一个 controller（`Hands(ctrl)`），默认的 `NullController`
只决策不按键。这里把它补上、接口对齐 `Hands`。

接口（与 ai/hands.py 约定一致）：
  - `press(name)` / `release(name)`：非阻塞按住 / 松开（移动、切菜、喷火）
  - `tap(name, hold)`：单击一次
  - `release_all()`：抬起所有键，防卡键

键位沿用 legacy 项目实机验证过的 P2 分屏键位。
"""
import subprocess
import sys

if sys.platform == 'darwin':
    import Quartz

from ai.output.key_scheduler import DeadlineKeys

MAC_KEY = {
    "up": 126,
    "down": 125,
    "left": 123,
    "right": 124,
    "pickup": 60,
    "chop": 44,     # P2 分屏：切菜 / 洗盘 / 喷火同键（WorkstationInteract = /）
    "dash": 61,     # P2 分屏：冲刺（LogicalButtonID.Dash = 右 Alt / 右 Option）
}
KEY = MAC_KEY if sys.platform == 'darwin' else {
    'up': 'up', 'down': 'down', 'left': 'left', 'right': 'right',
    'pickup': 'right_shift', 'chop': 'right_ctrl',
}


class Controller(object):
    """CGEvent 键盘注入。构造时先松全部键并聚焦游戏窗口。"""

    def __init__(self, bindings=None):
        self._windows = None
        if sys.platform == 'win32':
            from ai.output.windows_input import WindowsInput
            self._windows = WindowsInput(bindings)
            # 本机右 Alt 会干扰 P1，Windows 暂不发送冲刺键。
            send = lambda name, down: self._windows.send(name, down) if name != 'dash' else None
        elif sys.platform == 'darwin':
            send = lambda name, down: self._key(MAC_KEY[name], down)
        else:
            raise RuntimeError('真实输入支持 Windows/macOS；其他平台使用 --dry-run')
        self._keys = DeadlineKeys(send)
        if self._windows is None:
            self.release_all_hard()
        self.focus_game()

    def focus_game(self):
        if self._windows is not None:
            return self._windows.focus_game()
        # 让游戏窗口成为前台，否则注入的键进不到游戏
        try:
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to set frontmost of '
                 'first process whose name is "Overcooked2" to true'],
                check=False)
        except Exception:
            pass

    def is_game_foreground(self):
        # type: () -> bool
        return self._windows is None or self._windows.is_game_foreground()

    def _key(self, kc, down):
        ev = Quartz.CGEventCreateKeyboardEvent(None, kc, down)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    def tap(self, name, hold=0.12):
        """非阻塞单击；即使规划超时，后台截止时间也会松键。"""
        self._keys.tap(name, hold)

    def tapping(self, name):
        # type: (str) -> bool
        return self._keys.tapping(name)

    def schedule_release(self, name, seconds):
        # type: (str, float) -> None
        self._keys.schedule_release(name, seconds)

    def hold(self, name, seconds):
        """定时按住后自动松开，不阻塞控制循环。"""
        self._keys.tap(name, seconds)

    def press(self, name):
        """按下不放（非阻塞），需配合 release() 使用。"""
        self._keys.press(name)

    def release(self, name):
        self._keys.release(name)

    def release_all(self):
        """抬起本进程实际按下的所有键（幂等，防卡键）。"""
        self._keys.release_all()

    def release_all_hard(self):
        """启动时一次性抬起所有 P2 键，清掉上一进程可能残留的按键。"""
        self._keys.release_all_hard(KEY)

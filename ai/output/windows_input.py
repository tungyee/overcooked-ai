# -*- coding: utf-8 -*-
"""Windows Unity 键盘输入：扫描码与前台窗口守卫，无第三方依赖。"""
import ctypes
import os
from ctypes import wintypes


SCAN_CODES = {
    "up": (0x48, True), "down": (0x50, True),
    "left": (0x4B, True), "right": (0x4D, True),
    "right_ctrl": (0x1D, True), "right_shift": (0x36, False),
    "right_alt": (0x38, True),
    "slash": (0x35, False), "enter": (0x1C, False),
    "space": (0x39, False),
}
DEFAULT_BINDINGS = {
    "up": "up", "down": "down", "left": "left", "right": "right",
    "pickup": "right_shift", "chop": "right_ctrl",
}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("payload",)
    _fields_ = [("type", wintypes.DWORD), ("payload", INPUT_UNION)]


def keyboard_event(key, down):
    # type: (str, bool) -> INPUT
    scan, extended = SCAN_CODES[key]
    flags = 0x0008 | (0x0001 if extended else 0) | (0 if down else 0x0002)
    return INPUT(type=1, ki=KEYBDINPUT(0, scan, flags, 0, 0))


class WindowsInput:
    def __init__(self, bindings=None):
        # type: (...) -> None
        self.bindings = dict(DEFAULT_BINDINGS)
        self.bindings.update(bindings or {})
        for key in self.bindings.values():
            if key not in SCAN_CODES:
                raise ValueError("不支持的 Windows 键位: %s" % key)
        if len(set(self.bindings.values())) != len(self.bindings):
            raise ValueError("P2 各动作不能使用重复键位")
        self._down = set()
        self._hwnd = None
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()
        hwnd = self._find_game()
        if hwnd:
            from ai.output.windows_privileges import integrity_report
            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if integrity_report(pid.value)["requires_elevation"]:
                raise PermissionError("游戏权限高于 AI，Windows UIPI 会阻止输入。请用 start_windows.cmd 自动提升 AI 权限。")

    def _configure_api(self):
        # type: () -> None
        self._enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                                wintypes.LPARAM)
        signatures = [
            (self.user32.SendInput, [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int], wintypes.UINT),
            (self.user32.GetForegroundWindow, [], wintypes.HWND),
            (self.user32.IsWindow, [wintypes.HWND], wintypes.BOOL),
            (self.user32.IsWindowVisible, [wintypes.HWND], wintypes.BOOL),
            (self.user32.GetWindowThreadProcessId, [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            (self.user32.EnumWindows, [self._enum_callback, wintypes.LPARAM], wintypes.BOOL),
            (self.user32.ShowWindow, [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            (self.user32.SetForegroundWindow, [wintypes.HWND], wintypes.BOOL),
            (self.kernel32.OpenProcess, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            (self.kernel32.QueryFullProcessImageNameW, [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            (self.kernel32.CloseHandle, [wintypes.HANDLE], wintypes.BOOL),
        ]
        for function, args, result in signatures:
            function.argtypes = args
            function.restype = result

    def _is_game_window(self, hwnd):
        # type: (...) -> bool
        if not self.user32.IsWindowVisible(hwnd):
            return False
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = self.kernel32.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return False
        try:
            path = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(path))
            return bool(self.kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size))
                        and os.path.basename(path.value).lower() == "overcooked2.exe")
        finally:
            self.kernel32.CloseHandle(handle)

    def _find_game(self):
        # type: () -> object
        if self._hwnd and self.user32.IsWindow(self._hwnd):
            return self._hwnd
        self._hwnd = None

        @self._enum_callback
        def visit(hwnd, unused):
            if self._is_game_window(hwnd):
                self._hwnd = hwnd
                return False
            return True

        self.user32.EnumWindows(visit, 0)
        return self._hwnd

    def is_game_foreground(self):
        # type: () -> bool
        hwnd = self._find_game()
        return bool(hwnd and self.user32.GetForegroundWindow() == hwnd)

    def focus_game(self):
        # type: () -> bool
        hwnd = self._find_game()
        if not hwnd:
            return False
        self.user32.ShowWindow(hwnd, 9)
        self.user32.SetForegroundWindow(hwnd)
        return self.is_game_foreground()

    def send(self, name, down):
        # type: (str, bool) -> None
        # 松键只清理由本进程发出的键，启动/菜单空闲时不干扰用户按键。
        if down and not self.is_game_foreground():
            return
        if not down and name not in self._down:
            return
        event = keyboard_event(self.bindings[name], down)
        if self.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) != 1:
            raise OSError(ctypes.get_last_error(), "SendInput 失败；游戏与 AI 应使用相同权限运行")
        if down:
            self._down.add(name)
        else:
            self._down.discard(name)

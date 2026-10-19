"""按键输出：跨拍按住的移动方向 + Tap 去抖与冷却。

为什么单列一层：按键是**跨拍状态**（按住哪个方向、上次 tap 在哪一拍），
而原语是每拍重建的树节点，塞进原语就记不住"我刚按过"。旧实现把它放在
Agent 上（oc/ai/agent.py:1163 的 _set_move / move_dir / move_extra），
那是 §2.2 批判的 God Object 的一部分。

controller 注入（不自己 import）：离线测试传一个记录调用的假 controller
就能断言键序列，不碰 Quartz。Plan 2 的交付标准之一是"不注入按键"。

Tap 去抖的由来（spec §7.2）：游戏里拾取是 toggle（oc/ai/agent.py:921
注释"拾取是 toggle，连点会拿起又放下"），而 bt.Sequence 每拍从头 tick、
已 SUCCESS 的子节点照旧被 tick。两者相乘就是"反复拿起放下"。

**重要：set_move 的实现故意偏离旧实现。** 旧代码（legacy）在改变
main 方向或 extra 时，即使其中一个未变也照样会 release 和 re-press，导致
未变的方向键看到一次虚假的抬起（"移动一顿一顿"）。本模块的存在理由正是
spec §7.2 说的"别让游戏看到虚假的抬起"，所以改用集合差集：只按需改变键
状态，未变的方向永远不重按。
"""
import time
from typing import Dict, Optional, Set, Tuple

DIR2D = {"left": (-1, 0), "right": (1, 0), "up": (0, 1), "down": (0, -1)}
OPPOSITE = {"left": "right", "right": "left", "up": "down", "down": "up"}

# 同一按键两次 tap 的最小间隔（拍）。20 Hz 下 3 拍 = 150 ms，
# 够插件推一帧新状态过来（5 Hz = 200 ms）判断上次 tap 有没有生效。
TAP_COOLDOWN_TICKS = 3


class Hands(object):
    """按键输出的唯一出口。原语通过 ctx["hands"] 调它。"""

    def __init__(self, ctrl):
        # type: (object) -> None
        self.ctrl = ctrl
        self.move_dir = None     # type: Optional[str]
        self.move_extra = None   # type: Optional[str]
        self.move_dash = False   # type: bool   # 长距离巡航时按住冲刺
        self.held_keys = set()   # type: Set[str]
        self._tick = 0
        self._last_tap = {}      # type: Dict[str, int]
        self._move_until = None  # type: Optional[float]
        self._motion_frame = None
        self._tapped = set()     # type: Set[str]  # 本拍 tap 过的键（给 trace）

    # ---------- 拍推进 ----------
    def tick(self):
        # type: () -> None
        """拍号 +1。主循环每拍调一次，冷却靠它推进。"""
        self._tick += 1
        self._tapped = set()     # tap 只属于按下的那一拍
        self.service()

    def service(self, now=None):
        # type: (Optional[float]) -> None
        """旧帧期间也处理松键；真实 controller 另有独立定时器兜住慢拍。"""
        now = time.monotonic() if now is None else now
        if self._move_until is not None and now + 1e-9 >= self._move_until:
            self.set_move(None)

    def pulse_move(self, d, seconds=0.05, extra=None, now=None, frame_seq=None, dash=False):
        # type: (str, float, Optional[str], Optional[float], Optional[int], bool) -> bool
        # 任务抢占会重建原语，但不能让同一旧帧又发起一次短按。
        if frame_seq is not None and frame_seq == self._motion_frame:
            return False
        self.set_move(d, extra, dash)
        now = time.monotonic() if now is None else now
        self._move_until = now + seconds
        schedule = getattr(self.ctrl, "schedule_release", None)
        if callable(schedule):
            for key in set(k for k in (d, extra) if k):
                schedule(key, seconds)
        self._motion_frame = frame_seq
        return True

    def move_on_frame(self, d, extra=None, frame_seq=None, dash=False):
        # type: (str, Optional[str], Optional[int], bool) -> bool
        if frame_seq is not None and frame_seq == self._motion_frame:
            return False
        self.set_move(d, extra, dash)
        self._motion_frame = frame_seq
        return True

    # ---------- 移动 ----------
    def set_move(self, d, extra=None, dash=False):
        # type: (Optional[str], Optional[str], bool) -> None
        """切换持续按住的移动方向；d=None 松开。extra 为顶带用的第二方向。
        dash 为冲刺（长距离巡航按住、近处精确站位松开）。

        只改变实际需要改变的键，未变的方向永远不重按——按住是持续态，
        重按会让游戏侧看到一次抬起，表现为移动一顿一顿。
        """
        # 显式改成持续移动时撤销前一次短按，旧截止时间不能松掉新方向。
        if self._move_until is not None and d is not None:
            for key in set(k for k in (self.move_dir, self.move_extra) if k):
                self.ctrl.release(key)
            self.move_dir = self.move_extra = None
        self._move_until = None
        if d == self.move_dir and extra == self.move_extra and dash == self.move_dash:
            return
        cur = set(k for k in (self.move_dir, self.move_extra, "dash" if self.move_dash else None) if k)
        want = set(k for k in (d, extra, "dash" if dash else None) if k)
        for k in sorted(cur - want):
            self.ctrl.release(k)
        for k in sorted(want - cur):
            self.ctrl.press(k)
        self.move_dir = d
        self.move_extra = extra
        self.move_dash = dash

    def counter_dir(self, belt_dir, cell):
        # type: (Dict[Tuple[int, int], Tuple[int, int]], Tuple[int, int]) -> Optional[str]
        """站在传送带上要顶住的反向键；不在带上则 None。"""
        bd = (belt_dir or {}).get(cell)
        if not bd:
            return None
        for name, vec in DIR2D.items():
            if vec[0] == -bd[0] and vec[1] == -bd[1]:
                return name
        return None

    # ---------- 单击 ----------
    def tap(self, key):
        # type: (str) -> bool
        """单击一次。冷却未到则不按键并返回 False。

        冷却按**键**计，不是全局：按了 pickup 不该挡住 chop。
        """
        last = self._last_tap.get(key)
        if last is not None and self._tick - last < TAP_COOLDOWN_TICKS:
            return False
        self.ctrl.tap(key)
        self._last_tap[key] = self._tick
        self._tapped.add(key)
        return True

    def tap_active(self, key):
        # type: (str) -> bool
        active = getattr(self.ctrl, "tapping", None)
        return bool(active(key)) if callable(active) else False

    # ---------- 观测（给 trace） ----------
    def active_keys(self):
        # type: () -> list
        """本拍所有按键：移动方向 + 冲刺 + 按住的动作键 + 本拍 tap 过的键。

        trace 过去只记 move_dir/move_extra，动作键一个都没记，于是「喷灭火
        器那 50 拍到底按住 chop 了没」这种问题无法从 trace 回答，只能猜
        （trace-20261014-011202 t=2477~2526：火没灭，keys 全空）。排查成本
        远高于这几行，所以把按键观测补全。

        tap 是瞬时的（按下即松），只在它发生的那一拍出现；hold 是持续态，
        跨拍都报。两者混在一个列表里按名字去重——复盘只关心「这一拍有没有
        这个键的输入」。
        """
        keys = set(self.held_keys)
        if self.move_dir:
            keys.add(self.move_dir)
        if self.move_extra:
            keys.add(self.move_extra)
        if self.move_dash:
            keys.add("dash")
        keys |= self._tapped
        return sorted(keys)

    def cancel_tap(self, key):
        # type: (str) -> None
        if self.tap_active(key):
            self.ctrl.release(key)

    def schedule_hold_release(self, key, seconds=0.12):
        # type: (str, float) -> None
        schedule = getattr(self.ctrl, "schedule_release", None)
        if callable(schedule):
            schedule(key, seconds)

    # ---------- 按住 ----------
    def hold(self, key):
        # type: (str) -> None
        """按下不放。已按住则直接返回（重按会让游戏看到一次抬起）。"""
        if key in self.held_keys:
            return
        self.ctrl.press(key)
        self.held_keys.add(key)

    def release(self, key):
        # type: (str) -> None
        """松开。未按住则空操作——halt 会无条件调它。"""
        if key not in self.held_keys:
            return
        self.ctrl.release(key)
        self.held_keys.discard(key)

    def release_all(self):
        # type: () -> None
        """全部收手并清空状态，含 tap 冷却。

        清冷却的理由：release_all 的调用方是中断（火情/脱困）与生命周期
        守卫，语义是"上一段执行作废"。若留着旧冷却，抢占方的第一个交互
        要白等几拍。
        """
        self.ctrl.release_all()
        self.move_dir = None
        self.move_extra = None
        self.move_dash = False
        self._move_until = None
        self.held_keys.clear()
        self._last_tap.clear()

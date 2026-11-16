"""对话策略：事件触发与限频（spec §5.1–5.2）。

职责边界：
- `should_trigger` 是纯谓词，唯一例外是 `speech` 去重时把规范化后的文本
  写回 `state.last_transcript`；`last_speech_tick` / `last_rescue_tick`
  由 worker 在**真正发出请求之后**回写。
"""
import queue
import threading
from typing import Any, List, Tuple

from ai.dialog.schema import DialogState


# 20 Hz 下的拍数。
SPEECH_DEDUPE_TICKS = 80        # 4 秒：同一句话的去重窗口（旧 tick 口径）
SPEECH_DEDUPE_S = 4.0           # 同一窗口的墙钟口径（推荐）
PROACTIVE_MIN_INTERVAL_TICKS = 80  # 4 秒：rescue 的最短间隔

# 规范化时丢弃的空白与标点；中文标点单独列出，避免依赖 unicodedata。
_TRANSCRIPT_DROP = set(
    " \t\r\n\u3000，。！？、；：,.!?;:…~～\"'“”‘’（）()【】《》<>[]"
)

def _normalize_transcript(text):
    # type: (Any) -> str
    """去空白、去标点、转小写；只用于判断「是不是同一句话」。"""
    return "".join(
        ch for ch in str(text or "") if ch not in _TRANSCRIPT_DROP
    ).lower()


def should_checkin(next_checkin_at, now, has_orders, in_level, live):
    # type: (Any, float, bool, bool, bool) -> bool
    """是否到点该主动报一次送单局面（定时 check-in）。

    - 没排期（None）不触发；
    - 不在关卡 / 已暂停 / 已退出（`live=False`）或当前没有订单时不聊；
    - 其余看单调时钟是否已到排期点。
    """
    if next_checkin_at is None:
        return False
    if not (has_orders and in_level and live):
        return False
    return now >= next_checkin_at


def should_trigger(event, state, tick, transcript="", now=None):
    # type: (str, DialogState, int, str, Any) -> bool
    """本拍是否应就该事件调一次 LLM。

    - `speech`：玩家触发，不受 4 秒主动开口限频；只对 4 秒内重复的同一句
      话去重。空/纯空白/纯标点的 final 直接返回 False 且不改状态。触发时
      写回 `last_transcript`，`last_speech_tick` 留给 worker。
    - `rescue`：主动开口，用 `last_rescue_tick` 做 4 秒限频。
    """
    if event == "speech":
        norm = _normalize_transcript(transcript)
        if not norm:
            # 空/纯标点的 final 不是一句话：不触发，也不能覆盖去重锚点，
            # 否则紧跟其后的同一句话会被误放行。
            return False
        if norm == state.last_transcript:
            if now is not None and state.last_speech_at:
                if now - state.last_speech_at < SPEECH_DEDUPE_S:
                    return False
            elif tick - state.last_speech_tick < SPEECH_DEDUPE_TICKS:
                return False
        state.last_transcript = norm
        if now is not None:
            state.last_speech_at = now
        return True
    if event == "rescue":
        return tick - state.last_rescue_tick >= PROACTIVE_MIN_INTERVAL_TICKS
    return False


def event_priority(event):
    # type: (Any) -> int
    """事件优先级：数字越小越先处理。玩家语音不在此队列。"""
    if not isinstance(event, (tuple, list)) or len(event) != 2:
        return 3
    name, payload = event
    if name in ("last_seconds", "round_summary"):
        return 2
    if name in ("checkin", "division", "division_review", "layout_review"):
        return 3
    if name == "batch":
        return 3
    return 3


class EventQueue(object):
    """有界事件队列：满了丢最低优先级的，新事件也参与比较。

    `DialogWorker._drain_events` 只依赖 `put_nowait` / `get_nowait` /
    `empty`，因此可以和普通 `queue.Queue` 互换。
    """

    def __init__(self, maxsize=3):
        # type: (int) -> None
        self.maxsize = max(1, int(maxsize))
        self._items = []  # type: List[Tuple[int, int, Any]]
        self._seq = 0
        self._lock = threading.Lock()

    def put_nowait(self, event):
        # type: (Any) -> None
        with self._lock:
            self._seq += 1
            self._items.append((event_priority(event), self._seq, event))
            self._items.sort(key=lambda row: (row[0], row[1]))
            if len(self._items) > self.maxsize:
                self._items = self._items[:self.maxsize]

    def get_nowait(self):
        # type: () -> Any
        with self._lock:
            if not self._items:
                raise queue.Empty
            return self._items.pop(0)[2]

    def empty(self):
        # type: () -> bool
        with self._lock:
            return not self._items

    def qsize(self):
        # type: () -> int
        with self._lock:
            return len(self._items)

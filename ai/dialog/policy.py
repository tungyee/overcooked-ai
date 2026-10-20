"""对话策略：事件触发、限频与新单合并（spec §5.1–5.2）。

职责边界：
- `should_trigger` 是纯谓词，唯一例外是 `speech` 去重时把规范化后的文本
  写回 `state.last_transcript`；`last_speech_tick` / `last_rescue_tick`
  由 worker 在**真正发出请求之后**回写。
- `merge_new_orders` 会修改 `state.pending_orders` / `state.last_order_tick`：
  它自己维护 1 秒合并窗口，返回「本拍是否该发事件」和完整批次。返回
  `True` 后批次即被清空，调用方必须负责投递，不能丢单。
"""
from typing import Any, List, Tuple

from ai.dialog.schema import DialogState


# 20 Hz 下的拍数。
SPEECH_DEDUPE_TICKS = 80        # 4 秒：同一句话的去重窗口
PROACTIVE_MIN_INTERVAL_TICKS = 80  # 4 秒：rescue 的最短间隔
ORDER_BATCH_TICKS = 20          # 1 秒：新单合并窗口

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


def should_trigger(event, state, tick, transcript=""):
    # type: (str, DialogState, int, str) -> bool
    """本拍是否应就该事件调一次 LLM。

    - `speech`：玩家触发，不受 4 秒主动开口限频；只对 4 秒内重复的同一句
      话去重。空/纯空白/纯标点的 final 直接返回 False 且不改状态。触发时
      写回 `last_transcript`，`last_speech_tick` 留给 worker。
    - `rescue`：主动开口，用 `last_rescue_tick` 做 4 秒限频。
    - `new_order`：真正的合并在 `merge_new_orders`；这里只回答「已挂起的
      批次是否已过 1 秒窗口」，不消费批次。
    """
    if event == "speech":
        norm = _normalize_transcript(transcript)
        if not norm:
            # 空/纯标点的 final 不是一句话：不触发，也不能覆盖去重锚点，
            # 否则紧跟其后的同一句话会被误放行。
            return False
        if (norm == state.last_transcript
                and tick - state.last_speech_tick < SPEECH_DEDUPE_TICKS):
            return False
        state.last_transcript = norm
        return True
    if event == "rescue":
        return tick - state.last_rescue_tick >= PROACTIVE_MIN_INTERVAL_TICKS
    if event == "new_order":
        return (bool(state.pending_orders)
                and tick - state.last_order_tick >= ORDER_BATCH_TICKS)
    return False


def merge_new_orders(order_ids, state, tick):
    # type: (Any, DialogState, int) -> Tuple[bool, List]
    """把新单并入 1 秒批次，返回 `(是否触发, 完整批次)`；不丢任何订单。

    - 批次从第一张新单到达时开始，`last_order_tick` 记批次起点；窗口内的
      追加**不刷新**起点（1 秒从第一张新单算起）。
    - 窗口内返回 `(False, 当前批次)`；窗口已过（包括 `order_ids=[]` 的
      一拍）返回 `(True, 整批)` 并清空。
    - 窗口已过又来了新单：先冲出旧批 `(True, 旧批)`，新单另起一批等下一拍；
      旧批和新单都保留，先后各发一次。
    """
    pending = state.pending_orders
    fresh = []
    for order_id in (order_ids or []):
        # 同一批里重复上报、或已挂在待发批次里的 id 都只保留一次：
        # 它没有丢，只是不该在一批里重复出现。
        if order_id not in pending and order_id not in fresh:
            fresh.append(order_id)

    if pending and tick - state.last_order_tick >= ORDER_BATCH_TICKS:
        fired = (True, list(pending))
        state.pending_orders = fresh
        state.last_order_tick = tick if fresh else 0
        return fired

    if fresh:
        if not pending:
            # 新批次：窗口从第一张单起算
            state.last_order_tick = tick
        pending.extend(fresh)

    return False, list(pending)

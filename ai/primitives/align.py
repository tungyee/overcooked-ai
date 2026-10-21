"""AlignToCell：句柄校验不符时往格心收紧站位。

委托后端（exec）下，对齐已并入 move/tap 的重瞄（插件按 handle 反馈重瞄），
Python 侧的 AlignToCell 是 no-op。保留 `_blacklist_pair` 静态方法：交互恢复
（interaction_recovery）在换位重试时仍用它对「(资源,格)」写短期拉黑。
"""
from typing import Any, Dict, Optional

from ai.exec.bt import Status
from ai.primitives.base import Primitive

# 收紧的拍数上限（委托后端下不消费该预算，保留仅用于接口兼容）。
ALIGN_MAX_TICKS = 40

# (资源,格) 拉黑的 TTL（拍）。与 stuck.ABANDON_TTL_TICKS 同量级：都是
# 「换个位置重试一次」的时间尺度。200 拍 = 10 秒。
ALIGN_PAIR_TTL_TICKS = 200


class AlignToCell(Primitive):
    """委托后端下对齐已并入 move/tap 的重瞄，本节点是 no-op。"""

    def __init__(self, target, kind, max_ticks=ALIGN_MAX_TICKS):
        # type: (Any, Optional[str], int) -> None
        Primitive.__init__(self)
        self.target = target
        self.kind = kind
        self.max_ticks = max_ticks

    def reset(self):
        # type: () -> None
        Primitive.reset(self)

    def tick(self, ctx):
        # type: (Dict) -> str
        self._enter(ctx)
        return Status.SUCCESS

    @staticmethod
    def _blacklist_pair(ctx, station, cell):
        # type: (Dict, Dict, Any) -> None
        """记下「这一格对这个资源用不了」。

        资源 id 走 `P.resource_id`（spec §5.2 的 `(grid, name)`），与 task 的
        `claims()` 同一套，便于日后对账。

        scratch 可能还不是 StuckScratch（卡住守卫是跨模式前置，可能先于任何
        模式进入），复用 stuck 里的保障函数而不是自己再写一遍类型检查。
        """
        from ai.primitives.stuck import _ensure_stuck_scratch
        from ai.query import predicates as P
        scratch = _ensure_stuck_scratch(ctx)
        if cell is None:
            return
        key = (P.resource_id(station), tuple(cell))
        scratch.bad_pairs[key] = (ctx.get("tick") or 0) + ALIGN_PAIR_TTL_TICKS

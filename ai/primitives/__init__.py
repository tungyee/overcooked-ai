"""执行原语包。

带强制前后校验的叶子节点（spec §7.2）。
"""

from ai.primitives.base import (
    Primitive,
    resolve,
    _blocked_cells,
    target_cell,
    target_grid_callbacks,
    _dir_towards,
)
from ai.primitives.align import (
    AlignToCell,
    ALIGN_MAX_TICKS,
    ALIGN_PAIR_TTL_TICKS,
)
from ai.primitives.move import Approach, Face
from ai.primitives.tap import Tap, Confirm, Settle
from ai.primitives.hold import HoldUntil, TapWhileHolding, WaitUntil
from ai.primitives.interact import EnsureHandsFree, InteractWith
from ai.primitives.throw import ThrowTo
from ai.primitives.stuck import (
    MarkBlocked,
    Nudge,
    AbandonTarget,
    Timeout,
)

__all__ = [
    "Primitive",
    "resolve",
    "target_grid_callbacks",
    "Approach",
    "Face",
    "AlignToCell",
    "ALIGN_MAX_TICKS",
    "ALIGN_PAIR_TTL_TICKS",
    "Tap",
    "Confirm",
    "Settle",
    "HoldUntil",
    "TapWhileHolding",
    "WaitUntil",
    "EnsureHandsFree",
    "InteractWith",
    "ThrowTo",
    "MarkBlocked",
    "Nudge",
    "AbandonTarget",
    "Timeout",
]

"""执行原语包。

带强制前后校验的叶子节点（spec §7.2）。
"""

from ai.primitives.base import (
    Primitive,
    resolve,
    _blocked_cells,
    target_cell,
    _dir_towards,
)
from ai.primitives.move import Approach, Face
from ai.primitives.tap import Tap, Confirm, Settle
from ai.primitives.hold import HoldUntil, TapWhileHolding
from ai.primitives.interact import EnsureHandsFree, InteractWith
from ai.primitives.stuck import (
    MarkBlocked,
    Nudge,
    AbandonTarget,
    Timeout,
)

__all__ = [
    "Primitive",
    "resolve",
    "Approach",
    "Face",
    "Tap",
    "Confirm",
    "Settle",
    "HoldUntil",
    "TapWhileHolding",
    "EnsureHandsFree",
    "InteractWith",
    "MarkBlocked",
    "Nudge",
    "AbandonTarget",
    "Timeout",
]

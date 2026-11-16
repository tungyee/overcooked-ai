# -*- coding: utf-8 -*-
"""RideRaft：开舢板过河到对岸（5-1 过桥寿司）。

按键控制、可站人的舢板（ai/space/raft.py）把人从一岛驮到另一岛。本 task =
「走上船 → 触发移动 → 等靠岸」；语义完成（船停靠 target 侧）由 Raft 原语
的 pred 判，与 ReceiveConveyor 的「按开关」同一思路。

身份 = item（目标岸 'left'/'right'）。无资源认领，可空手也可手持料过河。
"""
from typing import Any, Dict, Tuple

from ai import primitives as PR
from ai.exec.bt import Sequence
from ai.space import raft
from ai.tasks.base import TASK_TIMEOUT_TICKS, Task, _register

# 开船过河的整段预算（拍）：走上船 + 触发 + 等靠岸。船单程几秒，留足余量。
RIDE_TIMEOUT_TICKS = TASK_TIMEOUT_TICKS + 120


@_register
class RideRaft(Task):
    """开舢板到 target 侧。"""

    TYPE = "RideRaft"
    DESC = "开舢板过河到对岸"
    LABEL = "过河"
    HANDS = "any"

    @property
    def target_side(self):
        # type: () -> str
        return self._item

    def claims(self):
        return ()

    def done(self, ctx):
        # type: (Dict) -> bool
        return raft.docked_side(ctx["w"]) == self.target_side

    def feasible(self, ctx):
        # type: (Dict) -> bool
        w = ctx["w"]
        if self.done(ctx):
            return False
        side = raft.docked_side(w)
        if side is None or side == self.target_side:
            return False
        # 船得停在本侧（P2 这一侧）才上得去；停在对面就得先等玩家开回来或
        # 对面的人上船。此刻够不着就不发（避免凭空等船）。
        if not raft.on_my_side(w):
            return False
        return raft.board_cell(w) is not None

    def build(self):
        # type: () -> Any
        target = self.target_side

        def board(c):
            return raft.board_cell(c["w"])

        def arrived(c):
            return raft.docked_side(c["w"]) == target

        return PR.Timeout(Sequence([
            PR.GoToCell(board),
            PR.Raft(target, arrived, timeout=RIDE_TIMEOUT_TICKS),
        ]), RIDE_TIMEOUT_TICKS)

# -*- coding: utf-8 -*-
"""出餐口「盘已交出去」判定的回归（trace-20261019-122319）。

根因：P2 端着盘站在出餐口旁（面朝出餐口）时，手持盘的坐标略在角色前方，
落进出餐口的矩形范围内。`_plate_on_serving_hatch` 只比坐标、不看 held_by，
于是把「还端在手里的盘」误判成「已经交出去了」→ DeliverPlate 变不可行 →
planner 切走 → 盘坐标离开框又变可行 → 来回振荡，送餐永远送不出去。
"""
from types import SimpleNamespace

from ai.tasks.assembly import _plate_on_serving_hatch

# 出餐口（plate_station）矩形框，与 trace 里 workstation_plate_station 一致。
_HATCH = {
    "t": "plate_station",
    "minx": 15.0, "minz": 9.0, "maxx": 17.4, "maxz": 10.8,
}


def _ctx():
    return {"w": SimpleNamespace(stations=[dict(_HATCH)])}


def test_held_plate_near_hatch_is_not_delivered():
    """端着、但坐标落在出餐口框内 → 不算「已交出去」。"""
    plate = {"x": 15.5, "z": 9.2, "held_by": "Player 2"}
    assert _plate_on_serving_hatch(_ctx(), plate) is False


def test_placed_plate_on_hatch_is_delivered():
    """真的放在出餐口上（非手持）→ 仍判「已交出去」。"""
    plate = {"x": 15.5, "z": 9.2, "held_by": None}
    assert _plate_on_serving_hatch(_ctx(), plate) is True

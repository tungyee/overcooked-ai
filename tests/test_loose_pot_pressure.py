# -*- coding: utf-8 -*-
"""散锅（被端下灶的锅）的时间压力与分层回归。

实机 trace-20261015-165358：`RescueCooked` 在 t=1657 把一口刚熟的米锅从灶
端到台面 (6.0, 4.8)，锅里的熟米一直挂到收盘 t=3636 —— **1979 拍 ≈ 99 秒
没人碰**，同期 #4/#5/#7 三个黄瓜单都只缺这份米，#4/#5 过期。

根因：`_time_pressure` 只查 `world.cookers`，散锅在 `world.items` 里查不到
→ 返回 0.0 → 既不进 L1/L2，也不是备菜类型 → 掉 L7 清理层。trace 里那个
`TakeCookedToPlate(..., utensil_pot_01 (2), ...)` 候选 184 拍都是
`ok=True, layer=7, top_layer=6`：可行、分数算出来了，但层比任何备菜低一级,
词典序下结构上选不中。

`TakeCookedToPlate._source` 早就能识别散锅（cook.py 的 cooked: token + cooked
字段双判据），是压力函数这一侧漏了。
"""
from ai.core import planner
from ai import tasks as T
from ai.core.planner_layers import L_CLEANUP, L_PREP, L_RESCUE, layer_of_task
from ai.state.world import World


def _world(cookers=None, items=None):
    return World({
        "orders": [], "stations": [], "recipes": [],
        "chefs": [{"name": "Player 2", "x": 1.0, "z": 1.0, "held": None}],
        "plates": [], "workstations": [], "cookers": cookers or [],
        "items": items or [], "extinguishers": [], "grid": {"nodeMap": None},
    })


def _loose_pot(name="utensil_pot_01 (2)", cooked="Cooked",
               detail=("ing:SushiRice",), x=6.0, z=4.8):
    return {"n": name, "x": x, "z": z, "cooked": cooked,
            "detail": list(detail)}


def _take(source="utensil_pot_01 (2)"):
    return T.TakeCookedToPlate("order4", (source, "plate_01"),
                               "BoiledSushiRice")


def test_loose_cooked_pot_has_rescue_pressure():
    """台面上的熟锅压力不能是 0——它和灶上的熟锅一样该马上去盛。"""
    w = _world(items=[_loose_pot()])
    assert planner._time_pressure(_take(), w) >= 1.0


def test_loose_cooked_pot_lands_in_rescue_layer():
    """压力 >= 1.0 → L2 救料层，结构性压过一切备菜（实机那 184 拍的修复）。"""
    w = _world(items=[_loose_pot()])
    assert layer_of_task(_take(), w) == L_RESCUE


def test_loose_raw_pot_has_no_pressure():
    """生锅没压力：它要么还在等人端上灶，要么就是个空壳，不该抢 L2。"""
    w = _world(items=[_loose_pot(cooked="Raw", detail=())])
    assert planner._time_pressure(_take(), w) == 0.0
    assert layer_of_task(_take(), w) == L_PREP


def test_loose_pot_with_cooked_token_only():
    """detail 给 rich token 而 cooked 字段缺失时也要认（两条判据任一成立）。"""
    w = _world(items=[_loose_pot(cooked=None,
                                 detail=("cooked:SushiRice:Pot",))])
    assert planner._time_pressure(_take(), w) >= 1.0


def test_burning_loose_pot_is_irreversible():
    """糊了的散锅压力满格 → L1，比刚熟的更急（它还要灭火）。"""
    w = _world(items=[_loose_pot(cooked="Burned")])
    assert planner._time_pressure(_take(), w) == 2.0


def test_unknown_source_id_still_zero():
    """源 id 既不是灶也不在 items 里 → 0.0（别凭空造压力）。"""
    w = _world(items=[_loose_pot(name="utensil_pot_01 (9)")])
    assert planner._time_pressure(_take(), w) == 0.0


def test_cooker_lookup_still_wins():
    """灶上查得到就用灶的连续进度，不走散锅退路（回归保护）。"""
    cooker = {"n": "cooker_01", "x": 1.0, "z": 1.0, "cooked": False,
              "isCooking": True, "burning": False, "onFire": False,
              "cooking_progress": 3.6, "cooking_time": 12.0}
    w = _world(cookers=[cooker], items=[_loose_pot(name="cooker_01")])
    # ratio = 3.6/24 = 0.15 → 快熟渐变的起点，恰好 0.0
    assert planner._time_pressure(_take("cooker_01"), w) == 0.0

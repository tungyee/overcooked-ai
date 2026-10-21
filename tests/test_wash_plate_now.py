# -*- coding: utf-8 -*-
"""洗盘「要不要洗」软闸从 feasible 拆进 L4 层谓词（Task 3，spec §A.3）。

Task 2 加了 `now` 硬指令闸（_select_imperative），但它只在 `feasible()` 过滤后
的候选里选——而 WashPlate.feasible 仍把「场上还有干净盘 / 装配盘」判成不可行，
于是 `now 洗盘` 在有干净盘时根本够不到 WashPlate 候选。本任务把这条「要不要洗」
策略从 feasible 拆出来，移到 L4 层谓词：

  - feasible 只保留物理约束（有脏盘 / 续洗态）；
  - L4 层谓词改成 `plate_pressure >= 2.0 and not placed_plates`（clean==0 且
    无装配盘才进解卡层）。

代价是「有干净盘就别洗」从硬闸降级成层优先级：WashPlate 掉到 L7，但 `now 洗盘`
能通过硬指令闸选到它。
"""
from ai import tasks as T
from ai.core import planner
from ai.core import planner_layers as PL
from ai.core.planner import Candidate
from ai.dialog.schema import Preference
from ai.state.blackboard import Blackboard
from ai.state.world import World


RECIPES = [
    {"name": "Sushi_Cucumber",
     "comp": ["Seaweed", "BoiledSushiRice", "Cucumber"]},
    {"name": "BoiledSushiRice", "comp": ["SushiRice"], "cook": "Pot"},
    {"name": "SushiRice"},
    {"name": "Seaweed"},
    {"name": "Cucumber"},
]


def _now_pref(*pairs, **kw):
    # type: (tuple, dict) -> Preference
    return Preference(kind="now", owner="P2", weight=20.0,
                      scope=frozenset(pairs), **kw)


def _wash_world(plates=(), items=()):
    # type: (tuple, tuple) -> World
    """最小关卡：一个洗碗池 + 一个拼装格 + 盘堆/晾干架。

    plates 元素是 (n, gx, gz, contents)：用世界坐标反解拼装格位置，避免手抄
    origin/cell。items 直接追加（脏盘摞等）。
    """
    stations = [
        {"t": "washing", "n": "sink_01", "gx": 7, "gz": 1},
        {"t": "plate_return", "n": "DryingPart", "gx": 8, "gz": 1},
        {"t": "counter", "n": "assembly", "gx": 5, "gz": 5},
        {"t": "plate_stack", "n": "stack1", "gx": -1, "gz": 1},
    ]
    w = World({
        "orders": [{"id": "1", "dish": "Sushi_Cucumber", "remaining": 60,
                    "lifetime": 130,
                    "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]}],
        "stations": stations,
        "recipes": RECIPES,
        "chefs": [{"name": "Player 2", "x": 1.0, "z": 1.0, "held": None}],
        "plates": [],
        "workstations": [],
        "cookers": [],
        "items": [],
        "extinguishers": [],
        "grid": {"nodeMap": None},
    })
    for n, gx, gz, contents in plates:
        x, z = w.to_world(gx, gz)
        w.plates.append({"n": n, "x": x, "z": z, "contents": list(contents)})
    w.items.extend(list(items))
    return w


def _ctx(w):
    # type: (World) -> dict
    return {"w": w, "bb": Blackboard()}


def _dirty():
    return {"n": "DirtyPlateStack", "x": 3.0, "z": 0.0}


# ---------- feasible 软闸拆除 ----------

def test_wash_feasible_when_clean_plates_remain():
    """干净盘 >0 + 有脏盘 → feasible True（物理可行，不再是 False）。

    这是软闸拆出 feasible 的核心断言：`now 洗盘` 要能选到 WashPlate，feasible
    就不能再因为「还有干净盘」而否决它。干净盘放在 (2,3)——既不在取盘位
    （plate_stack/plate_return），也不在拼装格，因此 done 与 placed 都 False，
    旧代码里只有 `clean_plate_count > 0` 这一条会拦它。
    """
    w = _wash_world(plates=[("p1", 2, 3, ())], items=[_dirty()])
    assert T.WashPlate(resources=("sink_01",)).feasible(_ctx(w)) is True


def test_wash_infeasible_no_dirty():
    """无脏盘 → 仍 False（物理约束保留）。"""
    w = _wash_world(plates=[], items=[])
    assert T.WashPlate(resources=("sink_01",)).feasible(_ctx(w)) is False


# ---------- L4 层谓词收紧 ----------

def test_wash_layer_only_when_plate_blocked():
    """layer_of_task(WashPlate) 只在 clean==0 and not placed 时为 L4，否则 L7。"""
    wash = T.WashPlate(resources=("sink_01",))

    # clean==0 且无装配盘 → L4 解卡
    w0 = _wash_world(plates=[], items=[_dirty()])
    assert PL.layer_of_task(wash, w0) == PL.L_UNBLOCK

    # 干净盘有剩（1 块，非拼装格）→ 不是 L4，掉 L7
    w1 = _wash_world(plates=[("p1", 2, 3, ())], items=[_dirty()])
    assert PL.layer_of_task(wash, w1) == PL.L_CLEANUP

    # 干净盘充足（2 块）→ 仍 L7
    w2 = _wash_world(plates=[("p1", 2, 3, ()), ("p2", 3, 3, ())],
                     items=[_dirty()])
    assert PL.layer_of_task(wash, w2) == PL.L_CLEANUP


def test_wash_not_l4_when_placed_plate():
    """clean==0 但台上已有装配盘 → 不 L4（回归 guard：装配中却去洗盘）。

    实机复现的根因：盘已摆在拼装格上（装配盘=1），旧规则却因 clean <
    PLATE_BUFFER 跑去洗盘。现在软闸在层谓词里：placed 为 True 时即便
    clean==0 也不进 L4。
    """
    w = _wash_world(plates=[("p1", 5, 5, ("Seaweed",))], items=[_dirty()])
    wash = T.WashPlate(resources=("sink_01",))
    assert planner._placed_plates(w) is True          # 装配盘确实被算作就位
    assert PL.layer_of_task(wash, w) == PL.L_CLEANUP


def test_place_clean_plate_keeps_unblock_layer():
    """PlaceCleanPlate 保留「有干净盘就摆」的解卡信号（clean==1 → L4）。

    L4 谓词对 WashPlate 收紧成 clean==0，但不能对 PlaceCleanPlate 同样收紧：
    它的 feasible 要求干净空盘（clean>=1，plate.py:157-164），clean==0 时压根
    不是候选；若也套用 clean==0，PlaceCleanPlate 就从 L4 永远消失，
    W_NO_PLATE_PLACED 白名单项（只登记在 L4）死代码化。所以按 task 类型拆分：
    WashPlate 只在 clean==0 且无装配盘时进 L4，PlaceCleanPlate 在 clean<=1
    （配合 feasible 即 clean==1）进 L4。
    """
    w = _wash_world(plates=[("p1", 2, 3, ())])   # 1 块干净空盘，无装配盘
    place = T.PlaceCleanPlate("1", ("p1", (5, 5)))
    wash = T.WashPlate(resources=("sink_01",))

    assert PL.layer_of_task(place, w) == PL.L_UNBLOCK
    assert PL.layer_of_task(wash, w) == PL.L_CLEANUP   # clean==1 不洗，掉 L7


# ---------- `now 洗盘` 端到端 ----------

def test_now_wash_selects_when_clean_remain():
    """`now 洗盘` 且干净盘有剩时能选中 WashPlate。

    feasible 不再否决「有干净盘」，于是硬指令闸能选到 WashPlate（虽然它现在
    在 L7，不是 L4——闸跳过层选择）。
    """
    w = _wash_world(plates=[("p1", 2, 3, ())], items=[_dirty()])
    wash = T.WashPlate(resources=("sink_01",))
    assert wash.feasible(_ctx(w)) is True             # 干净盘有剩仍可行

    bb = Blackboard()
    bb.tick = 10
    bb.preferences = [_now_pref(("WashPlate", None))]

    chosen = planner._select_imperative(
        [Candidate(wash, [], 15)], w, bb)
    assert chosen is not None
    assert chosen.task == wash

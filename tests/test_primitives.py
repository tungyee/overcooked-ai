"""原语单测。

全程假 controller + 假 World，零真实按键注入——Plan 2 的交付标准。
"""
import pytest

from ai import primitives as PR
from ai.bt import Status
from ai.hands import Hands
from ai.navigation import distance_field
from ai.recipes import RecipeBook
from ai.blackboard import Blackboard


class FakeCtrl(object):
    def __init__(self):
        self.calls = []

    def tap(self, name, hold=0.12):
        self.calls.append(("tap", name))

    def press(self, name):
        self.calls.append(("press", name))

    def release(self, name):
        self.calls.append(("release", name))

    def release_all(self):
        self.calls.append(("release_all",))


class FakeWorld(object):
    """只提供原语要用的属性。"""

    def __init__(self, stations=None, plates=None, workstations=None,
                 cookers=None, items=None, chefs=None, recipes=None,
                 belt_cells=None, belt_dir=None, walkable=None, cell=1.2):
        self.stations = stations or []
        self.plates = plates or []
        self.workstations = workstations or []
        self.cookers = cookers or []
        self.items = items or []
        self.chefs = chefs or []
        self.recipes = recipes or []
        self.belt_cells = set(belt_cells or ())
        self.belt_dir = belt_dir or {}
        self._walk = walkable or (lambda gx, gz: True)
        self._cell = cell

    def walkable(self, gx, gz):
        return self._walk(gx, gz)

    def to_grid(self, x, z):
        return (int(round(x / self._cell)), int(round(z / self._cell)))

    def to_world(self, gx, gz):
        return (gx * self._cell, gz * self._cell)


def make_ctx(w=None, chef=None, start=(0, 0), tick=0, bb=None):
    """装配一个 ctx。walkable 默认整行 gz==0 可走。"""
    w = w if w is not None else FakeWorld(
        walkable=lambda gx, gz: gz == 0 and -2 <= gx <= 8)
    chef = chef if chef is not None else {
        "name": "Player 2", "x": start[0] * 1.2, "z": start[1] * 1.2,
        "held": None,
        "interactions": {"interactable": None, "pickup": None,
                         "placement": None, "grid": None,
                         "gx": None, "gz": None,
                         "pickup_gx": None, "pickup_gz": None,
                         "placement_gx": None, "placement_gz": None},
    }
    ctrl = FakeCtrl()
    return {
        "w": w,
        "chef": chef,
        "hands": Hands(ctrl),
        "bb": bb if bb is not None else Blackboard(),
        "field": distance_field(start, w.walkable, w.belt_dir),
        "book": RecipeBook(w.recipes, w.stations),
        "tick": tick,
    }, ctrl


def _inter(**kw):
    base = {"interactable": None, "pickup": None, "placement": None,
            "grid": None, "gx": None, "gz": None,
            "pickup_gx": None, "pickup_gz": None,
            "placement_gx": None, "placement_gz": None}
    base.update(kw)
    return base


# ---------- target 解析 ----------

def test_resolve_static_dict():
    ctx, _ = make_ctx()
    st = {"n": "x", "gx": 1, "gz": 0}
    assert PR.resolve(st, ctx) is st


def test_resolve_callable_is_called_each_tick():
    """Review Focus 5：target 每拍重解析，不缓存构造时的快照。

    树是跨拍复用的对象（spec §7.6），构造时抓的 dict 第二拍就是过期
    数据——锅被端走、盘被端走都会让它失真。
    """
    ctx, _ = make_ctx()
    seen = []

    def resolver(c):
        seen.append(c["tick"])
        return {"n": "x", "gx": 1, "gz": 0}

    PR.resolve(resolver, ctx)
    ctx["tick"] = 1
    PR.resolve(resolver, ctx)
    assert seen == [0, 1]


def test_resolve_none_and_missing():
    ctx, _ = make_ctx()
    assert PR.resolve(None, ctx) is None
    assert PR.resolve(lambda c: None, ctx) is None


# ---------- Approach ----------

def test_approach_already_in_position_is_success():
    """交互信号已指向目标 → 立刻 SUCCESS，不多走一步。"""
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(placement_gx=1, placement_gz=0)}
    ctx, ctrl = make_ctx(chef=chef)
    node = PR.Approach({"n": "c", "gx": 1, "gz": 0}, "placement")
    assert node.tick(ctx) == Status.SUCCESS
    assert ("press", "right") not in ctrl.calls


def test_approach_moves_toward_target():
    """未到位 → RUNNING 且按住朝目标的方向键。"""
    ctx, ctrl = make_ctx(start=(0, 0))
    node = PR.Approach({"n": "c", "gx": 5, "gz": 0}, "placement")
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "right"
    assert ("press", "right") in ctrl.calls


def test_approach_stops_pressing_on_arrival():
    """到位（站到目标相邻格）那一拍必须松开移动键，否则会滑过交互位。"""
    ctx, ctrl = make_ctx(start=(0, 0))
    node = PR.Approach({"n": "c", "gx": 5, "gz": 0}, "placement")
    node.tick(ctx)
    assert ctx["hands"].move_dir == "right"
    # 走到目标 (5,0) 的相邻可交互格 (4,0)
    ctx["chef"]["x"] = 4 * 1.2
    ctx["chef"]["z"] = 0.0
    assert node.tick(ctx) == Status.SUCCESS
    assert ctx["hands"].move_dir is None


def test_approach_unreachable_is_failure():
    """不可达 → FAILURE（spec §7.2），交还 Planner 重算。"""
    w = FakeWorld(walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 2)
    ctx, _ = make_ctx(w=w)
    node = PR.Approach({"n": "island", "gx": 40, "gz": 40}, "placement")
    assert node.tick(ctx) == Status.FAILURE


def test_approach_falls_back_to_belt_interact_cell():
    """非传送带交互位不可达时，退回传送带位，而不是判 FAILURE。

    实机复现：黄瓜摆在台面上，台面一侧是传送带、另一侧的交互位在岛对面
    走不到——旧逻辑只看非传送带位，直接判不可达卡死。
    """
    w = FakeWorld(
        walkable=lambda gx, gz: (gx, gz) in {(0, 2), (1, 2), (1, 0)},
        belt_cells={(1, 2)}, belt_dir={(1, 2): (1, 0)})
    ctx, _ = make_ctx(w=w, start=(0, 2))
    node = PR.Approach({"n": "c", "gx": 1, "gz": 1}, "placement")
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "right"


def test_approach_target_gone_is_failure():
    """Review Focus 5：目标中途消失 → FAILURE，不抛 AttributeError。"""
    ctx, _ = make_ctx()
    node = PR.Approach(lambda c: None, "pickup")
    assert node.tick(ctx) == Status.FAILURE


def test_approach_target_without_grid_is_failure():
    """站点无 gx/gz 且无世界坐标 → 无法定位，FAILURE。"""
    ctx, _ = make_ctx()
    assert PR.Approach({"n": "nowhere"}, "pickup").tick(ctx) == Status.FAILURE


def test_approach_derives_cell_from_world_coords():
    """动态物（灭火器）只有 x/z：用 World.to_grid 换算。"""
    ctx, _ = make_ctx(start=(0, 0))
    ext = {"n": "utensil_fire_extinguisher_01", "x": 3 * 1.2, "z": 0.0}
    node = PR.Approach(ext, "pickup")
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "right"


def test_approach_target_underfoot_is_success():
    """目标就在脚下那一格 → 立刻 SUCCESS，不许走开再走回来。

    实机复现（trace-20261014-004742 t=11000~11580，火烧到关卡结束）：
    灭火器掉在 P2 自己站的格 (6,2)，`interactions.pickup_gx/gz` 已经
    指向 (6,2)。旧逻辑只取目标**四邻**当 goal（allow_target=False），
    于是要走到 (5,2) 去——一走开，距离场又把它拉回 (6,2)，表现为
    [5,2]↔[6,2] 来回震荡 700 拍，灭火器始终拿不到。

    散落物（投掷落地的料、掉落的灭火器）没有站点语义，**它可能就在
    脚下**，这是与固定站点的本质差别。
    """
    ctx, ctrl = make_ctx(start=(3, 0))
    ext = {"n": "utensil_fire_extinguisher_01", "x": 3 * 1.2, "z": 0.0}
    node = PR.Approach(ext, "pickup")
    assert node.tick(ctx) == Status.SUCCESS
    assert ctx["hands"].move_dir is None, "脚下的东西不该先走开"
    assert ("press", "left") not in ctrl.calls
    assert ("press", "right") not in ctrl.calls


def test_approach_station_underfoot_still_uses_neighbor():
    """固定站点（灶/板/池）仍走四邻：人站不到站点格上去。

    `allow_target` 这条短路只对「目标格可走」成立——站点格不可走，
    所以这里 goal 仍是相邻格，与旧行为一致。
    """
    w = FakeWorld(walkable=lambda gx, gz: gz == 0 and -2 <= gx <= 8 and gx != 5)
    ctx, _ = make_ctx(w=w, start=(0, 0))
    node = PR.Approach({"n": "cooker", "gx": 5, "gz": 0}, "placement")
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "right"


def test_approach_presses_belt_counter_key():
    """站在传送带上横穿时同时顶住反向键，否则被推走（spec §7.2）。"""
    w = FakeWorld(walkable=lambda gx, gz: -2 <= gx <= 8 and -1 <= gz <= 4,
                  belt_cells={(0, 0)}, belt_dir={(0, 0): (1, 0)})
    ctx, ctrl = make_ctx(w=w, start=(0, 0))
    node = PR.Approach({"n": "c", "gx": 0, "gz": 4}, "placement")
    node.tick(ctx)
    assert ctx["hands"].move_dir == "up"
    assert ctx["hands"].move_extra == "left", "往上穿越往右推的带，要顶左"


def test_approach_halt_releases_movement():
    """halt 只松键（spec §7.6 纪律 2）。"""
    ctx, ctrl = make_ctx(start=(0, 0))
    node = PR.Approach({"n": "c", "gx": 5, "gz": 0}, "placement")
    node.tick(ctx)
    node.halt()
    assert ctx["hands"].move_dir is None


def test_approach_respects_blocked_cells_from_scratch():
    """脱困拉黑的格子要绕开（MarkBlocked 写 scratch，Approach 读它）。"""
    from ai.blackboard import StuckScratch
    w = FakeWorld(walkable=lambda gx, gz: -1 <= gz <= 1 and 0 <= gx <= 4)
    bb = Blackboard()
    bb.scratch = StuckScratch(bad_cells={(1, 0): 999})
    ctx, _ = make_ctx(w=w, start=(0, 0), bb=bb)
    node = PR.Approach({"n": "c", "gx": 4, "gz": 0}, "placement")
    node.tick(ctx)
    assert ctx["hands"].move_dir in ("up", "down"), \
        "正前方被拉黑，该绕路而不是直撞"


# ---------- Face ----------

def test_face_already_facing_is_success():
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(gx=1, gz=0)}
    ctx, ctrl = make_ctx(chef=chef)
    node = PR.Face({"n": "c", "gx": 1, "gz": 0})
    assert node.tick(ctx) == Status.SUCCESS
    assert ctrl.calls == []


def test_face_presses_toward_target():
    """转向靠压方向键（移动方向 = 面向方向），这是旧 _fire_tick 的
    face_fire 子状态在做的事。"""
    ctx, ctrl = make_ctx(start=(0, 0))
    node = PR.Face({"n": "c", "gx": 1, "gz": 0})
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "right"


def test_face_succeeds_once_interaction_grid_matches():
    ctx, _ = make_ctx(start=(0, 0))
    node = PR.Face({"n": "c", "gx": 1, "gz": 0})
    node.tick(ctx)
    ctx["chef"]["interactions"] = _inter(gx=1, gz=0)
    assert node.tick(ctx) == Status.SUCCESS
    assert ctx["hands"].move_dir is None


def test_face_succeeds_on_name_only_interaction():
    """出餐口只报句柄名、不给 gx/gz 朝向信号：用 interact_ready 名字兜底。

    实机复现：送餐站在出餐口前，facing_cell 永远为 None，旧逻辑 8 拍兜底
    后 FAILURE，pickup 键发不出去；手动按 Shift 才上菜。
    """
    target = {"n": "workstation_plate_station", "x": 0.0, "z": -1.2}
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(placement="workstation_plate_station")}
    ctx, ctrl = make_ctx(chef=chef, start=(0, 0))
    node = PR.Face(target)
    assert node.tick(ctx) == Status.SUCCESS
    assert ctrl.calls == []


def test_face_target_gone_is_failure():
    ctx, _ = make_ctx()
    assert PR.Face(lambda c: None).tick(ctx) == Status.FAILURE


def test_face_timeout_is_failure():
    """转不过去（被挡住）→ 有限拍后 FAILURE，不无限压键。"""
    ctx, _ = make_ctx(start=(0, 0))
    node = PR.Face({"n": "c", "gx": 1, "gz": 0}, max_ticks=3)
    for _ in range(3):
        assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.FAILURE


# ---------- Tap（Review Focus 1）----------

def test_tap_presses_once_and_succeeds():
    ctx, ctrl = make_ctx()
    node = PR.Tap("pickup")
    assert node.tick(ctx) == Status.SUCCESS
    assert ctrl.calls == [("tap", "pickup")]


def test_tap_does_not_repress_on_retick():
    """核心用例：Sequence 每拍从头 tick，Tap 第二次 tick 不许再按。

    拾取是 toggle（oc/ai/agent.py:921），再按一次 = 放下刚拿起的东西。
    """
    ctx, ctrl = make_ctx()
    node = PR.Tap("pickup")
    node.tick(ctx)
    ctx["hands"].tick()
    ctx["hands"].tick()
    ctx["hands"].tick()      # 冷却已过，但边沿标记还在
    assert node.tick(ctx) == Status.SUCCESS
    assert ctrl.calls.count(("tap", "pickup")) == 1


def test_tap_presses_again_after_reset():
    """reset（halt / task 身份变化）后允许重按——那是新的一次激活期。"""
    ctx, ctrl = make_ctx()
    node = PR.Tap("pickup")
    node.tick(ctx)
    node.reset()
    for _ in range(3):
        ctx["hands"].tick()
    node.tick(ctx)
    assert ctrl.calls.count(("tap", "pickup")) == 2


def test_tap_blocked_by_hands_cooldown_returns_running():
    """另一个节点刚按过同键 → 本节点这拍按不动，RUNNING 等下拍。

    返回 RUNNING 而不是 SUCCESS：谎称按成功会让 Sequence 直接走到
    Confirm，然后 Confirm 校验失败——多绕一圈且日志误导。
    """
    ctx, ctrl = make_ctx()
    ctx["hands"].tap("pickup")        # 别人先按了
    node = PR.Tap("pickup")
    assert node.tick(ctx) == Status.RUNNING
    assert ctrl.calls.count(("tap", "pickup")) == 1


def test_tap_releases_movement_before_pressing():
    """按键前先停移动——边滑边按会落空或串位（旧 _grab_burst 的做法）。"""
    ctx, _ = make_ctx()
    ctx["hands"].set_move("right")
    PR.Tap("pickup").tick(ctx)
    assert ctx["hands"].move_dir is None


def test_tap_with_target_requires_interact_ready():
    """带 target 的 Tap 在交互信号不对时 FAILURE，不瞎按。

    瞎按的后果是把手里的东西放到错误的格子上。
    """
    ctx, ctrl = make_ctx()
    node = PR.Tap("pickup", {"n": "c", "gx": 5, "gz": 0}, "placement")
    assert node.tick(ctx) == Status.FAILURE
    assert ctrl.calls == []


def test_tap_with_target_ready_presses():
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(placement_gx=1, placement_gz=0)}
    ctx, ctrl = make_ctx(chef=chef)
    node = PR.Tap("pickup", {"n": "c", "gx": 1, "gz": 0}, "placement")
    assert node.tick(ctx) == Status.SUCCESS
    assert ("tap", "pickup") in ctrl.calls


def test_tap_halt_clears_edge_flag():
    ctx, ctrl = make_ctx()
    node = PR.Tap("pickup")
    node.tick(ctx)
    node.halt()
    for _ in range(3):
        ctx["hands"].tick()
    node.tick(ctx)
    assert ctrl.calls.count(("tap", "pickup")) == 2


# ---------- Confirm ----------

def test_confirm_pred_true_is_success():
    ctx, _ = make_ctx()
    assert PR.Confirm(lambda c: True).tick(ctx) == Status.SUCCESS


def test_confirm_retries_then_fails():
    """不满足 → 先 RUNNING 等状态刷新，retries 用尽才 FAILURE。

    等待是必需的：插件 5 Hz 推帧，刚按下的那一拍世界还没更新。
    """
    ctx, _ = make_ctx()
    node = PR.Confirm(lambda c: False, retries=2)
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.FAILURE


def test_confirm_succeeds_mid_retry():
    ctx, _ = make_ctx()
    flips = [False, False, True]
    node = PR.Confirm(lambda c: flips.pop(0), retries=3)
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.SUCCESS


def test_confirm_reset_restarts_budget():
    ctx, _ = make_ctx()
    node = PR.Confirm(lambda c: False, retries=1)
    node.tick(ctx)
    assert node.tick(ctx) == Status.FAILURE
    node.reset()
    assert node.tick(ctx) == Status.RUNNING


def test_confirm_double_condition_catches_dropped_item():
    """spec §7.5 第二类故障：只查"手空了"不够，东西可能掉地上。

    双条件由调用方写进 pred；这个测试钉住"写对了就能拦住"。
    """
    plate = {"n": "p1", "x": 1.2, "z": 0.0, "contents": []}
    w = FakeWorld(plates=[plate])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)

    def placed_in_plate(c):
        from ai import predicates as Pr
        return (Pr.hands_free(c["chef"])
                and "cucumber" in Pr.plate_contents(plate))

    assert PR.Confirm(placed_in_plate, retries=0).tick(ctx) == Status.FAILURE
    plate["contents"] = ["Cucumber"]
    assert PR.Confirm(placed_in_plate, retries=0).tick(ctx) == Status.SUCCESS


# ---------- Settle ----------

def test_settle_counts_down_then_succeeds():
    ctx, _ = make_ctx()
    node = PR.Settle(2)
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.SUCCESS


def test_settle_zero_is_immediate_success():
    ctx, _ = make_ctx()
    assert PR.Settle(0).tick(ctx) == Status.SUCCESS


def test_settle_reset_restarts():
    ctx, _ = make_ctx()
    node = PR.Settle(1)
    node.tick(ctx)
    assert node.tick(ctx) == Status.SUCCESS
    node.reset()
    assert node.tick(ctx) == Status.RUNNING


def test_settle_releases_movement():
    """等状态刷新期间不该还按着方向键。"""
    ctx, _ = make_ctx()
    ctx["hands"].set_move("right")
    PR.Settle(1).tick(ctx)
    assert ctx["hands"].move_dir is None


# ---------- HoldUntil ----------

def test_hold_until_presses_and_runs():
    ctx, ctrl = make_ctx()
    node = PR.HoldUntil("chop", lambda c: False, timeout=5)
    assert node.tick(ctx) == Status.RUNNING
    assert ("press", "chop") in ctrl.calls
    assert "chop" in ctx["hands"].held_keys


def test_hold_until_releases_on_success():
    """条件成立 → 松键 + SUCCESS。不松键就是卡键。"""
    ctx, ctrl = make_ctx()
    flips = [False, True]
    node = PR.HoldUntil("chop", lambda c: flips.pop(0), timeout=5)
    node.tick(ctx)
    assert node.tick(ctx) == Status.SUCCESS
    assert ("release", "chop") in ctrl.calls
    assert ctx["hands"].held_keys == set()


def test_hold_until_immediate_success_does_not_press():
    """条件一开始就成立 → 不按键。切好的菜不该再切一刀。"""
    ctx, ctrl = make_ctx()
    assert PR.HoldUntil("chop", lambda c: True, timeout=5).tick(ctx) \
        == Status.SUCCESS
    assert ctrl.calls == []


def test_hold_until_timeout_releases_and_fails():
    """超时 → 松键 + FAILURE（spec §7.2）。"""
    ctx, ctrl = make_ctx()
    node = PR.HoldUntil("chop", lambda c: False, timeout=2)
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.FAILURE
    assert ("release", "chop") in ctrl.calls
    assert ctx["hands"].held_keys == set()


def test_hold_until_halt_releases_key():
    """Review Focus 2：被中断抢占时必须松键，否则按键卡死到进程退出。

    这是本项目已踩过的坑（AI_REVIEW.md Bug#2 加 halt() 的由来），
    spec §11.5 把"零残留按键"列为浸泡测试硬标准。
    """
    ctx, ctrl = make_ctx()
    node = PR.HoldUntil("chop", lambda c: False, timeout=50)
    node.tick(ctx)
    assert "chop" in ctx["hands"].held_keys
    node.halt()
    assert ("release", "chop") in ctrl.calls
    assert ctx["hands"].held_keys == set()


def test_hold_until_halt_then_retick_presses_again():
    """halt 后重新 tick 是新的一次按住，预算也重置。"""
    ctx, ctrl = make_ctx()
    node = PR.HoldUntil("chop", lambda c: False, timeout=2)
    node.tick(ctx)
    node.tick(ctx)
    node.halt()
    assert node.tick(ctx) == Status.RUNNING
    assert ctrl.calls.count(("press", "chop")) == 2


def test_hold_until_timeout_is_required():
    """spec §7.2："timeout 无默认值，必须显式传"。

    给默认值等于把"这个动作该等多久"的领域知识藏进原语里，
    切菜和喷灭火器的合理时长差一个量级。
    """
    with pytest.raises(TypeError):
        PR.HoldUntil("chop", lambda c: True)


def test_hold_until_releases_movement():
    ctx, _ = make_ctx()
    ctx["hands"].set_move("right")
    PR.HoldUntil("chop", lambda c: False, timeout=5).tick(ctx)
    assert ctx["hands"].move_dir is None


def test_hold_until_position_check_fails_when_pushed_off():
    """位置感知：人被推离目标 → 松键 FAILURE，而不是空按死等。

    实机复现：切菜时被传送带推走，HoldUntil 不松键、done 永不成立 → 卡死。
    加了 target 后，interact_ready 一失手就 FAILURE，让 Sequence 重置重新
    Approach。
    """
    target = {"n": "board", "gx": 1, "gz": 0}
    chef = {"name": "Player 2", "x": 1.2, "z": 0.0, "held": None,
            "interactions": _inter(gx=1, gz=0)}
    ctx, ctrl = make_ctx(chef=chef)
    node = PR.HoldUntil("chop", lambda c: False, timeout=50, target=target)
    assert node.tick(ctx) == Status.RUNNING
    assert ("press", "chop") in ctrl.calls
    # 人被推走：interactions 不再指向目标
    ctx["chef"]["interactions"]["gx"] = 2
    assert node.tick(ctx) == Status.FAILURE
    assert ("release", "chop") in ctrl.calls
    assert ctx["hands"].held_keys == set()


# ---------- TapWhileHolding（spec §7.2，传送带必需）----------

def test_tap_while_holding_presses_hold_then_taps():
    """顶住反向键的同时单击：hold 在 tap 之前，顺序不能反。"""
    ctx, ctrl = make_ctx()
    node = PR.TapWhileHolding("pickup", "left")
    assert node.tick(ctx) == Status.SUCCESS
    assert ctrl.calls.index(("press", "left")) < \
        ctrl.calls.index(("tap", "pickup"))


def test_tap_while_holding_releases_hold_after_tap():
    """单击完松开顶带键——继续按着会让角色往反方向走。"""
    ctx, ctrl = make_ctx()
    PR.TapWhileHolding("pickup", "left").tick(ctx)
    assert ("release", "left") in ctrl.calls
    assert ctx["hands"].held_keys == set()


def test_tap_while_holding_is_edge_triggered():
    ctx, ctrl = make_ctx()
    node = PR.TapWhileHolding("pickup", "left")
    node.tick(ctx)
    for _ in range(3):
        ctx["hands"].tick()
    assert node.tick(ctx) == Status.SUCCESS
    assert ctrl.calls.count(("tap", "pickup")) == 1


def test_tap_while_holding_derives_hold_key_from_belt():
    """hold_key 传可调用：站在往右推的带上 → 顶左。"""
    w = FakeWorld(belt_cells={(0, 0)}, belt_dir={(0, 0): (1, 0)},
                  walkable=lambda gx, gz: True)
    ctx, ctrl = make_ctx(w=w, start=(0, 0))
    node = PR.TapWhileHolding(
        "pickup",
        lambda c: c["hands"].counter_dir(
            c["w"].belt_dir, (0, 0)))
    node.tick(ctx)
    assert ("press", "left") in ctrl.calls


def test_tap_while_holding_without_hold_key_still_taps():
    """不在传送带上（hold_key 解析为 None）→ 退化为普通单击。"""
    ctx, ctrl = make_ctx()
    node = PR.TapWhileHolding("pickup", lambda c: None)
    assert node.tick(ctx) == Status.SUCCESS
    assert ("tap", "pickup") in ctrl.calls
    assert not any(c[0] == "press" for c in ctrl.calls)


def test_tap_while_holding_halt_releases():
    ctx, ctrl = make_ctx()
    node = PR.TapWhileHolding("pickup", "left")
    node.tick(ctx)
    node.halt()
    assert ctx["hands"].held_keys == set()


def test_tap_while_holding_target_not_ready_is_failure():
    ctx, ctrl = make_ctx()
    node = PR.TapWhileHolding("pickup", "left",
                              {"n": "c", "gx": 5, "gz": 0}, "pickup")
    assert node.tick(ctx) == Status.FAILURE
    assert ctrl.calls == []


# ---------- EnsureHandsFree（spec §7.2 / §7.4）----------

def test_ensure_hands_free_is_idempotent_when_empty():
    """手已空 → 直接 SUCCESS，零按键。

    spec §7.2："幂等——手已空直接 SUCCESS，故可无条件置于任何树首。"
    """
    ctx, ctrl = make_ctx()
    assert PR.EnsureHandsFree().tick(ctx) == Status.SUCCESS
    assert ctrl.calls == []


def test_ensure_hands_free_keeps_held_target():
    """keep 命中（手里正是任务要保留的目标物）→ 直接 SUCCESS，不放下不按键。"""
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter()}
    ctx, ctrl = make_ctx(chef=chef)
    node = PR.EnsureHandsFree(keep=lambda c: c["chef"]["held"] == "Cucumber")
    assert node.tick(ctx) == Status.SUCCESS
    assert ctrl.calls == []


def test_ensure_hands_free_walks_to_counter_when_holding():
    """手里有东西 → 走向就近合法空台面（不是就地丢地上）。

    spec §7.4："放就近的合法空台面而非地上（地上虽也能捡，但可能挡路
    或在传送带上被冲走）。"
    """
    sts = [{"t": "counter", "n": "free", "gx": 3, "gz": 0,
            "x": 3 * 1.2, "z": 0.0}]
    w = FakeWorld(stations=sts,
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 4)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter()}
    ctx, ctrl = make_ctx(w=w, chef=chef)
    assert PR.EnsureHandsFree().tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "right"


def test_ensure_hands_free_succeeds_once_hands_empty():
    """放下成功（held 变空）→ SUCCESS。"""
    sts = [{"t": "counter", "n": "free", "gx": 1, "gz": 0,
            "x": 1.2, "z": 0.0}]
    w = FakeWorld(stations=sts, walkable=lambda gx, gz: gz == 0)
    # 真实帧里通用句柄 gx/gz 与 placement_gx/gz 会同时指向目标台面；
    # Face 走的是通用句柄，所以这里两个都填。
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter(gx=1, gz=0,
                                   placement_gx=1, placement_gz=0)}
    ctx, ctrl = make_ctx(w=w, chef=chef)
    node = PR.EnsureHandsFree()
    node.tick(ctx)                       # 到位 → 面向 → tap
    assert ("tap", "pickup") in ctrl.calls
    chef["held"] = None
    assert node.tick(ctx) == Status.SUCCESS


def test_ensure_hands_free_turns_before_tapping():
    """到交互位但没朝向目标 → 先转身，不许直接按键。

    旧实现只认 placement 句柄，帧里 `interactions.gx` 在厨师左右掉头时每拍
    都在两边跳；不转身就按，键落到游戏里时朝向已经不是那一拍的了。
    """
    sts = [{"t": "counter", "n": "free", "gx": 1, "gz": 0,
            "x": 1.2, "z": 0.0}]
    w = FakeWorld(stations=sts, walkable=lambda gx, gz: gz == 0)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter(placement_gx=1, placement_gz=0)}   # 通用句柄还没亮
    ctx, ctrl = make_ctx(w=w, chef=chef)
    node = PR.EnsureHandsFree()

    assert node.tick(ctx) == Status.RUNNING
    assert ("tap", "pickup") not in ctrl.calls, "没朝向就不该按"
    assert ctx["hands"].move_dir == "right", "应该朝目标压方向键"

    chef["interactions"] = _inter(gx=1, gz=0,
                                  placement_gx=1, placement_gz=0)
    node.tick(ctx)
    assert ("tap", "pickup") in ctrl.calls, "朝向对了才按"


def test_ensure_hands_free_reports_failure_when_tap_does_nothing():
    """按下去了但世界没变 → FAILURE，不能谎报 SUCCESS。

    实机事故：游戏忽略这次按键（锅一直在手上），节点却报 SUCCESS，
    "清手"这个前置被静默跳过，下游 tree 拿着上一口空锅继续跑，最后
    卡成 TakeCookedToPlate 端空锅反复去盘子倒的死循环。
    """
    sts = [{"t": "counter", "n": "free", "gx": 1, "gz": 0,
            "x": 1.2, "z": 0.0}]
    w = FakeWorld(stations=sts, walkable=lambda gx, gz: gz == 0)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter(gx=1, gz=0,
                                   placement_gx=1, placement_gz=0)}
    ctx, ctrl = make_ctx(w=w, chef=chef)
    node = PR.EnsureHandsFree()

    statuses = [node.tick(ctx) for _ in range(6)]   # held 始终没变
    assert ("tap", "pickup") in ctrl.calls, "确实按过"
    assert Status.SUCCESS not in statuses, "世界没变就绝不能报成功"
    assert Status.FAILURE in statuses, "校验超限要如实报 FAILURE"


def test_ensure_hands_free_latches_drop_target():
    """落点在本次激活里锁定：并列最近的台面不许每拍换一个。

    实测 countertop(3) 与 (17) 到厨师的路径代价都是 0，每拍重算会随位置
    来回换目标，表现为原地左右横跳。
    """
    sts = [{"t": "counter", "n": "left", "gx": -1, "gz": 0, "x": -1.2, "z": 0.0},
           {"t": "counter", "n": "right", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0}]
    w = FakeWorld(stations=sts, walkable=lambda gx, gz: gz == 0 and -2 <= gx <= 2)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)
    node = PR.EnsureHandsFree()
    node.tick(ctx)
    picked = node._tgt["n"]
    for _ in range(5):
        node.tick(ctx)
        assert node._tgt["n"] == picked, "激活期间落点不应改主意"


def test_ensure_hands_free_pick_hook_overrides_target():
    """pick 插口能改落点——物料感知规则（空锅回灶台…）从这里接进来。"""
    sts = [{"t": "counter", "n": "near", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0},
           {"t": "cooking", "n": "cooker", "gx": 3, "gz": 0, "x": 3.6, "z": 0.0}]
    w = FakeWorld(stations=sts, walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 4)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "utensil_pot_01",
            "interactions": _inter()}
    ctx, ctrl = make_ctx(w=w, chef=chef)

    def to_cooker(c):
        return c["w"].stations[1]          # 忽略默认的"就近空台面"

    node = PR.EnsureHandsFree(pick=to_cooker)
    node.tick(ctx)
    assert node._tgt["n"] == "cooker"
    assert ctx["hands"].move_dir == "right"
    assert ("tap", "pickup") not in ctrl.calls


def test_ensure_hands_free_home_cooker_false_never_targets_cooker():
    """home_cooker=False：手里的锅只去台面/地板，绝不回灶台。

    灭火端下来的锅正是这个场景——刚被清空的那口灶就是一张"空灶"，
    放回去＝接着烧（ai/tasks/fire.py）。
    """
    sts = [{"t": "cooking", "n": "cooker", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0},
           {"t": "counter", "n": "free", "gx": -1, "gz": 0, "x": -1.2, "z": 0.0}]
    cookers = [{"n": "cooker", "cooked": None}]          # 空灶（灶上没锅）
    items = [{"n": "utensil_pot_01", "x": 0.0, "z": 0.0,
              "has_contents": True, "cooked": "Raw"}]    # 手里那口生料锅
    w = FakeWorld(stations=sts, cookers=cookers, items=items,
                  walkable=lambda gx, gz: gz == 0 and -2 <= gx <= 2)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "utensil_pot_01",
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)

    default = PR.EnsureHandsFree()
    default.tick(ctx)
    assert default._tgt["n"] == "cooker", "默认行为是空灶归位"

    node = PR.EnsureHandsFree(home_cooker=False)
    node.tick(ctx)
    assert node._tgt["n"] == "free", "关掉归位后只能去台面"


def test_ensure_hands_free_burnt_pot_goes_to_counter_not_cooker():
    """糊锅不进"空锅归位"：它既不是空的也不是生料锅，回灶＝接着烧。

    这是灭火/灭火后最容易复燃的一条：刚清空的那口灶正是"空灶"。
    """
    sts = [{"t": "cooking", "n": "cooker", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0},
           {"t": "counter", "n": "free", "gx": -1, "gz": 0, "x": -1.2, "z": 0.0}]
    cookers = [{"n": "cooker", "cooked": None}]             # 空灶在招手
    items = [{"n": "utensil_pot_01", "x": 0.0, "z": 0.0,
              "has_contents": True, "cooked": "Burnt"}]
    w = FakeWorld(stations=sts, cookers=cookers, items=items,
                  walkable=lambda gx, gz: gz == 0 and -2 <= gx <= 2)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "utensil_pot_01",
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)

    node = PR.EnsureHandsFree()
    node.tick(ctx)
    assert node._tgt["n"] == "free", "糊锅只能去台面/地板"


def test_ensure_hands_free_falls_back_to_floor():
    """没有合法空台面 → 退到地板格（spec §6.1 落点约束）。"""
    w = FakeWorld(walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 3)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)
    # 没有 counter 站点，走地板兜底：应产生移动或就地放下，而非 FAILURE
    assert PR.EnsureHandsFree().tick(ctx) in (
        Status.RUNNING, Status.SUCCESS)


def test_ensure_hands_free_no_legal_spot_is_failure():
    """全图无合法落点 → FAILURE 交还 Planner，不原地死等。

    spec §6.1："只有腾手本身不可能时（如手持物无处可放）才判不可行。"
    """
    sts = [{"t": "cooking", "n": "c", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0}]
    w = FakeWorld(stations=sts,
                  walkable=lambda gx, gz: (gx, gz) in ((0, 0), (2, 0)))
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)
    assert PR.EnsureHandsFree().tick(ctx) == Status.FAILURE


def test_ensure_hands_free_halt_releases():
    sts = [{"t": "counter", "n": "free", "gx": 3, "gz": 0,
            "x": 3 * 1.2, "z": 0.0}]
    w = FakeWorld(stations=sts,
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 4)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)
    node = PR.EnsureHandsFree()
    node.tick(ctx)
    node.halt()
    assert ctx["hands"].move_dir is None


# ---------- InteractWith（spec §7.3 的核心产出）----------

def test_interact_with_requires_expect():
    """spec §7.3："构造要求必须给 expect，没有后置校验就建不出节点。

    这把"验证优先"从口号变成结构强制。"
    """
    with pytest.raises(TypeError):
        PR.InteractWith({"n": "c", "gx": 1, "gz": 0}, "pickup")


def test_interact_with_full_sequence():
    """approach → face → tap → confirm，四步都走到。"""
    crate = {"t": "crate", "n": "crate1", "ing": "Cucumber",
             "gx": 2, "gz": 0, "x": 2 * 1.2, "z": 0.0}
    w = FakeWorld(stations=[crate],
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 3)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter()}
    ctx, ctrl = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(crate, "pickup",
                           lambda c: c["chef"].get("held") == "Cucumber")

    assert node.tick(ctx) == Status.RUNNING          # 走路
    assert ctx["hands"].move_dir == "right"

    # 到位：走到 crate (2,0) 的相邻格 (1,0)，且 pickup 句柄已指向 crate
    ctx["chef"]["x"] = 1 * 1.2
    ctx["chef"]["z"] = 0.0
    chef["interactions"] = _inter(pickup_gx=2, pickup_gz=0, gx=2, gz=0)
    assert node.tick(ctx) == Status.RUNNING          # 到位 → tap，等确认
    assert ("tap", "pickup") in ctrl.calls

    chef["held"] = "Cucumber"
    assert node.tick(ctx) == Status.SUCCESS


def test_interact_with_holds_belt_counter_key_on_tap():
    """站在传送带上拾取时，边顶住反向键边按（spec §7.2）。

    InteractWith 用 TapWhileHolding 而非 Tap：站普通地面时 hold_key 解析
    为 None、退化为普通单击；站传送带上时按住反向键防被推走。
    """
    target = {"n": "counter", "gx": 0, "gz": -1}
    w = FakeWorld(walkable=lambda gx, gz: gz == 0 and -2 <= gx <= 2,
                  belt_cells={(0, 0)}, belt_dir={(0, 0): (1, 0)})
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(pickup_gx=0, pickup_gz=-1,
                                   gx=0, gz=-1)}
    ctx, ctrl = make_ctx(w=w, chef=chef, start=(0, 0))
    node = PR.InteractWith(target, "pickup",
                           lambda c: c["chef"].get("held") == "Cucumber")
    node.tick(ctx)
    assert ("press", "left") in ctrl.calls, "要顶住往右推的传送带"
    assert ("tap", "pickup") in ctrl.calls
    assert ("release", "left") in ctrl.calls


def test_interact_with_expect_false_eventually_fails():
    """按了但世界没变 → 重试用尽后 FAILURE，交还 Planner。"""
    crate = {"t": "crate", "n": "c", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0}
    w = FakeWorld(stations=[crate], walkable=lambda gx, gz: gz == 0)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(pickup_gx=1, pickup_gz=0, gx=1, gz=0)}
    ctx, _ = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(crate, "pickup", lambda c: False, retries=1)
    seen = [node.tick(ctx) for _ in range(6)]
    assert Status.FAILURE in seen


def test_interact_with_target_gone_is_failure():
    """Review Focus 5：目标中途消失 → FAILURE，不崩。"""
    ctx, _ = make_ctx()
    node = PR.InteractWith(lambda c: None, "pickup", lambda c: False)
    assert node.tick(ctx) == Status.FAILURE


def test_interact_with_skips_face_when_disabled():
    """face=False：拾取地面物不需要精确朝向（旧 _goto_grab_cell 的场景）。"""
    it = {"n": "utensil_fire_extinguisher_01", "x": 1.2, "z": 0.0}
    w = FakeWorld(items=[it], walkable=lambda gx, gz: gz == 0)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(pickup="utensil_fire_extinguisher_01")}
    ctx, ctrl = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(it, "pickup",
                           lambda c: c["chef"].get("held") is not None,
                           face=False)
    node.tick(ctx)
    assert ("tap", "pickup") in ctrl.calls


def test_interact_with_halt_releases_everything():
    crate = {"t": "crate", "n": "c", "gx": 3, "gz": 0, "x": 3 * 1.2, "z": 0.0}
    w = FakeWorld(stations=[crate],
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 4)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(crate, "pickup", lambda c: False)
    node.tick(ctx)
    node.halt()
    assert ctx["hands"].move_dir is None
    assert ctx["hands"].held_keys == set()


def test_interact_with_fails_when_hands_change_unexpectedly():
    """spec §7.5 第三类故障：手部连续性不变式。

    原话："**误放下 / 误拾**（没打算放却放了）→ 每个原语开头确认'手里的
    东西与我以为的一致'，不一致说明中间发生意外 → 停下来让 Planner 按
    现实重算。"

    场景：走向箱子的途中，手里莫名多了个东西（被 P1 塞过来、传送带冲来
    一块料、误拾）。继续执行会把这个意外物品放进盘里。
    """
    crate = {"t": "crate", "n": "c", "gx": 3, "gz": 0, "x": 3 * 1.2, "z": 0.0}
    w = FakeWorld(stations=[crate],
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 4)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(crate, "pickup", lambda c: False)
    assert node.tick(ctx) == Status.RUNNING      # 第一拍：记下手是空的
    chef["held"] = "Lettuce"                     # 没按键，手里却多了东西
    assert node.tick(ctx) == Status.FAILURE
    assert ctx["hands"].move_dir is None


def test_interact_with_tolerates_hands_change_after_tap():
    """按下之后手里本该变——那是 expect 的职责，不是异常。

    若不区分"按键前/后"，拾取成功的那一拍会被不变式误判成意外，
    每次取料都 FAILURE。
    """
    crate = {"t": "crate", "n": "c", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0}
    w = FakeWorld(stations=[crate], walkable=lambda gx, gz: gz == 0)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(pickup_gx=1, pickup_gz=0, gx=1, gz=0)}
    ctx, ctrl = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(crate, "pickup",
                           lambda c: c["chef"].get("held") == "Cucumber")
    node.tick(ctx)                               # 到位并 tap
    assert ("tap", "pickup") in ctrl.calls
    chef["held"] = "Cucumber"                    # 拾取生效
    assert node.tick(ctx) == Status.SUCCESS


def test_interact_with_first_tick_never_trips_invariant():
    """首次 tick 没有"上一拍"，手里有东西也不该算不一致。

    否则所有"手持某物去放下"的 task 第一拍就 FAILURE。
    """
    counter = {"t": "counter", "n": "c", "gx": 1, "gz": 0,
               "x": 1.2, "z": 0.0}
    w = FakeWorld(stations=[counter], walkable=lambda gx, gz: gz == 0)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(counter, "placement", lambda c: False)
    assert node.tick(ctx) != Status.FAILURE


def test_interact_with_does_not_double_tap_across_ticks():
    """Review Focus 1 在复合原语上同样成立：等确认期间不重按。"""
    crate = {"t": "crate", "n": "c", "gx": 1, "gz": 0, "x": 1.2, "z": 0.0}
    w = FakeWorld(stations=[crate], walkable=lambda gx, gz: gz == 0)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None,
            "interactions": _inter(pickup_gx=1, pickup_gz=0, gx=1, gz=0)}
    ctx, ctrl = make_ctx(w=w, chef=chef)
    node = PR.InteractWith(crate, "pickup", lambda c: False, retries=5)
    for _ in range(4):
        node.tick(ctx)
        ctx["hands"].tick()
    assert ctrl.calls.count(("tap", "pickup")) == 1, \
        "等确认期间重按 = 拿起又放下"


# ---------- 脱困三原语（spec §7.2）----------

def test_mark_blocked_writes_scratch():
    """L1：把卡住的格拉黑，下次 Approach 的距离场绕开它。"""
    from ai.blackboard import StuckScratch
    bb = Blackboard()
    bb.scratch = StuckScratch()
    ctx, _ = make_ctx(bb=bb, tick=100)
    assert PR.MarkBlocked((3, 1), ttl=80).tick(ctx) == Status.SUCCESS
    assert bb.scratch.bad_cells[(3, 1)] == 180


def test_mark_blocked_creates_scratch_if_missing():
    """scratch 为 None 时自建——卡住守卫可能先于任何模式进入。"""
    bb = Blackboard()
    ctx, _ = make_ctx(bb=bb, tick=0)
    PR.MarkBlocked((1, 1), ttl=10).tick(ctx)
    assert bb.scratch is not None
    assert (1, 1) in bb.scratch.bad_cells


def test_mark_blocked_accepts_callable_cell():
    from ai.blackboard import StuckScratch
    bb = Blackboard()
    bb.scratch = StuckScratch()
    ctx, _ = make_ctx(bb=bb, tick=5)
    PR.MarkBlocked(lambda c: (7, 7), ttl=10).tick(ctx).lower()
    assert (7, 7) in bb.scratch.bad_cells


def test_mark_blocked_none_cell_is_failure():
    ctx, _ = make_ctx()
    assert PR.MarkBlocked(lambda c: None, ttl=10).tick(ctx) == Status.FAILURE


def test_nudge_presses_direction_for_n_ticks():
    """L2：侧向微移固定拍数。"""
    ctx, ctrl = make_ctx()
    node = PR.Nudge("up", ticks=2)
    assert node.tick(ctx) == Status.RUNNING
    assert ctx["hands"].move_dir == "up"
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.SUCCESS
    assert ctx["hands"].move_dir is None


def test_nudge_releases_on_halt():
    ctx, _ = make_ctx()
    node = PR.Nudge("up", ticks=5)
    node.tick(ctx)
    node.halt()
    assert ctx["hands"].move_dir is None


def test_nudge_callable_direction():
    ctx, _ = make_ctx()
    PR.Nudge(lambda c: "down", ticks=1).tick(ctx)
    assert ctx["hands"].move_dir == "down"


def test_nudge_none_direction_is_failure():
    ctx, _ = make_ctx()
    assert PR.Nudge(lambda c: None, ticks=1).tick(ctx) == Status.FAILURE


def test_abandon_target_writes_scratch_not_commitment():
    """L3：拉黑目标并记进 scratch。

    不直接清 bb.commitment：spec §4.5 规定 Executor 对 commitment 只有
    steps_done / retries 的写权。Planner 在可行性过滤时读 scratch.abandoned
    （Plan 3 接），这是守住写入规则的唯一做法。
    """
    from ai.blackboard import StuckScratch
    bb = Blackboard()
    bb.scratch = StuckScratch()
    before = bb.commitment.active_task
    ctx, _ = make_ctx(bb=bb, tick=50)
    assert PR.AbandonTarget("cooker1", ttl=200).tick(ctx) == Status.SUCCESS
    assert bb.scratch.abandoned["cooker1"] == 250
    assert bb.commitment.active_task is before, \
        "Executor 不许直接动 commitment.active_task（spec §4.5）"


def test_abandon_target_accepts_station_dict():
    """传站点 dict 时按实例名 n 拉黑（资源 id 规则，spec §4.4）。"""
    from ai.blackboard import StuckScratch
    bb = Blackboard()
    bb.scratch = StuckScratch()
    ctx, _ = make_ctx(bb=bb, tick=0)
    PR.AbandonTarget({"n": "workstation_cooker_01 (3)"}, ttl=10).tick(ctx)
    assert "workstation_cooker_01 (3)" in bb.scratch.abandoned


def test_abandon_target_none_is_failure():
    ctx, _ = make_ctx()
    assert PR.AbandonTarget(lambda c: None, ttl=10).tick(ctx) \
        == Status.FAILURE


# ---------- Timeout 装饰（spec §7.2 结构性安全网）----------

def test_timeout_passes_through_child_status():
    ctx, _ = make_ctx()
    assert PR.Timeout(PR.Settle(0), 10).tick(ctx) == Status.SUCCESS


def test_timeout_fires_after_max_ticks():
    """整个 task 级超时：今天只有单步重试，没有"这个 task 卡了 30 拍"
    的兜底（spec §7.2）。防死循环的最后一道闸。"""
    ctx, _ = make_ctx()
    node = PR.Timeout(PR.Settle(100), max_ticks=3)
    for _ in range(3):
        assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.FAILURE


def test_timeout_halts_child_when_firing():
    """超时转 FAILURE 时必须 halt 子树，否则 HoldUntil 的键留着。"""
    ctx, ctrl = make_ctx()
    child = PR.HoldUntil("chop", lambda c: False, timeout=100)
    node = PR.Timeout(child, max_ticks=1)
    node.tick(ctx)
    assert "chop" in ctx["hands"].held_keys
    assert node.tick(ctx) == Status.FAILURE
    assert ctx["hands"].held_keys == set()


def test_timeout_budget_resets_after_child_completes():
    """子树 SUCCESS 后预算重置——树是跨拍复用的，不重置会越跑越紧。"""
    ctx, _ = make_ctx()
    child = PR.Settle(1)
    node = PR.Timeout(child, max_ticks=2)
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.SUCCESS
    assert node.tick(ctx) == Status.RUNNING, "预算该重置了"


def test_timeout_halt_propagates():
    ctx, _ = make_ctx()
    child = PR.HoldUntil("chop", lambda c: False, timeout=100)
    node = PR.Timeout(child, max_ticks=50)
    node.tick(ctx)
    node.halt()
    assert ctx["hands"].held_keys == set()


# ---------- EnsureHandsFree 的落点分派（按手持物条件拼）----------

def _pot_world(has_contents, cooked, cooker_cooked=None):
    sts = [{"t": "counter", "n": "counter", "gx": 1, "gz": 0,
            "x": 1.2, "z": 0.0},
           {"t": "cooking", "n": "cooker", "gx": 3, "gz": 0,
            "x": 3.6, "z": 0.0}]
    return FakeWorld(
        stations=sts, cookers=[{"n": "cooker", "cooked": cooker_cooked}],
        items=[{"n": "utensil_pot_01", "x": 0.0, "z": 0.0,
                "has_contents": has_contents, "cooked": cooked}],
        walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 4)


def _pot_ctx(w):
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "utensil_pot_01",
            "interactions": _inter()}
    return make_ctx(w=w, chef=chef)


def test_ensure_hands_free_routes_empty_pot_to_cooker():
    """空锅 → 空灶台，不是就近空台面。

    实测：游戏忽略"把锅放到普通台面"的按键（25 拍内十几次 tap，一次没
    成），而锅↔灶、锅→盘都成功过。空锅归位走灶台既是 ReturnPot 的语义，
    也是唯一被验证可行的落点。
    """
    ctx, _ = _pot_ctx(_pot_world(has_contents=False, cooked="Raw"))
    node = PR.EnsureHandsFree()
    node.tick(ctx)
    assert node._tgt["n"] == "cooker"
    assert ctx["hands"].move_dir == "right"


def test_ensure_hands_free_never_routes_cooked_pot_to_cooker():
    """熟锅不上灶台 —— 放回去会重新点火，接着烧糊。"""
    ctx, _ = _pot_ctx(_pot_world(has_contents=True, cooked="Cooked"))
    node = PR.EnsureHandsFree()
    node.tick(ctx)
    assert node._tgt["n"] == "counter"


def test_ensure_hands_free_empty_pot_falls_back_without_empty_cooker():
    """一口空灶都没有 → 退到台面，不能就地卡死。"""
    ctx, _ = _pot_ctx(_pot_world(has_contents=False, cooked="Raw",
                                 cooker_cooked=True))
    node = PR.EnsureHandsFree()
    node.tick(ctx)
    assert node._tgt["n"] == "counter"


def test_ensure_hands_free_unknown_pot_state_is_not_treated_as_empty():
    """锅的内容读不到 → 不当空锅，退到默认台面。"""
    sts = [{"t": "counter", "n": "counter", "gx": 1, "gz": 0,
            "x": 1.2, "z": 0.0},
           {"t": "cooking", "n": "cooker", "gx": 3, "gz": 0,
            "x": 3.6, "z": 0.0}]
    w = FakeWorld(stations=sts, cookers=[{"n": "cooker", "cooked": None}],
                  items=[],                      # items[] 里没有这口锅
                  walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 4)
    ctx, _ = _pot_ctx(w)
    node = PR.EnsureHandsFree()
    node.tick(ctx)
    assert node._tgt["n"] == "counter"


# ---------- WaitUntil（不按键等谓词）----------

def test_wait_until_succeeds_when_pred_true():
    w = FakeWorld()
    ctx, _ = make_ctx(w)
    node = PR.WaitUntil(lambda c: True, timeout=10)
    assert node.tick(ctx) == Status.SUCCESS


def test_wait_until_running_until_pred_then_success():
    w = FakeWorld()
    ctx, _ = make_ctx(w)
    flag = {"ready": False}
    node = PR.WaitUntil(lambda c: flag["ready"], timeout=10)
    assert node.tick(ctx) == Status.RUNNING
    assert node.tick(ctx) == Status.RUNNING
    flag["ready"] = True
    assert node.tick(ctx) == Status.SUCCESS


def test_wait_until_fails_on_timeout():
    w = FakeWorld()
    ctx, _ = make_ctx(w)
    node = PR.WaitUntil(lambda c: False, timeout=3)
    s = Status.RUNNING
    for _ in range(4):
        s = node.tick(ctx)
    assert s == Status.FAILURE


def test_throw_to_succeeds_when_hands_free():
    ctx, _ = make_ctx()
    node = PR.ThrowTo(lambda c: {"x": 5.0, "z": 0.0})
    assert node.tick(ctx) == Status.SUCCESS


def test_throw_to_runs_when_holding():
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter()}
    ctx, _ = make_ctx(chef=chef)
    node = PR.ThrowTo(lambda c: {"x": 5.0, "z": 0.0})
    assert node.tick(ctx) in (Status.RUNNING, Status.SUCCESS)


def test_ensure_hands_free_strict_no_fallback_to_floor():
    """strict=True：指定落点（pick）找不到时不退地板，直接 FAILURE 交还 Planner。"""
    w = FakeWorld(walkable=lambda gx, gz: gz == 0 and 0 <= gx <= 3)
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": _inter()}
    ctx, _ = make_ctx(w=w, chef=chef)
    # 无 counter 站点：默认 pick 找不到空台面。非 strict 会退地板，strict 必须 FAILURE。
    assert PR.EnsureHandsFree(strict=True).tick(ctx) == Status.FAILURE

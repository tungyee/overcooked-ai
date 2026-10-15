"""task 身份 / 可行性 / 完成判定 / 树结构单测。

ctx 的装配复用 tests/test_primitives.py 的做法（假 World + 假 controller），
零按键注入。
"""
import pytest

from ai import primitives as PR
from ai import tasks as T
from ai.bt import Condition, Guard, RepeatUntil, Selector, Sequence, Status
from ai.blackboard import Blackboard
from ai.hands import Hands
from ai.navigation import distance_field
from ai.recipes import RecipeBook


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
    def __init__(self, **kw):
        self.stations = kw.get("stations") or []
        self.plates = kw.get("plates") or []
        self.workstations = kw.get("workstations") or []
        self.cookers = kw.get("cookers") or []
        self.items = kw.get("items") or []
        self.chefs = kw.get("chefs") or []
        self.orders = kw.get("orders") or []
        self.recipes = kw.get("recipes") or []
        self.extinguishers = kw.get("extinguishers") or []
        self.level = kw.get("level") or "Sushi_1_4_2P"
        self.belt_cells = set(kw.get("belt_cells") or ())
        self.belt_dir = kw.get("belt_dir") or {}
        self.score = kw.get("score") or {}
        self._walk = kw.get("walkable") or (lambda gx, gz: True)
        self._cell = 1.2

    def walkable(self, gx, gz):
        return self._walk(gx, gz)

    def to_grid(self, x, z):
        return (int(round(x / self._cell)), int(round(z / self._cell)))

    def to_world(self, gx, gz):
        return (gx * self._cell, gz * self._cell)


def inter(**kw):
    base = {"interactable": None, "pickup": None, "placement": None,
            "grid": None, "gx": None, "gz": None,
            "pickup_gx": None, "pickup_gz": None,
            "placement_gx": None, "placement_gz": None}
    base.update(kw)
    return base


def make_ctx(w, chef=None, bb=None, tick=0, start=(0, 0)):
    chef = chef if chef is not None else {
        "name": "Player 2", "x": start[0] * 1.2, "z": start[1] * 1.2,
        "held": None, "interactions": inter()}
    ctrl = FakeCtrl()
    cur = w.to_grid(chef["x"], chef["z"])
    return {
        "w": w,
        "chef": chef,
        "hands": Hands(ctrl),
        "bb": bb if bb is not None else Blackboard(),
        "field": distance_field(cur, w.walkable, w.belt_dir),
        "book": RecipeBook(w.recipes, w.stations),
        "tick": tick,
    }, ctrl


# ---------- 身份（spec §6.0 / Review Focus 3）----------

class Dummy(T.Task):
    TYPE = "Dummy"

    def claims(self):
        return ((self.resources[0], "use"),) if self.resources else ()

    def feasible(self, ctx):
        return True

    def done(self, ctx):
        return False

    def build(self):
        return Sequence([])


def test_task_key_is_four_tuple():
    t = Dummy(order_id="1", resources=("plate_A",), item="Cucumber")
    assert t.key == ("Dummy", "1", ("plate_A",), "Cucumber")


def test_tasks_differing_in_resource_are_different():
    """核心：绑定不同盘的同类 task 是两个 task（spec §6.0）。

    否则两单都缺黄瓜时，P1 放入盘 B 会让针对盘 A 的 task 误报成功。
    """
    a = Dummy(order_id="1", resources=("plate_A",), item="Cucumber")
    b = Dummy(order_id="2", resources=("plate_B",), item="Cucumber")
    assert a != b
    assert a.key != b.key
    assert len({a, b}) == 2


def test_tasks_with_same_four_items_are_equal():
    a = Dummy(order_id="1", resources=("p",), item="X")
    b = Dummy(order_id="1", resources=("p",), item="X")
    assert a == b and hash(a) == hash(b)
    assert len({a, b}) == 1


def test_task_is_immutable():
    """不可变值对象：Planner 每拍重算、把它当字典键用，可变会出错。"""
    t = Dummy(order_id="1", resources=("p",), item="X")
    with pytest.raises(AttributeError):
        t.order_id = "2"


def test_task_resources_normalized_to_tuple():
    """传 list 也存成 tuple，否则 hash 会炸。"""
    t = Dummy(resources=["a", "b"])
    assert t.resources == ("a", "b")
    assert hash(t) is not None


def test_task_repr_shows_identity():
    t = Dummy(order_id="1", resources=("plate_A",), item="Cucumber")
    r = repr(t)
    assert "Dummy" in r and "plate_A" in r and "Cucumber" in r


def test_task_base_methods_are_abstract():
    class Bare(T.Task):
        TYPE = "Bare"
    t = Bare()
    for name in ("claims", "feasible", "done", "build"):
        with pytest.raises(NotImplementedError):
            fn = getattr(t, name)
            fn({}) if name in ("feasible", "done") else fn()


# ---------- claims_available（spec §4.2）----------

def test_claims_available_when_unclaimed():
    w = FakeWorld()
    ctx, _ = make_ctx(w)
    t = Dummy(resources=("cooker1",))
    assert T.claims_available(t, ctx) is True


def test_claims_available_false_when_claimed_by_other():
    """spec §4.2："定义了认领表却没有任何可行性谓词读它"是初版致命缺陷。

    这一条优先于各 task 自己的谓词。
    """
    bb = Blackboard()
    bb.claim("cooker1", "use", owner="P1", order_id="9", tick=0)
    ctx, _ = make_ctx(FakeWorld(), bb=bb, tick=1)
    assert T.claims_available(Dummy(resources=("cooker1",)), ctx) is False


def test_claims_available_true_when_claim_expired():
    """TTL 过期的认领不算阻塞（spec §4.1 触发点 4 的兜底）。"""
    bb = Blackboard()
    bb.claim("cooker1", "use", owner="P1", order_id="9", tick=0, ttl=5)
    ctx, _ = make_ctx(FakeWorld(), bb=bb, tick=100)
    assert T.claims_available(Dummy(resources=("cooker1",)), ctx) is True


def test_claims_available_true_for_own_claim():
    """自己认领的资源当然可用——否则第二拍就判自己不可行。"""
    bb = Blackboard()
    bb.claim("cooker1", "use", owner=T.OWNER, order_id="1", tick=0)
    ctx, _ = make_ctx(FakeWorld(), bb=bb, tick=1)
    assert T.claims_available(Dummy(resources=("cooker1",)), ctx) is True


def test_claims_available_action_level_granularity():
    """spec §4.0：键是 (resource, action)，锁一个动作不锁整个资源。

    P1 往 1 号锅放了生饭 → (cooker1,"fetch_to") 被锁，但
    (cooker1,"take_cooked") 依然开放——取熟饭正是最有价值的接力。
    """
    class TakeCooked(T.Task):
        TYPE = "TakeCooked"

        def claims(self):
            return ((self.resources[0], "take_cooked"),)

        def feasible(self, ctx):
            return True

        def done(self, ctx):
            return False

        def build(self):
            return Sequence([])

    bb = Blackboard()
    bb.claim("cooker1", "fetch_to", owner="P1", order_id="9", tick=0)
    ctx, _ = make_ctx(FakeWorld(), bb=bb, tick=1)
    assert T.claims_available(TakeCooked(resources=("cooker1",)), ctx) is True


def test_claims_available_no_claims_is_true():
    ctx, _ = make_ctx(FakeWorld())
    assert T.claims_available(Dummy(), ctx) is True


def test_task_types_registry_covers_all_subclasses():
    """TASK_TYPES 是 tree_viz 的入口，漏登记的 task 就看不到。"""
    assert isinstance(T.TASK_TYPES, dict)
    for name, cls in T.TASK_TYPES.items():
        assert issubclass(cls, T.Task)
        assert cls.TYPE == name


def test_package_facade_exports_base_names():
    """子模块边界是实现细节：外部只认 `from ai import tasks as T`。

    tasks 拆成包是为满足 spec §11.5 的 400 行上限，但这不该泄漏到调用方。
    """
    for name in ("Task", "TASK_TYPES", "OWNER", "claims_available",
                 "ingredient_available", "ingredient_source"):
        assert hasattr(T, name), "包门面漏了 %s" % name


# ---------- 盘管理（spec §6.1 第 1/2/14 号）----------

RECIPES = [
    {"name": "Sushi_Cucumber", "comp":
        ["Seaweed", "BoiledSushiRice", "Cucumber"], "cook": "null"},
    {"name": "BoiledSushiRice", "comp": ["SushiRice"], "cook": "Pot"},
    {"name": "Seaweed", "comp": [], "cook": "null"},
    {"name": "Cucumber", "comp": [], "cook": "null"},
    {"name": "SushiRice", "comp": [], "cook": "null"},
]
ORDER = {"id": "1", "dish": "Sushi_Cucumber",
         "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]}

COUNTER = {"t": "counter", "n": "assembly", "gx": 7, "gz": 0,
           "x": 7 * 1.2, "z": 0.0}
STACK = {"t": "plate_stack", "n": "stack1", "gx": -1, "gz": 0,
         "x": -1.2, "z": 0.0}
DRYING = {"t": "plate_return", "n": "DryingPart", "gx": -3, "gz": 0,
          "x": -3 * 1.2, "z": 0.0}
SINK = {"t": "washing", "n": "sink1", "gx": -5, "gz": 0,
        "x": -5 * 1.2, "z": 0.0}


def plate(n, gx, gz, contents=(), count=None):
    return {"n": n, "x": gx * 1.2, "z": gz * 1.2,
            "contents": list(contents),
            "count": len(contents) if count is None else count}


def test_place_clean_plate_claims_plate_and_counter():
    """认领盘 + 拼装格（spec §6.1 第 1 号）。"""
    t = T.PlaceCleanPlate(order_id="1", resources=("p1", (7, 0)))
    assert set(t.claims()) == {("p1", "place"), ((7, 0), "assemble")}


def test_place_clean_plate_feasible_when_plate_clean_and_counter_free():
    w = FakeWorld(stations=[COUNTER, STACK], recipes=RECIPES,
                  orders=[ORDER], plates=[plate("p1", -1, 0)])
    ctx, _ = make_ctx(w)
    t = T.PlaceCleanPlate(order_id="1", resources=("p1", (7, 0)))
    assert t.feasible(ctx) is True


def test_place_clean_plate_infeasible_when_plate_not_clean():
    """盘里已有料 → 不是"干净盘"，不该再拿它去摆。"""
    w = FakeWorld(stations=[COUNTER, STACK], recipes=RECIPES,
                  orders=[ORDER], plates=[plate("p1", -1, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    t = T.PlaceCleanPlate(order_id="1", resources=("p1", (7, 0)))
    assert t.feasible(ctx) is False


def test_place_clean_plate_infeasible_when_plate_gone():
    """认领的盘不在快照里（被端走）→ 不可行（spec §4.1 触发点 3）。"""
    w = FakeWorld(stations=[COUNTER, STACK], recipes=RECIPES,
                  orders=[ORDER], plates=[])
    ctx, _ = make_ctx(w)
    t = T.PlaceCleanPlate(order_id="1", resources=("p1", (7, 0)))
    assert t.feasible(ctx) is False


def test_place_clean_plate_infeasible_when_already_done():
    """盘已在拼装格上 → 已完成，不再可行（上下游咬合）。"""
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0)])
    ctx, _ = make_ctx(w)
    t = T.PlaceCleanPlate(order_id="1", resources=("p1", (7, 0)))
    assert t.done(ctx) is True
    assert t.feasible(ctx) is False


def test_place_clean_plate_done_binds_specific_plate():
    """Review Focus 3：完成判定绑定具体盘。

    盘 B 到了拼装格不该让针对盘 A 的 task 报成功。
    """
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("pB", 7, 0), plate("pA", -1, 0)])
    ctx, _ = make_ctx(w)
    assert T.PlaceCleanPlate("1", ("pA", (7, 0))).done(ctx) is False
    assert T.PlaceCleanPlate("1", ("pB", (7, 0))).done(ctx) is True


def test_place_clean_plate_tree_is_two_interactions():
    """spec §7.3：树退化为几步线性顺序。拿盘 + 放盘。"""
    t = T.PlaceCleanPlate(order_id="1", resources=("p1", (7, 0)))
    tree = t.build()
    assert isinstance(tree, PR.Timeout)
    assert isinstance(tree.child, Sequence)
    assert len(tree.child.children) == 3
    assert isinstance(tree.child.children[0], PR.EnsureHandsFree)


def test_wash_plate_claims_sink():
    assert T.WashPlate(resources=("sink1",)).claims() == (("sink1", "wash"),)


def test_wash_plate_feasible_when_dirty_exists_and_no_clean():
    w = FakeWorld(stations=[SINK, DRYING], recipes=RECIPES, orders=[ORDER],
                  items=[{"n": "DirtyPlateStack", "x": 0.0, "z": -12.0}],
                  plates=[])
    ctx, _ = make_ctx(w)
    assert T.WashPlate(resources=("sink1",)).feasible(ctx) is True


def test_wash_plate_feasible_with_one_clean_plate_remaining():
    """提前补盘：只剩 1 块干净盘时允许洗，不等到用光拼装被卡死。

    那块盘要放在**拼装格**（已在用），不能放盘堆/晾干架——后者是 `done` 的
    判据（"洗出来的盘已经在可取位"），放那儿这条用例测的就不是"提前补盘"了。
    """
    w = FakeWorld(stations=[SINK, DRYING, STACK, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  items=[{"n": "DirtyPlateStack", "x": 0.0, "z": -12.0}],
                  plates=[plate("p1", 7, 0)])
    ctx, _ = make_ctx(w)
    assert T.WashPlate(resources=("sink1",)).feasible(ctx) is True


def test_wash_plate_infeasible_when_enough_clean_plates():
    """干净盘 >= PLATE_BUFFER(2) 时不洗，避免无谓洗盘。"""
    w = FakeWorld(stations=[SINK, DRYING, STACK], recipes=RECIPES,
                  orders=[ORDER],
                  items=[{"n": "DirtyPlateStack", "x": 0.0, "z": -12.0}],
                  plates=[plate("p1", -1, 0), plate("p2", -1, 1)])
    ctx, _ = make_ctx(w)
    assert T.WashPlate(resources=("sink1",)).feasible(ctx) is False


def test_wash_plate_infeasible_without_dirty_plates():
    w = FakeWorld(stations=[SINK, DRYING], recipes=RECIPES, orders=[ORDER],
                  items=[], plates=[])
    ctx, _ = make_ctx(w)
    assert T.WashPlate(resources=("sink1",)).feasible(ctx) is False


def test_wash_plate_done_requires_clean_plate_at_drying_or_stack():
    """spec §6.1 第 2 号注：洗完的盘从**晾干架**出来，不是盘堆。

    初版完成判定只写"出现可用干净盘"没说去哪取，实施时会漏。
    """
    w = FakeWorld(stations=[SINK, DRYING], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", -3, 0)])     # 在晾干架位置
    ctx, _ = make_ctx(w)
    assert T.WashPlate(resources=("sink1",)).done(ctx) is True

    w2 = FakeWorld(stations=[SINK, DRYING], recipes=RECIPES, orders=[ORDER],
                   plates=[plate("p1", 2, 0)])     # 在别处的空盘
    ctx2, _ = make_ctx(w2)
    assert T.WashPlate(resources=("sink1",)).done(ctx2) is False


def test_wash_plate_infeasible_when_already_done():
    """`done` 已成立就不该再可行（下游 PlaceCleanPlate 的 feasible 咬合）。

    实机 trace-20261014-221306：晾干架上躺着一块干净盘（clean=1 < PLATE_BUFFER）、
    回盘台又有脏盘摞，于是 `feasible` 恒真、WashPlate 以 4.00 分连吃 220 拍
    （t=6117~6193、7228~7370），而 `done` 从第一拍起就是 True——树一建起来
    `HoldUntil` 的谓词立刻成立，整段既不按洗盘键也不碰那块干净盘。
    同期 `PlaceCleanPlate` 可行 1101 拍、一次没选上。
    """
    w = FakeWorld(stations=[SINK, DRYING], recipes=RECIPES, orders=[ORDER],
                  items=[{"n": "DirtyPlateStack", "x": 0.0, "z": -12.0}],
                  plates=[plate("p1", -3, 0)])       # 已在晾干架 → done
    ctx, _ = make_ctx(w)
    task = T.WashPlate(resources=("sink1",))
    assert task.done(ctx) is True
    assert task.feasible(ctx) is False


def test_reset_plate_feasible_when_contents_match_no_order():
    """spec §6.1 第 14 号：盘内容与任何现存订单需求都不匹配 → 清盘。

    场景①：盘里被放错料，错料盘停在拼装格上占着位置。
    """
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0, ["Lettuce"])])
    ctx, _ = make_ctx(w)
    t = T.ResetPlate(order_id=None, resources=("p1", (7, 0)))
    assert t.feasible(ctx) is True


def test_reset_plate_infeasible_when_contents_fit_some_order():
    """内容还对得上某单 → 别清，那是半成品。"""
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    t = T.ResetPlate(order_id=None, resources=("p1", (7, 0)))
    assert t.feasible(ctx) is False


def test_reset_plate_infeasible_when_plate_empty():
    """空盘没什么要清的。"""
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0)])
    ctx, _ = make_ctx(w)
    assert T.ResetPlate(None, ("p1", (7, 0))).feasible(ctx) is False


def test_reset_plate_feasible_when_order_expired_with_full_plate():
    """场景②：端着料齐的盘，订单过期了（orders 为空）。

    没有这个 task，手里的满盘无法恢复（spec §6.1 第 14 号）。
    内容用实测帧的真实词汇。
    """
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[],
                  plates=[plate("p1", 7, 0,
                                ["CookedCompositeAssembledNode",
                                 "Cucumber", "Seaweed"])])
    ctx, _ = make_ctx(w)
    assert T.ResetPlate(None, ("p1", (7, 0))).feasible(ctx) is True


def test_reset_plate_spares_plate_with_cooked_token():
    """绝不能把正在正常装配的盘扣进垃圾桶。

    盘里刚倒进熟饭 → contents 是 ['CookedCompositeAssembledNode']，
    而订单需求写的是 BoiledSushiRice。若 any_order_wants 做字面比较，
    这张盘会被判成"对不上任何订单"，ResetPlate 把队友刚倒好的饭倒掉。
    """
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0,
                                ["CookedCompositeAssembledNode"])])
    ctx, _ = make_ctx(w)
    assert T.ResetPlate(None, ("p1", (7, 0))).feasible(ctx) is False


def test_reset_plate_spares_fully_assembled_plate():
    """做完的三料盘更不能清——它该走 DeliverPlate。"""
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0,
                                ["CookedCompositeAssembledNode",
                                 "Cucumber", "Seaweed"])])
    ctx, _ = make_ctx(w)
    assert T.ResetPlate(None, ("p1", (7, 0))).feasible(ctx) is False


def test_reset_plate_done_when_empty_or_moved_off_counter():
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0)])
    ctx, _ = make_ctx(w)
    assert T.ResetPlate(None, ("p1", (7, 0))).done(ctx) is True

    w2 = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                   plates=[plate("p1", 2, 0, ["Lettuce"])])
    ctx2, _ = make_ctx(w2)
    assert T.ResetPlate(None, ("p1", (7, 0))).done(ctx2) is True


def test_plate_tasks_are_registered():
    for name in ("PlaceCleanPlate", "WashPlate", "ResetPlate"):
        assert name in T.TASK_TYPES


# ---------- 烹饪（spec §6.1 第 3/4/5/6 号）----------

COOKER = {"t": "cooking", "n": "workstation_cooker_01", "gx": 5, "gz": 0,
          "x": 5 * 1.2, "z": 0.0}
COOKER2 = {"t": "cooking", "n": "workstation_cooker_01 (2)", "gx": 5,
           "gz": 2, "x": 5 * 1.2, "z": 2 * 1.2}
RICE_CRATE = {"t": "crate", "n": "crate_rice", "ing": "SushiRice",
              "chop": None, "chop_stages": None,
              "gx": -5, "gz": 0, "x": -5 * 1.2, "z": 0.0}


def cooker(name, **kw):
    base = {"n": name, "x": 5 * 1.2, "z": 0.0, "isCooking": False,
            "isTurnedOn": False, "cooked": None, "burning": False,
            "onFire": False}
    base.update(kw)
    return base


def test_extinguish_target_sticks_to_nearest_not_list_order():
    """灭火目标按**就近**选，且新灶着火不许把人从眼前那口灶拖走。

    实机（trace-20261014-011202 t=2404~2478）：P2 端着灭火器走到灶 (2)
    跟前（目标 (7,1)、已到 goal (6,1)），这时灶 (3) 也着了火——`fire_station`
    取 `burning(c)[0]`，即 `world.cookers` 的**列表序**第一个，于是目标
    瞬间翻转到 (7,-2)，人被拖去灶 (3)。喷了 50 拍两口灶的火都还在，直到
    关卡结束。

    列表序不是距离序。改成按离 P2 的距离选，于是「已经站在跟前的那口」
    天然最近、不会被后着火的灶抢走。
    """
    near = cooker("workstation_cooker_01 (2)", cooked=True,
                  burning=True, onFire=True)
    near.update(x=1 * 1.2, z=0.0)          # 紧挨 P2（make_ctx 默认在原点）
    far = cooker("workstation_cooker_01 (3)", cooked=True,
                 burning=True, onFire=True)
    far.update(x=9 * 1.2, z=0.0)
    stations = [
        {"t": "cooking", "n": "workstation_cooker_01 (2)",
         "gx": 1, "gz": 0, "x": 1 * 1.2, "z": 0.0},
        {"t": "cooking", "n": "workstation_cooker_01 (3)",
         "gx": 9, "gz": 0, "x": 9 * 1.2, "z": 0.0},
    ]
    # 列表序故意把**远**的那口放在前面：旧实现会选它
    w = FakeWorld(stations=stations, recipes=RECIPES, orders=[ORDER],
                  cookers=[far, near])
    ctx, _ = make_ctx(w)

    tree = T.Extinguish().build()
    spray = tree.child.child.children[1]          # Selector 分支2（取灭火器→喷）
    fire_station = spray.children[3].target       # Approach(fire_station)
    got = fire_station(ctx)
    assert got is not None
    assert got["n"] == "workstation_cooker_01 (2)", \
        "该选离 P2 最近的那口着火灶，不是 cookers 列表里的第一个"


def test_fetch_to_cooker_claims_cooker():
    t = T.FetchToCooker("1", ("workstation_cooker_01",), "BoiledSushiRice")
    assert t.claims() == (("workstation_cooker_01", "fetch_to"),)


def test_fetch_to_cooker_feasible_with_empty_pot_and_raw():
    """空锅（cooked=False 表示有锅但没熟）+ 生料在箱子里 → 可做。"""
    w = FakeWorld(stations=[COOKER, RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False)])
    ctx, _ = make_ctx(w)
    t = T.FetchToCooker("1", ("workstation_cooker_01",), "BoiledSushiRice")
    assert t.feasible(ctx) is True


def test_fetch_to_cooker_infeasible_without_pot():
    """cooked=None 表示灶上没锅（插件约定）→ 没锅没法下料。"""
    w = FakeWorld(stations=[COOKER, RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)])
    ctx, _ = make_ctx(w)
    t = T.FetchToCooker("1", ("workstation_cooker_01",), "BoiledSushiRice")
    assert t.feasible(ctx) is False


def test_fetch_to_cooker_infeasible_when_already_cooking():
    """已在煮 → 完成了，不再可行（上下游咬合）。"""
    w = FakeWorld(stations=[COOKER, RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01",
                                  isCooking=True, cooked=False)])
    ctx, _ = make_ctx(w)
    t = T.FetchToCooker("1", ("workstation_cooker_01",), "BoiledSushiRice")
    assert t.done(ctx) is True
    assert t.feasible(ctx) is False


def test_fetch_to_cooker_infeasible_when_on_fire():
    """着火的锅不放料（spec §5.3："Normal 模式下火的存在必须影响规划"）。"""
    w = FakeWorld(stations=[COOKER, RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False,
                                  onFire=True)])
    ctx, _ = make_ctx(w)
    t = T.FetchToCooker("1", ("workstation_cooker_01",), "BoiledSushiRice")
    assert t.feasible(ctx) is False


def test_fetch_to_cooker_infeasible_without_raw_source():
    """生料既不在箱子也不在场上 → 不可做。"""
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False)])
    ctx, _ = make_ctx(w)
    t = T.FetchToCooker("1", ("workstation_cooker_01",), "BoiledSushiRice")
    assert t.feasible(ctx) is False


def test_fetch_to_cooker_binds_specific_cooker():
    """绑定具体灶：1 号灶在煮不该让 2 号灶的 task 报完成。"""
    w = FakeWorld(stations=[COOKER, COOKER2, RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", isCooking=True,
                                  cooked=False),
                           cooker("workstation_cooker_01 (2)",
                                  cooked=False)])
    ctx, _ = make_ctx(w)
    assert T.FetchToCooker("1", ("workstation_cooker_01",),
                           "BoiledSushiRice").done(ctx) is True
    assert T.FetchToCooker("1", ("workstation_cooker_01 (2)",),
                           "BoiledSushiRice").done(ctx) is False


def test_take_cooked_feasible_when_cooked_and_plate_lacks():
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True)],
                  plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    t = T.TakeCookedToPlate("1", ("workstation_cooker_01", "p1"),
                            "BoiledSushiRice")
    assert t.feasible(ctx) is True


def test_take_cooked_feasible_while_cooking():
    """煮中也可行：端盘盛饭要提前去端盘、站到锅前等熟（实机：等熟了才去拿盘
    会来不及烧糊）。"""
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01",
                                  isCooking=True, cooked=False)],
                  plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    t = T.TakeCookedToPlate("1", ("workstation_cooker_01", "p1"),
                            "BoiledSushiRice")
    assert t.feasible(ctx) is True


def test_take_cooked_infeasible_when_burning():
    """烧糊的料倒进盘里会毁掉整盘 —— 该走 Discard。"""
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True,
                                  burning=True)],
                  plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    t = T.TakeCookedToPlate("1", ("workstation_cooker_01", "p1"),
                            "BoiledSushiRice")
    assert t.feasible(ctx) is False


def test_take_cooked_infeasible_when_plate_already_has_it():
    """盘里已有熟料 → done 成立、feasible 不成立。

    ⚠️ 盘内容用的是实测词汇 CookedCompositeAssembledNode，不是熟料名
    BoiledSushiRice——801 帧实测盘里从不出现熟料的字面名。写
    ["BoiledSushiRice"] 的测试会过，但那是在测一个现实中不存在的状态。
    """
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True)],
                  plates=[plate("p1", 7, 0,
                                ["CookedCompositeAssembledNode"])])
    ctx, _ = make_ctx(w)
    t = T.TakeCookedToPlate("1", ("workstation_cooker_01", "p1"),
                            "BoiledSushiRice")
    assert t.done(ctx) is True
    assert t.feasible(ctx) is False


def test_take_cooked_not_done_on_literal_cooked_name():
    """反向钉死：盘里字面写着熟料名**不算**倒进去了。

    这个状态在真实帧里不存在。若实现退回字面比较，这个测试仍会过，
    所以它配合上一个测试一起用：上面证明 token 能认出，这里证明实现
    不是简单地"两种都认"而掩盖了问题。
    """
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True)],
                  plates=[plate("p1", 7, 0, ["BoiledSushiRice"])])
    ctx, _ = make_ctx(w)
    t = T.TakeCookedToPlate("1", ("workstation_cooker_01", "p1"),
                            "BoiledSushiRice")
    assert t.done(ctx) is False


def test_take_cooked_done_binds_specific_plate():
    """Review Focus 3：P1 把熟饭倒进盘 B，不让针对盘 A 的 task 报成功。"""
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True)],
                  plates=[plate("pA", 7, 0, ["Seaweed"]),
                          plate("pB", 7, 2,
                                ["CookedCompositeAssembledNode"])])
    ctx, _ = make_ctx(w)
    assert T.TakeCookedToPlate("1", ("workstation_cooker_01", "pA"),
                               "BoiledSushiRice").done(ctx) is False
    assert T.TakeCookedToPlate("1", ("workstation_cooker_01", "pB"),
                               "BoiledSushiRice").done(ctx) is True


def test_take_cooked_tree_keeps_target_plate():
    """端盘盛饭：树首 EnsureHandsFree 要留住目标盘，别放下再捡。"""
    tree = T.TakeCookedToPlate("1", ("workstation_cooker_01", "p1"),
                               "BoiledSushiRice").build()
    keep = tree.child.children[0]._keep          # Timeout -> Sequence -> EnsureHandsFree
    assert keep is not None

    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[dict(plate("p1", 7, 0, ["Seaweed"]),
                               name="equipment_plate_01")])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "equipment_plate_01", "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert keep(ctx) is True


def test_take_cooked_feasible_while_holding_target_plate():
    """执行中：手持目标盘也算可行（盘离开原位不该判不可行）。"""
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True)],
                  plates=[dict(plate("p1", 7, 0, ["Seaweed"]),
                               name="equipment_plate_01")])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "equipment_plate_01", "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    t = T.TakeCookedToPlate("1", ("workstation_cooker_01", "p1"),
                            "BoiledSushiRice")
    assert t.feasible(ctx) is True


def test_return_pot_feasible_only_when_holding_cookware():
    """spec §6.1 第 5 号：可行性是纯观测事实"手持空锅"，不依赖记忆。

    这取代了 AI_REVIEW.md §4 的 RecoverPotAction 与 dropped_cooked_pot
    全局 bool。
    """
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "utensil_pot_01", "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert T.ReturnPot(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is True

    ctx2, _ = make_ctx(w)       # 手空
    assert T.ReturnPot(resources=("workstation_cooker_01",)) \
        .feasible(ctx2) is False


def test_return_pot_infeasible_when_cooker_occupied():
    """灶上已有锅 → 放不下，该找别的灶。"""
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False)])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "utensil_pot_01", "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert T.ReturnPot(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is False


def test_return_pot_done_when_hands_free_and_cooker_has_pot():
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False)])
    ctx, _ = make_ctx(w)       # 手空
    assert T.ReturnPot(resources=("workstation_cooker_01",)) \
        .done(ctx) is True


def test_return_pot_uses_cookware_recipes_not_keywords():
    """spec §9：锅具识别走配方数据。

    OvenCakeTin 在关键词表里只碰巧匹配 "tin"；这里断言配方里有它时
    必然识别得出（换关卡的正确性来源）。
    """
    recipes = RECIPES + [{"name": "Cake_Plain", "comp": ["X"],
                          "cook": "OvenCakeTin"}]
    w = FakeWorld(stations=[COOKER], recipes=recipes, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "equipment_OvenCakeTin_01", "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert T.ReturnPot(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is True


def test_rescue_cooked_feasible_when_no_plate_wants_it():
    """spec §6.1 第 6 号：熟了但没盘要它 → 端锅离灶停烧糊计时。"""
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True)],
                  plates=[])
    ctx, _ = make_ctx(w)
    assert T.RescueCooked(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is True


def test_rescue_cooked_infeasible_when_a_plate_wants_it():
    """有盘要这料 → 该走 TakeCookedToPlate，别浪费一趟。"""
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES,
                  orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True)],
                  plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    assert T.RescueCooked(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is False


def test_rescue_cooked_feasible_when_about_to_burn_even_if_plate_wants_it():
    """快糊/已糊的锅要立刻端离灶台止损，哪怕有盘要这料。

    实机：多煮出来的锅配给没盘的单，TakeCookedToPlate 发不出来，又因为
    有别的盘要饭，RescueCooked 判不可行 → 活活烧糊。
    """
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True,
                                  cooking_progress=24.0, cooking_time=12.0)],
                  plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    assert T.RescueCooked(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is True


def test_deliver_plate_infeasible_when_plate_already_on_serving_hatch():
    """盘已经交到出餐口 → 不可行，别用它再送下一单。

    实机（trace-20261014-004742 t=4134~4146 / t=3238~3249）：送餐 success
    之后，盘在快照里还会滞留约 11 拍（游戏侧动画 + 5 Hz 推帧延迟），
    Planner 看见「盘存在 + 料齐」就把这只已经交出去的盘绑给下一单，
    11 拍后盘真的消失才 failure。每送一单都白搭一次，还污染连续失败计数
    （`scratch.abandoned`）。

    判据不靠记忆：盘的坐标落在出餐口站点上就说明它已经交出去了，这是
    **当前快照里的观测事实**，符合无状态重算的约定。
    """
    hatch = {"t": "plate_station", "n": "workstation_plate_station",
             "gx": 9, "gz": 0, "x": 9 * 1.2, "z": 0.0,
             "minx": 9 * 1.2 - 1.2, "minz": -1.2,
             "maxx": 9 * 1.2 + 1.2, "maxz": 1.2}
    served = plate("p_served", 9, 0,
                   ["CookedCompositeAssembledNode", "Cucumber", "Seaweed"])
    w = FakeWorld(stations=[hatch, COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[served])
    ctx, _ = make_ctx(w)
    task = T.DeliverPlate(ORDER["id"], ("p_served",), None)
    assert task.feasible(ctx) is False, "交到出餐口的盘不能再绑给下一单"


def test_deliver_plate_feasible_when_plate_still_on_counter():
    """盘还在拼装台上（不在出餐口）→ 照常可行，不许误杀正常候选。"""
    hatch = {"t": "plate_station", "n": "workstation_plate_station",
             "gx": 9, "gz": 0, "x": 9 * 1.2, "z": 0.0,
             "minx": 9 * 1.2 - 1.2, "minz": -1.2,
             "maxx": 9 * 1.2 + 1.2, "maxz": 1.2}
    ready = plate("p_ready", 2, 0,
                  ["CookedCompositeAssembledNode", "Cucumber", "Seaweed"])
    w = FakeWorld(stations=[hatch, COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[ready])
    ctx, _ = make_ctx(w)
    task = T.DeliverPlate(ORDER["id"], ("p_ready",), None)
    assert task.feasible(ctx) is True


def test_rescue_cooked_feasible_when_too_late_to_fetch_plate():
    """来不及端盘来盛 → 先端锅离灶止损，别等到着火。

    实机（trace-20261014-011202 t=2056~2184）：灶 (2) 的饭 progress 16.9
    已熟，P2 去取盘再走回来要 ~130 拍，锅 ~140 拍后烧糊着火。旧
    `_about_to_burn` 要求 `progress >= 2*ctime`——**那一刻已经着火了**，
    于是 P2 一路端着盘往锅跑，到 t=2184 才选中 RescueCooked，下一拍
    Arbiter 就切进 Firefighting，晚了整整一个状态。

    现在改成带余量的预测：烧糊剩余时间 < 端盘那一趟的预计耗时 + 安全余量
    就压过「有盘要这料」。这里 progress=20/ctime=12 → 还剩 4 秒 = 80 拍，
    短于 RESCUE_SAFETY_TICKS + 取盘往返，应判可行。
    """
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True,
                                  cooking_progress=20.0, cooking_time=12.0)],
                  plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    assert T.RescueCooked(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is True


def test_rescue_cooked_infeasible_when_plenty_of_time_to_fetch_plate():
    """刚熟、时间充裕 → 仍该走 TakeCookedToPlate，不许抢着端锅。

    端锅只是止损（料留着但占着台面、还要再搬一次），有盘要这料且来得及
    时，直接盛进盘才是正途。这条钉住上面那个预测不会把所有熟锅都抢走。
    """
    w = FakeWorld(stations=[COOKER, COUNTER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True,
                                  cooking_progress=12.0, cooking_time=12.0)],
                  plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    assert T.RescueCooked(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is False


def test_extinguish_feasible_when_burning_and_done_when_out():
    """灭火 task（全局）：有任一灶着火/烧糊可行，火全灭后 done。"""
    t = T.Extinguish()
    assert t.TYPE == "Extinguish"
    assert t.claims() == ()            # 全局灭火不按灶认领

    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True,
                                  burning=True)])
    ctx, _ = make_ctx(w)
    assert t.feasible(ctx) is True
    assert t.done(ctx) is False

    w2 = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                   cookers=[cooker("workstation_cooker_01", cooked=True,
                                   burning=False)])
    ctx2, _ = make_ctx(w2)
    assert t.done(ctx2) is True


def test_extinguish_tree_loops_pot_then_spray():
    """灭火树 = RepeatUntil(Selector[端锅优先, 喷])：全局一棵树循环所有灶。

    顺序钉住：只要还有带锅的着火灶，就只端锅（锅在灶上会一直烧、火复燃）；
    没有带锅了才喷，喷的子树外面包 Guard 每拍盯新锅。
    """
    tree = T.Extinguish().build()
    assert isinstance(tree, PR.Timeout)
    assert tree.max_ticks == T.FIREFIGHT_TIMEOUT_TICKS, \
        "整局火情是循环树，不能用默认 200 拍兜底（会中途砍树）"

    loop = tree.child
    assert isinstance(loop, RepeatUntil)
    sel = loop.child
    assert isinstance(sel, Selector)
    assert len(sel.children) == 2

    pot_branch, spray_branch = sel.children
    assert isinstance(pot_branch, Sequence)
    assert isinstance(pot_branch.children[0], Condition)
    assert isinstance(pot_branch.children[1], PR.EnsureHandsFree)
    assert pot_branch.children[1]._home_cooker is False, "端锅前清手也不许把锅搁回灶台"
    assert isinstance(pot_branch.children[2], PR.InteractWith)
    assert pot_branch.children[2].kind == "pickup"
    assert isinstance(pot_branch.children[3], PR.EnsureHandsFree)
    assert pot_branch.children[3]._home_cooker is False, "端下来的锅绝不许放回灶台"

    assert isinstance(spray_branch, Sequence)
    assert isinstance(spray_branch.children[0], Condition)
    assert isinstance(spray_branch.children[-1], Guard)      # 喷时盯新锅
    hold = spray_branch.children[-1].child
    assert isinstance(hold, PR.HoldUntil)
    assert hold.key == "chop"


def test_extinguish_pot_pickup_expects_holding_pot():
    """端锅这步的 expect 是「手上有锅」：端下即成立，不绑具体灶名。

    换灶不换树的关键——不依赖灶实例名，端起任何一口锅都算完成这一步。
    """
    tree = T.Extinguish().build()
    pot_pickup = tree.child.child.children[0].children[2]
    assert isinstance(pot_pickup, PR.InteractWith)
    assert pot_pickup.kind == "pickup"

    # 手空 → expect 不成立（真去端锅）
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True,
                                  burning=True)])
    ctx, _ = make_ctx(w)
    assert pot_pickup.expect(ctx) is False

    # 手上有锅 → expect 成立（端下来了）
    w2 = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "utensil_pot_01",
            "interactions": inter()}
    ctx2, _ = make_ctx(w2, chef=chef)
    assert pot_pickup.expect(ctx2) is True


def test_extinguish_pot_branch_ignores_empty_pot_on_fire():
    """端锅分支只认「糊锅」（burning=True），空锅/生锅即使灶在烧也不端。

    实机复现：火蔓延到旁边空锅灶上，若把空锅也当「要端的锅」，树会卡在端空锅
    上、永远不去喷火（上一局 trace 里火没灭的原因）。
    """
    tree = T.Extinguish().build()
    pot_cond = tree.child.child.children[0].children[0]   # RepeatUntil -> Selector -> 分支1 -> Condition(any_pot)
    assert isinstance(pot_cond, Condition)

    # 空锅 + 灶着火（onFire=True, burning=False）→ 不该触发端锅
    w_empty = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                        cookers=[cooker("workstation_cooker_01", cooked=False,
                                        onFire=True, burning=False)])
    ctx_empty, _ = make_ctx(w_empty)
    assert pot_cond.fn(ctx_empty) is False

    # 糊锅（burning=True）→ 触发端锅
    w_burnt = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                        cookers=[cooker("workstation_cooker_01", cooked=True,
                                        onFire=True, burning=True)])
    ctx_burnt, _ = make_ctx(w_burnt)
    assert pot_cond.fn(ctx_burnt) is True


def test_rescue_cooked_done_when_pot_off_cooker():
    """完成判定是"锅已离灶"（cooked 变 None = 灶上没锅）。

    spec §6.1："已确认的游戏机制：把锅从灶台端起即停止烧糊，不必倒进
    盘子。"
    """
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)])
    ctx, _ = make_ctx(w)
    assert T.RescueCooked(resources=("workstation_cooker_01",)) \
        .done(ctx) is True


def test_cooking_tasks_are_registered():
    for name in ("FetchToCooker", "TakeCookedToPlate", "ReturnPot",
                 "RescueCooked"):
        assert name in T.TASK_TYPES


# ---------- 切菜（spec §6.1 第 7/8 号 + Review Focus 4）----------

BOARD = {"t": "chopping", "n": "board_01", "gx": 1, "gz": 3,
         "x": 1.2, "z": 3 * 1.2}
BOARD2 = {"t": "chopping", "n": "board_01 (2)", "gx": 3, "gz": 3,
          "x": 3 * 1.2, "z": 3 * 1.2}
CUKE_CRATE = {"t": "crate", "n": "crate_cuke", "ing": "Cucumber",
              "chop": "ChoppedCucumber", "chop_stages": 8,
              "gx": -1, "gz": 3, "x": -1.2, "z": 3 * 1.2}

# 盘里要的是切好的黄瓜（ChoppedCucumber），所以订单需求列 Cucumber
# 时 Planner 会先走 FetchToBoard + Chop。这里用一张显式订单避免歧义。
ORDER_CHOP = {"id": "9", "dish": "Sushi_Cucumber",
              "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]}


def board(name, item=None):
    return {"n": name, "item": item, "interacting": False, "interacters": []}


def test_fetch_to_board_claims_board():
    t = T.FetchToBoard("9", ("board_01",), "Cucumber")
    assert t.claims() == (("board_01", "fetch_to"),)


def test_fetch_to_board_feasible_when_board_free_and_raw_available():
    w = FakeWorld(stations=[BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP], workstations=[board("board_01")])
    ctx, _ = make_ctx(w)
    assert T.FetchToBoard("9", ("board_01",), "Cucumber") \
        .feasible(ctx) is True


def test_fetch_to_board_infeasible_when_board_occupied():
    """板上已有东西 → 放不下。"""
    w = FakeWorld(stations=[BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "SushiFish")])
    ctx, _ = make_ctx(w)
    assert T.FetchToBoard("9", ("board_01",), "Cucumber") \
        .feasible(ctx) is False


def test_fetch_to_board_infeasible_for_unchoppable_item():
    """海苔不可切（crate.chop 为 null）→ 没理由放板上。"""
    w = FakeWorld(stations=[BOARD, RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP], workstations=[board("board_01")])
    ctx, _ = make_ctx(w)
    assert T.FetchToBoard("9", ("board_01",), "SushiRice") \
        .feasible(ctx) is False


def test_fetch_to_board_done_when_raw_on_board():
    w = FakeWorld(stations=[BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "Cucumber")])
    ctx, _ = make_ctx(w)
    t = T.FetchToBoard("9", ("board_01",), "Cucumber")
    assert t.done(ctx) is True
    assert t.feasible(ctx) is False


def test_fetch_to_board_done_binds_specific_board():
    """绑定具体板：料在 2 号板不该让 1 号板的 task 报完成。"""
    w = FakeWorld(stations=[BOARD, BOARD2, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01"),
                                board("board_01 (2)", "Cucumber")])
    ctx, _ = make_ctx(w)
    assert T.FetchToBoard("9", ("board_01",), "Cucumber").done(ctx) is False
    assert T.FetchToBoard("9", ("board_01 (2)",), "Cucumber") \
        .done(ctx) is True


def test_fetch_to_board_tree_has_loose_first_and_no_batch_throw():
    """切料上板树 = Selector 三分支：散落优先 / 箱子取 / 已在手兜底。

    分支顺序钉住「散落不用从箱子取」（分支1 的 Condition 是 has_loose），
    分支3 无 Condition、是「已在手/兜底」单次 pickup→drop。

    分支2 曾是「RepeatUntil 批量取+投 N 份，再捡散落上板」，与
    FetchToCooker 单料同构、也同样坏：必须投满 N 份才轮到 drop()，一轮
    取+投约 80 拍而整树只有 TASK_TIMEOUT_TICKS=200，第二轮没结束就被砍
    → 料全堆在地上、板上一直空着。砧板一次只放一根（feasible 要求板上
    为空），批量投更没有意义。现在钉住它**不再有**循环与投掷。
    """
    tree = T.FetchToBoard("9", ("board_01",), "Cucumber").build()
    assert isinstance(tree, PR.Timeout)
    sel = tree.child
    assert isinstance(sel, Selector)
    assert len(sel.children) == 3

    loose, crate, held = sel.children
    assert isinstance(loose, Sequence)
    assert isinstance(loose.children[0], Condition)
    assert isinstance(crate, Sequence)
    assert isinstance(crate.children[0], Condition)

    def walk(node):
        yield node
        for attr in ("child", "children"):
            sub = getattr(node, attr, None)
            if sub is None:
                continue
            for n in (sub if isinstance(sub, (list, tuple)) else [sub]):
                for x in walk(n):
                    yield x

    nodes = list(walk(tree))
    assert not any(isinstance(n, RepeatUntil) for n in nodes), \
        "投满 N 份才上板必然超时，drop 永远执行不到"
    assert not any(isinstance(n, PR.ThrowTo) for n in nodes)

    # 三个分支都必须以「往板上放」收尾
    for branch in sel.children:
        last = branch.children[-1]
        assert isinstance(last, PR.InteractWith)
        assert last.kind == "placement"

    # 分支3 无 Condition，是「已在手/兜底」单次路径
    assert isinstance(held, Sequence)
    assert not any(isinstance(n, Condition) for n in held.children)


def test_fetch_to_cooker_single_raw_tree_has_no_batch_throw():
    """单料下锅树 = Selector[散落优先, 箱子取→下锅]，**两分支都要能到 drop**。

    这里曾是「RepeatUntil 批量取+投 N 份，再捡散落下锅」。树结构决定了
    必须投满 N 份才轮到 drop()，而一轮取+投实测约 80 拍、整树裹在
    TASK_TIMEOUT_TICKS=200 里——第二轮没结束就被砍，drop() 一次都没执行
    过，米全堆在地上、灶始终空着（trace-20261014-011202，2527 拍 0 送达）。

    钉住：① 没有 RepeatUntil；② 没有 ThrowTo；③ 每个分支都以 placement
    收尾（真的会下锅）。
    """
    tree = T.FetchToCooker(
        "1", ("cooker", ("SushiRice",)), "BoiledSushiRice").build()
    assert isinstance(tree, PR.Timeout)
    sel = tree.child
    assert isinstance(sel, Selector)
    assert len(sel.children) == 2

    def walk(node):
        yield node
        for attr in ("child", "children"):
            sub = getattr(node, attr, None)
            if sub is None:
                continue
            for n in (sub if isinstance(sub, (list, tuple)) else [sub]):
                for x in walk(n):
                    yield x

    nodes = list(walk(tree))
    assert not any(isinstance(n, RepeatUntil) for n in nodes), \
        "单料下锅不该有批量循环：投满 N 份才下锅必然超时"
    assert not any(isinstance(n, PR.ThrowTo) for n in nodes), \
        "N=1 时投掷摊销为零，只剩落点不可控的缺点"

    # 两个分支都必须以「往灶里放」收尾
    for branch in sel.children:
        assert isinstance(branch, Sequence)
        last = branch.children[-1]
        assert isinstance(last, PR.InteractWith)
        assert last.kind == "placement", "每个分支都要真的走到下锅"


def test_chop_feasible_when_raw_on_board():
    w = FakeWorld(stations=[BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "Cucumber")])
    ctx, _ = make_ctx(w)
    assert T.Chop("9", ("board_01",), "Cucumber").feasible(ctx) is True


def test_chop_infeasible_when_board_empty():
    w = FakeWorld(stations=[BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP], workstations=[board("board_01")])
    ctx, _ = make_ctx(w)
    assert T.Chop("9", ("board_01",), "Cucumber").feasible(ctx) is False


def test_chop_done_uses_recipe_chop_target_not_prefix():
    """Review Focus 4：完成判定是"板上产物 == 配方期望的加工态"。

    spec §7.3 定稿原话："Chop 的完成判定不用字符串前缀匹配。初版写
    item == Chopped*，但多阶段切配或产物命名不以 Chopped 开头时会误判。"
    """
    w = FakeWorld(stations=[BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "ChoppedCucumber")])
    ctx, _ = make_ctx(w)
    t = T.Chop("9", ("board_01",), "Cucumber")
    assert t.done(ctx) is True
    assert t.feasible(ctx) is False


def test_chop_done_with_non_chopped_prefix_product():
    """核心反例：产物名叫 SlicedTuna，不以 Chopped 开头。

    前缀匹配会认为"还没切好"，于是无限切下去。
    """
    tuna_crate = {"t": "crate", "n": "crate_tuna", "ing": "Tuna",
                  "chop": "SlicedTuna", "chop_stages": 4,
                  "gx": -1, "gz": 3, "x": -1.2, "z": 3 * 1.2}
    w = FakeWorld(stations=[BOARD, tuna_crate], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "SlicedTuna")])
    ctx, _ = make_ctx(w)
    assert T.Chop("9", ("board_01",), "Tuna").done(ctx) is True


def test_chop_not_done_when_product_merely_starts_with_chopped():
    """反向保护：板上是**别的**切好料时不许报成功。

    绑定 Cucumber 的 Chop 看到 ChoppedSushiFish 不是自己干完了。
    """
    fish_crate = {"t": "crate", "n": "crate_fish", "ing": "SushiFish",
                  "chop": "ChoppedSushiFish", "chop_stages": 8,
                  "gx": -3, "gz": 3, "x": -3 * 1.2, "z": 3 * 1.2}
    w = FakeWorld(stations=[BOARD, CUKE_CRATE, fish_crate], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "ChoppedSushiFish")])
    ctx, _ = make_ctx(w)
    assert T.Chop("9", ("board_01",), "Cucumber").done(ctx) is False


def test_chop_infeasible_for_unchoppable_item():
    """chop_target 为 None → 这料压根不能切，别站那儿按键。"""
    w = FakeWorld(stations=[BOARD, RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "SushiRice")])
    ctx, _ = make_ctx(w)
    assert T.Chop("9", ("board_01",), "SushiRice").feasible(ctx) is False


def test_chop_tree_holds_chop_key_with_explicit_timeout():
    """切菜树 = Approach + Face + HoldUntil（spec §7.3 的样例）。

    HoldUntil 的 timeout 必须显式传（spec §7.2），这里断言它不是 None。
    """
    tree = T.Chop("9", ("board_01",), "Cucumber").build()
    assert isinstance(tree, PR.Timeout)
    seq = tree.child
    assert isinstance(seq, Sequence)
    holds = [n for n in seq.children if isinstance(n, PR.HoldUntil)]
    assert len(holds) == 1
    assert holds[0].key == "chop"
    assert isinstance(holds[0].timeout, int) and holds[0].timeout > 0


def test_chop_timeout_covers_max_stages():
    """timeout 必须够切完最多阶段的料，否则多阶段切配永远切不完。

    实测 Cucumber / SushiFish 的 chop_stages 都是 8。固定一个太小的
    timeout 会让 HoldUntil 在切完前判 FAILURE，然后 Planner 重选同一个
    task、又切一半——无限循环且永不完成。

    早停不靠 timeout 而靠 done 谓词：切好那一拍 HoldUntil 立刻 SUCCESS，
    所以上限取保守值不会浪费时间。
    """
    holds = [n for n in T.Chop("9", ("board_01",), "Cucumber")
             .build().child.children
             if isinstance(n, PR.HoldUntil)]
    assert holds[0].timeout >= T.CHOP_TICKS_PER_STAGE * 8, \
        "timeout 不够切完 8 阶段的料（实测 Cucumber/SushiFish 都是 8）"


def test_chop_early_stops_when_done_before_timeout():
    """切好那一拍立刻 SUCCESS，不等 timeout 耗尽。"""
    w = FakeWorld(stations=[BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER_CHOP],
                  workstations=[board("board_01", "Cucumber")])
    chef = {"name": "Player 2", "x": 1.2, "z": 2 * 1.2, "held": None,
            "interactions": inter(gx=1, gz=3)}
    ctx, ctrl = make_ctx(w, chef=chef)
    tree = T.Chop("9", ("board_01",), "Cucumber").build()
    assert tree.tick(ctx) == Status.RUNNING        # 正在按住切
    assert "chop" in ctx["hands"].held_keys
    w.workstations = [board("board_01", "ChoppedCucumber")]
    assert tree.tick(ctx) == Status.SUCCESS
    assert ctx["hands"].held_keys == set(), "切完必须松键"


def test_chop_tasks_are_registered():
    for name in ("FetchToBoard", "Chop"):
        assert name in T.TASK_TYPES


# ---------- 装盘与出餐（spec §6.1 第 9/10 号）----------

PLATE_STATION = {"t": "plate_station", "n": "workstation_plate_station",
                 "gx": 9, "gz": 0, "x": 9 * 1.2, "z": 0.0}
SEAWEED_CRATE = {"t": "crate", "n": "crate_seaweed", "ing": "Seaweed",
                 "chop": None, "chop_stages": None,
                 "gx": -7, "gz": 0, "x": -7 * 1.2, "z": 0.0}

# 实测词汇：做完的盘长这样（801 帧验证，见 Task 5）
FULL = ["CookedCompositeAssembledNode", "Cucumber", "Seaweed"]


def test_fetch_to_plate_claims_nothing():
    """spec §6.1 第 9 号的认领列是空的。

    料在箱子里无限供应，盘已由 PlaceCleanPlate 认领过——重复认领盘会把
    自己的下游 task（DeliverPlate）挡在外面。
    """
    t = T.FetchToPlate("1", ("p1",), "Seaweed")
    assert t.claims() == ()


def test_fetch_to_plate_feasible_when_plate_placed_and_lacks():
    w = FakeWorld(stations=[COUNTER, SEAWEED_CRATE], recipes=RECIPES,
                  orders=[ORDER], plates=[plate("p1", 7, 0)])
    ctx, _ = make_ctx(w)
    assert T.FetchToPlate("1", ("p1",), "Seaweed").feasible(ctx) is True


def test_fetch_to_plate_infeasible_when_plate_gone():
    """盘被端走 → 不可行，不能崩（Review Focus 5）。"""
    w = FakeWorld(stations=[COUNTER, SEAWEED_CRATE], recipes=RECIPES,
                  orders=[ORDER], plates=[])
    ctx, _ = make_ctx(w)
    assert T.FetchToPlate("1", ("p1",), "Seaweed").feasible(ctx) is False


def test_fetch_to_plate_infeasible_when_ingredient_unavailable():
    """没有箱子也没散落 → 取不到。"""
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0)])
    ctx, _ = make_ctx(w)
    assert T.FetchToPlate("1", ("p1",), "Seaweed").feasible(ctx) is False


def test_fetch_to_plate_done_and_infeasible_when_present():
    w = FakeWorld(stations=[COUNTER, SEAWEED_CRATE], recipes=RECIPES,
                  orders=[ORDER], plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    t = T.FetchToPlate("1", ("p1",), "Seaweed")
    assert t.done(ctx) is True
    assert t.feasible(ctx) is False


def test_fetch_to_plate_done_binds_specific_plate():
    """Review Focus 3 的原始出处（spec §6.0 原话）。

    两单都缺黄瓜，P1 把黄瓜放进盘 B。若 done 不绑定盘，针对盘 A 的 task
    会误报 SUCCESS——AI 以为干完了，盘 A 其实还空着。
    """
    w = FakeWorld(stations=[COUNTER, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER], plates=[plate("pA", 7, 0),
                                          plate("pB", 7, 2, ["Cucumber"])])
    ctx, _ = make_ctx(w)
    assert T.FetchToPlate("1", ("pA",), "Cucumber").done(ctx) is False
    assert T.FetchToPlate("2", ("pB",), "Cucumber").done(ctx) is True


def test_fetch_to_plate_done_for_cooked_item_uses_token():
    """熟料的"已在盘里"靠不透明 token 判（见 Task 5）。

    盘里从不出现 BoiledSushiRice 的字面名。字面比较会让 FetchToPlate
    一直认为盘缺熟料，跟 TakeCookedToPlate 抢同一件事。
    """
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0,
                                ["CookedCompositeAssembledNode"])])
    ctx, _ = make_ctx(w)
    assert T.FetchToPlate("1", ("p1",), "BoiledSushiRice").done(ctx) is True


def test_fetch_to_plate_takes_chopped_form_from_board():
    """切好的料：取 ChoppedCucumber，但完成判定看 Cucumber。

    item 三处（FetchToBoard / Chop / FetchToPlate）都是生料名，取料目标
    查 chop_target、完成判定查 plate_has（内部走 want_tokens/plate_tokens）
    ——同一个 item 的两种形态。
    """
    w = FakeWorld(stations=[COUNTER, BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER], plates=[plate("p1", 7, 0)],
                  workstations=[board("board_01", "ChoppedCucumber")])
    ctx, _ = make_ctx(w)
    t = T.FetchToPlate("1", ("p1",), "Cucumber")
    assert t.feasible(ctx) is True
    tree = t.build()
    assert isinstance(tree, PR.Timeout)


def test_fetch_to_plate_prefers_chopped_when_item_needs_chopping():
    """需切的料没切好时不该从箱子直接拿生料放盘。

    生黄瓜进盘做不成寿司。该等 Chop 干完——所以这里判不可行，把机会
    让给 FetchToBoard / Chop。
    """
    w = FakeWorld(stations=[COUNTER, BOARD, CUKE_CRATE], recipes=RECIPES,
                  orders=[ORDER], plates=[plate("p1", 7, 0)],
                  workstations=[board("board_01")])
    ctx, _ = make_ctx(w)
    assert T.FetchToPlate("1", ("p1",), "Cucumber").feasible(ctx) is False


def test_deliver_plate_claims_plate():
    t = T.DeliverPlate("1", ("p1",))
    assert t.claims() == (("p1", "deliver"),)


def test_deliver_plate_feasible_when_complete_and_order_alive():
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0, FULL)])
    ctx, _ = make_ctx(w)
    assert T.DeliverPlate("1", ("p1",)).feasible(ctx) is True


def test_deliver_plate_infeasible_when_plate_incomplete():
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0, ["Seaweed"])])
    ctx, _ = make_ctx(w)
    assert T.DeliverPlate("1", ("p1",)).feasible(ctx) is False


def test_deliver_plate_infeasible_when_order_gone():
    """订单过期 → 别送了，该走 ResetPlate（spec §6.1 第 14 号场景②）。"""
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[],
                  plates=[plate("p1", 7, 0, FULL)])
    ctx, _ = make_ctx(w)
    assert T.DeliverPlate("1", ("p1",)).feasible(ctx) is False


def test_deliver_plate_done_when_order_disappeared():
    """信号一：订单消失。基线由 Executor 在建树时抓。"""
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[],
                  plates=[])
    ctx, _ = make_ctx(w)
    ctx["baseline"] = T.delivery_baseline_from(
        FakeWorld(orders=[ORDER], recipes=RECIPES))
    assert T.DeliverPlate("1", ("p1",)).done(ctx) is True


def test_deliver_plate_done_when_score_increased():
    """信号二：分数增加——人类同时送餐时订单消失不可靠（spec §6.1）。

    这里订单仍在（另一张同菜单），但 deliveries 涨了，说明送出去了。
    """
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[ORDER],
                  plates=[], score={"deliveries": 1, "base": 60})
    ctx, _ = make_ctx(w)
    ctx["baseline"] = T.delivery_baseline_from(
        FakeWorld(orders=[ORDER], recipes=RECIPES,
                  score={"deliveries": 0, "base": 0}))
    assert T.DeliverPlate("1", ("p1",)).done(ctx) is True


def test_deliver_plate_not_done_when_combo_resets():
    """实测反例：连击断掉时 combo/multiplier 归零，没人送餐。

    帧 686 实测 gone=set()、(2,120,11,2,2)→(2,120,11,0,0)。若"分数增加"
    看整个 score dict 是否变化，连击断掉会被误判成送餐成功——AI 以为送
    出去了，那盘菜一直留在手上。只看 deliveries 与 base。
    """
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[ORDER],
                  plates=[plate("p1", 7, 0, FULL)],
                  score={"deliveries": 2, "base": 120, "tips": 11,
                         "combo": 0, "multiplier": 0})
    ctx, _ = make_ctx(w)
    ctx["baseline"] = T.delivery_baseline_from(
        FakeWorld(orders=[ORDER], recipes=RECIPES,
                  score={"deliveries": 2, "base": 120, "tips": 11,
                         "combo": 2, "multiplier": 2}))
    assert T.DeliverPlate("1", ("p1",)).done(ctx) is False


def test_deliver_plate_not_done_while_still_holding():
    """手里还端着盘 → 没送到（双信号都要求"手空且……"）。"""
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[],
                  plates=[])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "equipment_plate_01", "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    ctx["baseline"] = T.delivery_baseline_from(
        FakeWorld(orders=[ORDER], recipes=RECIPES))
    assert T.DeliverPlate("1", ("p1",)).done(ctx) is False


def test_deliver_plate_done_without_baseline_is_false():
    """没基线不能瞎猜"送到了"——宁可多站一拍，不可误判。"""
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[],
                  plates=[])
    ctx, _ = make_ctx(w)
    assert ctx.get("baseline") is None
    assert T.DeliverPlate("1", ("p1",)).done(ctx) is False


def test_deliver_plate_tree_picks_up_then_serves():
    tree = T.DeliverPlate("1", ("p1",)).build()
    assert isinstance(tree, PR.Timeout)
    guard = tree.child
    assert isinstance(guard, Guard)          # 外层包 Guard：P1 抢盘就 abort
    seq = guard.child
    assert isinstance(seq, Sequence)
    assert len(seq.children) == 3
    assert isinstance(seq.children[0], PR.EnsureHandsFree)
    assert all(isinstance(n, PR.InteractWith) for n in seq.children[1:])


def test_deliver_plate_serves_at_plate_station_not_plate_return():
    """出餐口是 plate_station；plate_return 是脏盘回收口/晾干架。

    旧 bug 把 serving 目标写成 plate_return，组装好的盘放那里永远不结算
    （实机：端着做完的盘在回收口附近反复打转）。
    """
    tree = T.DeliverPlate("1", ("p1",)).build()
    serving_iw = tree.child.child.children[2]
    w = FakeWorld(stations=[
        PLATE_STATION,
        {"t": "plate_return", "n": "DryingPart", "x": 0.0, "z": 0.0},
    ], recipes=RECIPES, orders=[ORDER])
    ctx, _ = make_ctx(w)
    st = serving_iw.target(ctx)
    assert st is not None
    assert st.get("t") == "plate_station"


def test_deliver_plate_hold_check_matches_plate_name_not_id():
    """手持判定按盘名匹配，不是实例 id。

    chef.held 存盘名（equipment_plate_01），而 task 绑定的是实例 id（-78262）。
    旧代码拿 id 去 held_matches 恒为假 → 树首 EnsureHandsFree 把手里的目标盘
    当杂物放下、pickup 又捡回，送餐那步永远走不到（实机：端盘在空柜台反复
    放/捡，看起来像找不到出餐口）。
    """
    tree = T.DeliverPlate("1", ("p1",)).build()
    seq = tree.child.child
    keep = seq.children[0]._keep        # EnsureHandsFree 的 keep
    expect = seq.children[1].expect      # pickup 的 expect
    p = plate("p1", 0, 0, FULL)
    p["name"] = "equipment_plate_01"
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[ORDER],
                  plates=[p])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "equipment_plate_01", "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert keep(ctx) is True
    assert expect(ctx) is True


def test_deliver_plate_aborts_when_p1_grabs_plate():
    """P1 端走目标盘 → Guard abort（Observer Abort，同灭火）。

    绑定后 P1 抢盘：Guard 每拍重查 not_grabbed_by_p1，发现 P1 持盘就
    FAILURE 交还 Planner，不再傻追 P1 手里的盘。
    """
    tree = T.DeliverPlate("1", ("p1",)).build()
    guard = tree.child
    assert isinstance(guard, Guard)
    pred = guard.pred
    p = plate("p1", 0, 0, FULL)
    p["name"] = "equipment_plate_01"
    w = FakeWorld(stations=[PLATE_STATION], recipes=RECIPES, orders=[ORDER],
                  plates=[p])
    # 没有 P1 → 不 abort
    ctx, _ = make_ctx(w)
    assert pred(ctx) is True
    # P1 端走这块盘 → abort
    w.chefs = [{"name": "Player 1", "held": "equipment_plate_01"}]
    ctx, _ = make_ctx(w)
    assert pred(ctx) is False


def test_assembly_tasks_are_registered():
    for name in ("FetchToPlate", "DeliverPlate"):
        assert name in T.TASK_TYPES


# ---------- 异常清理与预取（spec §6.1 第 11/12/13/15 号）----------

BIN = {"t": "bin", "n": "Bin 1", "gx": -7, "gz": 2,
       "x": -7 * 1.2, "z": 2 * 1.2}
ORDER_FISH2 = {"id": "2", "dish": "Sushi_Cucumber",
               "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]}


def test_discard_claims_cooker_and_bin():
    """spec §6.1 第 11 号：认领含垃圾桶——烧焦内容只能倒进垃圾桶。"""
    t = T.Discard(resources=("workstation_cooker_01",))
    assert set(t.claims()) == {("workstation_cooker_01", "discard"),
                               ("bin", "discard")}


def test_discard_feasible_when_burning():
    w = FakeWorld(stations=[COOKER, BIN], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True,
                                  burning=True)])
    ctx, _ = make_ctx(w)
    assert T.Discard(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is True


def test_discard_infeasible_when_not_burning():
    """没糊的锅不丢——那是好料。"""
    w = FakeWorld(stations=[COOKER, BIN], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True)])
    ctx, _ = make_ctx(w)
    assert T.Discard(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is False


def test_discard_infeasible_without_bin():
    """没垃圾桶就倒不掉（spec §6.1："不是随便放下"）。"""
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True,
                                  burning=True)])
    ctx, _ = make_ctx(w)
    assert T.Discard(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is False


def test_discard_done_when_pot_empty_on_cooker():
    """完成判定：锅空 且 在灶上（cooked=False = 有锅未熟 = 空锅）。"""
    w = FakeWorld(stations=[COOKER, BIN], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False,
                                  burning=False)])
    ctx, _ = make_ctx(w)
    assert T.Discard(resources=("workstation_cooker_01",)).done(ctx) is True


def test_discard_not_done_while_still_burning():
    w = FakeWorld(stations=[COOKER, BIN], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=True,
                                  burning=True)])
    ctx, _ = make_ctx(w)
    assert T.Discard(resources=("workstation_cooker_01",)).done(ctx) is False


def test_discard_tree_goes_to_bin_then_returns_pot():
    """spec §6.1：完整流程是 取锅 → 去垃圾桶倒掉 → 锅放回灶台。"""
    tree = T.Discard(resources=("workstation_cooker_01",)).build()
    assert isinstance(tree, PR.Timeout)
    assert len(tree.child.children) == 3


def test_recover_loose_pot_feasible_when_pot_on_floor():
    """spec §6.1 第 12 号：可行性是纯观测事实"地上有锅"。

    与第 5 号同源——这是火情丢锅能自动回收的机制，不依赖
    dropped_cooked_pot 之类的记忆字段。
    """
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)],
                  items=[{"n": "utensil_pot_01", "x": 2.4, "z": 0.0}])
    ctx, _ = make_ctx(w)
    t = T.RecoverLoosePot(resources=("workstation_cooker_01",),
                          item="utensil_pot_01")
    assert t.feasible(ctx) is True


def test_recover_loose_pot_infeasible_for_cooked_pot():
    """柜台上的熟锅是「留料待用」，不是该捡回灶台的散锅。

    否则 RescueCooked 刚把熟锅救到台面，下一拍就被 RecoverLoosePot 端回
    灶台重新点火烧糊（实机复现）。
    """
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)],
                  items=[{"n": "utensil_pot_01", "x": 2.4, "z": 0.0,
                          "detail": ["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"]}])
    ctx, _ = make_ctx(w)
    t = T.RecoverLoosePot(resources=("workstation_cooker_01",),
                          item="utensil_pot_01")
    assert t.feasible(ctx) is False


def test_recover_loose_pot_infeasible_without_loose_pot():
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)],
                  items=[])
    ctx, _ = make_ctx(w)
    assert T.RecoverLoosePot(resources=("workstation_cooker_01",),
                             item="utensil_pot_01").feasible(ctx) is False


def test_recover_loose_pot_ignores_non_cookware_items():
    """地上的黄瓜不是锅——锅具识别走配方数据（spec §9）。"""
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)],
                  items=[{"n": "Cucumber", "x": 2.4, "z": 0.0}])
    ctx, _ = make_ctx(w)
    assert T.RecoverLoosePot(resources=("workstation_cooker_01",),
                             item="Cucumber").feasible(ctx) is False


def test_recover_loose_pot_infeasible_when_cooker_occupied():
    """目标灶已有锅 → 放不下。"""
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False)],
                  items=[{"n": "utensil_pot_01", "x": 2.4, "z": 0.0}])
    ctx, _ = make_ctx(w)
    assert T.RecoverLoosePot(resources=("workstation_cooker_01",),
                             item="utensil_pot_01").feasible(ctx) is False


def test_recover_loose_pot_done_when_pot_on_cooker():
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=False)],
                  items=[])
    ctx, _ = make_ctx(w)
    assert T.RecoverLoosePot(resources=("workstation_cooker_01",),
                             item="utensil_pot_01").done(ctx) is True


def test_stash_held_feasible_when_holding_irrelevant_item():
    """spec §6.1 第 13 号：手里莫名拿着无用物（误拾、订单被抢后剩的料）。"""
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Lettuce",
            "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert T.StashHeld().feasible(ctx) is True


def test_stash_held_infeasible_when_holding_needed_item():
    """手里拿着某单要的料 → 别放下，去装盘。"""
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Seaweed",
            "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert T.StashHeld().feasible(ctx) is False


def test_stash_held_infeasible_when_holding_cookware():
    """手持锅走 ReturnPot / RecoverLoosePot，不走 StashHeld。

    锅扔台面上会让灶位空着没锅可用。
    """
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "utensil_pot_01", "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert T.StashHeld().feasible(ctx) is False


def test_stash_held_accepts_burnt_pot():
    """糊锅是例外：没人收它，先搁台面把手腾出来。

    ReturnPot 不收（返灶＝再烧一次）、RecoverLoosePot 只认地上的锅、
    Discard 只管灶上还在烧的那口——全都不管就会一直攥在手里。
    """
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "utensil_pot_01", "interactions": inter()}
    w.items = [{"n": "utensil_pot_01", "x": 0.0, "z": 0.0,
                "has_contents": True, "cooked": "Burnt"}]
    ctx, _ = make_ctx(w, chef=chef)
    assert T.StashHeld().feasible(ctx) is True


def test_stash_held_infeasible_when_hands_free():
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER])
    ctx, _ = make_ctx(w)
    assert T.StashHeld().feasible(ctx) is False


def test_stash_held_done_when_hands_free():
    w = FakeWorld(stations=[COUNTER], recipes=RECIPES, orders=[ORDER])
    ctx, _ = make_ctx(w)
    assert T.StashHeld().done(ctx) is True


def test_stash_held_tree_is_ensure_hands_free():
    """落点约束与 RescueCooked 共用（spec §6.1）。"""
    tree = T.StashHeld().build()
    assert isinstance(tree, PR.Timeout)
    assert any(isinstance(n, PR.EnsureHandsFree)
               for n in tree.child.children)


def test_stash_held_has_no_order_id():
    """spec §6.0：StashHeld / RecoverLoosePot 与订单无关，order_id 为 None。

    打分时订单价值项记 0（spec §6.0 倒数第二段）。
    """
    assert T.StashHeld().order_id is None


def test_prefetch_feasible_when_two_orders_need_it():
    """spec §6.1 第 15 号的世界谓词：≥2 单需要 且 手空 且 台面没有它。

    "必然需要"的可观测近似——两单都要的料提前取一份不会浪费。
    """
    w = FakeWorld(stations=[SEAWEED_CRATE], recipes=RECIPES,
                  orders=[ORDER, ORDER_FISH2])
    ctx, _ = make_ctx(w)
    assert T.Prefetch(item="Seaweed").feasible(ctx) is True


def test_prefetch_infeasible_for_single_order_item():
    """只有一单要 → 不提前取，正常流程会取。"""
    w = FakeWorld(stations=[SEAWEED_CRATE], recipes=RECIPES, orders=[ORDER])
    ctx, _ = make_ctx(w)
    assert T.Prefetch(item="Seaweed").feasible(ctx) is False


def test_prefetch_infeasible_when_hands_busy():
    w = FakeWorld(stations=[SEAWEED_CRATE], recipes=RECIPES,
                  orders=[ORDER, ORDER_FISH2])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0, "held": "Cucumber",
            "interactions": inter()}
    ctx, _ = make_ctx(w, chef=chef)
    assert T.Prefetch(item="Seaweed").feasible(ctx) is False


def test_prefetch_infeasible_when_already_loose_on_counter():
    """台面上已有一份 → 别再取（完成判定即"料已在台面"）。"""
    w = FakeWorld(stations=[SEAWEED_CRATE], recipes=RECIPES,
                  orders=[ORDER, ORDER_FISH2],
                  items=[{"n": "Seaweed", "x": 2.4, "z": 0.0}])
    ctx, _ = make_ctx(w)
    t = T.Prefetch(item="Seaweed")
    assert t.done(ctx) is True
    assert t.feasible(ctx) is False


def test_prefetch_infeasible_for_cooked_item():
    """熟料没法"提前取"——得先下锅煮，那是 FetchToCooker 的活。"""
    w = FakeWorld(stations=[RICE_CRATE], recipes=RECIPES,
                  orders=[ORDER, ORDER_FISH2])
    ctx, _ = make_ctx(w)
    assert T.Prefetch(item="BoiledSushiRice").feasible(ctx) is False


def test_cleanup_tasks_are_registered():
    for name in ("Discard", "RecoverLoosePot", "StashHeld", "Prefetch"):
        assert name in T.TASK_TYPES


def test_all_spec_tasks_are_registered():
    """spec §6.1 的 task 全部登记（Idle 不在候选集，归 Plan 3）。

    这是 TASK_TYPES 完整性的总卡口：漏登记不会报错，只是 tree_viz
    看不到那组 task。
    """
    expected = {
        "PlaceCleanPlate", "WashPlate",                       # 1, 2
        "FetchToCooker", "TakeCookedToPlate", "ReturnPot",    # 3, 4, 5
        "RescueCooked",                                       # 6
        "Extinguish",                                         # 6.5（灭火）
        "FetchToBoard", "Chop",                               # 7, 8
        "FetchToPlate",                                       # 9
        "DeliverPlate",                                       # 10
        "Discard", "RecoverLoosePot", "StashHeld",            # 11, 12, 13
        "ResetPlate",                                         # 14
        "Prefetch",                                           # 15
        "Mix",                                                # 16（拌合，阶段 4）
        "PlaceToRelay",                                       # 17（1-3 中转传盘）
        "FillCooker",                                         # 18（订单无关填灶）
    }
    assert set(T.TASK_TYPES) == expected, \
        "缺 %r / 多 %r" % (sorted(expected - set(T.TASK_TYPES)),
                           sorted(set(T.TASK_TYPES) - expected))


def test_return_pot_refuses_cooked_pot():
    """熟锅不返灶 —— 放回去会重新点火、接着烧糊（spec §6.1 第 6 号）。

    熟锅该走 RescueCooked：端起锅放台面留料，不是放回灶台。
    """
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "utensil_pot_01", "interactions": inter()}
    w.items = [{"n": "utensil_pot_01", "x": 0.0, "z": 0.0,
                "has_contents": True, "cooked": "Cooked"}]
    ctx, _ = make_ctx(w, chef=chef)
    assert T.ReturnPot(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is False

    w.items = [{"n": "utensil_pot_01", "x": 0.0, "z": 0.0,
                "has_contents": False, "cooked": "Raw"}]     # 空锅 → 可以
    assert T.ReturnPot(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is True


def test_return_pot_refuses_burnt_pot():
    """糊锅也不返灶 —— 糊料留在灶上会接着烧（实机：火灭了又被送回去）。

    糊锅既不是"生料锅"（ReturnPot 收）也不是"熟锅"（RescueCooked 端走
    留料），content 状态必须区分出来；没人收时交给 StashHeld 搁台面。
    """
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)])
    chef = {"name": "Player 2", "x": 0.0, "z": 0.0,
            "held": "utensil_pot_01", "interactions": inter()}
    w.items = [{"n": "utensil_pot_01", "x": 0.0, "z": 0.0,
                "has_contents": True, "cooked": "Burnt"}]     # 糊锅
    ctx, _ = make_ctx(w, chef=chef)
    assert T.ReturnPot(resources=("workstation_cooker_01",)) \
        .feasible(ctx) is False


def test_recover_loose_pot_binds_to_that_pot():
    """捡地上那口锅的 pickup 步必须绑到**那一口**，不能只判"手里是锅具"。

    只判锅具的话，手里若已经有另一口锅，expect 立刻成立 → pickup 短路 →
    那口锅被放上灶就算成功，而地上被点名那口纹丝不动，`done()`（该灶有锅）
    却是真：日志说干完了，实际没干。
    """
    w = FakeWorld(stations=[COOKER], recipes=RECIPES, orders=[ORDER],
                  cookers=[cooker("workstation_cooker_01", cooked=None)])
    tree = T.RecoverLoosePot("order1", ("workstation_cooker_01",),
                             "utensil_pot_01 (3)").build()
    pickup = tree.child.children[0]

    def chef(held):
        return {"name": "Player 2", "x": 0.0, "z": 0.0, "held": held,
                "interactions": inter()}

    assert pickup.expect(make_ctx(w, chef=chef("utensil_pot_01 (3)"))[0]) is True
    assert pickup.expect(make_ctx(w, chef=chef("utensil_pot_01"))[0]) is False
    assert pickup.expect(make_ctx(w, chef=chef(None))[0]) is False

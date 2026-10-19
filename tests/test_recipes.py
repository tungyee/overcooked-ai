"""配方语义单测。

入参是 recipes / stations 列表而非 World（find_ingredient 例外——它要
扫 chefs / workstations / plates / items 四个数组），单测不造完整快照。
"""
import os

import pytest

from ai.query import recipes as R

# 实测帧的真实数据（tests/fixtures/real_frame_sushi_1_4.json）
RECIPES = [
    {"name": "Sushi_Fish", "t": "CompositeOrderNode", "comp":
        ["Seaweed", "BoiledSushiRice", "SushiFish"], "cook": "null"},
    {"name": "Sushi_Cucumber", "t": "CompositeOrderNode", "comp":
        ["Seaweed", "BoiledSushiRice", "Cucumber"], "cook": "null"},
    {"name": "BoiledSushiRice", "t": "CookedCompositeOrderNode",
     "comp": ["SushiRice"], "cook": "Pot", "plate": "CompositeSushi"},
    {"name": "Seaweed", "t": "IngredientOrderNode", "comp": [],
     "cook": "null", "plate": "CompositeSushi"},
    {"name": "Cucumber", "t": "IngredientOrderNode", "comp": [],
     "cook": "null", "plate": "CompositeSushi"},
    {"name": "SushiFish", "t": "IngredientOrderNode", "comp": [],
     "cook": "null", "plate": "CompositeSushi"},
    {"name": "SushiRice", "t": "IngredientOrderNode", "comp": [],
     "cook": "null", "plate": "null"},
]

STATIONS = [
    {"t": "crate", "n": "DispenserCrate 3 (1)", "ing": "Seaweed",
     "chop": None, "chop_stages": None, "gx": -5, "gz": 4},
    {"t": "crate", "n": "DispenserCrate 3 (5)", "ing": "SushiFish",
     "chop": "ChoppedSushiFish", "chop_stages": 8, "gx": -1, "gz": 4},
    {"t": "crate", "n": "DispenserCrate 3 (3)", "ing": "SushiRice",
     "chop": None, "chop_stages": None, "gx": -5, "gz": 3},
    {"t": "crate", "n": "DispenserCrate 3 (6)", "ing": "Cucumber",
     "chop": "ChoppedCucumber", "chop_stages": 8, "gx": -1, "gz": 3},
]

ORDER_FISH = {"id": "1", "dish": "Sushi_Fish",
              "composition": ["Seaweed", "BoiledSushiRice", "SushiFish"]}


def book():
    return R.RecipeBook(RECIPES, STATIONS)


def test_requirements_from_order_composition():
    assert book().requirements(ORDER_FISH) == (
        "Seaweed", "BoiledSushiRice", "SushiFish")


def test_requirements_falls_back_to_recipe_comp():
    """订单没带 composition 时从配方的 comp 取。

    实测帧的订单都带 composition，但它不保证永远在——dish 名字查配方
    是一条独立可用的路，缺了它这种帧会让整单无候选、AI 站着不动。
    """
    assert book().requirements({"dish": "Sushi_Cucumber"}) == (
        "Seaweed", "BoiledSushiRice", "Cucumber")


def test_requirements_unknown_order_is_empty():
    assert book().requirements({"dish": "NoSuchDish"}) == ()
    assert book().requirements(None) == ()


def test_needs_cooking():
    b = book()
    assert b.needs_cooking("BoiledSushiRice") is True
    assert b.needs_cooking("Cucumber") is False
    assert b.needs_cooking("NoSuch") is False


def test_raw_and_cookware_of_cooked_item():
    b = book()
    assert b.raw_of("BoiledSushiRice") == "SushiRice"
    assert b.cookware_of("BoiledSushiRice") == "Pot"
    assert b.raw_of("Cucumber") is None
    assert b.cookware_of("Cucumber") is None


def test_missing_reverse_expansion():
    """spec §6.0：从需求反向展开，已满足的不生成候选。"""
    b = book()
    plate = {"n": "p1", "contents": ["Seaweed"]}
    assert b.missing(ORDER_FISH, plate) == ("BoiledSushiRice", "SushiFish")


def test_missing_with_no_plate_is_everything():
    assert book().missing(ORDER_FISH, None) == (
        "Seaweed", "BoiledSushiRice", "SushiFish")


def test_missing_complete_plate_uses_real_frame_vocabulary():
    """核心用例：一张做完的盘长什么样，取自实测帧。

    801 帧实测：做完的 Sushi_Fish 盘是
        ['CookedCompositeAssembledNode', 'SushiFish', 'Seaweed']
    而订单要的是 ['Seaweed', 'BoiledSushiRice', 'SushiFish']。
    若按字面比较，BoiledSushiRice 永远报缺。
    """
    plate = {"contents": ["CookedCompositeAssembledNode", "SushiFish",
                          "Seaweed"]}
    assert book().missing(ORDER_FISH, plate) == ()


def test_missing_cooked_item_matched_by_opaque_token():
    """熟料只靠 token 匹配，字面名永不出现在盘里。"""
    b = book()
    plate = {"contents": ["CookedCompositeAssembledNode"]}
    assert b.plate_has(plate, "BoiledSushiRice") is True
    assert b.missing(ORDER_FISH, plate) == ("Seaweed", "SushiFish")


def test_want_tokens_rules():
    b = book()
    # 1. 需烹饪 → rich + opaque 两个 token
    assert b.want_tokens("BoiledSushiRice") == {
        "cooked:sushirice:pot", R.COOKED_PLATE_TOKEN}
    # 2. 需切的料 → 生料名（不是 chop_target）
    assert b.want_tokens("SushiFish") == {"sushifish"}
    assert b.want_tokens("Cucumber") == {"cucumber"}
    # 3. 其余 → 原名
    assert b.want_tokens("Seaweed") == {"seaweed"}
    assert b.want_tokens(None) == set()


def test_chopped_item_matches_by_raw_name_not_chop_target():
    """实测：切好的料进盘后回落到生料名。

    板上是 ChoppedSushiFish，倒进盘后 contents 里是 SushiFish
    （帧 118→146 的 WS/plate 联动已验证）。若用 chop_target 去匹配，
    FetchToPlate.done 永不成立。
    """
    b = book()
    assert b.chop_target("SushiFish") == "ChoppedSushiFish"
    assert b.want_tokens("SushiFish") == {"sushifish"}
    plate = {"contents": ["SushiFish"]}
    assert b.plate_has(plate, "SushiFish") is True


def test_missing_is_case_insensitive():
    """盘里的名字与配方名大小写不保证一致；不容忍会让整单永远"缺料"。"""
    plate = {"contents": ["seaweed", "SUSHIFISH"]}
    assert book().missing(ORDER_FISH, plate) == ("BoiledSushiRice",)


def test_missing_ignores_extra_contents():
    """盘里多放了料不影响"还缺什么"的判断。"""
    plate = {"contents": ["Seaweed", "Lettuce"]}
    assert book().missing(ORDER_FISH, plate) == (
        "BoiledSushiRice", "SushiFish")


@pytest.fixture(scope="module")
def real_frames():
    """真实录制帧。缺夹具时跳过——没录帧的环境仍能跑完其余测试。

    与 tests/test_real_frames.py 的 frames 夹具同源但不共享（那个是
    module-scope，跨文件不可见），所以这里自己加载一份。
    """
    from ai.exec.replay import load_frames
    path = os.path.join(os.path.dirname(__file__), "fixtures",
                        "full_level.ndjson.gz")
    if not os.path.exists(path):
        pytest.skip("缺 tests/fixtures/full_level.ndjson.gz")
    fs = load_frames(path)
    assert fs, "样本帧文件为空"
    return fs


def test_plate_has_on_real_frames(real_frames):
    """真实帧全扫：每张"看起来做完了"的盘，都该被判为某张单料齐。

    这是对装盘态映射的端到端验证——若映射错了，三料盘会被判成还缺料，
    AI 永远不出餐。
    """
    checked = 0
    for f in real_frames:
        orders = f.get("orders") or []
        if not orders:
            continue
        b = R.RecipeBook(f.get("recipes"), f.get("stations"))
        for p in (f.get("plates") or []):
            contents = p.get("contents") or []
            if len(contents) < 3:
                continue
            checked += 1
            assert any(b.missing(o, p) == () for o in orders), \
                "三料盘 %r 没被任何订单判为料齐" % (contents,)
    assert checked > 0, "真实帧里没有三料盘，这个测试没验到东西"


# ---------- 切配（Review Focus 4）----------

def test_chop_target_from_crate_data():
    b = book()
    assert b.chop_target("Cucumber") == "ChoppedCucumber"
    assert b.chop_target("SushiFish") == "ChoppedSushiFish"
    assert b.chop_target("Seaweed") is None, "chop 为 null 的料不可切"
    assert b.chop_target("NoSuch") is None


def test_chop_target_not_prefix_matching():
    """Review Focus 4：产物名不以 Chopped 开头也必须正确。

    spec §7.3 点名这是初版的误判源。假一个 chop 产物叫 SlicedTuna——
    前缀匹配会认为"板上的 SlicedTuna 不是 Chopped*，还没切好"，于是
    无限切下去。
    """
    b = R.RecipeBook(RECIPES, [
        {"t": "crate", "ing": "Tuna", "chop": "SlicedTuna", "chop_stages": 4},
    ])
    assert b.chop_target("Tuna") == "SlicedTuna"
    assert b.needs_chopping("Tuna") is True


def test_chop_stages():
    b = book()
    assert b.chop_stages("Cucumber") == 8
    assert b.chop_stages("Seaweed") == 0
    assert b.chop_stages("NoSuch") == 0


def test_chop_stages_malformed_is_zero():
    b = R.RecipeBook(RECIPES, [
        {"t": "crate", "ing": "X", "chop": "ChoppedX", "chop_stages": "abc"},
    ])
    assert b.chop_stages("X") == 0


def test_case_insensitive_lookups():
    b = book()
    assert b.chop_target("cucumber") == "ChoppedCucumber"
    assert b.needs_cooking("boiledsushirice") is True


# ---------- 找料 ----------

class FakeWorld(object):
    def __init__(self, chefs=None, workstations=None, plates=None,
                 items=None, stations=None):
        self.chefs = chefs or []
        self.workstations = workstations or []
        self.plates = plates or []
        self.items = items or []
        self.stations = stations if stations is not None else STATIONS


def test_find_ingredient_held_self_only():
    """held 只认自己（P2）；P1 手里的料拿不到，应跳过继续找箱子。"""
    w = FakeWorld(chefs=[{"name": "Player 2", "held": "Cucumber"}])
    got = R.find_ingredient(w, "Cucumber")
    assert got == {"kind": "held", "chef": "Player 2"}

    w = FakeWorld(chefs=[{"name": "Player 1", "held": "Cucumber"}])
    got = R.find_ingredient(w, "Cucumber")
    assert got["kind"] == "crate"


def test_find_ingredient_board():
    ws = {"n": "board1", "item": "ChoppedCucumber"}
    w = FakeWorld(workstations=[ws])
    got = R.find_ingredient(w, "ChoppedCucumber")
    assert got["kind"] == "board" and got["st"] is ws


def test_find_ingredient_board_merges_station_coords():
    """board 分支要带上同名 stations[] 条目的坐标。

    实机复现：workstations[] 只有 n/item/interacting/interacters，没有
    gx/gz/x/z；FetchToPlate 取板上的料时 Approach 拿不到目标格，每拍
    FAILURE 死循环（角色定在砧板前不动）。坐标在同名 stations[] 条目里。
    """
    ws = {"n": "countertop_01_chopping_board_wood_no_edge",
          "item": "ChoppedSushiFish"}
    stations = [{"t": "chopping",
                 "n": "countertop_01_chopping_board_wood_no_edge",
                 "gx": 5, "gz": 3, "x": 20.4, "z": -2.4}]
    w = FakeWorld(workstations=[ws], stations=stations)
    got = R.find_ingredient(w, "ChoppedSushiFish")
    assert got["kind"] == "board"
    st = got["st"]
    assert st["gx"] == 5 and st["gz"] == 3
    assert st["x"] == 20.4 and st["z"] == -2.4
    # 原 workstation 字段保留，n 仍在（interact_ready 的名匹配兜底要用）
    assert st["n"] == "countertop_01_chopping_board_wood_no_edge"
    assert st["item"] == "ChoppedSushiFish"


def test_find_ingredient_plate():
    p = {"n": "p1", "contents": ["Cucumber"]}
    w = FakeWorld(plates=[p])
    got = R.find_ingredient(w, "Cucumber")
    assert got["kind"] == "plate" and got["st"] is p


def test_find_ingredient_exclude_plate_skips_to_crate():
    """盘上已有该料时，exclude_plate 跳过盘、继续找到箱子。

    实机：order#1 的盘里已有海苔/鱼，order#2 取料不该被盘挡住（箱子里无限供应）。
    """
    p = {"n": "p1", "contents": ["Seaweed"]}
    w = FakeWorld(plates=[p])
    got = R.find_ingredient(w, "Seaweed", exclude_plate=True)
    assert got["kind"] == "crate"


def test_find_ingredient_loose():
    it = {"n": "Cucumber", "x": 1.0, "z": 2.0}
    w = FakeWorld(items=[it])
    got = R.find_ingredient(w, "Cucumber")
    assert got["kind"] == "loose" and got["st"] is it


def test_find_ingredient_crate():
    got = R.find_ingredient(FakeWorld(), "Cucumber")
    assert got["kind"] == "crate"
    assert got["st"]["n"] == "DispenserCrate 3 (6)"


def test_find_ingredient_priority_held_over_crate():
    """手上优先于箱子：已经拿着的东西不该再去箱子取一份。"""
    w = FakeWorld(chefs=[{"name": "Player 2", "held": "Cucumber"}])
    assert R.find_ingredient(w, "Cucumber")["kind"] == "held"


def test_find_ingredient_exact_name_not_substring():
    """精确名匹配：Cucumber 与 ChoppedCucumber 是两种不同的料。

    宽松匹配会让"去拿生黄瓜"的 task 看到板上的切好黄瓜就以为料到手了。
    """
    w = FakeWorld(workstations=[{"n": "b", "item": "ChoppedCucumber"}],
                  stations=[])
    assert R.find_ingredient(w, "Cucumber") is None


def test_find_ingredient_missing_is_none():
    assert R.find_ingredient(FakeWorld(stations=[]), "Lettuce") is None
    assert R.find_ingredient(FakeWorld(), None) is None
    assert R.find_ingredient(FakeWorld(), "") is None

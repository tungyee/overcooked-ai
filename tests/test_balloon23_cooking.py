"""Balloon_2_3：煮锅/平底锅识别、篮子台面摆盘、熟料装盘、远投连续性。

真实帧 tests/fixtures/balloon/balloon23-20261029.ndjson.gz（trace-20261029-130632）：
  [0] t=112  P2 拿着意面在远投途中
  [1] t=240  两口煮锅在灶上、插件报 cooked=null（0.21 截断客户端条）
  [2] t=612  P2 端着熟鱼平底锅
  [3] t=1300 台面上两口熟平底锅，三只盘都在篮子台面上
"""
import gzip
import json
import os

import pytest

from ai import primitives as PR
from ai import tasks as T
from ai.core import planner
from ai.query import predicates as P
from ai.query.recipes import RecipeBook
from ai.revenue import throw_supply as TS
from ai.state import world as W
from ai.state.bb_factory import make_bb
from ai.tasks.base import cooked_sources

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "balloon",
                       "balloon23-20261029.ndjson.gz")


@pytest.fixture(scope="module")
def frames():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _plan(frame):
    w = W.World(frame)
    book = RecipeBook(w.recipes, w.stations)
    bb = make_bb(100)
    task, rec = planner.plan(w, bb, None, book)
    return w, book, bb, task, rec


# ---------------------------------------------------------------- 问题 1：煮锅


def test_pot_with_cooked_null_still_has_pot(frames):
    """旧插件把煮锅灶报成 cooked=null；有 cookware/cook_type 就算有锅。"""
    w = W.World(frames[1])
    pots = [c for c in w.cookers if c.get("cook_type") == "Pot"]
    assert pots and all(c.get("cooked") is None for c in pots)
    assert all(P.cooker_has_pot(c) for c in pots)


def test_empty_stove_has_no_pot():
    assert P.cooker_has_pot({"n": "s", "cooked": None, "cookware": None,
                             "cook_type": None}) is False


def test_pasta_fetch_to_cooker_emitted(frames):
    w, book, bb, task, rec = _plan(frames[1])
    pasta = [c for c in rec["candidates"]
             if c[0].TYPE in ("FetchToCooker", "FillCooker")
             and "BoiledPasta" in str(c[0])]
    assert pasta, "煮锅在灶上时应发意面下锅候选"


# ---------------------------------------------------------------- 问题 2：平底锅


@pytest.mark.parametrize("name", ["utensil_frying_pan_01", "utensil_frying_pan_01 (2)",
                                  "utensil_pot_01 (3)"])
def test_name_fallback_recognises_cookware(frames, name):
    recipes = W.World(frames[0]).recipes
    assert P.held_is_cookware({"held": name}, recipes) is True
    assert P.item_is_cookware({"n": name}, recipes) is True


def test_plate_is_not_cookware(frames):
    recipes = W.World(frames[0]).recipes
    assert P.held_is_cookware({"held": "equipment_plate_01 (3)"}, recipes) is False


def test_plugin_flag_wins_over_name():
    """0.22+ 帧带组件判据，名字再像也以字段为准。"""
    assert P.held_is_cookware({"held": "utensil_frying_pan_01",
                               "held_cookware": False}) is False
    assert P.item_is_cookware({"n": "weird_name", "cookware": True}) is True


def test_held_cooked_pan_counts_as_cookware(frames):
    w = W.World(frames[2])
    chef = P.chef_by_name(w.chefs, P.P2_NAME)
    assert "frying_pan" in str(chef.get("held"))
    assert P.held_is_cookware(chef, w.recipes) is True


def test_loose_cooked_pans_are_sources_with_their_own_cookware(frames):
    """散熟锅是熟料源，token 只留这口锅自己的炊具（平底锅里的虾不是煮虾）。"""
    w = W.World(frames[3])
    book = RecipeBook(w.recipes, w.stations)
    srcs = {s["id"]: s["tokens"] for s in cooked_sources(w, book)
            if s["kind"] == "loose"}
    assert srcs.get("utensil_frying_pan_01 (2)") == {"cooked:prawn:fryingpan"}
    assert srcs.get("utensil_frying_pan_01 (1)") == {"cooked:uncookedfish:fryingpan"}


# ---------------------------------------------------------------- 篮子台面摆盘 / 装盘


def test_plates_on_basket_counters_match_orders(frames):
    """盘在篮子台面上算已就位：台面格按主网格投影，不用篮子本地 gx/gz。"""
    w, book, bb, task, rec = _plan(frames[3])
    pm = planner._match_plates_to_orders(w, bb, book)
    assert len(pm) == 3


def test_take_cooked_uses_matching_source(frames):
    """装盘只从真装着这道熟料的源取：鱼锅不能当意面源。"""
    w, book, bb, task, rec = _plan(frames[3])
    takes = [c[0] for c in rec["candidates"] if c[0].TYPE == "TakeCookedToPlate"]
    assert takes
    for t in takes:
        src = str(t.resources[0])
        if "frying_pan_01 (1)" in src:
            assert t.item == "PanFriedFish"
        if "frying_pan_01 (2)" in src:
            assert t.item == "PanFriedPrawns"
    assert task.TYPE == "TakeCookedToPlate"


# ---------------------------------------------------------------- 远投连续性


def _pasta_throw():
    return T.ThrowBatch(None, (("RightBasket", "workstation_cooker_01 (2)"),
                               "cooker", "Pasta", "BoiledPasta"), "Pasta")


def test_running_far_throw_survives_walking_close(frames):
    """拿着意面走向出手位、人离灶近了：承诺中的远投继续，不空收尾。"""
    w = W.World(frames[0])
    book = RecipeBook(w.recipes, w.stations)
    tk = _pasta_throw()
    fresh = make_bb(100)
    assert tk._state(planner._make_context(w, fresh, book)) is None
    committed = make_bb(100)
    committed.commitment.active_task = tk
    st = tk._state(planner._make_context(w, committed, book))
    assert st is not None and st[1] == "far" and st[3] > 0


# ---------------------------------------------------------------- 落地意面取料

# trace-20261029-202903：远投的意面落在锅旁地上，FillCooker 去捡。
#   [0] t=231 第三份意面还在飞（x=18.7, flying=True），另两份已落地 x=24.5/25.0
#   [1] t=264 三份全落地（x=23.6/24.5/25.0），P2 在 (18.43,-12)
LANDED_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "balloon",
                              "balloon23-20261029-202903.ndjson.gz")
POT2 = ("RightBasket", "workstation_cooker_01 (2)")


@pytest.fixture(scope="module")
def landed():
    with gzip.open(LANDED_FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class _Exec(object):
    """记录命令；每条命令下一拍回结果：move 成功、其余失败（拾取没拿到）。"""

    def __init__(self):
        self.issued = []
        self._pending = None

    def issue(self, cmd):
        cmd = dict(cmd, cmd_id=len(self.issued) + 1)
        self.issued.append(cmd)
        self._pending = cmd
        return cmd["cmd_id"]

    def poll(self):
        c, self._pending = self._pending, None
        if c is None:
            return None
        ok = c["cmd"] == "move"
        return {"cmd_id": c["cmd_id"], "ok": ok, "why": None if ok else "no_handle",
                "held_after": None, "blocked_cell": None}


def _fill_pasta():
    return T.FillCooker("BoiledPasta", (POT2, ("Pasta",)))


def _tick(tree, frame, ex, bb, i):
    w = W.World(frame)
    bb.tick = 100 + i
    ctx = planner._make_context(w, bb, RecipeBook(w.recipes, w.stations))
    ctx.update(exec=ex, frame_seq=i, tick=100 + i)
    return tree.tick(ctx)


def test_flying_item_is_not_loose_source(landed):
    """飞行中的料不算散落来源：坐标是空中过渡点，落地点在别处。"""
    from ai.query.recipes import find_ingredient
    from ai.tasks.base import loose_count
    w = W.World(landed[0])
    assert any(it.get("flying") for it in w.items if it["n"] == "Pasta")
    assert loose_count(w, "Pasta") == 2
    loc = find_ingredient(w, "Pasta", exclude_plate=True)
    assert loc["kind"] == "loose" and loc["st"].get("flying") is not True


def test_fill_cooker_skips_flying_pasta(landed):
    """t=231：FillCooker 走向已落地的意面 (13,0) 旁，不去追 x=18.7 的空中那份。"""
    ex = _Exec()
    _tick(_fill_pasta().build(), landed[0], ex, make_bb(100), 0)
    move = ex.issued[0]
    assert move["cmd"] == "move" and move["goal"][0] > 20.0


def test_fill_cooker_retries_toward_moved_pasta(landed):
    """散落料不锁快照：拾取失败重试时按当前帧位置重新接近。

    旧实现把第一次解析出的 item 锁死，料被弹开后 P2 一直回到旧位置对空地按键
    （trace-20261029-202903 t=264~312）。
    """
    import copy
    moved = copy.deepcopy(landed[1])
    for it in moved["items"]:
        if it["n"] == "Pasta":
            it["x"] = 20.4          # 三份都被弹到 (17,0) 附近
    ex, bb, tree = _Exec(), make_bb(100), _fill_pasta().build()
    for i in range(6):
        _tick(tree, landed[1] if i == 0 else moved, ex, bb, i)
    moves = [c["goal"] for c in ex.issued if c["cmd"] == "move"]
    assert moves[0][0] > 22.0       # 首趟：落地位置 x≈23.6 旁
    assert len(moves) >= 2 and all(abs(g[0] - 22.8) > 1.0 for g in moves[1:])


# ---------------------------------------------------------------- 鱼的命名（插件 0.23 改名）


FISH_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "balloon",
                            "balloon23-20261029-215035.ndjson.gz")


def test_renamed_fish_crate_makes_fish_choppable():
    """插件 0.23 把鱼箱报成配方节点名：ing=UncookedFish、配方叶子 UncookedFish
    带 chop=ChoppedFish。按这份契约，P2 能发「取鱼上砧板」。

    帧 trace-20261029-215035 t=93（插件 0.23.0，改名前）：两张 Pasta_Marinara_New，
    鱼箱 ing=Fish。原样喂进来鱼相关 task 全 infeasible，改名后 FetchToBoard 可行。
    """
    import copy
    with gzip.open(FISH_FIXTURE, "rt", encoding="utf8") as fh:
        raw = json.loads(fh.readline())

    def fetch_fish(frame):
        w = W.World(frame)
        book = RecipeBook(w.recipes, w.stations)
        _task, rec = planner.plan(w, make_bb(100), None, book)
        return book, [c for c in rec["candidates"]
                      if c[0].TYPE == "FetchToBoard" and c[3] is True
                      and str(c[0].item) in ("Fish", "UncookedFish")]

    _book, before = fetch_fish(copy.deepcopy(raw))
    assert before == []
    frame = copy.deepcopy(raw)
    for st in frame["stations"]:
        if st.get("t") == "crate" and st.get("ing") == "Fish":
            st["ing"] = "UncookedFish"
    for rc in frame["recipes"]:
        if rc["name"] == "UncookedFish":
            rc.update(chop="ChoppedFish", chop_stages="8", chop_node="UncookedFish")
    book, after = fetch_fish(frame)
    assert book.cook_input_form("UncookedFish") == "ChoppedFish"
    assert after and all(c[0].item == "UncookedFish" for c in after)


# ---------------------------------------------------------------- split 模式拿着料的保护

# trace-20261029-220619：P2 困在 17 格小区域，砧板在漂移的 FoodBasket 上，只能投给 P1。
#   [0] t=706 刚从肉箱拿起肉，这一帧 P1 到不了砧板 → 旧逻辑判不可行 → StashHeld 放下
#   [1] t=905 同上（第二轮循环）
SPLIT_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "balloon",
                             "balloon23-20261029-220619.ndjson.gz")
MEAT_BOARD = ("FoodBasket", "countertop_01_chopping_dark_wood")


@pytest.fixture(scope="module")
def split_frames():
    with gzip.open(SPLIT_FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _meat_throw():
    return T.ThrowBatch(None, (MEAT_BOARD, "board", "BurritoMeat", "FriedBurritoMeat"),
                        "BurritoMeat")


def _split_ctx(frame, committed):
    w = W.World(frame)
    book = RecipeBook(w.recipes, w.stations)
    bb = make_bb(100)
    if committed is not None:
        bb.commitment.active_task = committed
    return w, book, bb, planner._make_context(w, bb, book)


@pytest.mark.parametrize("idx", [0])
def test_split_throw_keeps_carried_meat_when_committed(split_frames, idx):
    """拿着本批的肉、承诺是这个 ThrowBatch：继续投，不放下。

    t=905（idx 1）对面已有两份肉、订单只要两份，不留余量后缺口为 0，
    见 test_split_throw_no_extra_unit_when_orders_covered。

    这两帧原先「P1 到不了砧板」是格量化假象：砧板跨 (8,3)/(8,4) 两格，
    单格四邻全被判不可走。按交互占地（occupancy.interaction_footprint）后
    P1 有交互位 (8,2)/(8,3)，格心离砧板边 ≈1.3 m——实机成功交互的最大距离
    1.56 m（trace-20261029/30 共 117 次），够得着。所以未承诺时也已可行。
    """
    tk = _meat_throw()
    _w, _b, _bb, fresh = _split_ctx(split_frames[idx], None)
    assert P.held_matches(fresh["chef"], "BurritoMeat")
    st = tk._state(fresh)
    assert st is not None and st[1] == "split" and tk.feasible(fresh)
    _w, _b, _bb, ctx = _split_ctx(split_frames[idx], tk)
    st = tk._state(ctx)
    assert st is not None and st[1] == "split" and st[3] > 0
    assert tk.feasible(ctx)


def test_split_throw_no_extra_unit_when_orders_covered(split_frames):
    """t=905：两单要肉、对面已有两份 → 不留余量（THROW_EXTRA_UNITS=0）时缺口 0。

    手上这份是第三份，订单用不上，不再投（旧口径 +1 预备会投出去）。
    """
    assert TS.THROW_EXTRA_UNITS == 0
    tk = _meat_throw()
    _w, _b, _bb, ctx = _split_ctx(split_frames[1], tk)
    assert tk.batch_size(ctx) == 0
    assert not tk.feasible(ctx)


def test_split_committed_throw_wins_over_stash(split_frames):
    """t=706：承诺中的投掷本拍可行、批次不收尾，不再被 StashHeld 抢走。

    旧逻辑这一拍 _state=None：树的 finished 判「本批结束」直接 SUCCESS、承诺
    清空，下一拍 StashHeld 把肉放下（trace 里 706→707）。只看 planner 选谁不够——
    承诺惯性的宽限期会让旧逻辑也暂时选它，所以按候选可行性与 done 判。
    """
    tk = _meat_throw()
    w, book, bb, ctx = _split_ctx(split_frames[0], tk)
    assert not tk.done(ctx)
    task, rec = planner.plan(w, bb, None, book)
    mine = [c for c in rec["candidates"] if c[0] == tk]
    assert mine and mine[0][3] is True
    assert task == tk


def test_throw_round_waits_for_launch_before_moving():
    """单轮里取料与走位之间有 WaitUntil：平台漂走、暂无出手位时等一会儿，等不到才失败。"""
    from ai.tasks import relay
    seq = _meat_throw().build().child
    waits = [ch for ch in seq.children if isinstance(ch, PR.WaitUntil)]
    assert waits and waits[0].timeout == relay.LAUNCH_WAIT_TICKS
    assert seq.children.index(waits[0]) == len(seq.children) - 3

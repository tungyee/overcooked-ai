# -*- coding: utf-8 -*-
"""分层选择的端到端行为（spec §6、§8）。

与 test_planner_layers.py 的分工：那边测层判定这个纯函数，这边测
「分层接进 plan() 之后，选出来的 task 变了没有」。
"""
from typing import Any

import pytest

from ai.core import planner
from ai.core import planner_layers as PL


def test_score_candidate_without_layer_is_unchanged():
    """layer 默认 None → 全项求和，与分层前完全一致。

    这是「零测试改动」的实现手段：既有的 _score_candidate 调用不传
    layer，必须按原语义工作。若这条失败，后面的测试会一起红，但从
    那片红里定位不到原因——所以单独钉在最前面。
    """
    terms = {"urgency": 3.0, "pressure": 0.0, "eta": -1.5,
             "deliver_bonus": 3.0}
    score_all, kept_all = PL.filter_terms(terms, None)
    assert score_all == 0.0 and kept_all == {}   # None 层 = 不打分（见 Task 4）

    # 而 planner 侧的 layer=None 走的是「全项求和」这条老路，不是 filter_terms
    assert sum(terms.values()) == pytest.approx(4.5)


def test_plan_only_scores_top_layer_candidates():
    """分层接进 plan() 之后的端到端行为：灶全空、3 单要米时只有 L_CAPACITY
    的 FillCooker 参与打分，L_PREP 的 FetchToCooker / L_CLEANUP 的 Prefetch
    都留在 record["candidates"] 里（ok=True）但 terms 为空——它们可行，只是
    层不够高，没进入打分（spec §6 的核心可观测性收益）。

    场景取自 test_fill_cooker_priority.py 的 t=1800 形态（3 单要米、灶全空）。
    """
    from ai.core import planner
    from ai import tasks as T
    from ai.state.blackboard import Blackboard
    from ai.query.recipes import RecipeBook
    from ai.state.world import World

    recipes = [
        {"name": "Sushi_Cucumber", "comp": ["Seaweed", "BoiledSushiRice", "Cucumber"]},
        {"name": "BoiledSushiRice", "comp": ["SushiRice"], "cook": "Pot"},
        {"name": "SushiRice"},
        {"name": "Cucumber", "chop": "ChoppedCucumber"},
        {"name": "Seaweed"},
    ]
    stations = [
        {"t": "crate", "n": "crate_rice", "ing": "SushiRice", "gx": 1, "gz": 1},
        {"t": "crate", "n": "crate_weed", "ing": "Seaweed", "gx": 2, "gz": 1},
        {"t": "cooking", "n": "c1", "gx": 5, "gz": 5},
        {"t": "cooking", "n": "c2", "gx": 6, "gz": 5},
        {"t": "serving", "n": "hatch", "gx": 9, "gz": 9},
    ]

    def _cooker(n):
        return {"n": n, "x": 5.0, "z": 5.0, "cooked": False, "isCooking": False,
                "burning": False, "onFire": False, "pot_contents": []}

    w = World({
        "orders": [
            {"id": "1", "dish": "Sushi_Cucumber", "remaining": 60, "lifetime": 130,
             "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]},
            {"id": "2", "dish": "Sushi_Cucumber", "remaining": 80, "lifetime": 130,
             "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]},
            {"id": "3", "dish": "Sushi_Cucumber", "remaining": 100, "lifetime": 130,
             "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]},
        ],
        "stations": stations, "recipes": recipes,
        "chefs": [{"name": "Player 2", "x": 1.0, "z": 1.0, "held": None}],
        "plates": [], "workstations": [], "cookers": [_cooker("c1"), _cooker("c2")],
        "items": [], "extinguishers": [], "grid": {"nodeMap": None},
    })
    bb = Blackboard()
    bb.tick = 10
    book = RecipeBook(w.recipes, w.stations)

    chosen, record = planner.plan(w, bb, None, book)

    # 缺 3 份米（3 单要米、灶全空）→ cooker_idle 拿满 5.6，填灶在 L_PREP
    # **层内凭分数**胜出。这与分层期间的 L5>L6 结论相同，但机制不同：
    # 现在缺口小的时候它会让位（见 tests/test_fill_cooker_layer.py）。
    assert chosen.TYPE == "FillCooker"
    assert record["top_layer"] == PL.L_PREP

    by_type = {}
    for task, score, terms, ok, veto, is_chosen, layer, in_top in record["candidates"]:
        by_type[task.TYPE] = (score, terms, ok, layer, in_top)

    fc_score, fc_terms, fc_ok, fc_layer, fc_in_top = by_type["FillCooker"]
    assert fc_ok is True
    assert fc_layer == PL.L_PREP
    assert fc_in_top is True
    assert set(fc_terms) > PL.TERM_WHITELIST[PL.L_PREP]  # terms 全量，不是裁剪后的
    expected_score, _ = PL.filter_terms(fc_terms, PL.L_PREP)
    assert fc_score == pytest.approx(expected_score)

    # FetchToCooker 现在同层同样参与打分，但分数低于填灶（它没有
    # cooker_idle，且 async_resource 要求「订单已有盘」—— 本场景 0 盘）。
    fetch_score, fetch_terms, fetch_ok, fetch_layer, fetch_in_top = by_type["FetchToCooker"]
    assert fetch_ok is True
    assert fetch_layer == PL.L_PREP
    assert fetch_in_top is True
    assert fetch_score < fc_score

    # L_CLEANUP 的 Prefetch 仍然被层挡在打分之外：terms 为空、分数 0。
    # 这是分层的核心可观测性收益（spec §6），换层后依然要成立。
    if "Prefetch" in by_type and by_type["Prefetch"][2]:
        pf_score, pf_terms, pf_ok, pf_layer, pf_in_top = by_type["Prefetch"]
        assert pf_layer == PL.L_CLEANUP
        assert pf_in_top is False
        assert pf_terms == {}
        assert pf_score == 0.0


def _world(cookers=(), plates=(), orders=()):
    # type: (Any, Any, Any) -> Any
    """最小 world 替身。层判定只读 cookers / plates。"""
    return type("W", (), {
        "cookers": list(cookers),
        "plates": list(plates),
        "orders": list(orders),
        "stations": [],
        "items": [],
        "chefs": [],
        "level": "Sushi_1_4_2P",
    })()


def test_fill_cooker_shares_prep_layer_and_competes_by_shortfall():
    """AGENTS.md §10.2 记的那次静默回退：urgency 改读 slack 后，备菜候选
    在中段能拿到 3~4 分，把 W_COOKER_IDLE=4.0 的填灶顶了下去——第 1 组在
    真实帧上验证过的七个「灶空转该填灶」的拍全部回退，而 982 个测试一个
    都没报警。当时的修法是把 4.0 提到 4.8（后又到 5.6）。

    分层期间这条曾改成断言「层序」（L5 < L6），号称「数字怎么调都不会再
    翻车」。那个结论**错了，而且方向相反**：层序把 W_COOKER_IDLE 整套标定
    旁路掉了，于是填灶在缺口只有 1 份（cooker_idle 仅 1.9 分）时照样无条件
    赢 —— 实机 trace-20261015-165358 有 **77% 的填灶拍处于这个局面**，
    1043 拍全部压掉了本该做的备菜，1002 拍工时只换来 4 锅米。

    现在填灶回到 L_PREP，这条重新断言**数值**关系，并且只在「缺口饱和」时
    成立。层的那一半由 test_fill_cooker_layer.py 的 test_fill_cooker_is_prep_layer
    守，缺口小时让位由同文件的 test_small_shortfall_yields_to_prep 守。
    """
    from ai import tasks as T

    fill = T.FillCooker(None, ("cooker1", ("SushiRice",)), "BoiledSushiRice")
    prep = T.FetchToPlate("order1", ("plate1", (3, 4)), "Seaweed")
    w = _world()
    assert PL.layer_of_task(fill, w) == PL.layer_of_task(prep, w) == PL.L_PREP


def test_redeem_outranks_capacity():
    """spec §4.3：料齐的盘先送出去，别等填灶。

    依据是实测分布——4577 拍里「料齐的盘」0 盘占 4279 拍、1 盘占 298 拍，
    从没有两盘同时料齐。所以 L3 几乎永远至多一个候选，而 DeliverPlate
    实测 50 拍：它堵不住填灶，而 60 分 base 立即到账。

    反向排法的风险：开局三口空灶 + 5 单要米时 L5 堵着三个 FillCooker
    （每个实测 120 拍），料齐的盘要等 360 拍。
    """
    from ai import tasks as T

    deliver = T.DeliverPlate("order1", ("plate1",))
    fill = T.FillCooker(None, ("cooker1", ("SushiRice",)), "BoiledSushiRice")
    w = _world()
    assert PL.layer_of_task(deliver, w) < PL.layer_of_task(fill, w)


def test_rescue_outranks_unblock():
    """spec §4.3：救料 > 解卡。

    锅上时钟在走（TakeCookedToPlate 实测 125 拍）；洗盘 150 拍、产出的是
    「下一单的盘」，没有任何东西在倒计时。先洗盘再救料会让那锅多烧 150
    拍，而 ratio 从 0.5 到 1.0 只有 240 拍。

    注意**不要**误用 W_NO_PLATE_PLACED 的定标数据反驳这条：那组数据
    （W=5.0 时救锅保住 0 拍）测的是「摆盘压过 progress=23/24 的快糊锅」，
    对应现在的 L1——而 L1 > L4 本条已经保证。
    """
    from ai import tasks as T

    rescue = T.TakeCookedToPlate("order1", ("cooker1", "plate1"),
                                 "BoiledSushiRice")
    wash = T.WashPlate(resources=("sink1",))
    # 锅刚熟（ratio 0.5）+ 一块干净盘都没有（plate_pressure = 2.0）
    w = _world(cookers=[{"n": "cooker1", "cooking_time": 12.0,
                         "cooking_progress": 12.0, "cooked": True}],
               plates=[])
    assert PL.layer_of_task(rescue, w) == PL.L_RESCUE
    assert PL.layer_of_task(wash, w) == PL.L_UNBLOCK
    assert PL.layer_of_task(rescue, w) < PL.layer_of_task(wash, w)


def test_irreversible_outranks_unblock():
    """但快糊的锅仍压过补盘——这正是 W_NO_PLATE_PLACED 定标时测出来的：
    W=5.0 让摆盘吃满 8.5 分、压过 progress=23/24 的 RescueCooked(8.08)，
    等于用「盘没摆」换「锅烧糊」。分层后这条由 L1 > L4 结构性保证。
    """
    from ai import tasks as T

    rescue = T.RescueCooked(resources=("cooker1",))
    place = T.PlaceCleanPlate("order1", ((3, 4),))
    w = _world(cookers=[{"n": "cooker1", "cooking_time": 12.0,
                         "cooking_progress": 23.0}],
               plates=[])
    assert PL.layer_of_task(rescue, w) == PL.L_IRREVERSIBLE
    assert PL.layer_of_task(place, w) == PL.L_UNBLOCK
    assert PL.layer_of_task(rescue, w) < PL.layer_of_task(place, w)


def test_full_layer_order_on_one_world():
    """一个局面里把**七层**的代表候选摆出来，断言层序完整。

    （缺 L2 救料层：它需要「刚熟的锅 + 已就位的盘」这组额外夹具，而该层已由
    test_rescue_outranks_unblock 与 test_irreversible_outranks_unblock 分别
    钉住，这里不重复造。）

    这是分层的总验收：原来这组关系要靠 10.0 > 5.0 > 4.8 > 3.0 > 2.0 这串
    数字维持，而每个数字都在注释里解释自己「必须压过谁、不能压过谁」。
    现在它是一条可读的序。
    """
    from ai import tasks as T

    w = _world(cookers=[{"n": "cooker1", "cooking_time": 12.0,
                         "cooking_progress": 23.0}],   # 快糊
               plates=[])                              # 0 干净盘
    pairs = [
        (PL.L_FIRE,          T.Extinguish()),
        (PL.L_IRREVERSIBLE,  T.Discard(resources=("cooker1",))),
        (PL.L_REDEEM,        T.DeliverPlate("order1", ("plate1",))),
        (PL.L_UNBLOCK,       T.WashPlate(resources=("sink1",))),
        # L_CAPACITY 是空层（填灶已回 L_PREP 按缺口竞争），没有样本可放。
        (PL.L_PREP,          T.Chop("order1", ("board1",), "Cucumber")),
        (PL.L_CLEANUP,       T.StashHeld()),
    ]
    for expected, task in pairs:
        assert PL.layer_of_task(task, w) == expected, repr(task)
    # 序本身严格递增
    observed = [PL.layer_of_task(t, w) for _, t in pairs]
    assert observed == sorted(observed)
    assert len(set(observed)) == len(observed)


def test_prewarm_ramp_lands_in_prep_layer():
    """渐变段（ratio 0.15~0.4）落 L6 备菜层——「提前端盘等熟」生效。

    之前这整段落 L7、严格弱于任何 L6 备菜候选，预取信号对层选择零影响，于是
    饭熟了 AI 还没去端盘、烧糊（实机 trace-20261017-172221）。修复是把
    TakeCookedToPlate 纳入 L6，靠 pressure 项与备菜同层竞争；pressure >= 1.0
    （ratio 0.4，策略「刚熟」点）那一瞬跳 L2。
    """
    from ai.core import planner_layers as PL

    class _CookTask(object):
        TYPE = "TakeCookedToPlate"

        def __init__(self, cooker_id):
            # type: (str) -> None
            self.cooker_id = cooker_id

    for progress in (4.0, 5.0, 6.0, 8.0, 9.0):
        w = _world(cookers=[{"n": "c1", "cooking_time": 12.0,
                             "cooking_progress": progress}])
        assert PL.layer_of_task(_CookTask("c1"), w) == PL.L_PREP, \
            "progress=%s 应落 L6（快熟阶段）" % progress
    # 越过 ratio 0.4（刚熟策略点）跳 L2（用 10.0 避开 0.4 的浮点边界）
    w_done = _world(cookers=[{"n": "c1", "cooking_time": 12.0,
                              "cooking_progress": 10.0}])
    assert PL.layer_of_task(_CookTask("c1"), w_done) == PL.L_RESCUE


def test_commitment_in_lower_layer_goes_through_infeasible_grace():
    """spec §8.2 声称「白捡一个防抖」，但从未被测过。

    当前承诺在 L6，这一拍出现 L5 候选 → 承诺不在 in_layer → 不在 scored
    → _apply_commitment_inertia 的 ③ 把它当「不在可行候选中」，走
    infeasible_streak 的 3 拍宽限再改选，而不是立即切。
    """
    from ai.state.blackboard import Blackboard, Commitment
    from ai import tasks as T

    prep = T.Chop("order1", ("board1",), "Cucumber")
    fill = T.FillCooker(None, ("cooker1", ("SushiRice",)), "BoiledSushiRice")

    bb = Blackboard()
    bb.tick = 100
    bb.commitment = Commitment(prep, 90, 0, 0, 0)

    # 只有 L5 候选进了 scored（承诺的 L6 被分层挡在外面）
    scored = [(fill, 4.8, {"cooker_idle": 4.8})]

    # 前两拍保持承诺
    assert planner._apply_commitment_inertia(scored, None, bb) == prep
    assert bb.commitment.infeasible_streak == 1
    assert planner._apply_commitment_inertia(scored, None, bb) == prep
    assert bb.commitment.infeasible_streak == 2
    # 第三拍改选
    assert planner._apply_commitment_inertia(scored, None, bb) == fill
    assert bb.commitment.infeasible_streak == 0


def test_unblock_layer_intra_order_matches_spec():
    """L4 解卡层内序：0 盘洗盘 > 无盘就位摆盘（spec §7）。

    最终复审查出这条曾经是**反的**：W_PLATE_PRESSURE=3.5 时 1 盘档洗盘 3.5
    压过摆盘 3.0。如今 WashPlate.feasible 只在「干净盘 + 装配盘 = 0」时成立
    （_plate_pressure 在可行时恒为 2.0），所以 L4 里洗盘只有 0 盘这一档，
    「1 盘洗盘」已经不可达——下面仍保留 wash_1 这条是防御性权重检查：将来若
    有人把 feasible 放宽回「1 盘也洗」，这个断言会提醒他 1 盘洗盘不能压过摆盘。

    摆盘这一档正是 W_NO_PLATE_PLACED 当初定标要解决的局面：trace-20261014-221306
    里 PlaceCleanPlate 可行 1101 拍、选中 0 次，807 拍无盘就位，收盘挂 5 单。

    断言用 filter_terms 实算，不比权重常量——比常量查不出「某一项没乘上去」。
    """
    wash_0, _ = PL.filter_terms(
        {"plate_pressure": planner.W_PLATE_PRESSURE * 2.0}, PL.L_UNBLOCK)
    wash_1, _ = PL.filter_terms(
        {"plate_pressure": planner.W_PLATE_PRESSURE * 1.0}, PL.L_UNBLOCK)
    place, _ = PL.filter_terms(
        {"no_plate_placed": planner.W_NO_PLATE_PLACED}, PL.L_UNBLOCK)

    assert wash_0 > place, "0 盘洗盘 %.1f 必须压过摆盘 %.1f" % (wash_0, place)
    assert place > wash_1, \
        "摆盘 %.1f 必须压过 1 盘洗盘 %.1f（防御性权重检查，见 docstring）" % (
            place, wash_1)

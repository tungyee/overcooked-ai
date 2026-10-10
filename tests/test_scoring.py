"""打分权重完整集测试（Task 4）。

重点验证 Plan 4 新增的 6 项权重：
  1. 异步资源优先：FetchToCooker > FetchToPlate
  2. 接力加成：承接 P1 上游的下游 task
  3. 完成度：盘里食材越接近完成优先级越高
  4. 食材复用：一次取料满足多个订单
  5. Combo 延续：连续送同类菜加分
  6. 截止前瞻：会导致烧糊/超时的 task 惩罚
"""
import pytest
from ai import planner
from ai.blackboard import Blackboard, Commitment, P1Model, Intents
from ai.world import World
from ai import tasks as T


def _make_test_world(orders=None, stations=None, cookers=None):
    # type: (list, list, list) -> World
    """构造测试世界。"""
    state = {
        "chefs": [
            {"name": "Player 1", "x": 5.0, "z": 5.0, "held": None},
            {"name": "Player 2", "x": 10.0, "z": 10.0, "held": None}
        ],
        "orders": orders or [],
        "stations": stations or [],
        "cookers": cookers or [],
        "plates": [],
        "recipes": [],
        "workstations": [],
        "grid": {"nodeMap": None}
    }
    world = World(state)
    world.tick = 0
    return world


def test_async_resource_priority_fetch_to_cooker():
    """异步资源优先：FetchToCooker（下锅后自己跑）分数高于 FetchToPlate。"""
    # 构造场景：两个候选任务
    world = _make_test_world(
        orders=[{"id": "order1", "name": "Sushi", "remaining": 30, "composition": ["BoiledSushiRice", "SushiFish"]}],
        stations=[]
    )

    bb = Blackboard()
    bb.tick = 0
    bb.commitment = Commitment()
    bb.p1_model = P1Model()
    bb.intents = Intents()

    # 候选1: FetchToCooker（异步资源）
    cand1 = planner.Candidate(
        task=T.FetchToCooker("order1", ("cooker1",), "Rice"),
        preconditions=[],
        estimated_steps=15
    )

    # 候选2: FetchToPlate（同步资源，需要人等）
    cand2 = planner.Candidate(
        task=T.PlaceCleanPlate("order1", ("plate_station1",), None),
        preconditions=[],
        estimated_steps=10
    )

    # 打分
    score1, terms1 = planner._score_candidate(world, bb, cand1)
    score2, terms2 = planner._score_candidate(world, bb, cand2)

    # 断言：FetchToCooker 有异步加分
    assert "async_resource" in terms1
    assert terms1["async_resource"] > 0
    assert "async_resource" not in terms2 or terms2.get("async_resource", 0) == 0

    # 断言：在其他条件相同时，FetchToCooker 分数更高
    assert score1 > score2, f"FetchToCooker({score1}) 应高于 PlaceCleanPlate({score2})"


def test_relay_bonus_when_p1_upstream_complete():
    """接力加成：P1 完成上游任务（下锅），AI 的下游任务（取熟饭）加分。"""
    world = _make_test_world(
        orders=[{"id": "order1", "name": "Sushi", "remaining": 25, "composition": ["BoiledSushiRice"]}],
        stations=[],
        cookers=[{"t": "cooker", "n": "cooker1", "item": "Rice", "cooked": True, "burning": False, "gx": 5, "gz": 5}]
    )
    world.tick = 100

    bb = Blackboard()
    bb.tick = 100
    bb.commitment = Commitment()
    bb.intents = Intents()

    # P1 刚完成 FetchToCooker（下锅）
    bb.p1_model = P1Model()
    bb.p1_model.p1_active_task = "just_completed_fetch_to_cooker1"
    bb.p1_model.confidence = 0.8

    # 候选：TakeCookedToPlate（接力任务）
    cand = planner.Candidate(
        task=T.TakeCookedToPlate("order1", (("cooker1",), (None,)), "BoiledSushiRice"),
        preconditions=[],
        estimated_steps=15
    )

    score, terms = planner._score_candidate(world, bb, cand)

    # 断言：有接力加成
    assert "relay_bonus" in terms
    assert terms["relay_bonus"] > 0, "接力任务应有加成"


def test_completion_ratio_high_when_plate_near_done():
    """完成度：盘里食材接近完成（2/3）时，剩余 task 分数高。

    夹具按**真实帧**的形状写：盘在 `plates[]`、内容字段是 `contents`、
    熟料是不透明 token、归属走 `bb.plate_order`（盘→单）。
    旧夹具把盘写成 stations 的一项、内容字段用 `item`——那正是这个权重
    在实机上恒为 0 的原因（见 planner._order_completion_ratio 的说明）。
    """
    world = _make_test_world(
        orders=[{
            "id": "order1",
            "name": "Sushi",
            "remaining": 20,
            "composition": ["BoiledSushiRice", "SushiFish", "Seaweed"]
        }],
        stations=[]
    )
    world.recipes = [{"name": "BoiledSushiRice", "comp": ["SushiRice"],
                      "cook": "Pot"}]
    world.plates = [{"n": "plate1", "x": 6.0, "z": 6.0, "count": 2,
                     "contents": ["CookedCompositeAssembledNode",
                                  "SushiFish"]}]
    world.tick = 50

    bb = Blackboard()
    bb.tick = 50
    bb.commitment = Commitment()
    bb.p1_model = P1Model()
    bb.intents = Intents()

    # 候选：放最后一个食材（完成度 2/3）
    cand = planner.Candidate(
        task=T.FetchToBoard("order1", ("board1",), "Seaweed"),
        preconditions=[],
        estimated_steps=10
    )

    score, terms = planner._score_candidate(world, bb, cand,
                                            plate_match={"plate1": "order1"})

    # 断言：有完成度加成
    assert "completion_ratio" in terms
    assert terms["completion_ratio"] > 0, "接近完成的订单应有加成"


def test_completion_ratio_zero_when_plate_not_attributed():
    """没有归属时完成度算未知（0），不许拿"第一只盘"冒充该单的盘。

    旧实现查不到归属就 `return world.plates[0]`，于是每一张单都拿同一只盘
    算完成度——比恒 0 更糟。
    """
    world = _make_test_world(
        orders=[{"id": "order1", "name": "Sushi", "remaining": 20,
                 "composition": ["Seaweed"]}],
        stations=[]
    )
    world.plates = [{"n": "plate1", "x": 6.0, "z": 6.0, "count": 1,
                     "contents": ["Seaweed"]}]     # 有料，但不是这单的
    world.tick = 50
    bb = Blackboard()
    bb.tick = 50
    bb.commitment = Commitment()
    bb.p1_model = P1Model()
    bb.intents = Intents()
    assert planner._order_completion_ratio(
        T.FetchToBoard("order1", ("board1",), "Seaweed"), world, bb) == 0.0


def test_ingredient_reuse_bonus_when_multiple_orders_need_same():
    """食材复用：取一次黄瓜同时满足两个订单，加成。"""
    world = _make_test_world(
        orders=[
            {"id": "order1", "name": "Salad", "remaining": 25, "composition": ["Cucumber"]},
            {"id": "order2", "name": "Salad", "remaining": 28, "composition": ["Cucumber"]}
        ],
        stations=[
            {"t": "plate", "n": "plate1", "item": [], "gx": 3, "gz": 3},
            {"t": "plate", "n": "plate2", "item": [], "gx": 4, "gz": 4}
        ]
    )
    world.tick = 30

    bb = Blackboard()
    bb.tick = 30
    bb.commitment = Commitment()
    bb.p1_model = P1Model()
    bb.intents = Intents()
    bb.plate_assignments = {"order1": ("plate1",), "order2": ("plate2",)}

    # 候选：取黄瓜（可复用，满足 2 个订单）
    cand = planner.Candidate(
        task=T.FetchToBoard("order1", ("board1",), "Cucumber"),
        preconditions=[],
        estimated_steps=10
    )

    score, terms = planner._score_candidate(world, bb, cand)

    # 断言：有复用加成
    assert "ingredient_reuse" in terms
    assert terms["ingredient_reuse"] > 0, "复用食材应有加成"


def test_combo_continuation_when_last_dish_matches():
    """Combo 延续：上次送出 Sushi，这次再送 Sushi 加分。"""
    world = _make_test_world(
        orders=[{"id": "order1", "name": "Sushi", "remaining": 15, "composition": ["BoiledSushiRice"]}],
        stations=[
            {"t": "plate", "n": "plate1", "item": ["BoiledSushiRice"], "gx": 5, "gz": 5}
        ]
    )
    world.tick = 200

    bb = Blackboard()
    bb.tick = 200
    bb.commitment = Commitment()
    bb.p1_model = P1Model()
    bb.intents = Intents()
    bb.last_delivered_dish = "Sushi"  # 上次送的是 Sushi
    bb.plate_assignments = {"order1": ("plate1",)}

    # 候选：送餐（同类菜）
    cand = planner.Candidate(
        task=T.DeliverPlate("order1", ("counter1",), None),
        preconditions=[],
        estimated_steps=5
    )

    score, terms = planner._score_candidate(world, bb, cand)

    # 断言：有 combo 加成
    assert "combo_continue" in terms
    assert terms["combo_continue"] > 0, "连续送同类菜应有 combo 加成"


def test_deadline_penalty_when_cooker_will_burn():
    """截止前瞻：启动任务后会导致锅烧糊，惩罚。"""
    world = _make_test_world(
        orders=[{"id": "order1", "name": "Sushi", "remaining": 20, "composition": ["BoiledSushiRice"]}],
        stations=[],
        cookers=[
            # 锅里有熟饭，即将烧糊（cooking_time=19.6秒，距离burning只剩0.4秒=8拍）
            {"t": "cooker", "n": "cooker1", "item": "Rice", "cooked": True, "burning": False, "cooking_time": 19.6, "x": 6.0, "z": 6.0, "gx": 6, "gz": 6}
        ]
    )
    world.tick = 100

    bb = Blackboard()
    bb.tick = 100
    bb.commitment = Commitment()
    bb.p1_model = P1Model()
    bb.intents = Intents()
    bb.cooker_assignments = {"order1": ("cooker1",)}

    # 候选：去做一个耗时长的任务（会错过取熟饭的时机）
    cand = planner.Candidate(
        task=T.Chop("order1", ("board1",), "Cucumber"),  # 切菜需要 20 步
        preconditions=[],
        estimated_steps=20
    )

    score, terms = planner._score_candidate(world, bb, cand)

    # 断言：有截止惩罚
    assert "deadline_lookahead" in terms
    assert terms["deadline_lookahead"] < 0, "会错过截止的任务应被惩罚"


def test_no_deadline_penalty_when_safe():
    """截止前瞻：任务执行时间安全，无惩罚。"""
    world = _make_test_world(
        orders=[{"id": "order1", "name": "Sushi", "remaining": 40, "composition": ["BoiledSushiRice"]}]
    )
    world.tick = 50

    bb = Blackboard()
    bb.tick = 50
    bb.commitment = Commitment()
    bb.p1_model = P1Model()
    bb.intents = Intents()

    # 候选：快速任务，不会错过截止
    cand = planner.Candidate(
        task=T.PlaceCleanPlate("order1", ("plate_station1",), None),
        preconditions=[],
        estimated_steps=10
    )

    score, terms = planner._score_candidate(world, bb, cand)

    # 断言：无截止惩罚或惩罚为 0
    assert "deadline_lookahead" not in terms or terms["deadline_lookahead"] == 0


def test_deliver_plate_hand_penalty_uses_pickup_not_clear():
    """holding_plated_dish 的"手不对"是去捡盘，不是腾手，代价应更小。

    实机：DeliverPlate 被 HAND_CLEAR_STEPS(10) 惩罚压着，做好的盘送不出去，
    反而去 FetchToBoard 取生料。
    """
    world = _make_test_world(
        orders=[{"id": "order1", "name": "Sushi", "remaining": 30,
                 "composition": ["Seaweed"]}]
    )
    world.chefs[1]["held"] = None   # P2 空手

    bb = Blackboard()
    bb.tick = 0
    bb.commitment = Commitment()
    bb.p1_model = P1Model()
    bb.intents = Intents()

    cand = planner.Candidate(
        task=T.DeliverPlate("order1", ("p1",)),
        preconditions=["holding_plated_dish"],
        estimated_steps=5,
    )

    score, terms = planner._score_candidate(world, bb, cand)
    assert terms["hands"] == -(planner.W_ETA * planner.HAND_PICKUP_STEPS)


def test_all_weights_integrated():
    """综合测试：所有 10 项权重都参与打分。"""
    world = _make_test_world(
        orders=[{"id": "order1", "name": "Sushi", "remaining": 15, "composition": ["BoiledSushiRice"]}]
    )
    world.tick = 100

    bb = Blackboard()
    bb.tick = 100
    bb.commitment = Commitment()
    bb.p1_model = P1Model()
    bb.intents = Intents()

    cand = planner.Candidate(
        task=T.FetchToCooker("order1", ("cooker1",), "Rice"),
        preconditions=[],
        estimated_steps=15
    )

    score, terms = planner._score_candidate(world, bb, cand)

    # 断言：基础 4 项存在
    assert "urgency" in terms
    assert "pressure" in terms
    assert "eta" in terms
    assert "p1_conflict" in terms

    # 断言：总分是所有项的和（验证公式完整性）
    expected_total = sum(terms.values())
    assert abs(score - expected_total) < 0.01, f"总分({score})应等于各项之和({expected_total})"

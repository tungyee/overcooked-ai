"""打分权重完整集测试（Task 4）。

重点验证 Plan 4 新增的 5 项权重：
  1. 异步资源优先：FetchToCooker > FetchToPlate
  2. 完成度：盘里食材越接近完成优先级越高
  3. 食材复用：一次取料满足多个订单
  4. Combo 延续：连续送同类菜加分
  5. 截止前瞻：会导致烧糊/超时的 task 惩罚
"""
import pytest
from ai.core import planner
from ai.state.blackboard import Blackboard, Commitment
from ai.state.world import World
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
    assert planner._order_completion_ratio(
        T.FetchToBoard("order1", ("board1",), "Seaweed"), world, bb) == 0.0


def test_completion_bonus_is_convex_in_the_ratio():
    """完成度加分要凸：最后一样料远比第一样值钱。

    线性 ratio 给不出这个落差 —— 2/3 比 1/3 只多 W*0.33，而实机 urgency 的
    组间差就有 0.35，正好抵掉。实机 trace-20261014-204822 t=1353：order#1 的盘
    已有米+海苔（2/3，差一样黄瓜就能送），却以 2.24 输给往 1/3 的盘加第二样
    （2.69），于是五张盘各做一半、全部烂尾（本局收盘挂 5 单）。

    凸性的理由是收益结构：盘不齐一分不得，齐了就是 60 分 base。离齐一步之遥
    的那一手是**兑现**，中间那些手只是**投资**。
    """
    f = planner._completion_bonus
    assert f(0.0) == 0.0
    # 最后一步（2/3 → 齐）的增益必须大于第一步（0 → 1/3）
    assert f(2.0 / 3) - f(1.0 / 3) > f(1.0 / 3) - f(0.0)


def test_completion_bonus_beats_urgency_spread_at_two_thirds():
    """2/3 的盘必须压过「给更急的单的空盘加第一样料」。

    这是上面那个凸性要达到的具体量级：t=1353 的实况是 compl 差 0.34 对
    urgency 差 0.35。加分曲线得让 2/3 与 1/3 的差距明显超过同批候选里
    urgency 的典型组间差（约 0.5），否则改了形态也不改结论。
    """
    gap = planner._completion_bonus(2.0 / 3) - planner._completion_bonus(1.0 / 3)
    assert gap > 0.5, gap


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


def test_ingredient_reuse_excludes_plates_already_holding_it():
    """已经放了该料的盘不该再计入复用（实机 trace-20261014-011202）。

    根因：旧实现读 `plate["item"]`——帧里的字段叫 `contents`，于是"盘已有
    该料"永不成立，Seaweed 的 reuse 恒等于「所有要海苔的订单数」，拿满
    2.0 分压过 FetchToCooker 的异步 2.0 分。实测后果：4 个盘挨个摆海苔，
    锅一直空着（trace t=800/1000/1230 海苔 1.05 恒压米饭 0.90）。
    """
    world = _make_test_world(
        orders=[
            {"id": "order1", "name": "Sushi", "remaining": 25,
             "composition": ["Seaweed"]},
            {"id": "order2", "name": "Sushi", "remaining": 28,
             "composition": ["Seaweed"]},
        ],
    )
    # 两只盘都已经摆好海苔 → 复用计数应为 0
    world.plates = [
        {"n": "plate1", "x": 6.0, "z": 6.0, "count": 1,
         "contents": ["Seaweed"], "detail": ["ing:Seaweed"]},
        {"n": "plate2", "x": 7.0, "z": 7.0, "count": 1,
         "contents": ["Seaweed"], "detail": ["ing:Seaweed"]},
    ]
    world.tick = 30

    bb = Blackboard()
    bb.tick = 30
    bb.commitment = Commitment()

    count = planner._ingredient_reuse_count(
        T.FetchToPlate("order1", ("plate1",), "Seaweed"), world, bb,
        plate_match={"plate1": "order1", "plate2": "order2"})
    assert count == 0, "两只盘都已有海苔，不该再算复用"


def test_ingredient_reuse_counts_only_plates_still_lacking():
    """一只盘已有、一只盘还缺 → 复用计数为 1（不足以拿加成）。"""
    world = _make_test_world(
        orders=[
            {"id": "order1", "name": "Sushi", "remaining": 25,
             "composition": ["Seaweed"]},
            {"id": "order2", "name": "Sushi", "remaining": 28,
             "composition": ["Seaweed"]},
        ],
    )
    world.plates = [
        {"n": "plate1", "x": 6.0, "z": 6.0, "count": 1,
         "contents": ["Seaweed"], "detail": ["ing:Seaweed"]},
        {"n": "plate2", "x": 7.0, "z": 7.0, "count": 0,
         "contents": [], "detail": []},
    ]
    world.tick = 30

    bb = Blackboard()
    bb.tick = 30
    bb.commitment = Commitment()

    count = planner._ingredient_reuse_count(
        T.FetchToPlate("order2", ("plate2",), "Seaweed"), world, bb,
        plate_match={"plate1": "order1", "plate2": "order2"})
    assert count == 1, "只剩一只盘缺海苔"


def test_wash_plate_tree_faces_the_sink_stack():
    """洗盘的取脏盘步必须转身（face=True）、目标必须带坐标。

    实机（trace-20261014-004742）：WashPlate 出现 11 次，**全部 failure**，
    每次都横穿整张地图到脏盘摞旁的 (-1,-4) 再立刻失败，白跑 60~90 拍。

    根因是脏盘摞被当成散落物：树里用 `face=False` 直接 Tap，可它其实是
    **固定站点**（stations 里的 workstation_plate_return，有 gx/gz 和
    pickup 句柄）。站对位置但朝向不对时，`interactions.pickup` 是 null
    —— 对比 t=3753（yaw=117，句柄就绪）与 t=8421（yaw=270 朝左，句柄全
    null）即可见。不转身就永远按不到。

    另外 items[] 里的 DirtyPlateStack 只有世界坐标 x/z 而没有 gx/gz，
    直接拿去 Approach 要靠 to_grid 换算；用 stations 那条更稳。
    """
    from ai import primitives as PR
    from ai.exec.bt import Selector, Sequence

    tree = T.WashPlate(resources=("WashingPart",)).build()
    seq = tree.child
    assert isinstance(seq, Sequence)
    sel = seq.children[0]
    assert isinstance(sel, Selector)
    pickup = sel.children[0].children[0]
    assert isinstance(pickup, PR.InteractWith)
    assert pickup._face is not None, \
        "脏盘摞是固定站点，不转身则 pickup 句柄永远为 null"


def test_wash_plate_dirty_target_prefers_station_with_grid():
    """取脏盘的目标优先用带 gx/gz 的回盘台站点，而非只有世界坐标的 item。

    `items[]` 里的 DirtyPlateStack 只有 x/z；同位置的 stations 条目
    （实测 {'t':'plate_return','n':'workstation_plate_return','gx':-1,'gz':-5}）
    才带网格，Approach / interact_ready 都按它判更稳。晾干架也是
    plate_return，但那是取**干净**盘的位置，必须排除。
    """
    from ai.query.recipes import RecipeBook

    world = _make_test_world(
        stations=[
            {"t": "plate_return", "n": "workstation_plate_return",
             "gx": -1, "gz": -5, "x": 13.2, "z": -12.0},
            {"t": "plate_return", "n": "DryingPart",
             "gx": -7, "gz": 0, "x": 6.0, "z": -6.0},
        ])
    world.items = [{"n": "DirtyPlateStack", "x": 13.2, "z": -12.0}]

    tree = T.WashPlate(resources=("WashingPart",)).build()
    dirty = tree.child.children[0].children[0].children[0].target
    ctx = {"w": world, "book": RecipeBook(world.recipes, world.stations)}
    got = dirty(ctx)
    assert got is not None
    assert got.get("n") == "workstation_plate_return"
    assert (got.get("gx"), got.get("gz")) == (-1, -5)


def test_wash_plate_dirty_target_none_when_no_dirty_stack():
    """没有脏盘摞时返回 None（InteractWith 据此 FAILURE，交还 Planner）。"""
    from ai.query.recipes import RecipeBook

    world = _make_test_world(
        stations=[{"t": "plate_return", "n": "workstation_plate_return",
                   "gx": -1, "gz": -5, "x": 13.2, "z": -12.0}])
    world.items = []
    tree = T.WashPlate(resources=("WashingPart",)).build()
    dirty = tree.child.children[0].children[0].children[0].target
    ctx = {"w": world, "book": RecipeBook(world.recipes, world.stations)}
    assert dirty(ctx) is None


def test_combo_bonus_goes_to_the_queue_head_order():
    """Combo 加成给**屏幕队首**那一单，而不是"同类菜"。

    这条曾经断言「上次送 Sushi、这次再送 Sushi 就加分」——那个判据与游戏
    机制无关。真实机制是按队首依次送才累加、跳单或过期归零（57 次送达实测
    无反例，详见 tests/test_combo.py 与 ai/planner._combo_head_id）。
    """
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
    bb.plate_assignments = {"order1": ("plate1",)}

    cand = planner.Candidate(
        task=T.DeliverPlate("order1", ("counter1",), None),
        preconditions=[],
        estimated_steps=5
    )

    score, terms = planner._score_candidate(world, bb, cand)

    # order1 是唯一的单，天然是队首 → 吃到队首加分、不吃跳单惩罚
    assert terms["combo_head"] > 0, "送队首应有 combo 加成"
    assert terms["combo_break"] == 0.0


def test_deadline_penalty_when_cooker_will_burn():
    """截止前瞻：启动任务后会导致锅烧糊，惩罚。

    字段语义按真实帧（trace-20261014-011202 实测）：
      - `cooking_time` 是**常量容量**（这关恒为 12），不是已耗时；
      - `cooking_progress` 是进度，单位≈秒（实测 1.07/秒）；
      - 熟点 = progress 达到 cooking_time，糊点 = 2×cooking_time。
    这里 progress=23.5 / ctime=12 → 距糊点 0.5 秒 = 10 拍。
    """
    world = _make_test_world(
        orders=[{"id": "order1", "name": "Sushi", "remaining": 20, "composition": ["BoiledSushiRice"]}],
        stations=[],
        cookers=[
            {"t": "cooker", "n": "cooker1", "item": "Rice", "cooked": True,
             "burning": False, "cooking_progress": 23.5, "cooking_time": 12,
             "x": 6.0, "z": 6.0, "gx": 6, "gz": 6}
        ]
    )
    world.tick = 100

    bb = Blackboard()
    bb.tick = 100
    bb.commitment = Commitment()
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


def test_burn_eta_reads_progress_not_cooking_time():
    """烧糊剩余时间必须读 `cooking_progress`，不是 `cooking_time`。

    旧实现把 `cooking_time` 当成"已经煮了多久"：

        time_in_cooked_state = max(cooking_time - 10, 0)

    但真实帧里 `cooking_time` 是常量容量（这关恒 12），于是它算出的
    "距烧糊时间" 恒为常数，与锅的真实进度无关——这条前瞻实际从未按进度
    触发过（trace-20261014-011202：锅从 progress 16.9 一路烧到 24 着火，
    期间 planner 没给出任何烧糊惩罚）。
    """
    # 刚熟（progress≈ctime）→ 还有满额 12 秒 = 240 拍
    fresh = {"n": "c", "cooked": True, "burning": False,
             "cooking_progress": 12.0, "cooking_time": 12}
    assert planner._burn_eta_ticks(fresh) == pytest.approx(240.0)

    # 快糊（progress 23.5）→ 只剩 0.5 秒 = 10 拍
    hot = {"n": "c", "cooked": True, "burning": False,
           "cooking_progress": 23.5, "cooking_time": 12}
    assert planner._burn_eta_ticks(hot) == pytest.approx(10.0)

    # 已糊/着火 → 0
    burnt = {"n": "c", "cooked": True, "burning": True,
             "cooking_progress": 24.0, "cooking_time": 12}
    assert planner._burn_eta_ticks(burnt) == 0.0

    # 没在煮 → None（无烧糊截止）
    idle = {"n": "c", "cooked": False, "burning": False,
            "cooking_progress": 0, "cooking_time": 12}
    assert planner._burn_eta_ticks(idle) is None

    # 同一口灶若两个字段同值，旧实现与新实现会给出不同答案——
    # 钉住新实现不再被 cooking_time 单独决定。
    assert planner._burn_eta_ticks(
        {"n": "c", "cooked": True, "burning": False,
         "cooking_progress": 20.0, "cooking_time": 12}) == pytest.approx(80.0)


def test_no_deadline_penalty_when_safe():
    """截止前瞻：余量充足时无惩罚。

    remaining 从 40 提到 130：这一项现在读 slack（这一单剩下的**全部**活 vs
    剩余时间）而不是「单个 task 的常数估计 vs remaining*0.8」。剩 40 秒做一份
    还要煮米的寿司（实测一份约 37 秒手上工时）本就不安全，旧口径判它"安全"
    只是因为 est=10 拍=0.5 秒永远小于 remaining*0.8——那条前瞻整局只触发
    33 拍，等于从未工作。见 ai/planner._deadline_forecast。
    """
    world = _make_test_world(
        orders=[{"id": "order1", "name": "Sushi", "remaining": 130, "composition": ["BoiledSushiRice"]}]
    )
    world.tick = 50

    bb = Blackboard()
    bb.tick = 50
    bb.commitment = Commitment()

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

    cand = planner.Candidate(
        task=T.DeliverPlate("order1", ("p1",)),
        preconditions=["holding_plated_dish"],
        estimated_steps=5,
    )

    score, terms = planner._score_candidate(world, bb, cand)
    assert terms["hands"] == -(planner.W_ETA * planner.HAND_PICKUP_STEPS)


def test_all_weights_integrated():
    """综合测试：所有权重都参与打分。"""
    world = _make_test_world(
        orders=[{"id": "order1", "name": "Sushi", "remaining": 15, "composition": ["BoiledSushiRice"]}]
    )
    world.tick = 100

    bb = Blackboard()
    bb.tick = 100
    bb.commitment = Commitment()

    cand = planner.Candidate(
        task=T.FetchToCooker("order1", ("cooker1",), "Rice"),
        preconditions=[],
        estimated_steps=15
    )

    score, terms = planner._score_candidate(world, bb, cand)

    # 断言：基础 3 项存在
    assert "urgency" in terms
    assert "pressure" in terms
    assert "eta" in terms

    # 断言：总分是所有项的和（验证公式完整性）
    expected_total = sum(terms.values())
    assert abs(score - expected_total) < 0.01, f"总分({score})应等于各项之和({expected_total})"


def test_deliver_plate_gets_delivery_bonus_and_outranks_prep():
    """送餐优先：料齐盘的 DeliverPlate 应有 deliver_bonus，压过备菜候选。"""
    world = _make_test_world(
        orders=[{"id": "order1", "name": "Sushi", "remaining": 30,
                 "composition": ["BoiledSushiRice", "SushiFish"]}],
        stations=[]
    )
    world.chefs[1]["held"] = None   # P2 空手

    bb = Blackboard()
    bb.tick = 0
    bb.commitment = Commitment()

    deliver = planner.Candidate(
        task=T.DeliverPlate("order1", ("p1",)),
        preconditions=["holding_plated_dish"],
        estimated_steps=5,
    )
    prep = planner.Candidate(
        task=T.FetchToCooker("order1", ("cooker1",), "Rice"),
        preconditions=[],
        estimated_steps=15,
    )

    s_deliver, terms = planner._score_candidate(world, bb, deliver)
    s_prep, _ = planner._score_candidate(world, bb, prep)

    assert "deliver_bonus" in terms
    assert terms["deliver_bonus"] > 0
    assert s_deliver > s_prep, f"DeliverPlate({s_deliver}) 应高于备菜({s_prep})"


def test_wash_plate_gets_pressure_scaled_by_clean_plate_count():
    """洗盘压力：干净空盘越少，WashPlate 分越高（提前补盘，不等到用光）。"""
    world = _make_test_world(stations=[{"t": "washing", "n": "sink1"}])
    bb = Blackboard()
    bb.commitment = Commitment()
    cand = planner.Candidate(task=T.WashPlate(resources=("sink1",)),
                             preconditions=[], estimated_steps=10)

    def clean(*ns):
        return [{"n": n, "count": 0, "contents": []} for n in ns]

    # 只剩 1 块干净盘 → 压力 1.0
    world.plates = clean("p1")
    _, terms = planner._score_candidate(world, bb, cand)
    assert terms["plate_pressure"] == planner.W_PLATE_PRESSURE * 1.0

    # 用光 → 压力 2.0
    world.plates = []
    _, terms2 = planner._score_candidate(world, bb, cand)
    assert terms2["plate_pressure"] == planner.W_PLATE_PRESSURE * 2.0

    # 充足（>=2）→ 无压力
    world.plates = clean("p1", "p2")
    _, terms3 = planner._score_candidate(world, bb, cand)
    assert terms3["plate_pressure"] == 0.0


def test_async_resource_suppressed_when_order_has_no_plate():
    """没盘就位时，下锅/切菜不拿异步加分——避免提前备料抢走放盘。

    实机：洗好碗后干净盘躺晾干架，P2 却一直去下锅/切菜（async 2.0）而不去
    端盘（PlaceCleanPlate 纯负分）。异步优先只在「订单已有盘」时成立。
    """
    world = _make_test_world(
        orders=[{"id": "order1", "name": "Sushi", "remaining": 30,
                 "composition": ["BoiledSushiRice"]}])
    bb = Blackboard()
    bb.commitment = Commitment()

    cand = planner.Candidate(
        task=T.FetchToCooker("order1", ("cooker1",), "Rice"),
        preconditions=[], estimated_steps=15)

    # 无盘匹配信息（保守路径）→ 仍给 async，保持原「异步优先」语义
    _, terms = planner._score_candidate(world, bb, cand)
    assert terms["async_resource"] > 0

    # 盘↔订单匹配里没有 order1（没盘）→ 不给 async
    _, terms2 = planner._score_candidate(world, bb, cand, plate_match={})
    assert terms2["async_resource"] == 0.0

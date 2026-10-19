# -*- coding: utf-8 -*-
"""订单 ETA 与 slack 回归（第 2 组 2.1~2.3）。

旧 `_deadline_forecast` 整局只触发 33 拍，因为它拿 `candidate.estimated_steps`
当 ETA——那是一张按 TYPE 查的常数表（EST_FETCH/CHOP/COOK_OPS/SERVE =
10/20/15/5），10 拍 = 0.5 秒，对 130 秒的单寿命 `est > remaining*0.8` 几乎
永不成立；而且它只算**当前一个 task**，不是「这一单剩下的全部活」。

本模块的常数全部来自 trace 实测（见 ai/eta.py 的表），这里钉住：
  1. 单位耗时与两个成功订单的实际工时吻合；
  2. 沉没进度（盘里已有的料、锅里煮了一半的米）要折掉；
  3. 多单共享一双手 → 队列累计；
  4. slack < 0 的单判为必死。
"""
import pytest

from ai.revenue import eta as E
from ai.query.recipes import RecipeBook
from ai.state.world import World


RECIPES = [
    {"name": "Sushi_Cucumber", "comp": ["Seaweed", "BoiledSushiRice", "Cucumber"]},
    {"name": "Sushi_Fish", "comp": ["Seaweed", "BoiledSushiRice", "SushiFish"]},
    {"name": "BoiledSushiRice", "comp": ["SushiRice"], "cook": "Pot"},
    {"name": "SushiRice"},
    {"name": "Cucumber", "chop": "ChoppedCucumber"},
    {"name": "SushiFish", "chop": "ChoppedSushiFish"},
    {"name": "Seaweed"},
]

STATIONS = [
    {"t": "crate", "n": "crate_rice", "ing": "SushiRice", "gx": 1, "gz": 1},
    {"t": "crate", "n": "crate_weed", "ing": "Seaweed", "gx": 2, "gz": 1},
    {"t": "crate", "n": "crate_cuc", "ing": "Cucumber", "chop": "ChoppedCucumber",
     "chop_stages": 4, "gx": 3, "gz": 1},
    {"t": "chopping", "n": "board", "gx": 4, "gz": 1},
    {"t": "cooking", "n": "c1", "gx": 5, "gz": 5},
    {"t": "washing", "n": "sink", "gx": 8, "gz": 1},
    {"t": "serving", "n": "hatch", "gx": 9, "gz": 9},
]


def order(oid="1", dish="Sushi_Cucumber", remaining=130.0):
    comp = {"Sushi_Cucumber": ["Seaweed", "BoiledSushiRice", "Cucumber"],
            "Sushi_Fish": ["Seaweed", "BoiledSushiRice", "SushiFish"]}[dish]
    return {"id": oid, "dish": dish, "composition": comp,
            "remaining": remaining, "lifetime": 130}


def cooker(n="c1", cooked=None, cooking=False, contents=(), progress=0):
    return {"n": n, "x": 5.0, "z": 5.0, "cooked": cooked, "isCooking": cooking,
            "burning": False, "onFire": False, "pot_contents": list(contents),
            "cooking_progress": progress, "cooking_time": 12}


def plate(n="p1", detail=()):
    return {"n": n, "x": 1.0, "z": 1.0, "contents": [str(d).lower() for d in detail],
            "detail": list(detail)}


def world(orders=None, plates=(), cookers=(), held=None):
    return World({
        "orders": orders if orders is not None else [order()],
        "stations": STATIONS, "recipes": RECIPES,
        "chefs": [{"name": "Player 2", "x": 1.0, "z": 1.0, "held": held}],
        "plates": list(plates), "workstations": [], "cookers": list(cookers),
        "items": [], "extinguishers": [], "grid": {"nodeMap": None},
    })


def book(w):
    return RecipeBook(w.recipes, w.stations)


# ---------- 单位耗时的实测标定 ----------

def test_unit_costs_come_from_measurements():
    """单位耗时必须落在实测区间（中位数 ± 容差），不是凭空取整。

    trace-20261014-120406 成功 task 的中位耗时：FetchToPlate 136、
    FetchToBoard 139、FetchToCooker 157、TakeCookedToPlate 123、Chop 30、
    DeliverPlate 45、WashPlate 152、PlaceCleanPlate 175（另一局 004742 的
    FetchToPlate 112 / FetchToCooker 75 / TakeCooked 131 / Deliver 55 同量级）。
    """
    assert 100 <= E.COST_FETCH_TO_PLATE <= 150
    assert 100 <= E.COST_FETCH_TO_BOARD <= 150
    assert 70 <= E.COST_FETCH_TO_COOKER <= 160
    assert 100 <= E.COST_TAKE_COOKED <= 150
    assert 25 <= E.COST_CHOP <= 40
    assert 40 <= E.COST_DELIVER <= 100
    assert 120 <= E.COST_PLACE_PLATE <= 180


def test_full_order_eta_matches_measured_successful_orders():
    """一份寿司约 35~40 秒手上工时（order#1 实测 781 拍、order#2 697 拍）。

    这个数字本身就是结论：130s 寿命 ÷ 37s ≈ 同时最多托住 3 单，而本局并发
    5 单——必然丢 2~3 个。估计器必须落在这个量级，否则后面的 slack 全错。
    """
    w = world(plates=[plate()])          # 盘已就位（与 order#1/#2 的实况一致）
    ticks = E.order_eta_ticks(w, book(w), w.orders[0], plate_match={"p1": "1"})
    assert 600 <= ticks <= 950, ticks


def test_order_without_plate_costs_more():
    """没盘要先去取/洗，ETA 必须更高——否则 slack 会乐观。"""
    w1 = world(plates=[plate()])
    w2 = world(plates=[])
    b1, b2 = book(w1), book(w2)
    with_plate = E.order_eta_ticks(w1, b1, w1.orders[0], plate_match={"p1": "1"})
    without = E.order_eta_ticks(w2, b2, w2.orders[0], plate_match={})
    assert without > with_plate


# ---------- 沉没进度要折掉 ----------

def test_plated_ingredients_are_discounted():
    """盘里已有海苔 → 不再为它计一次取料。"""
    w_empty = world(plates=[plate("p1")])
    w_part = world(plates=[plate("p1", ["ing:Seaweed"])])
    full = E.order_eta_ticks(w_empty, book(w_empty), w_empty.orders[0],
                             plate_match={"p1": "1"})
    part = E.order_eta_ticks(w_part, book(w_part), w_part.orders[0],
                             plate_match={"p1": "1"})
    assert part < full
    assert full - part >= E.COST_FETCH_TO_PLATE * 0.5


def test_cooking_in_progress_is_discounted():
    """米已在煮（9.5/12）→ 不再计一次下锅，且等待只剩尾段。"""
    w_cold = world(plates=[plate()], cookers=[cooker()])
    w_hot = world(plates=[plate()],
                  cookers=[cooker(cooking=True, contents=["ing:SushiRice"],
                                  progress=9.5)])
    cold = E.order_eta_ticks(w_cold, book(w_cold), w_cold.orders[0],
                             plate_match={"p1": "1"}, cooker_match={})
    hot = E.order_eta_ticks(w_hot, book(w_hot), w_hot.orders[0],
                            plate_match={"p1": "1"}, cooker_match={"c1": "1"})
    assert hot < cold


def test_cooked_pot_ready_is_cheaper_than_raw():
    w = world(plates=[plate()],
              cookers=[cooker(cooked=True, progress=12,
                              contents=["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"])])
    ready = E.order_eta_ticks(w, book(w), w.orders[0],
                              plate_match={"p1": "1"}, cooker_match={"c1": "1"})
    w2 = world(plates=[plate()], cookers=[cooker()])
    raw = E.order_eta_ticks(w2, book(w2), w2.orders[0], plate_match={"p1": "1"})
    assert ready < raw


def test_nearly_complete_order_is_cheap():
    """只差送餐的单 ETA 应接近一次送餐的代价。

    盘内容用真实帧的词汇：切好的黄瓜在盘里记作 `ing:Cucumber`（**不是**
    ChoppedCucumber）——trace-20261014-120406 里全部 detail 只出现四种值：
    `CookedCompositeAssembledNode(Pot){ ing:SushiRice }`、`ing:Cucumber`、
    `ing:Seaweed`、`ing:SushiFish`。
    """
    p = plate("p1", ["ing:Seaweed",
                     "CookedCompositeAssembledNode(Pot){ ing:SushiRice }",
                     "ing:Cucumber"])
    w = world(plates=[p])
    ticks = E.order_eta_ticks(w, book(w), w.orders[0], plate_match={"p1": "1"})
    assert ticks <= E.COST_DELIVER * 2


# ---------- 烹饪是关键路径而非求和 ----------

def test_cook_wait_is_critical_path_not_sum():
    """煮饭的 12 秒里人是自由的（可以去切鱼），不该与手上工时相加。

    关键路径 = max(手上工时, 锅上时钟)，求和会把 ETA 夸大到人人必死。
    """
    w = world(plates=[plate()], cookers=[cooker()])
    b = book(w)
    hands = E.order_hand_ticks(w, b, w.orders[0], plate_match={"p1": "1"})
    total = E.order_eta_ticks(w, b, w.orders[0], plate_match={"p1": "1"})
    cook_ticks = 12 * E.TICKS_PER_SECOND
    assert total < hands + cook_ticks
    assert total >= hands


# ---------- 队列累计（单手约束）----------

def test_queue_accumulates_hand_time_across_orders():
    """每单单独算都「来得及」，但 P2 只有一双手。

    本局 order#3 吞掉 1214 拍还是过期，就是因为没人算过「为它花时间会让
    #4/#5 饿死」。队首用自己的 ETA，第二单要加上队首占用的手上工时。
    """
    orders = [order("1", "Sushi_Cucumber", 130), order("2", "Sushi_Fish", 130),
              order("3", "Sushi_Cucumber", 130)]
    w = world(orders=orders, plates=[plate("p1"), plate("p2"), plate("p3")])
    rows = E.order_queue(w, book(w),
                         plate_match={"p1": "1", "p2": "2", "p3": "3"})
    etas = [r.eta for r in rows]
    assert etas == sorted(etas), etas          # 队列按完工时刻递增
    assert etas[2] > etas[0] * 2               # 第三单要排在前两单之后


def test_queue_orders_by_deadline_first():
    """最早截止优先（EDF）：队列顺序按 remaining 升序，不按 id。"""
    orders = [order("9", "Sushi_Cucumber", 120), order("5", "Sushi_Fish", 40),
              order("7", "Sushi_Cucumber", 80)]
    w = world(orders=orders, plates=[])
    rows = E.order_queue(w, book(w), plate_match={})
    assert [r.order_id for r in rows] == ["5", "7", "9"]


# ---------- slack 与必死判定 ----------

def test_slack_is_remaining_minus_eta():
    w = world(plates=[plate()])
    rows = E.order_queue(w, book(w), plate_match={"p1": "1"})
    r = rows[0]
    assert r.slack == pytest.approx(
        r.remaining * E.TICKS_PER_SECOND - r.eta, abs=1.0)


def test_doomed_when_eta_exceeds_remaining():
    """只剩 5 秒、还差三样料 → 必死，别再投入。"""
    w = world(orders=[order("1", "Sushi_Cucumber", 5.0)], plates=[])
    rows = E.order_queue(w, book(w), plate_match={})
    assert rows[0].doomed is True


def test_not_doomed_when_plenty_of_time():
    w = world(orders=[order("1", "Sushi_Cucumber", 130.0)], plates=[plate()])
    rows = E.order_queue(w, book(w), plate_match={"p1": "1"})
    assert rows[0].doomed is False


def test_five_concurrent_orders_overflow_the_queue():
    """本局实况：5 单并发、一份 ≈37s 工时、单寿命 130s → 队列必然溢出。

    这是「order#3 吞掉 1214 拍仍过期、#4/#5 全程饿死」的量化形式。注意钉住
    的是**队列口径算出溢出**，不是「所以要判死」：这批单全都很新
    （remaining/lifetime 0.62~0.92），judgment 该留给新鲜度闸，见
    test_fresh_concurrent_orders_are_not_doomed_yet。
    """
    orders = [order(str(i), "Sushi_Cucumber", 130.0 - i * 10) for i in range(1, 6)]
    w = world(orders=orders, plates=[])
    rows = E.order_queue(w, book(w), plate_match={})
    assert rows[0].slack > 0, "队首来得及"
    assert sum(1 for r in rows if r.slack < 0) >= 3, \
        [(r.order_id, r.slack) for r in rows]


def test_fresh_concurrent_orders_are_not_doomed_yet():
    """实机 t=549 的原始局面：刚到的 #4 不该被判死。

    数值取自 trace-20261014-204822 t=549（#4 出现后第 19 拍就被拉黑）。那一拍
    #4 的 remaining=129.0/130s —— 寿命剩 99%。早期不该放弃任何单，该做的是排
    优先级；队列会因为前面送出而松动（本局 t=3352 order#3 正是这样解禁，只是
    已经太晚）。
    """
    orders = [order("1", "Sushi_Cucumber", 108.9),
              order("2", "Sushi_Cucumber", 108.9),
              order("3", "Sushi_Fish", 118.9),
              order("4", "Sushi_Fish", 129.0)]
    w = world(orders=orders, plates=[])
    rows = {r.order_id: r for r in E.order_queue(w, book(w), plate_match={})}
    assert rows["4"].doomed is False, \
        [(r.order_id, r.remaining, r.slack, r.doomed) for r in rows.values()]


def test_doomed_orders_do_not_consume_queue_capacity():
    """判死的单不占队列容量——既然放弃它，工时就该让给后面的单。

    少了这一步判死会沿队列雪崩：真实帧 t=1800 原本把 #4~#7 全判死，而实况是
    #3/#4/#5 过期、#6/#7 送达成功。放开容量后结论变成「#3 可救、#4/#5 必死、
    #6/#7 可救」，与实况逐条吻合。
    """
    # 中间插一个铁定做不完的单（只剩 2 秒），它不该把后面的单也拖死
    orders = [order("1", "Sushi_Cucumber", 130.0),
              order("2", "Sushi_Fish", 2.0),        # 必死
              order("3", "Sushi_Cucumber", 130.0)]
    w = world(orders=orders, plates=[plate("p1"), plate("p3")])
    rows = {r.order_id: r for r in E.order_queue(
        w, book(w), plate_match={"p1": "1", "p3": "3"})}
    assert rows["2"].doomed is True
    # #3 的 eta 不含 #2 的工时：必死单让出了容量
    assert rows["3"].eta == rows["1"].eta + rows["3"].hand_ticks
    assert rows["3"].doomed is False


def test_fresh_order_is_never_doomed():
    """刚到的单不判死，哪怕队列算出它必死。

    实机 trace-20261014-204822：order#4 在 t=530 出现、t=549 判死，当时
    remaining=129.0/130s（寿命剩 99%）；#5 同样 19 拍被拉黑。队列累计下只要
    有 4 单以上，队尾**定义上** slack<0 —— 判死退化成排位歧视。
    """
    orders = [order("1", "Sushi_Fish", 100.0), order("2", "Sushi_Fish", 105.0),
              order("3", "Sushi_Fish", 110.0),
              order("4", "Sushi_Fish", 129.0)]      # 寿命剩 99%
    w = world(orders=orders, plates=[])
    rows = {r.order_id: r for r in E.order_queue(w, book(w), plate_match={})}
    assert rows["4"].slack < 0, "队列仍应算出它来不及（口径没变）"
    assert rows["4"].doomed is False, "但太新，不该判死"


def test_stale_tail_order_is_still_doomed():
    """新鲜度闸只豁免新单：已经过半还排在队尾的单照样判死。"""
    orders = [order("1", "Sushi_Fish", 60.0), order("2", "Sushi_Fish", 62.0),
              order("3", "Sushi_Fish", 64.0), order("4", "Sushi_Fish", 66.0)]
    w = world(orders=orders, plates=[])
    rows = {r.order_id: r for r in E.order_queue(w, book(w), plate_match={})}
    assert rows["4"].doomed is True, [(r.order_id, r.slack) for r in rows.values()]


def test_doom_freshness_floor_is_a_ratio_not_absolute():
    """判死豁免按 remaining/lifetime，不按绝对秒数。"""
    assert E._too_fresh_to_doom({"remaining": 129.0, "lifetime": 130}) is True
    assert E._too_fresh_to_doom({"remaining": 60.0, "lifetime": 130}) is False
    # 缺字段 → 不豁免（保守，按原规则判）
    assert E._too_fresh_to_doom({"remaining": 129.0}) is False


# ---------- 独立 slack（打分口径）----------

def test_solo_slack_is_monotone_in_remaining():
    """同款菜：到期越早，独立 slack 越小（= 越紧迫）。

    这正是队列累计 slack 做不到的事 —— 它让队尾单 slack 最小，于是
    「排得越后越紧急」。实机 94% 的可比候选组因此倒挂。
    """
    orders = [order("1", "Sushi_Fish", 90.0), order("2", "Sushi_Fish", 100.0),
              order("3", "Sushi_Fish", 110.0)]
    w = world(orders=orders, plates=[])
    solo = E.solo_slack_by_order(w, book(w), plate_match={})
    assert solo["1"] < solo["2"] < solo["3"], solo


def test_queue_slack_is_the_one_that_inverts():
    """钉住两个口径的差别本身：队列 slack 对 remaining 是**反**的。

    若哪天队列 slack 也变单调了，说明 busy 累计没生效（那是另一个 bug），
    这条会报警。
    """
    orders = [order("1", "Sushi_Fish", 90.0), order("2", "Sushi_Fish", 100.0),
              order("3", "Sushi_Fish", 110.0)]
    w = world(orders=orders, plates=[])
    q = E.slack_by_order(w, book(w), plate_match={})
    assert q["1"] > q["3"], q          # 队首反而最从容


def test_queue_exposes_edf_rank():
    orders = [order("9", "Sushi_Cucumber", 120), order("5", "Sushi_Fish", 40),
              order("7", "Sushi_Cucumber", 80)]
    w = world(orders=orders, plates=[])
    rows = E.order_queue(w, book(w), plate_match={})
    assert [(r.order_id, r.rank) for r in rows] == [("5", 0), ("7", 1), ("9", 2)]


def test_queue_degrades_without_orders():
    assert E.order_queue(world(orders=[]), book(world(orders=[]))) == []

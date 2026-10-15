# -*- coding: utf-8 -*-
"""洗盘不能被新权重挤掉（第 1~4 组叠加后的安全网）。

整局重放发现 WashPlate 的选中次数从 15 降到 0。逐帧核对后确认**不是** bug：
  - 本局干净空盘数分布是 1 盘 31 拍、2 盘 155、3 盘 12、4 盘 23，从未见底；
  - clean==2 时 WashPlate.feasible 本就为假（PLATE_BUFFER=2），占了大多数；
  - 仅 1840~1920 这 5 个采样点 clean==1，WashPlate 得 4.00 分、以 0.44 分之差
    输给 FetchToCooker（4.44）——此时手里还有一张盘，不至于卡死。

但「一张盘都没有」是会把拼装彻底卡死的局面，必须保证那时洗盘压得过一切备菜。
本模块钉住这条下限，防止后续继续加权时把它挤掉。
"""
from ai import planner, tasks as T
from ai.blackboard import Blackboard
from ai.recipes import RecipeBook
from tests.test_fill_cooker_priority import STATIONS, _cooker, _world


def _score(w, tasks):
    bb = Blackboard()
    book = RecipeBook(w.recipes, w.stations)
    cands = [planner.Candidate(t, [], 15) for t in tasks]
    # plate_match 要真算一遍（不能省成 None）：摆盘压力项按「有没有盘就位」
    # 判，而 None 在本模块的约定里是「没有匹配信息」，省掉它这一项永不触发。
    plate_match = planner._match_plates_to_orders(w, bb, book)
    return {t.TYPE: s
            for t, s, _ in planner._score_tasks(tasks, cands, w, bb, book,
                                                plate_match)}


def test_wash_outranks_everything_when_no_clean_plate():
    """0 干净盘 = 拼装卡死，洗盘必须优先于备菜与填灶。

    **断言的是「层」而不是分数（Ruling 42）**：分层后这件事由 L4 解卡 > L5 异步
    产能 / L6 备菜的**控制流**保证——`FillCooker` 在 L5，压根不进 L4 的打分。
    原先这条测试走 `_score_tasks` 直接比扁平分，而 `W_PLATE_PRESSURE` 从 3.5 降到
    2.5（修 L4 层内序，见该权重的注释）后，扁平分上 `FillCooker` 4.10 反超
    `WashPlate` 3.50 —— 但那在生产上无意义，因为两者不同层、从不同场竞争。

    **若分层被移除**，必须把本断言恢复成按分数比较，并重标 `W_PLATE_PRESSURE`
    使 0 盘档压过 `W_COOKER_IDLE`。
    """
    from ai import planner_layers as PL

    w = _world([_cooker("c1"), _cooker("c2")], plates=[])
    wash = PL.layer_of_task(T.WashPlate(resources=("sink",)), w)
    fill = PL.layer_of_task(
        T.FillCooker(None, ("c1", ("SushiRice",)), "BoiledSushiRice"), w)
    prep = PL.layer_of_task(T.FetchToPlate("1", ("p1",), "Seaweed"), w)
    assert wash < fill, "洗盘(L%d)必须高于填灶(L%d)" % (wash, fill)
    assert wash < prep, "洗盘(L%d)必须高于备菜(L%d)" % (wash, prep)


def test_place_outranks_prep_when_no_plate_is_placed():
    """场上一块盘都没就位 = 拼装未开始，摆盘必须压过备菜与填灶。

    这是「0 干净盘要洗盘」的对偶局面：盘有、但不在拼装格上，同样拼装不了。
    实机 trace-20261014-221306：晾干架躺着唯一那块干净盘，`PlaceCleanPlate`
    可行 1101 拍、选中 0 次——每拍都被 FillCooker(4.8) / WashPlate(4.0) /
    FetchToBoard 压住，于是后半局 807 拍（24%）plate_order 为空：锅在煮、
    菜在切，而没有一只盘接得住，最后 5 单挂在收盘。

    量级定在「压过备菜与填灶、但不压过快糊的锅」：灶空转丢的是 12 秒时钟、
    没盘丢的是整条装配链，而锅会真的烧糊——见 test_place_yields_to_burning_pot。
    """
    w = _world([_cooker("c1"), _cooker("c2")], plates=[])
    by = _score(w, [T.PlaceCleanPlate("1", ("p1", (0, 0))),
                    T.FetchToPlate("1", ("p1",), "Seaweed"),
                    T.FetchToBoard("1", ("board",), "Cucumber"),
                    T.FillCooker(None, ("c1", ("SushiRice",)),
                                 "BoiledSushiRice")])
    assert by["PlaceCleanPlate"] == max(by.values()), by


def test_place_yields_to_burning_pot():
    """摆盘不能压过快糊的锅：盘在那儿等着，锅会真的烧糊。

    这是摆盘压力的上限卡口。W_NO_PLATE_PLACED 取 5.0（与 W_PLATE_PRESSURE
    对称）时，摆盘吃满 8.5 分、连 `cooking_progress`=23/24 的 RescueCooked
    （8.08）都压得过去——本局 662 帧重放里救锅从 14 拍掉到 0 拍。3.0 保住了
    这条边界。

    实机 `RescueCooked` 由 housekeeping 发射、**不带 order_id**（救锅是救锅，
    不绑单），所以它拿不到 urgency——这里照同样的形态构造，否则给它一个
    order_id 会凭空补上 3~5 分 urgency，把这条边界测松。

    订单取 `remaining` 极小的那张（实机 t=7300 的 order#5 只剩 0.4s）：摆盘
    是订单相关 task，urgency 会吃满 5.0，这才是它能顶到 8.5 分的来源。用
    remaining=60 的从容单只给 3.7 分，测不出上限。
    """
    burning = _cooker("c1", cooked=True, cooking=True, contents=("SushiRice",))
    burning["cooking_progress"] = 23.0     # 快糊（2*cooking_time=24）
    burning["cooking_time"] = 12
    w = _world([burning], plates=[],
               orders=[{"id": "1", "dish": "Sushi_Cucumber", "remaining": 0.4,
                        "lifetime": 130,
                        "composition": ["Seaweed", "BoiledSushiRice",
                                        "Cucumber"]}])
    by = _score(w, [T.PlaceCleanPlate("1", ("p1", (0, 0))),
                    T.RescueCooked(None, ("c1",))])
    assert by["RescueCooked"] > by["PlaceCleanPlate"], by


def test_no_plate_weight_stays_under_burning_pot_ceiling():
    """量级上限：摆盘的合计不能够到「快糊的锅」那一档。

    为什么要单列这条数值断言：上面那条行为用例在**合成帧**上只能把摆盘推到
    5.75（单子少 → combo 项小），实机 t=7300 是 8.5——合成帧测不出 5.0 越界，
    W 调到 5.0 它照样通过。这里直接按权重算上限，才是真正会先炸的那道闸。

    摆盘能拿到的上限 = urgency 满分 + combo 队首折扣 + 摆盘压力；
    快糊的锅 = W_PRESSURE * 2.0。前者必须严格小于后者。
    """
    place_ceiling = (planner.W_URGENCY
                     + planner.W_COMBO_HEAD * planner.COMBO_PREP_DISCOUNT
                     + planner.W_NO_PLATE_PLACED)
    burning_pot = planner.W_PRESSURE * 2.0
    assert place_ceiling < burning_pot, \
        "摆盘上限 %.2f 已够到快糊的锅 %.2f，锅会烧糊（实测 W=5.0 时救锅从 " \
        "14 拍掉到 0 拍）" % (place_ceiling, burning_pot)


def test_wash_pressure_weight_covers_prep_ceiling():
    """量级下限：0 盘时的 plate_pressure 必须高于备菜**实测**的最大合计。

    **口径已修正（Ruling 23）**：原先把五项同时吃满相加当上限
    （urgency 5.0 + async 2.0 + reuse 0.5 + completion 1.0 + combo 2.0 = 10.5），
    而那个组合内部自相矛盾、实机不可能出现：
      - urgency 满分需要 slack <= 0（订单必死），而必死单会被
        _is_doomed_investment 从候选里否决，压根不参与打分；
      - completion_ratio 需要盘里已有料，而本条讨论的正是「0 干净盘」；
      - async_resource 只给 FetchToCooker/Chop，与 completion_ratio 适用的
        task 也不重合。

    实测备菜合计是 4.97（urgency 3.72 + ingredient_reuse 1.0 + combo_head 0.25，
    灶全空 3 单要米，见 ai/planner.py 的 W_COOKER_IDLE 注释）。这与 A 组
    Ruling 13 修掉的是同一类错误——拿理论上限冒充实测值。

    测法：用 _score_candidate 实算，不用权重常量相加。
    """
    w = _world([_cooker("c1"), _cooker("c2")], plates=[])
    by = _score(w, [T.FetchToPlate("1", ("p1",), "Seaweed"),
                    T.FetchToBoard("1", ("board",), "Cucumber"),
                    T.WashPlate(resources=("sink",))])
    prep_actual = max(by["FetchToPlate"], by["FetchToBoard"])
    assert by["WashPlate"] > prep_actual, \
        "0 盘时洗盘分 %.2f 没压过备菜实测合计 %.2f，拼装会被卡死" % (
            by["WashPlate"], prep_actual)

# -*- coding: utf-8 -*-
"""权重约束表：序关系的可执行规格。

**为什么要有这张表**：`ai/planner.py` 的 17 项权重，每条注释写的都是序关系
约束而非数值依据——「必须压过备菜的实际合计，但不能压过端刚熟的锅」。约束是
真正的规格，那组浮点数只是它的一个手工解。

而 n 项加权和有 O(n²) 对交互，单组测试结构上查不出其中任何一对。AGENTS.md
§10.2 记的那次就是解失效：urgency 改读 slack 后，另一组验证过的七个「灶空转
该填灶」的拍全部回退，当时 982 个测试一个都没报警。

**这张表是分层改造的安全网**：分层后它应当原样通过，否则说明分层改变了既有
序关系（spec §11）。

表的形态是 (局面, 必须赢的 task, 必须输的 task 们, 为什么)，一条一行。加新
权重时在这里加一行，比在 17 条注释里描述关系可靠。
"""
from ai.core import planner
from ai import tasks as T
from ai.state.blackboard import Blackboard
from ai.query.recipes import RecipeBook
from tests.test_fill_cooker_priority import STATIONS, _cooker, _world


def _score(w, tasks, orders=None):
    # type: (object, list, object) -> dict
    """给一组 task 打分，返回 {TYPE: score}。

    必须真算一遍 plate_match（不能省成 None）：摆盘压力项按「有没有盘就位」
    判，而 None 在 planner 的约定里是「没有匹配信息」——省掉它那一项永不触发
    （见 `_order_has_plate` 的注释）。
    """
    bb = Blackboard()
    book = RecipeBook(w.recipes, w.stations)
    cands = [planner.Candidate(t, [], 15) for t in tasks]
    plate_match = planner._match_plates_to_orders(w, bb, book)
    scored = planner._score_tasks(tasks, cands, w, bb, book, plate_match)
    return {t.TYPE: s for t, s, _ in scored}


# ---------- 局面构造器 ----------

def _world_hot_pot(progress=23.0):
    # type: (float) -> object
    """一口灶上的锅，progress 可调。12 = 刚熟、18 = 3/4、24 = 快糊。"""
    c = _cooker("c1", cooked=(progress >= 12.0), cooking=True,
                contents=["sushirice"])
    c["cooking_time"] = 12.0
    c["cooking_progress"] = progress
    return _world([c, _cooker("c2")], plates=[_PLATE_EMPTY])


_PLATE_EMPTY = {"n": "p1", "name": "plate", "x": 3.0, "z": 3.0,
                "contents": [], "detail": []}


def test_hot_pot_outranks_prep():
    """锅快糊了 → 端锅/救锅压过一切备菜。

    这是最硬的一条：锅会毁、料会没、还要灭火；而备菜的产出只是晚一点。
    """
    w = _world_hot_pot(progress=23.0)
    by = _score(w, [
        T.RescueCooked(resources=("c1",)),
        T.FetchToPlate("1", ("p1", (3, 3)), "Seaweed"),
        T.FetchToBoard("1", ("board",), "Cucumber"),
    ])
    assert by["RescueCooked"] == max(by.values()), by


def test_fill_cooker_outranks_prep():
    """灶空转 → 填灶压过备菜（AGENTS.md §10.2 的那次回退）。

    灶空转的机会成本是 12 秒不可压缩的时钟，补不回来。实测
    trace-20261014-120406 的 1700~3270 窗口三口灶 59% 空转，而同期 planner
    一直在摆海苔、切鱼，order#3/4/5 三单全在这窗口过期。
    """
    w = _world([_cooker("c1"), _cooker("c2")], plates=[_PLATE_EMPTY])
    by = _score(w, [
        T.FillCooker(None, ("c1", ("SushiRice",)), "BoiledSushiRice"),
        T.FetchToPlate("1", ("p1", (3, 3)), "Seaweed"),
        T.FetchToBoard("1", ("board",), "Cucumber"),
    ])
    assert by["FillCooker"] == max(by.values()), by


def test_deliver_outranks_prep():
    """料齐的盘 → 送餐压过继续备菜。60 分 base 立即到账。"""
    plate_full = {"n": "p1", "name": "plate", "x": 3.0, "z": 3.0,
                  "contents": ["Seaweed", "BoiledSushiRice", "Cucumber"],
                  "detail": ["ing:Seaweed", "cooked:SushiRice:Pot",
                             "ing:ChoppedCucumber"]}
    w = _world([_cooker("c1")], plates=[plate_full])
    by = _score(w, [
        T.DeliverPlate("1", ("p1",)),
        T.FetchToPlate("2", ("p1", (3, 3)), "Seaweed"),
        T.FetchToBoard("2", ("board",), "Cucumber"),
    ])
    assert by["DeliverPlate"] == max(by.values()), by


def test_hot_pot_outranks_deliver():
    """锅快糊 vs 料齐的盘 → 先救锅。

    依据：锅会烧糊（料没了 + 要灭火），盘不会。注释里写的是
    「W_DELIVER_BONUS 要低于快糊锅的 pressure（W_PRESSURE*2.0=10.0）」。
    """
    plate_full = {"n": "p1", "name": "plate", "x": 3.0, "z": 3.0,
                  "contents": ["Seaweed", "BoiledSushiRice", "Cucumber"],
                  "detail": ["ing:Seaweed", "cooked:SushiRice:Pot",
                             "ing:ChoppedCucumber"]}
    c = _cooker("c1", cooked=True, cooking=True, contents=["sushirice"])
    c["cooking_time"] = 12.0
    c["cooking_progress"] = 23.0
    w = _world([c], plates=[plate_full])
    by = _score(w, [
        T.RescueCooked(resources=("c1",)),
        T.DeliverPlate("1", ("p1",)),
    ])
    assert by["RescueCooked"] > by["DeliverPlate"], by


def test_hot_pot_outranks_fill_cooker():
    """端刚熟的锅 > 填另一口空灶。

    `W_COOKER_IDLE` 的注释：「不能压过端刚熟的锅（W_PRESSURE*1.0=5.0，锅会
    烧糊、盘不会）」。5.6 > 5.0——这条断言钉住它当前是否仍成立。
    """
    c = _cooker("c1", cooked=True, cooking=True, contents=["sushirice"])
    c["cooking_time"] = 12.0
    c["cooking_progress"] = 12.0        # 刚熟
    w = _world([c, _cooker("c2")], plates=[_PLATE_EMPTY])
    by = _score(w, [
        T.TakeCookedToPlate("1", ("c1", "p1"), "BoiledSushiRice"),
        T.FillCooker(None, ("c2", ("SushiRice",)), "BoiledSushiRice"),
    ])
    assert by["TakeCookedToPlate"] > by["FillCooker"], by


def test_wash_outranks_all_when_no_clean_plate():
    """0 干净盘 = 拼装链被卡死，洗盘必须优先于备菜与填灶。

    与 tests/test_plate_supply_guard.py 的那条重复是刻意的：那个文件是针对
    一次具体回退的安全网，这里是序关系表的一行。两处同时红比只有一处红更
    容易定位。

    **断言的是「层」而不是分数（Ruling 42）**：分层后这件事由 L4 解卡 > L5 异步
    产能 / L6 备菜的**控制流**保证——`FillCooker` 在 L5，压根不进 L4 的打分。
    原先这条测试走 `_score_tasks` 直接比扁平分，而 `W_PLATE_PRESSURE` 从 3.5 降到
    2.5（修 L4 层内序，见该权重的注释）后，扁平分上 `FillCooker` 4.10 反超
    `WashPlate` 3.50 —— 但那在生产上无意义，因为两者不同层、从不同场竞争。

    **若分层被移除**，必须把本断言恢复成按分数比较，并重标 `W_PLATE_PRESSURE`
    使 0 盘档压过 `W_COOKER_IDLE`。
    """
    from ai.core import planner_layers as PL

    w = _world([_cooker("c1"), _cooker("c2")], plates=[])
    wash = PL.layer_of_task(T.WashPlate(resources=("sink",)), w)
    fill = PL.layer_of_task(
        T.FillCooker(None, ("c1", ("SushiRice",)), "BoiledSushiRice"), w)
    prep = PL.layer_of_task(
        T.FetchToPlate("1", ("p1", (3, 3)), "Seaweed"), w)
    assert wash < fill, "洗盘(L%d)必须高于填灶(L%d)" % (wash, fill)
    assert wash < prep, "洗盘(L%d)必须高于备菜(L%d)" % (wash, prep)


def test_hot_pot_outranks_wash():
    """快糊的锅 > 洗盘。

    `W_NO_PLATE_PLACED` 的定标过程记着：W=5.0 时摆盘吃满 8.5 分，连
    progress=23/24 的 RescueCooked（8.08）都压得过去——那是用「盘没摆」换
    「锅烧糊」，不划算。
    """
    c = _cooker("c1", cooked=True, cooking=True, contents=["sushirice"])
    c["cooking_time"] = 12.0
    c["cooking_progress"] = 23.0
    w = _world([c], plates=[])          # 0 干净盘 + 快糊的锅
    by = _score(w, [
        T.RescueCooked(resources=("c1",)),
        T.WashPlate(resources=("sink",)),
    ])
    assert by["RescueCooked"] > by["WashPlate"], by


def test_table_compares_scores_not_constants():
    """这张表的每一行都必须**实算**，不能只比权重常量。

    `tests/test_scoring_cooker_idle.py` 比的是常量
    （`W_COOKER_IDLE > W_ASYNC_RESOURCE + W_COMPLETION_RATIO`），那种断言查不出
    「权重够大但某一项没乘上去」这类 bug——cf2fc64 的手写加法、
    W_DEADLINE_LOOKAHEAD 从未被引用，都是这个形状。

    **两种合法判据（Ruling 43）**：分层之后，这张表里的行分成两类——
      - 调 `_score()`：序关系由**层内**打分决定（同层候选比分数）；
      - 调 `layer_of_task()`：序关系由**层序**决定（跨层候选根本不同场打分，
        比分数没有意义，见 test_wash_outranks_all_when_no_clean_plate）。
    两者都是「实算」，都不是比常量。所以本测试要求每行至少调用其中之一。

    **不按函数名豁免**：豁免名单会随时间变成垃圾场，而且会掩盖「这一行用的是
    另一种判据」这个有信息量的事实。按判据分类则强制每一行声明自己属于哪类。

    **用 AST 判而不是源码子串判**：原先写
    `assert "_score(" in inspect.getsource(fn)`，评审往表里加了个只比常量的
    假测试、但在**注释里**提了一句 `_score(` 字样，就骗过去了。

    **这条元测试的限度**：它抓「忘了实算」这种手滑，抓不到「调了 `_score` 但
    断言的是别的东西」。那需要追踪断言表达式的数据来源，成本远超收益。
    它是防手滑的闸，不是防刻意绕过的闸——别把它当铁证。
    """
    import ast
    import inspect

    # 两种合法判据：层内打分 / 跨层层序
    VERDICT_FNS = ("_score", "layer_of_task")

    module = inspect.getmodule(test_table_compares_scores_not_constants)
    tree = ast.parse(inspect.getsource(module))
    checked = 0
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        if node.name == "test_table_compares_scores_not_constants":
            continue
        called = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                if isinstance(sub.func, ast.Name):
                    called.add(sub.func.id)
                elif isinstance(sub.func, ast.Attribute):
                    called.add(sub.func.attr)      # PL.layer_of_task(...)
        assert called & set(VERDICT_FNS), (
            "%s 必须实算：调 _score（层内打分）或 layer_of_task（层序），"
            "不能只比权重常量" % node.name)
        checked += 1
    # 表是空的也算通过就没意义了
    assert checked >= 7, "只检查到 %d 个断言，表的规模不对" % checked

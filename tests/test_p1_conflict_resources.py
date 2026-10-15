# -*- coding: utf-8 -*-
"""P1 冲突的资源提取（spec §10）。

_extract_task_resources 原先手写遍历 board_id/cooker_id/plate_id/station_id/
counter_id 五个属性名，而 Task 基类已有 claims() 给出权威的
(resource_id, action)。后果：拼装格坐标（「坐标即身份」，spec §4.4）、
FillCooker 的 (cid, raws) 等资源看不见 → p1_conflict 对这些 task 恒为 0。
"""
from ai import planner
from ai import tasks as T


def test_extracts_resources_from_claims():
    """按 claims() 提取，不靠属性名猜。"""
    task = T.Chop("order1", ("board1",), "Cucumber")
    res = planner._extract_task_resources(task)
    assert "board1" in res


def test_extracts_assembly_cell_coordinates():
    """拼装格的身份是网格坐标而非实例名（spec §4.4「坐标即身份」）。
    手写属性名的老实现看不见它。"""
    task = T.PlaceCleanPlate("order1", ("p1", (3, 4)))
    res = planner._extract_task_resources(task)
    claimed = [r for r, _a in task.claims()]
    for r in claimed:
        assert r in res, "claims() 给出的资源 %r 必须能被提取到" % (r,)


def test_extracts_fill_cooker_resources():
    """FillCooker 的 resources 是 (cooker_id, raws 元组)——老实现只取
    元组第一个元素且要求它是 str，碰到嵌套元组就漏。"""
    task = T.FillCooker(None, ("cooker1", ("SushiRice",)), "BoiledSushiRice")
    res = planner._extract_task_resources(task)
    claimed = [r for r, _a in task.claims()]
    for r in claimed:
        assert r in res


def test_covers_every_task_types_claims():
    """全部 19 个 task 类型：claims() 给出的资源都要能提取到。

    这条是防回归的真正目标——以后加 task 不必再去那张属性名表里登记。
    """
    samples = [
        T.Chop("o1", ("board1",), "Cucumber"),
        T.FetchToBoard("o1", ("board1",), "Cucumber"),
        T.FetchToCooker("o1", ("cooker1", ("SushiRice",)), "BoiledSushiRice"),
        T.FillCooker(None, ("cooker1", ("SushiRice",)), "BoiledSushiRice"),
        T.TakeCookedToPlate("o1", ("cooker1", "plate1"), "BoiledSushiRice"),
        T.ReturnPot(resources=("cooker1",)),
        T.RescueCooked(resources=("cooker1",)),
        T.Discard(resources=("cooker1",)),
        T.WashPlate(resources=("sink1",)),
        T.PlaceCleanPlate("o1", ("p1", (3, 4))),
        T.FetchToPlate("o1", ("plate1", (3, 4)), "Seaweed"),
        T.DeliverPlate("o1", ("plate1",)),
        T.StashHeld(),
    ]
    for task in samples:
        res = planner._extract_task_resources(task)
        for r, _a in task.claims():
            assert r in res, "%s 的资源 %r 没被提取到" % (task.TYPE, r)


def test_does_not_lose_resources_that_claims_omits():
    """claims() 是动作级粒度，p1_conflict 要的是物理资源——不能因为
    claims() 返回空就让冲突检测失效。

    评审发现的回归：FetchToPlate.claims() 返回 ()，于是它的资源列表变成
    空集，p1_conflict 对它恒为 0（原先那版手写属性名表反而能检测到盘）。
    """
    task = T.FetchToPlate("o1", ("plate1", (3, 4)), "Seaweed")
    res = planner._extract_task_resources(task)
    assert "plate1" in res, res
    assert (3, 4) in res, res


def test_take_cooked_keeps_both_cooker_and_plate():
    """claims() 只给 cooker（动作级），但 P1 端走那只盘也是冲突。"""
    task = T.TakeCookedToPlate("o1", ("c1", "plate1"), "BoiledSushiRice")
    res = planner._extract_task_resources(task)
    assert "c1" in res and "plate1" in res, res


def test_coordinate_with_float_stays_intact():
    """坐标的判据是**结构**（长度 2 的数值对），不是「元素全为 int」。

    按类型猜的旧判据会把 (3, 4.0) 摊平成两个裸标量 3 和 4.0，于是
    _task_conflicts_with_p1 的 `in` 比较永不匹配。geometry.to_grid 当前返回
    int 所以不触发，但没有任何东西强制这一点（评审查出的埋雷）。
    """
    class _T(object):
        TYPE = "X"
        _resources = ((3, 4.0),)

        def claims(self):
            return ()

    assert planner._extract_task_resources(_T()) == [(3, 4.0)]


def test_nested_tuple_still_flattens():
    """FillCooker 的 (cooker_id, raws) 仍要摊平——它不是坐标。"""
    task = T.FillCooker(None, ("cooker1", ("SushiRice",)), "BoiledSushiRice")
    res = planner._extract_task_resources(task)
    assert "cooker1" in res
    assert ("cooker1", ("SushiRice",)) not in res


def test_bool_pair_is_not_a_coordinate():
    """isinstance(True, int) 为真，但 (True, False) 不是坐标。"""
    class _T(object):
        TYPE = "X"
        _resources = ((True, False),)

        def claims(self):
            return ()

    res = planner._extract_task_resources(_T())
    assert (True, False) not in res

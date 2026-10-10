import pytest

from ai import navigation as N
from ai.navigation import (
    BELT_REVERSE_COST,
    distance_field,
    interact_cells,
    interact_cells_with_belts,
    nearest_cost,
    step_cost,
)


def open_3x3():
    """0..2 x 0..2 全可走。"""
    return lambda gx, gz: 0 <= gx <= 2 and 0 <= gz <= 2


def test_step_cost_is_one_without_belt():
    assert step_cost({}, (0, 0), (1, 0)) == 1


def test_step_cost_penalizes_against_belt_direction():
    belt = {(0, 0): (1, 0)}  # 该格把人向 +x 推

    assert step_cost(belt, (0, 0), (-1, 0)) == BELT_REVERSE_COST
    assert step_cost(belt, (0, 0), (1, 0)) == 1
    assert step_cost(belt, (0, 0), (0, 1)) == 1


def test_distance_field_expands_uniformly():
    field = distance_field((0, 0), open_3x3(), {})

    assert field[(0, 0)] == 0
    assert field[(1, 0)] == 1
    assert field[(1, 1)] == 2
    assert field[(2, 2)] == 4


def test_distance_field_excludes_unwalkable():
    walkable = lambda gx, gz: 0 <= gx <= 2 and 0 <= gz <= 2 and not (gx == 1 and gz == 0)
    field = distance_field((0, 0), walkable, {})

    assert (1, 0) not in field
    # 仍可绕行到 (2,0)
    assert field[(2, 0)] == 4


def test_distance_field_respects_blocked_cells():
    field = distance_field((0, 0), open_3x3(), {}, blocked={(1, 0), (1, 1)})

    assert (1, 0) not in field
    assert (1, 1) not in field
    assert field[(0, 2)] == 2


def test_distance_field_prefers_cheap_path_around_belt():
    """逆带代价 8，绕路 1+1+1=3 更便宜，距离场应取 3。"""
    walkable = lambda gx, gz: 0 <= gx <= 1 and 0 <= gz <= 1
    belt = {(1, 0): (1, 0)}  # 站在 (1,0) 会被推向 +x，往 (0,0) 走是逆向

    field = distance_field((1, 0), walkable, belt)

    assert field[(0, 0)] == 3  # (1,0)->(1,1)->(0,1)->(0,0)


def test_distance_field_start_unwalkable_still_includes_start():
    """厨师可能正站在被判为不可走的格上（站点占用兜底误判），不能直接返回空。"""
    field = distance_field((5, 5), lambda gx, gz: False, {})

    assert field == {(5, 5): 0}


def test_interact_cells_returns_walkable_neighbors():
    cells = interact_cells(1, 1, open_3x3())

    assert set(cells) == {(0, 1), (2, 1), (1, 0), (1, 2)}


def test_interact_cells_excludes_belt_cells_by_default():
    cells = interact_cells(1, 1, open_3x3(), belt_cells={(0, 1), (2, 1)})

    assert set(cells) == {(1, 0), (1, 2)}


def test_interact_cells_falls_back_to_belt_when_no_alternative():
    """相邻格全是传送带时不能返回空，否则站点永远不可达。"""
    walkable = lambda gx, gz: (gx, gz) in {(0, 1), (2, 1)}
    cells = interact_cells(1, 1, walkable, belt_cells={(0, 1), (2, 1)})

    assert set(cells) == {(0, 1), (2, 1)}


def test_interact_cells_with_belts_includes_belt_cells():
    """兜底函数把传送带格也放回候选（可达性由调用方用距离场判）。"""
    cells = interact_cells_with_belts(
        1, 1, open_3x3(), belt_cells={(0, 1), (2, 1)})

    assert set(cells) == {(0, 1), (1, 0), (2, 1), (1, 2)}


def test_nearest_cost_picks_minimum():
    field = {(0, 0): 0, (1, 0): 1, (2, 0): 5}

    assert nearest_cost(field, [(2, 0), (1, 0)]) == ((1, 0), 1)


def test_nearest_cost_returns_none_when_unreachable():
    assert nearest_cost({(0, 0): 0}, [(9, 9)]) is None


def test_trace_back_straight_line():
    """一条直走廊：回溯出的路径应是连续四邻接格。"""
    walk = lambda gx, gz: gz == 0 and 0 <= gx <= 4
    field = distance_field((0, 0), walk, {})
    path = N.trace_back(field, (4, 0), {})
    assert path == [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)]


def test_trace_back_start_itself():
    walk = lambda gx, gz: True
    field = distance_field((0, 0), walk, {}, max_cells=50)
    assert N.trace_back(field, (0, 0), {}) == [(0, 0)]


def test_trace_back_unreachable_returns_none():
    walk = lambda gx, gz: gz == 0 and 0 <= gx <= 2
    field = distance_field((0, 0), walk, {})
    assert N.trace_back(field, (9, 9), {}) is None


def test_trace_back_path_is_contiguous_and_walkable():
    """L 形走廊：每一步必须是四邻接，且每格可走。"""
    def walk(gx, gz):
        return (gz == 0 and 0 <= gx <= 3) or (gx == 3 and 0 <= gz <= 3)
    field = distance_field((0, 0), walk, {})
    path = N.trace_back(field, (3, 3), {})
    assert path is not None
    assert path[0] == (0, 0) and path[-1] == (3, 3)
    for a, b in zip(path, path[1:]):
        assert abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1
        assert walk(b[0], b[1])


def test_trace_back_respects_asymmetric_belt_cost():
    """核心用例：step_cost 按离开的格子计，是非对称的。

    一条 5 格走廊，(2,0) 是往右推的传送带。从 (0,0) 出发往右走是顺带
    （离开 (2,0) 向右，代价 1）；若用"选 field 最小的邻居"做回溯，在
    传送带排上会选出代价账算不平的前驱。这里断言回溯出的路径每一步的
    代价账都对得上 field 的差值。
    """
    walk = lambda gx, gz: gz == 0 and 0 <= gx <= 4
    belt = {(2, 0): (1, 0)}
    field = distance_field((0, 0), walk, belt)
    path = N.trace_back(field, (4, 0), belt)
    assert path is not None
    for a, b in zip(path, path[1:]):
        assert field[a] + N.step_cost(belt, a, b) == field[b], \
            "回溯出的边必须是距离场里真正用过的那条边"


def test_trace_back_rejects_gradient_descent_trap():
    """反向走传送带时，最小邻居不是合法前驱。

    布局：(0,0) 起点，(1,0) 是往**左**推的带。从 (0,0) 走到 (2,0) 必须
    穿过 (1,0)，而离开 (1,0) 往右是逆带（代价 8）。
    field: (0,0)=0, (1,0)=1, (2,0)=9。
    回溯 (2,0) 时邻居只有 (1,0)，但必须验账 1 + 8 == 9 才认它——
    若只按"field 最小"选，遇到多邻居场景会选出 1 + 1 != 9 的非法边。
    """
    walk = lambda gx, gz: gz == 0 and 0 <= gx <= 2
    belt = {(1, 0): (-1, 0)}
    field = distance_field((0, 0), walk, belt)
    assert field[(2, 0)] == 9
    assert N.trace_back(field, (2, 0), belt) == [(0, 0), (1, 0), (2, 0)]


def test_trace_back_includes_p1_soft_cost_in_predecessor_check():
    """人类附近仍可通行，路径回溯必须使用建场时同一份软代价。"""
    walk = lambda gx, gz: gz == 0 and 0 <= gx <= 4
    soft = {(1, 0): 4, (3, 0): 2}
    belt = {(2, 0): (-1, 0)}
    field = distance_field((0, 0), walk, belt, soft_cost=soft)
    path = N.trace_back(field, (4, 0), belt, soft)
    assert path == [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0)]
    for a, b in zip(path, path[1:]):
        assert field[a] + N.step_cost(belt, a, b) + soft.get(b, 0) == field[b]



def test_first_step():
    walk = lambda gx, gz: gz == 0 and 0 <= gx <= 4
    field = distance_field((0, 0), walk, {})
    assert N.first_step(field, (4, 0), {}) == (1, 0)


def test_first_step_at_goal_returns_none():
    """已经站在目标格上：没有"下一步"，返回 None 而不是自己。"""
    walk = lambda gx, gz: True
    field = distance_field((0, 0), walk, {}, max_cells=50)
    assert N.first_step(field, (0, 0), {}) is None


def test_first_step_unreachable_returns_none():
    walk = lambda gx, gz: gz == 0 and 0 <= gx <= 2
    field = distance_field((0, 0), walk, {})
    assert N.first_step(field, (9, 9), {}) is None


def test_trace_back_on_real_frame_all_reachable_cells(level_frame):
    """真实帧全覆盖：每个可达格都能回溯出一条合法路径。

    spec 记梯度下降在真实帧 60 个可达格里有 2 个选出非法前驱
    （均在传送带排）。这个测试就是那条断言的反面——正确谓词应 0 失败。
    """
    from ai.world import World
    w = World(level_frame)
    chef = None
    for c in w.chefs:
        if c.get("x") is not None:
            chef = c
            break
    if chef is None:
        pytest.skip("样本里没有带世界坐标的厨师")
    start = w.to_grid(chef["x"], chef["z"])
    field = distance_field(start, w.walkable, w.belt_dir)
    assert len(field) > 10, "距离场太小，这个测试没在真实数据上跑"

    for cell in field:
        path = N.trace_back(field, cell, w.belt_dir)
        assert path is not None, "可达格却回溯不出路径：%r" % (cell,)
        assert path[0] == start and path[-1] == cell
        for a, b in zip(path, path[1:]):
            assert abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1
            assert w.walkable(b[0], b[1])
            assert field[a] + N.step_cost(w.belt_dir, a, b) == field[b]

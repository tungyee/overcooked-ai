# -*- coding: utf-8 -*-
"""走位诊断日志：过街标记与目标身份格式。"""
from ai.primitives import move as M


class _World(object):
    def __init__(self, level):
        self.level = level


def test_nav_crossing_marks_4_1_road_cells():
    path = [(-5, 1), (-4, 1), (-3, 1), (0, 1), (6, 1), (7, 1)]
    text = M._nav_crossing(_World("Sushi_4_1_P2"), path)
    assert "cross_road=" in text
    assert "(-3, 1)" in text and "(6, 1)" in text


def test_nav_crossing_empty_for_other_levels():
    assert M._nav_crossing(_World("Sushi_1_4_P2"), [(0, 1)]) == ""


def test_nav_crossing_empty_when_path_does_not_enter_road():
    assert M._nav_crossing(_World("Sushi_4_1_P2"), [(-8, 1), (-7, 1)]) == ""


def test_nav_target_includes_id_and_world_position():
    text = M._nav_target({"grid": "GridManager", "n": "-19140",
                          "x": 15.2, "z": 8.4})
    assert "GridManager:-19140" in text
    assert "15.2" in text and "8.4" in text

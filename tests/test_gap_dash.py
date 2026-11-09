# -*- coding: utf-8 -*-
"""跨缝路径的 move 命令必须带 dash 标记。"""
from ai.state.blackboard import Blackboard


class FakeWorld(object):
    def __init__(self):
        self.portal_map = None
        self.seam_gates = None
        self.lane_map = None
        self.belt_dir = {}
        self.terrain = {}

    def walkable(self, gx, gz):
        return True

    def to_world(self, gx, gz):
        return (gx * 1.2, gz * 1.2)


def _move_cmd(monkeypatch, has_gap):
    from ai.primitives import move as M
    monkeypatch.setattr(M.gaps, "path_has_gap", lambda w, path: has_gap)
    return M._move_cmd(FakeWorld(), [(0, 0), (1, 0)], None, Blackboard(),
                       [1.2, 0.0], None, None, 8000)


def test_move_cmd_sets_dash_when_path_has_gap(monkeypatch):
    assert _move_cmd(monkeypatch, True)["dash"] is True


def test_move_cmd_leaves_dash_false_without_gap(monkeypatch):
    assert _move_cmd(monkeypatch, False)["dash"] is False

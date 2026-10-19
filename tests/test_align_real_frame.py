# -*- coding: utf-8 -*-
"""句柄抢占的真实帧回归（trace-20261016-220409 t=9523）。

这一帧是本设计的唯一实机依据：P2 在格 (1,-4) 偏离格心 0.30/0.17，
placement 句柄被 1.4% 之差的出餐口抢走，于是 EnsureHandsFree 直接
FAILURE、树塌、重建、再塌，287 拍钉死到收盘。
"""
import json
import os

from ai.query import predicates as P
from ai import primitives as PR
from ai.state.blackboard import Blackboard
from ai.output.hands import Hands
from ai.space.navigation import distance_field
from ai.query.recipes import RecipeBook
from ai.state.world import World

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "align_stuck_20261016.json")

# 句柄「假对」的实机帧：偏离格心但 placement 句柄仍指向正确柜台
# （trace-20261018-134403 t=2400，卡锅 75 秒）。
FIXTURE_OFF_CENTER = os.path.join(
    os.path.dirname(__file__), "fixtures",
    "align_off_center_handle_ok_20261018.json")


class _Ctrl(object):
    def __init__(self):
        self.calls = []

    def tap(self, name, hold=0.12):
        self.calls.append(("tap", name))

    def press(self, name):
        self.calls.append(("press", name))

    def release(self, name):
        self.calls.append(("release", name))

    def release_all(self):
        pass


def _ctx(fixture=FIXTURE):
    with open(fixture) as fh:
        state = json.load(fh)
    w = World(state)
    chef = w.chef("Player 2")
    ctrl = _Ctrl()
    return {
        "w": w, "chef": chef, "hands": Hands(ctrl), "bb": Blackboard(),
        "field": distance_field(P.chef_cell(chef, w.to_grid), w.walkable,
                                w.belt_dir),
        "book": RecipeBook(w.recipes, w.stations), "tick": 0,
    }, ctrl


def _counter(w):
    return [s for s in w.stations
            if s.get("n") == "countertop_01_standard_wood (36)"][0]


def _counter20(w):
    return [s for s in w.stations
            if s.get("n") == "countertop_01_standard_wood (20)"][0]


def test_real_frame_handle_is_stolen_by_delivery_hatch():
    """钉住根因：两个字段指向不同目标，这是设计的出发点。"""
    ctx, _ = _ctx()
    w, chef = ctx["w"], ctx["chef"]
    counter = _counter(w)
    assert P.interact_ready(chef, counter, None, w.to_grid) is True, \
        "通用 gx/gz 指着台面（朝向对）"
    assert P.interact_ready(chef, counter, "placement", w.to_grid) is False, \
        "placement 句柄被出餐口抢走"


def test_real_frame_align_is_off_center_enough_to_act():
    """这一帧确实偏离格心超过容差——否则 AlignToCell 无从收紧。"""
    ctx, _ = _ctx()
    w, chef = ctx["w"], ctx["chef"]
    cur = P.chef_cell(chef, w.to_grid)
    assert cur == (1, -4)
    off = P.cell_center_offset(chef, cur, w.to_world)
    assert off > P.align_epsilon(w.mapping.cell), \
        "偏离 %.3f 应超过容差 %.3f" % (off, P.align_epsilon(w.mapping.cell))


def test_real_frame_ensure_hands_free_no_longer_fails_immediately():
    """修复后：第一拍不再 FAILURE，而是收紧站位。"""
    ctx, ctrl = _ctx()
    node = PR.EnsureHandsFree()
    got = [node.tick(ctx) for _ in range(4)]
    assert got[0] != "failure", "树首不该在第一拍就塌（这是 287 拍卡死的入口）"
    assert ("tap", "pickup") not in ctrl.calls, "句柄不对时不许瞎按"


def test_real_frame_trace_records_align_phase():
    """trace 要能看见收紧动作——否则 spec §6 的介入频次无从标定。

    用独立的 `aligning` 键而不是 `phase`：phase 归 MotionControl 所有
    （observe/pulse/cruise），覆盖它会搞坏 AGENTS.md §10.1 的 observe 占比
    归因。两个量要同时可见。
    """
    from ai.output import trace
    ctx, _ = _ctx()
    ctx["motion"] = {}
    node = PR.EnsureHandsFree()
    for _ in range(4):
        node.tick(ctx)
    rec = trace.execution_record("running", None, ctx)
    assert rec["motion"].get("aligning") is True
    assert rec["motion"].get("phase") in ("observe", "pulse", "cruise"), \
        "MotionControl 的 phase 不该被覆盖"


def test_off_center_with_correct_handle_still_nudges():
    """偏离格心时，即使 placement 句柄指向正确柜台，也要先收紧站位。

    这是 trace-20261018-134403 卡锅 75 秒的根因：placement 句柄（扫描 A）
    指向正确柜台 → interact_ready 为 True → 旧实现直接 SUCCESS、不收紧，
    于是 tap 发出、实际放不下、Confirm 超时 → 整棵树塌 → 原地重建死循环。
    """
    ctx, ctrl = _ctx(fixture=FIXTURE_OFF_CENTER)
    w, chef = ctx["w"], ctx["chef"]
    counter = _counter20(w)

    # 句柄「假对」：placement 句柄确实指向正确柜台
    assert P.interact_ready(chef, counter, "placement", w.to_grid) is True
    # 但偏离格心超过容差
    cur = P.chef_cell(chef, w.to_grid)
    off = P.cell_center_offset(chef, cur, w.to_world)
    assert off > P.align_epsilon(w.mapping.cell)

    # AlignToCell 必须收紧站位（RUNNING），不能直接 SUCCESS 放过
    node = PR.AlignToCell(counter, "placement")
    assert node.tick(ctx) == "running"
    assert ("tap", "pickup") not in ctrl.calls


def test_align_no_progress_with_correct_handle_falls_through():
    """走不动（no_progress）但句柄已指向目标时，放行去 tap 而不是失败死循环。

    trace-20261019-113524 的卡死根因：P2 站角落 [4,-3] 离格心 0.44，短按
    right 走不动 → no_progress FAILURE → 树重建 → 同一个落点再来一遍。句柄
    明明已指向正确柜台，却因为「必须站到格心」的门槛被永远挡在 tap 之外。
    """
    ctx, ctrl = _ctx(fixture=FIXTURE_OFF_CENTER)
    w = ctx["w"]
    counter = _counter20(w)
    assert P.interact_ready(ctx["chef"], counter, "placement", w.to_grid) is True

    node = PR.AlignToCell(counter, "placement")
    statuses = [node.tick(ctx) for _ in range(45)]
    assert "success" in statuses, "句柄对时 no_progress 应放行去 tap"
    assert "failure" not in statuses, "句柄对时不该失败死循环"


def test_align_no_progress_with_wrong_handle_blacklists_pair():
    """走不动且句柄不对时，拉黑 (资源, 格) 再失败，下次换位重试。"""
    ctx, ctrl = _ctx()  # FIXTURE：placement 句柄被出餐口抢走
    w = ctx["w"]
    counter = _counter(w)
    node = PR.AlignToCell(counter, "placement")
    statuses = [node.tick(ctx) for _ in range(45)]
    assert "failure" in statuses, "句柄不对仍应失败"

    key = (P.resource_id(counter), tuple(P.chef_cell(ctx["chef"], w.to_grid)))
    bad_pairs = getattr(ctx["bb"].scratch, "bad_pairs", None) or {}
    assert key in bad_pairs, "走不动且句柄不对要拉黑 (资源,格)，否则下次还是同一格"

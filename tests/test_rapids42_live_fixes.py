# -*- coding: utf-8 -*-
"""4-2 双竹筏实机 trace 的回归钉子（trace-20261111-001945）。

覆盖四类问题：
  - 传送带接缝闪断时 SendAcross 应该等，而不是 failure/放下成品盘；
  - 手持本任务要用的料时，不被 L2 熟锅抢占（避免拿起-放下循环）；
  - 手持成品盘时，需要空手的备菜 task 吃重罚（避免把盘放下）；
  - 不可行阈值按 5Hz 帧计，单帧抖动不切任务；
  - 原地动作不报 stuck。
"""
import gzip
import json
import os

import pytest

from ai import tasks as T
from ai.core import arbiter
from ai.core import planner
from ai.query import predicates as P
from ai.query.recipes import RecipeBook
from ai.space import channels as CH
from ai.state.bb_factory import make_bb
from ai.state.blackboard import Commitment, StuckScratch
from ai.state.world import World
from tests.test_exec_primitives import FakeExec

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                   "rapids42-live-fixes.ndjson.gz")
FIX_OWN = os.path.join(os.path.dirname(__file__), "fixtures", "rapids",
                       "rapids42-own-belt.ndjson.gz")

PID_OWN = "-18776"


def _frame(t, path=FIX):
    with gzip.open(path, "rt", encoding="utf8") as fh:
        return next(json.loads(line) for line in fh
                    if line.strip() and json.loads(line)["_trace_t"] == t)


def _world(frame):
    return World(json.loads(json.dumps(frame)))


def _ctx(world, tick=0):
    book = RecipeBook(world.recipes, world.stations, world.items)
    bb = make_bb(tick)
    return planner._make_context(world, bb, book), book


def _frame_world(t, path=FIX):
    frame = _frame(t, path)
    world = _world(frame)
    ctx, book = _ctx(world)
    return frame, world, ctx, book


def test_send_across_waits_through_belt_flicker():
    """接缝闪断（conveyor_slots 为空）时，刚见过的送件通道应允许等。"""
    frame, world, ctx, book = _frame_world(234, FIX_OWN)
    bb = ctx["bb"]
    task = T.SendAcross("2", (PID_OWN, CH.CONVEYOR), "plated_dish")

    assert not task.feasible(ctx)        # 没见过的通道不能送（会进本侧桶）
    bb.conveyor_open_until[CH.CONVEYOR] = bb.tick + CH.CONVEYOR_GRACE_TICKS
    assert task.feasible(ctx)            # 刚开过：允许端着盘等接缝重开

    ctx = dict(ctx, exec=FakeExec(), tick=bb.tick)
    assert task.build().tick(ctx) == "running"
    # 等待阶段不应为了腾手把盘子放下；也不应盲发 tap。
    assert not any(c.get("cmd") == "tap" for c in ctx["exec"].issued)


def test_no_feasible_freeze_becomes_send_wait():
    """t=2940 的真实停摆：接缝刚漂开时仍要生成 SendAcross 候选并等。"""
    _frame, world, ctx, book = _frame_world(2940)
    bb = ctx["bb"]
    assert CH.channel_keys(world, bb) == []      # 当前帧确实没有槽位
    bb.conveyor_open_until[CH.CONVEYOR] = bb.tick + CH.CONVEYOR_GRACE_TICKS
    chosen, record = planner.plan(world, bb, None, book)
    assert chosen is not None and chosen.TYPE == "SendAcross", chosen
    assert record["top_layer"] == 4
    assert not record.get("waiting") or record["waiting"].get("reason") != "no_feasible_task"


def test_hand_holding_needed_raw_blocks_rescue_preemption():
    """t=477：手里拿着 NuggetChoppedChicken，不能被 TakeCookedToPlate 抢走。"""
    _frame, world, ctx, book = _frame_world(477)
    bb = ctx["bb"]
    fill = T.FillCooker(None, (("Raft Two", "FryingStation#631208"),
                               ("NuggetChicken",)), "FriedChickenNuggets")
    take = T.TakeCookedToPlate("1", (("Raft Two", "FryingStation#634432"),
                                     "-19500"), "FriedChips")
    assert planner._holding_needed_for(fill, world, book)
    assert not planner._holding_needed_for(take, world, book)

    bb.commitment = Commitment(active_task=fill, steps_done=3)
    scored = [(take, 5.42, {}), (fill, 0.93, {})]
    assert planner._apply_commitment_inertia(scored, world, bb, book) == fill


def test_plated_dish_gets_hands_penalty_for_empty_hand_tasks():
    """t=600：手里端着盘子，FetchToBoard 这类要空手的 task 要吃重罚。"""
    _frame, world, ctx, book = _frame_world(600)
    bb = ctx["bb"]
    task = T.FetchToBoard("2", (("Raft Two",
                                 "countertop_01_chopping_board_swamp (2)"),),
                          "Lettuce")
    cand = planner.Candidate(task, ["hands_empty"], planner.EST_FETCH)
    _score, terms = planner._score_candidate(world, bb, cand, book)
    assert terms["hands"] == -planner.W_HANDS_VALUABLE

    # 空手时不罚。
    _frame2, world2, ctx2, book2 = _frame_world(2513)
    task2 = T.FetchToBoard("2", (("Raft Two",
                                  "countertop_01_chopping_board_swamp (2)"),),
                           "Lettuce")
    cand2 = planner.Candidate(task2, ["hands_empty"], planner.EST_FETCH)
    _score2, terms2 = planner._score_candidate(world2, ctx2["bb"], cand2, book2)
    assert terms2["hands"] == 0.0


def test_plated_dish_is_not_dropped_for_prep():
    """端着有内容的成品盘时，备菜 task 直接不可行，避免把盘放下。"""
    _frame, world, ctx, _book = _frame_world(2900)
    chef = P.chef_by_name(world.chefs, P.P2_NAME)
    assert P.held_is_plate(chef)
    task = T.FetchToBoard("2", (("Raft Two",
                                 "countertop_01_chopping_board_swamp (2)"),),
                          "Lettuce")
    cand = planner.Candidate(task, ["hands_empty"], planner.EST_FETCH)
    ok, veto = planner._feasibility_of(cand, ctx, ctx["bb"])
    assert not ok and veto == "hands_full_plate"

    # 空盘不算成品盘：允许先放下去备菜。
    _frame2, world2, ctx2, _book2 = _frame_world(600)
    task2 = T.FetchToBoard("2", (("Raft Two",
                                  "countertop_01_chopping_board_swamp (2)"),),
                           "Lettuce")
    cand2 = planner.Candidate(task2, ["hands_empty"], planner.EST_FETCH)
    ok2, veto2 = planner._feasibility_of(cand2, ctx2, ctx2["bb"])
    assert not (not ok2 and veto2 == "hands_full_plate")

    # 端着 -19502 时，不能被另一块盘 -19500 的 SendAcross 抢走（那会先把
    # 手里的盘放下）。t=2900 两块盘同时可送。
    bb = ctx["bb"]
    bb.conveyor_open_until[CH.CONVEYOR] = bb.tick + CH.CONVEYOR_GRACE_TICKS
    chosen, _record = planner.plan(world, bb, None, _book)
    assert chosen is not None and chosen.TYPE == "SendAcross"
    assert chosen.source_id == "-19502", chosen


def test_infeasible_threshold_survives_one_5hz_frame():
    """3 拍 < 一帧 4 拍；阈值必须至少撑过 2 帧，单帧抖动不切任务。"""
    _frame, world, ctx, book = _frame_world(477)
    bb = ctx["bb"]
    current = T.FetchToBoard("1", (("Raft Two",
                                    "countertop_01_chopping_board_swamp (2)"),),
                             "Lettuce")
    other = T.FetchToBoard("2", (("Raft Two",
                                  "countertop_01_chopping_board_swamp (2)"),),
                           "Tomato")
    bb.commitment = Commitment(active_task=current, steps_done=0)

    # 当前 task 不在候选里（模拟移动网格一帧不可达）。
    for _ in range(planner.INFEASIBLE_THRESHOLD - 1):
        assert planner._apply_commitment_inertia([(other, 1.0, {})],
                                                 world, bb) == current
    assert bb.commitment.infeasible_streak == planner.INFEASIBLE_THRESHOLD - 1
    assert planner._apply_commitment_inertia([(other, 1.0, {})],
                                             world, bb) == other
    assert planner.INFEASIBLE_THRESHOLD >= 8


def test_stationary_task_does_not_report_stuck():
    """切菜/盛盘站定是正常动作，不该被 trace 报成 stuck。"""
    _frame, world, ctx, _book = _frame_world(477)
    bb = ctx["bb"]
    chef = P.chef_by_name(world.chefs, P.P2_NAME)
    cell = P.chef_cell(chef, world.to_grid)
    bb.scratch = StuckScratch(last_cell=cell,
                              same_count=arbiter.STUCK_SAME_CELL_THRESHOLD)
    bb.commitment = Commitment(
        active_task=T.Chop("2", (("Raft Two",
                                 "countertop_01_chopping_board_swamp (2)"),),
                           "Lettuce"))
    assert not arbiter._is_stuck(world, bb)
    assert bb.scratch.same_count == 0

    # 移动类 task 仍然能报 stuck。
    bb.scratch.same_count = arbiter.STUCK_SAME_CELL_THRESHOLD
    bb.commitment = Commitment(
        active_task=T.FetchToBoard(
            "2", (("Raft Two", "countertop_01_chopping_board_swamp (2)"),),
            "Lettuce"))
    assert arbiter._is_stuck(world, bb)


def test_take_cooked_to_plate_sends_explicit_use_kind():
    """盛料交互必须显式发 kind=use，插件才能按 UseName/句柄校验，而不是
    默认 pickup 把盘子放到旁边的柜台上。"""
    from ai import primitives as PR
    from ai.exec.bt import Status

    _frame, world, ctx, _book = _frame_world(600)
    basket = next(it for it in world.items if it.get("n") == "FrierBasket (1)")
    ex = FakeExec()
    ctx = dict(ctx, exec=ex, tick=0)
    node = PR.TapWhileHolding("pickup", None, target=lambda c: basket,
                              kind="use")
    assert node.tick(ctx) == Status.RUNNING
    cmd = ex.issued[0]
    assert cmd["cmd"] == "tap" and cmd["kind"] == "use"
    assert cmd["target"]["name"] == "FrierBasket (1)"

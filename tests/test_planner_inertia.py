"""Planner 承诺惯性单测（Plan 3 Task 6）。

测试 _apply_commitment_inertia() 的核心逻辑：
  1. 进度加成：离出餐口一步时，另一锅 burning 不改主意
  2. 不可行迟滞：料被 P1 挡住一帧，不弃当前承诺
  3. 连续不可行超过阈值 → 放弃承诺
  4. 无承诺时选最高分
  5. 分数差距过大时切换任务
"""
import pytest

from ai.state.blackboard import Blackboard, Commitment
from ai.core.planner import (
    COMMITMENT_MARGIN,
    INFEASIBLE_THRESHOLD,
    PROGRESS_BONUS_PER_STEP,
    _apply_commitment_inertia,
)
from ai.query.recipes import RecipeBook
from ai.state.world import World
from ai import tasks as T


def _make_world(orders=None):
    # type: (list) -> World
    """构造最小 World。"""
    state = {
        "orders": orders or [],
        "stations": [],
        "recipes": [],
        "chefs": [],
        "workstations": [],
        "grid": {"nodeMap": None}
    }
    return World(state)


def _make_bb(commitment=None):
    # type: (Commitment) -> Blackboard
    """构造黑板。"""
    bb = Blackboard()
    bb.tick = 10
    if commitment:
        bb.commitment = commitment
    return bb


# ---------- 测试用例 ----------

def test_no_commitment_selects_highest_score():
    """无承诺时，选择最高分任务。"""
    w = _make_world()
    bb = _make_bb()

    task1 = T.Chop("order1", ("board_01",), "Tomato")
    task2 = T.Chop("order1", ("board_02",), "Lettuce")

    scored = [
        (task1, 5.0, {}),
        (task2, 8.0, {}),  # 最高分
    ]

    result = _apply_commitment_inertia(scored, w, bb)
    assert result == task2


def test_commitment_maintained_when_score_difference_small():
    """当前承诺分数与最高分差距小于阈值时，保持承诺。"""
    w = _make_world()
    current_task = T.Chop("order1", ("board_01",), "Tomato")
    commitment = Commitment(
        active_task=current_task,
        started_tick=5,
        steps_done=0,
        infeasible_streak=0
    )
    bb = _make_bb(commitment)

    task2 = T.Chop("order1", ("board_02",), "Lettuce")

    scored = [
        (current_task, 5.0, {}),
        (task2, 6.5, {}),  # 差距 1.5 < COMMITMENT_MARGIN (2.0)
    ]

    result = _apply_commitment_inertia(scored, w, bb)
    assert result == current_task


def test_commitment_switches_when_score_difference_large():
    """最高分与当前承诺差距超过阈值时，切换任务。"""
    w = _make_world()
    current_task = T.Chop("order1", ("board_01",), "Tomato")
    commitment = Commitment(
        active_task=current_task,
        started_tick=5,
        steps_done=0,
        infeasible_streak=0
    )
    bb = _make_bb(commitment)

    task2 = T.Chop("order1", ("board_02",), "Lettuce")

    scored = [
        (current_task, 5.0, {}),
        (task2, 10.0, {}),  # 差距 5.0 > COMMITMENT_MARGIN (2.0)
    ]

    result = _apply_commitment_inertia(scored, w, bb)
    assert result == task2


def test_commitment_inertia_progress_bonus():
    """进度加成：已完成步数增加改选门槛。"""
    w = _make_world()
    # DeliverPlate 预估 5 步，已完成 2 步 → progress_ratio = 0.4
    # progress_bonus = 0.4 * PROGRESS_BONUS_PER_STEP = 0.4
    # threshold = COMMITMENT_MARGIN + progress_bonus = 2.0 + 0.4 = 2.4
    current_task = T.DeliverPlate("order1", (("serve_01",),), None)
    commitment = Commitment(
        active_task=current_task,
        started_tick=5,
        steps_done=2,  # 已完成 2 步
        infeasible_streak=0
    )
    bb = _make_bb(commitment)

    task2 = T.Chop("order1", ("board_02",), "Lettuce")

    scored = [
        (current_task, 5.0, {}),
        (task2, 7.3, {}),  # 差距 2.3 < 2.4，应保持承诺
    ]

    result = _apply_commitment_inertia(scored, w, bb)
    assert result == current_task


def test_commitment_inertia_infeasible_streak_below_threshold():
    """不可行迟滞：连续 1 拍不可行（未达阈值），保持承诺。"""
    w = _make_world()
    current_task = T.Chop("order1", ("board_01",), "Tomato")
    commitment = Commitment(
        active_task=current_task,
        started_tick=5,
        steps_done=0,
        infeasible_streak=0
    )
    bb = _make_bb(commitment)

    # 当前 task 不在可行候选中
    task2 = T.Chop("order1", ("board_02",), "Lettuce")
    scored = [
        (task2, 8.0, {}),
    ]

    result = _apply_commitment_inertia(scored, w, bb)
    # 应保持承诺（返回当前 task），且 infeasible_streak 应增加
    assert result == current_task
    assert bb.commitment.infeasible_streak == 1


def test_commitment_abandoned_after_persistent_infeasible():
    """连续不可行超过阈值 → 放弃承诺。"""
    w = _make_world()
    current_task = T.Chop("order1", ("board_01",), "Tomato")
    commitment = Commitment(
        active_task=current_task,
        started_tick=5,
        steps_done=0,
        infeasible_streak=INFEASIBLE_THRESHOLD - 1  # 已经 2 次不可行
    )
    bb = _make_bb(commitment)

    # 当前 task 再次不在可行候选中（第 3 次）
    task2 = T.Chop("order1", ("board_02",), "Lettuce")
    scored = [
        (task2, 8.0, {}),
    ]

    result = _apply_commitment_inertia(scored, w, bb)
    # 应放弃承诺，选择最高分
    assert result == task2
    assert bb.commitment.infeasible_streak == 0


def test_commitment_infeasible_streak_reset_when_feasible():
    """当前 task 重新可行时，重置不可行计数。"""
    w = _make_world()
    current_task = T.Chop("order1", ("board_01",), "Tomato")
    commitment = Commitment(
        active_task=current_task,
        started_tick=5,
        steps_done=0,
        infeasible_streak=2  # 之前有 2 次不可行
    )
    bb = _make_bb(commitment)

    # 当前 task 重新出现在可行候选中
    task2 = T.Chop("order1", ("board_02",), "Lettuce")
    scored = [
        (current_task, 5.0, {}),
        (task2, 6.0, {}),  # 差距小，保持承诺
    ]

    result = _apply_commitment_inertia(scored, w, bb)
    assert result == current_task
    assert bb.commitment.infeasible_streak == 0  # 已重置


def test_commitment_with_high_progress_resists_better_task():
    """高进度任务抵抗更高分的诱惑。"""
    w = _make_world()
    # DeliverPlate 预估 5 步，已完成 4 步 → progress_ratio = 0.8
    # progress_bonus = 0.8 * 1.0 = 0.8
    # threshold = 2.0 + 0.8 = 2.8
    current_task = T.DeliverPlate("order1", (("serve_01",),), None)
    commitment = Commitment(
        active_task=current_task,
        started_tick=5,
        steps_done=4,  # 接近完成
        infeasible_streak=0
    )
    bb = _make_bb(commitment)

    task2 = T.Chop("order1", ("board_02",), "Lettuce")

    scored = [
        (current_task, 5.0, {}),
        (task2, 7.5, {}),  # 差距 2.5 < 2.8，应保持承诺
    ]

    result = _apply_commitment_inertia(scored, w, bb)
    assert result == current_task


def test_commitment_bypasses_inertia_when_about_to_burn():
    """快糊/已糊的锅要跳过承诺惯性，第一时间去端。

    实机：锅都熟了（pressure 拉满）还赖在 FetchToPlate 上不去端，因为分差
    0.66 < COMMITMENT_MARGIN(2.0)。
    """
    w = _make_world()
    # cooking_progress / cooking_time 同单位：24/12 → ratio = 24/(2*12) = 1.0（快糊）
    w.cookers = [{"n": "workstation_cooker_01", "cooked": True,
                  "cooking_progress": 24.0, "cooking_time": 12.0}]
    current_task = T.FetchToPlate("order1", ("p1",), "Seaweed")
    commitment = Commitment(active_task=current_task, started_tick=5,
                            steps_done=0, infeasible_streak=0)
    bb = _make_bb(commitment)

    top_task = T.TakeCookedToPlate("order3", ("workstation_cooker_01", "p3"),
                                   "BoiledSushiRice")
    scored = [
        (current_task, 1.39, {}),
        (top_task, 2.05, {}),   # 差距 0.66 < COMMITMENT_MARGIN，但快糊了要抢
    ]

    result = _apply_commitment_inertia(scored, w, bb)
    assert result == top_task


def test_commitment_bypasses_inertia_when_cooked():
    """锅一熟（pressure 1.0）就要跳过承诺惯性去端盘，不能等快糊。

    实机：米熟了 P2 还在给下一单下锅，因为分差 1.23 < COMMITMENT_MARGIN(2.0)，
    赖在 FetchToCooker 上不去盛饭。
    """
    w = _make_world()
    # 12/12 → ratio = 12/(2*12) = 0.5（刚熟）
    w.cookers = [{"n": "workstation_cooker_01", "cooked": True,
                  "cooking_progress": 12.0, "cooking_time": 12.0}]
    current_task = T.FetchToCooker("order3", ("workstation_cooker_01 (5)",),
                                   "BoiledSushiRice")
    commitment = Commitment(active_task=current_task, started_tick=5,
                            steps_done=0, infeasible_streak=0)
    bb = _make_bb(commitment)

    top_task = T.TakeCookedToPlate("order1", ("workstation_cooker_01", "p1"),
                                   "BoiledSushiRice")
    scored = [
        (current_task, 0.54, {}),
        (top_task, 1.77, {}),   # 差距 1.23 < COMMITMENT_MARGIN，但熟了要抢
    ]

    result = _apply_commitment_inertia(scored, w, bb)
    assert result == top_task


def test_empty_scored_returns_none():
    """空候选列表返回 None。"""
    w = _make_world()
    bb = _make_bb()

    scored = []
    result = _apply_commitment_inertia(scored, w, bb)
    assert result is None

# -*- coding: utf-8 -*-
"""Arbiter 模式仲裁单测。

重点验证：
  1. Stuck 是前置守卫，先于模式表生效（火中被堵 → 先脱困）。
  2. Firefighting 进入 / 退出条件（资源视角，P1 持唯一灭火器时不进）。
  3. 模式转移时清空 scratch。
  4. 中断 task 连续失败降级。
"""
import pytest
from ai import arbiter
from ai.blackboard import Blackboard, Commitment, StuckScratch, FireScratch
from ai.world import World


def _make_world(cookers=None, extinguishers=None, chefs=None, items=None):
    # type: (list, list, list, list) -> World
    """构造测试用 World。"""
    state = {
        "phase": "InLevel",
        "level": "test_level",
        "paused": False,
        "orders": [],
        "chefs": chefs or [],
        "stations": [],
        "recipes": [],
        "plates": [],
        "workstations": [],
        "cookers": cookers or [],
        "extinguishers": extinguishers or [],
        "items": items or [],
        "grid": {"size": 0.72, "nodeMap": []},
    }
    return World(state)


def _make_bb(mode="Normal", scratch=None, commitment=None):
    # type: (str, object, object) -> Blackboard
    """构造测试用 Blackboard。"""
    bb = Blackboard()
    bb.mode = mode
    bb.scratch = scratch
    if commitment:
        bb.commitment = commitment
    return bb


def _make_chef(name, x, z, held=None):
    # type: (str, float, float, str) -> dict
    """构造厨师数据。"""
    return {"name": name, "x": x, "z": z, "held": held}


def _make_cooker(n, on_fire=False, has_pot=True):
    # type: (str, bool, bool) -> dict
    """构造灶台数据。cooked=None 表示无锅，False/True 表示有锅。"""
    return {
        "n": n,
        "x": 1.0,
        "z": 1.0,
        "onFire": on_fire,
        "cooked": False if has_pot else None,
    }


def _make_extinguisher(n, held=None):
    # type: (str, str) -> dict
    """构造灭火器数据。"""
    return {"n": n, "x": 2.0, "z": 2.0, "t": "extinguisher", "held": held}


def test_stuck_guard_takes_precedence_over_firefighting():
    """火中被堵：Stuck 守卫先生效，不死锁在 Firefighting。"""
    # 构造：cooker onFire、AI 在原地连续 3 拍、灭火器可得
    cookers = [_make_cooker("cooker_1", on_fire=True)]
    extinguishers = [_make_extinguisher("ext_1", held=None)]
    chefs = [
        _make_chef("Player 2", 3.0, 3.0, None),
        _make_chef("Player 1", 5.0, 5.0, None),
    ]
    world = _make_world(cookers=cookers, extinguishers=extinguishers, chefs=chefs)

    # 先获取 AI 实际所在格子，然后设置 scratch
    ai_cell = world.to_grid(3.0, 3.0)
    scratch = StuckScratch()
    scratch.last_cell = ai_cell
    scratch.same_count = 3  # 已经卡了 3 拍
    bb = _make_bb(mode="Normal", scratch=scratch)

    mode, reason = arbiter.select_mode(world, bb)

    # 断言：检测到卡住，进入 Stuck 处理，mode 保持原值
    assert mode == "Normal"
    assert "stuck" in reason.lower()
    assert isinstance(bb.scratch, StuckScratch)


def test_firefighting_entry_with_available_extinguisher():
    """有未被处理的着火灶且灭火器可得 → 进入 Firefighting。"""
    cookers = [_make_cooker("cooker_1", on_fire=True)]
    extinguishers = [_make_extinguisher("ext_1", held=None)]
    chefs = [
        _make_chef("Player 2", 10.0, 10.0, None),
        _make_chef("Player 1", 20.0, 20.0, None),
    ]
    world = _make_world(cookers=cookers, extinguishers=extinguishers, chefs=chefs)
    bb = _make_bb(mode="Normal")

    mode, reason = arbiter.select_mode(world, bb)

    assert mode == "Firefighting"
    assert "fire" in reason.lower() or "cooker" in reason.lower()


def test_firefighting_not_entered_when_p1_holds_only_extinguisher():
    """P1 持唯一灭火器 → 不进 Firefighting，留在 Normal。"""
    cookers = [_make_cooker("cooker_1", on_fire=True)]
    # 唯一灭火器被 P1 持有
    extinguishers = [_make_extinguisher("ext_1", held="Player 1")]
    chefs = [
        _make_chef("Player 1", 5.0, 5.0, "extinguisher"),
        _make_chef("Player 2", 10.0, 10.0, None),
    ]
    world = _make_world(cookers=cookers, extinguishers=extinguishers, chefs=chefs)
    bb = _make_bb(mode="Normal")

    mode, reason = arbiter.select_mode(world, bb)

    # 不进 Firefighting，退回 Normal
    assert mode == "Normal"
    assert reason == "default"


def test_firefighting_exit_when_all_fires_handled():
    """所有火灭了 → 退出 Firefighting 回 Normal。"""
    # 没有着火的灶
    cookers = [_make_cooker("cooker_1", on_fire=False)]
    extinguishers = [_make_extinguisher("ext_1", held=None)]
    chefs = [_make_chef("Player 2", 10.0, 10.0, None)]
    world = _make_world(cookers=cookers, extinguishers=extinguishers, chefs=chefs)
    bb = _make_bb(mode="Firefighting", scratch=FireScratch())

    mode, reason = arbiter.select_mode(world, bb)

    assert mode == "Normal"
    assert reason == "default"


def test_mode_transition_clears_scratch():
    """模式转移时清空 bb.scratch。"""
    bb = _make_bb(mode="Normal", scratch=FireScratch())

    arbiter.on_mode_transition("Firefighting", "Normal", bb)

    assert bb.scratch is None


def test_no_clear_when_mode_unchanged():
    """模式未变化时不清空 scratch。"""
    scratch = StuckScratch()
    bb = _make_bb(mode="Normal", scratch=scratch)

    arbiter.on_mode_transition("Normal", "Normal", bb)

    assert bb.scratch is scratch


def test_interrupt_task_failure_degrades_to_normal():
    """Firefighting 模式下 task 连续失败 3 次 → 退回 Normal 并拉黑目标。"""
    cookers = [_make_cooker("cooker_1", on_fire=True)]
    extinguishers = [_make_extinguisher("ext_1", held=None)]
    chefs = [_make_chef("Player 2", 10.0, 10.0, None)]
    world = _make_world(cookers=cookers, extinguishers=extinguishers, chefs=chefs)

    # 连续失败 3 次
    commitment = Commitment()
    commitment.interrupt_failure_count = 3
    commitment.target_resource_id = "cooker_1"
    bb = _make_bb(mode="Firefighting", commitment=commitment)
    bb.tick = 100

    mode, reason = arbiter.select_mode(world, bb)

    # 强制退回 Normal
    assert mode == "Normal"
    assert "failure" in reason.lower() or "degrade" in reason.lower()
    # 拉黑目标
    assert hasattr(bb, "blacklisted_resources")
    assert "cooker_1" in bb.blacklisted_resources
    assert bb.blacklisted_resources["cooker_1"] == 100 + arbiter.BLACKLIST_TICKS


def test_not_stuck_when_moving():
    """AI 在移动（格子变化）→ 不判定卡住。"""
    cookers = []
    chefs = [_make_chef("Player 2", 3.0, 3.0, None)]
    world = _make_world(cookers=cookers, chefs=chefs)

    # AI 上一拍在 (4, 4)，本拍在 (4, 5)，移动了
    scratch = StuckScratch()
    scratch.last_cell = (4, 4)
    scratch.same_count = 2
    bb = _make_bb(mode="Normal", scratch=scratch)

    mode, reason = arbiter.select_mode(world, bb)

    # 不应进入 Stuck 处理
    assert "stuck" not in reason.lower()

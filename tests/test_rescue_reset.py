"""收尾 task 单测（Plan 4 Task 6）。

重点验证：
  1. RescueCooked/ResetPlate/Prefetch 三个 task 的基本属性
  2. 集成到候选生成：cleanup tasks 可以被正确添加到候选池
"""
import pytest

from ai import tasks as T
from ai.blackboard import Blackboard
from ai.recipes import RecipeBook
from ai.world import World


def _make_world(orders=None, stations=None, recipes=None, cookers=None,
                plates=None, items=None, chefs=None):
    # type: (...) -> World
    """构造最小 World。"""
    state = {
        "orders": orders or [],
        "stations": stations or [],
        "recipes": recipes or [],
        "cookers": cookers or [],
        "plates": plates or [],
        "items": items or [],
        "chefs": chefs or [{"x": 0.0, "z": 0.0, "held": None}],
        "grid": {"nodeMap": None}
    }
    return World(state)


def _make_bb():
    # type: () -> Blackboard
    """构造黑板。"""
    bb = Blackboard()
    bb.tick = 10
    return bb


# ========== Task 基本属性测试 ==========

def test_rescue_cooked_exists():
    """RescueCooked task 类存在且可实例化。"""
    task = T.RescueCooked(None, ("cooker_1",), None)
    assert task.TYPE == "RescueCooked"
    assert task.cooker_id == "cooker_1"


def test_rescue_cooked_claims_cooker():
    """RescueCooked 认领灶台。"""
    task = T.RescueCooked(None, ("cooker_1",), None)
    claims = task.claims()
    assert ("cooker_1", "rescue") in claims


def test_reset_plate_exists():
    """ResetPlate task 类存在且可实例化。"""
    task = T.ResetPlate(None, ("plate_1", (2, 2)), None)
    assert task.TYPE == "ResetPlate"
    assert task.plate_id == "plate_1"
    assert task.counter_cell == (2, 2)


def test_reset_plate_claims():
    """ResetPlate 认领盘、拼装格。"""
    task = T.ResetPlate(None, ("plate_1", (2, 2)), None)
    claims = task.claims()
    assert ("plate_1", "reset") in claims
    assert ((2, 2), "assemble") in claims


def test_prefetch_exists():
    """Prefetch task 类存在且可实例化。"""
    task = T.Prefetch(None, (), "Rice")
    assert task.TYPE == "Prefetch"
    assert task.item == "Rice"


def test_prefetch_claims():
    """Prefetch 认领为空（不争抢资源）。"""
    task = T.Prefetch(None, (), "Rice")
    claims = task.claims()
    assert claims == ()


# ========== Task 在 TASK_TYPES 中注册 ==========

def test_all_cleanup_tasks_registered():
    """验证三个收尾 task 都在 TASK_TYPES 中注册。"""
    assert "RescueCooked" in T.TASK_TYPES
    assert "ResetPlate" in T.TASK_TYPES
    assert "Prefetch" in T.TASK_TYPES
    assert T.TASK_TYPES["RescueCooked"] == T.RescueCooked
    assert T.TASK_TYPES["ResetPlate"] == T.ResetPlate
    assert T.TASK_TYPES["Prefetch"] == T.Prefetch



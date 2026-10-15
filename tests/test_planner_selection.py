"""Planner 候选打分与选择单测（Plan 3 Task 5）。

测试 select_task() 与 _score_candidate() 的核心逻辑：
  1. 选择估计步数最少的候选
  2. 无可行候选时返回 None
  3. 相同分数时选择列表中第一个
  4. 单个候选时正确选中
  5. 打分函数对不同候选的评分差异
"""
import pytest

from ai.blackboard import Blackboard
from ai.planner import Candidate, _score_candidate
from ai.recipes import RecipeBook
from ai.world import World
from ai import tasks as T


def _make_order(dish, composition, order_id="order1", remaining=3):
    # type: (str, list, str, int) -> dict
    """构造订单 dict。"""
    return {
        "dish": dish,
        "composition": composition,
        "id": order_id,
        "remaining": remaining
    }


def _make_world(orders=None, stations=None, recipes=None):
    # type: (list, list, list) -> World
    """构造最小 World。"""
    state = {
        "orders": orders or [],
        "stations": stations or [],
        "recipes": recipes or [],
        "chefs": [],
        "workstations": [],
        "grid": {"nodeMap": None}
    }
    return World(state)


def _make_bb():
    # type: () -> Blackboard
    """构造黑板。"""
    bb = Blackboard()
    bb.tick = 10
    return bb


# ---------- 测试用例 ----------

def test_score_prefers_lower_estimated_steps():
    """打分函数偏好估计步数更少的候选（ETA 权重生效）。"""
    order = _make_order("Test", ["Tomato"], remaining=3)
    w = _make_world(orders=[order])
    bb = _make_bb()

    # 创建两个候选：一个 10 步，一个 20 步
    cand_short = Candidate(
        task=T.Chop("order1", ("board_01",), "Tomato"),
        preconditions=[],
        estimated_steps=10
    )
    cand_long = Candidate(
        task=T.Chop("order1", ("board_02",), "Tomato"),
        preconditions=[],
        estimated_steps=20
    )

    score_short, terms_short = _score_candidate(w, bb, cand_short)
    score_long, terms_long = _score_candidate(w, bb, cand_long)

    # 步数少的分数应该更高（因为 ETA 用负号）
    assert score_short > score_long
    # 验证 terms 包含所需的键
    assert "urgency" in terms_short
    assert "pressure" in terms_short
    assert "eta" in terms_short


def test_score_urgency_boosts_with_lower_remaining():
    """订单 remaining 越小，紧迫度分数越高。

    两个 remaining 的取值要拉开到 slack 曲线的线性段里。urgency 现在读
    slack（remaining - ETA）而非裸 remaining，且在 slack <= 0 处饱和——
    remaining=1 和 10 两者 slack 都已为负、同为满分 5.0，分不出先后是**正确**
    行为（都来不及了，紧迫程度没有区别）。见 ai/planner._urgency_from_slack。
    """
    order_urgent = _make_order("Urgent", ["Tomato"], remaining=50)
    order_relaxed = _make_order("Relaxed", ["Tomato"], remaining=120)

    w_urgent = _make_world(orders=[order_urgent])
    w_relaxed = _make_world(orders=[order_relaxed])
    bb = _make_bb()

    # 相同的候选
    cand = Candidate(
        task=T.Chop("order1", ("board_01",), "Tomato"),
        preconditions=[],
        estimated_steps=10
    )

    score_urgent, terms_urgent = _score_candidate(w_urgent, bb, cand)
    score_relaxed, terms_relaxed = _score_candidate(w_relaxed, bb, cand)

    # 订单更紧迫时分数应该更高
    assert score_urgent > score_relaxed
    # 紧迫订单的 urgency 项应该更高
    assert terms_urgent["urgency"] > terms_relaxed["urgency"]


def test_returns_none_when_no_order():
    """无订单时返回 None。"""
    from ai.planner import select_task

    w = _make_world(orders=[])
    bb = _make_bb()
    book = RecipeBook([], [])

    selected = select_task(w, bb, None, book)

    assert selected is None


def test_plan_returns_decision_record():
    """plan() 返回 (chosen_task, record)，record 含 candidates 明细（spec §8.3）。"""
    from ai.planner import plan, select_task

    w = _make_world(orders=[])
    bb = _make_bb()
    book = RecipeBook([], [])

    chosen, record = plan(w, bb, None, book)
    assert chosen is None
    assert isinstance(record, dict)
    assert "candidates" in record
    assert record["candidates"] == []
    # 每拍匹配关系也随 record 返回（盘↔订单 / 灶↔订单），供 trace 落盘
    assert record["plate_match"] == {}
    assert record["cooker_match"] == {}

    # select_task 是 plan 的薄包装，行为一致
    assert select_task(w, bb, None, book) is None


def test_score_candidate_handles_missing_order():
    """候选的 task.order_id 不在 world.orders 中时不崩溃。"""
    w = _make_world(orders=[])
    bb = _make_bb()

    cand = Candidate(
        task=T.Chop("nonexistent_order", ("board_01",), "Tomato"),
        preconditions=[],
        estimated_steps=10
    )

    # 应该不崩溃，只是 urgency 部分不计分
    score, terms = _score_candidate(w, bb, cand)

    # 仍应有 ETA 的负分
    assert score < 0
    assert terms["urgency"] == 0.0  # 订单不存在，紧迫度为 0
    assert terms["eta"] < 0  # ETA 应该是负数


def test_score_identical_candidates_equal():
    """相同候选得分相同（用于 tie-breaking 验证）。"""
    order = _make_order("Test", ["Tomato"], remaining=5)
    w = _make_world(orders=[order])
    bb = _make_bb()

    cand1 = Candidate(
        task=T.Chop("order1", ("board_01",), "Tomato"),
        preconditions=[],
        estimated_steps=15
    )
    cand2 = Candidate(
        task=T.Chop("order1", ("board_02",), "Tomato"),
        preconditions=[],
        estimated_steps=15
    )

    score1, terms1 = _score_candidate(w, bb, cand1)
    score2, terms2 = _score_candidate(w, bb, cand2)

    # 相同 estimated_steps 和 order → 分数应该相同
    assert score1 == score2
    assert terms1 == terms2

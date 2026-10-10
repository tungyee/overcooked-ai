"""Agent 主循环单测（Plan 3 Task 8）。

重点验证：
  1. 13 步执行顺序正确。
  2. 步骤 ⑨ 写回 claims、步骤 ⑩ 登记 commitment。
  3. 认领的四个释放触发点生效。
  4. 单订单模式：选择并追踪订单。
"""
import pytest
from unittest.mock import MagicMock, patch, call
from ai.agent import Agent
from ai.blackboard import Blackboard, Commitment, Claim
from ai.world import World


@pytest.fixture
def mock_state_client():
    """Mock StateClient 返回假快照。"""
    with patch("ai.agent.StateClient") as MockClient:
        client = MockClient.return_value
        # 默认返回空快照
        client.receive.return_value = {
            "orders": [],
            "chefs": [{"name": "Player 2", "x": 1.0, "z": 1.0}],
            "stations": [],
            "cookers": [],
            "items": [],
            "extinguishers": [],
            "grid": {"size": 1.0, "nodeMap": []},
            "phase": "level"
        }
        yield client


@pytest.fixture
def agent(mock_state_client):
    """构造 Agent 实例（不启动主循环）。"""
    with patch("ai.agent.StateClient", return_value=mock_state_client):
        agent = Agent()
        agent.bb = Blackboard()
        agent.client = mock_state_client
        return agent


def test_agent_initialization():
    """Agent 初始化正确。"""
    with patch("ai.agent.StateClient"):
        agent = Agent()
        assert agent.tick == 0
        # Plan 4: current_order_id 已移除（多订单模式）
        assert agent.bb is not None


def test_skip_ticks_decrements_and_releases_keys(agent, mock_state_client):
    """步骤 ② skip_ticks > 0 时松键并递减。"""
    from ai.blackboard import StuckScratch

    agent.bb.scratch = StuckScratch()
    agent.bb.scratch.skip_ticks = 2

    # Mock hands
    agent.hands = MagicMock()

    # 执行一拍
    snapshot = mock_state_client.receive.return_value
    agent._tick_once(World(snapshot))

    # 验证松键
    agent.hands.release_all.assert_called_once()

    # 验证递减
    assert agent.bb.scratch.skip_ticks == 1


def test_reconcile_claims_called(agent, mock_state_client):
    """步骤 ③ 认领对账被调用。"""
    with patch("ai.reconcile.reconcile_claims") as mock_reconcile:
        mock_reconcile.return_value = None
        snapshot = mock_state_client.receive.return_value
        agent._tick_once(World(snapshot))

        # 验证 reconcile 被调用
        assert mock_reconcile.called


def test_mode_transition_halts_executor(agent, mock_state_client):
    """步骤 ⑦ 模式变化时 halt 并丢弃树。"""
    agent.bb.mode = "Normal"

    # Mock arbiter 返回新模式
    with patch("ai.arbiter.select_mode") as mock_select:
        mock_select.return_value = ("Firefighting", "fire detected")

        # Mock executor
        agent.executor_inst = MagicMock()

        snapshot = mock_state_client.receive.return_value
        agent._tick_once(World(snapshot))

        # 验证 halt_and_discard 被调用。模式切换（⑦）会 halt 一次；
        # Firefighting 下 chosen_task 为 None，⑫ 也会再 halt 一次（幂等）。
        agent.executor_inst.halt_and_discard.assert_called()

        # 验证模式更新
        assert agent.bb.mode == "Firefighting"


def test_no_task_halts_executor(agent, mock_state_client):
    """步骤 ⑫ 无任务可选时也要 halt 旧树松键。

    实机复现：RecoverLoosePot 因锅被 P1 端走变不可行，planner 选不到任务，
    但上一次 Approach 的移动键还按着，角色一直按 right 顶墙。
    """
    agent.bb.mode = "Normal"

    with patch("ai.arbiter.select_mode") as mock_mode, \
         patch("ai.planner.select_task") as mock_plan:
        mock_mode.return_value = ("Normal", "default")
        mock_plan.return_value = None  # 无可行任务

        agent.executor_inst = MagicMock()

        snapshot = mock_state_client.receive.return_value
        agent._tick_once(World(snapshot))

        agent.executor_inst.halt_and_discard.assert_called()


def test_claims_written_back_to_blackboard(agent, mock_state_client):
    """步骤 ⑨ 写回 claims 到黑板。"""
    agent.bb.mode = "Normal"

    # Mock planner：plan 返回 (task, record)
    with patch("ai.planner.plan") as mock_plan:
        mock_task = MagicMock()
        mock_task.key = ("Chop", "order1", ("board_01",), "Tomato")
        mock_task.claims.return_value = [("board_01", "chop")]
        mock_plan.return_value = (mock_task, {"candidates": []})

        # Mock executor
        agent.executor_inst = MagicMock()
        agent.executor_inst.tick.return_value = "running"

        # Mock trace to avoid JSON serialization issue
        with patch("ai.trace.emit"):
            snapshot = mock_state_client.receive.return_value
            snapshot["orders"] = [{"id": "order1", "items": ["Tomato"], "remaining": 30}]

            agent._tick_once(World(snapshot))

            # 验证 claims 写入黑板
            assert ("board_01", "chop") in agent.bb.claims


def test_commitment_registered(agent, mock_state_client):
    """步骤 ⑩ 登记 commitment。"""
    agent.bb.mode = "Normal"

    # Mock planner：plan 返回 (task, record)
    with patch("ai.planner.plan") as mock_plan:
        mock_task = MagicMock()
        mock_task.key = ("Chop", "order1", ("board_01",), "Tomato")
        mock_task.claims.return_value = []
        mock_plan.return_value = (mock_task, {"candidates": []})

        # Mock executor
        agent.executor_inst = MagicMock()
        agent.executor_inst.tick.return_value = "running"

        # Mock trace
        with patch("ai.trace.emit"):
            snapshot = mock_state_client.receive.return_value
            snapshot["orders"] = [{"id": "order1", "items": ["Tomato"], "remaining": 30}]

            agent._tick_once(World(snapshot))

            # 验证 commitment 登记（存 Task 对象本身，承诺惯性按四元身份比较）
            assert agent.bb.commitment is not None
            assert agent.bb.commitment.active_task == mock_task


def test_task_success_releases_claims(agent, mock_state_client):
    """task SUCCESS → 释放该 task 的认领。"""
    agent.bb.mode = "Normal"

    # 预先设置认领
    agent.bb.claims[("board_01", "chop")] = Claim("P2", "order1", 0)

    # Mock planner：plan 返回 (task, record)
    with patch("ai.planner.plan") as mock_plan:
        mock_task = MagicMock()
        mock_task.key = ("Chop", "order1", ("board_01",), "Tomato")
        mock_task.claims.return_value = [("board_01", "chop")]
        mock_plan.return_value = (mock_task, {"candidates": []})

        # Mock executor 返回 success
        agent.executor_inst = MagicMock()
        agent.executor_inst.tick.return_value = "success"

        # Mock trace
        with patch("ai.trace.emit"):
            snapshot = mock_state_client.receive.return_value
            snapshot["orders"] = [{"id": "order1", "items": ["Tomato"], "remaining": 30}]

            agent._tick_once(World(snapshot))

            # 验证认领被释放
            assert ("board_01", "chop") not in agent.bb.claims
            assert agent.bb.commitment is None


def test_order_selection_by_remaining(agent, mock_state_client):
    """Plan 4 多订单模式：不再需要单订单选择逻辑。"""
    # Plan 4: _update_current_order 已移除（多订单模式）
    # Planner 会枚举所有订单，按打分选择任务
    snapshot = mock_state_client.receive.return_value
    snapshot["orders"] = [
        {"id": "order1", "items": ["Tomato"], "remaining": 50},
        {"id": "order2", "items": ["Lettuce"], "remaining": 20},
        {"id": "order3", "items": ["Onion"], "remaining": 35}
    ]

    world = World(snapshot)

    # 验证所有订单都可见
    assert len(world.orders) == 3
    # Planner 会自动按 urgency 打分选择最紧迫的订单任务


def test_order_switch_when_current_gone(agent, mock_state_client):
    """Plan 4 多订单模式：订单消失自动处理。"""
    # Plan 4: current_order_id 已移除（多订单模式）
    # 订单消失时，reconcile_claims 会自动释放该订单的认领

    snapshot = mock_state_client.receive.return_value
    snapshot["orders"] = [
        {"id": "order2", "items": ["Lettuce"], "remaining": 30}
    ]

    world = World(snapshot)

    # 验证剩余订单可见
    assert len(world.orders) == 1
    assert world.orders[0]["id"] == "order2"


def test_trace_emit_called(agent, mock_state_client):
    """步骤 ⑭ 决策日志被调用。"""
    agent.bb.mode = "Normal"

    with patch("ai.agent.trace") as mock_trace:
        with patch("ai.agent.planner") as mock_planner:
            mock_task = MagicMock()
            mock_task.key = ("Chop", "order1", ("board_01",), "Tomato")
            mock_task.claims.return_value = []
            mock_planner.plan.return_value = (mock_task, {"candidates": []})

            agent.executor_inst = MagicMock()
            agent.executor_inst.tick.return_value = "running"

            snapshot = mock_state_client.receive.return_value
            snapshot["orders"] = [{"id": "order1", "items": ["Tomato"], "remaining": 30}]

            agent._tick_once(World(snapshot))

            # 验证 trace.emit 被调用
            mock_trace.emit.assert_called_once()

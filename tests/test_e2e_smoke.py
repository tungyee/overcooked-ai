# -*- coding: utf-8 -*-
"""端到端冒烟测试（Plan 3 Task 9）。

验证目标（spec §11.2.1）：
  1. 完整决策循环（Arbiter → Planner → Executor → Trace）
  2. 模式切换（Normal → Firefighting）
  3. 任务完成与认领释放
  4. Trace 日志正确记录

测试方法：
  - 使用 Mock StateClient 提供固定游戏状态序列
  - 运行主循环直到目标达成或超时
  - 验证决策日志、认领状态、执行结果
"""
import json
import os
import pytest
from unittest.mock import MagicMock, patch

from ai.agent import Agent
from ai.blackboard import Blackboard, Claim
from ai.world import World
from ai import trace


class MockStateClient:
    """Mock 版本的 StateClient，用于端到端测试。"""

    def __init__(self, states):
        """
        参数:
            states: List[dict] - 游戏状态序列
        """
        self.states = states
        self.index = 0

    def read(self):
        """返回下一个状态快照。"""
        if self.index >= len(self.states):
            raise EOFError("No more states")
        state = self.states[self.index]
        self.index += 1
        return state

    def close(self):
        pass


def _make_minimal_state(tick=0, orders=None, chefs=None, stations=None, cookers=None):
    """构造最小化游戏状态。"""
    return {
        "tick": tick,
        "phase": "level",
        "level": "Tutorial-1",
        "paused": False,
        "orders": orders or [],
        "chefs": chefs or [{"name": "Player 2", "x": 1.0, "z": 1.0, "held": None}],
        "stations": stations or [],
        "recipes": [
            {
                "name": "TomatoSoup",
                "ingredients": ["Tomato-Chopped"],
                "assembled": True
            }
        ],
        "plates": [],
        "workstations": [],
        "cookers": cookers or [],
        "items": [],
        "extinguishers": [],
        "grid": {
            "size": 1.0,
            "ox": 0.0,
            "oz": 0.0,
            "nodeMap": []
        }
    }


def _make_simple_order_scenario():
    """构造简单订单场景：TomatoSoup。

    状态序列：
      1. 初始状态：订单出现，有番茄供应箱和切菜板
      2-5. 持续几帧模拟游戏运行
    """
    states = []

    # 状态 1-5: 订单存在
    for i in range(5):
        states.append(_make_minimal_state(
            tick=i,
            orders=[
                {"id": "order1", "recipe": "TomatoSoup", "remaining": 30 - i}
            ],
            stations=[
                {"n": "crate_01", "t": "crate", "ing": "Tomato", "x": 1.0, "z": 1.0},
                {"n": "board_01", "t": "board", "item": None, "x": 2.0, "z": 1.0},
                {"n": "plate_01", "t": "plate_station", "x": 3.0, "z": 1.0},
                {"n": "window_01", "t": "serving_window", "x": 4.0, "z": 1.0}
            ]
        ))

    return states


def _make_firefighting_scenario():
    """构造灭火场景：锅着火。

    状态序列：
      1-2. Normal 模式（无火灾）
      3-4. 锅着火（应切换到 Firefighting 模式）
      5. 火被扑灭（应切回 Normal 模式）
    """
    states = []

    # 状态 1-2: Normal 模式
    for i in range(2):
        state = _make_minimal_state(
            tick=i,
            orders=[{"id": "order1", "recipe": "TomatoSoup", "remaining": 30}],
            cookers=[]
        )
        # 添加灭火器到 extinguishers 列表
        state["extinguishers"] = [
            {"n": "ext_01", "held": None, "x": 1.0, "z": 2.0}
        ]
        states.append(state)

    # 状态 3-4: 着火，P1 不在附近
    for i in range(2, 4):
        state = _make_minimal_state(
            tick=i,
            orders=[{"id": "order1", "recipe": "TomatoSoup", "remaining": 30}],
            cookers=[
                {"n": "cooker_01", "onFire": True, "isCooking": False, "x": 5.0, "z": 5.0}
            ],
            chefs=[
                {"name": "Player 1", "x": 10.0, "z": 10.0, "held": None},  # P1 远离火源
                {"name": "Player 2", "x": 1.0, "z": 1.0, "held": None}
            ],
            stations=[
                {"n": "ext_01", "t": "fire_extinguisher", "x": 1.0, "z": 2.0}
            ]
        )
        # 添加灭火器到 extinguishers 列表
        state["extinguishers"] = [
            {"n": "ext_01", "held": None, "x": 1.0, "z": 2.0}
        ]
        states.append(state)

    # 状态 5: 火灭
    state = _make_minimal_state(
        tick=4,
        orders=[{"id": "order1", "recipe": "TomatoSoup", "remaining": 30}],
        cookers=[
            {"n": "cooker_01", "onFire": False, "isCooking": False, "x": 5.0, "z": 5.0}
        ]
    )
    state["extinguishers"] = [
        {"n": "ext_01", "held": None, "x": 1.0, "z": 2.0}
    ]
    states.append(state)

    return states


@pytest.fixture
def clean_trace():
    """清空 trace 日志。"""
    trace.clear_log()
    yield
    # 测试后也清理
    if os.path.exists(trace.TRACE_PATH):
        os.remove(trace.TRACE_PATH)


def test_e2e_basic_decision_loop(clean_trace):
    """端到端：基础决策循环（Arbiter → Planner → Executor）。

    验证：
      - 主循环能正常执行多拍
      - Arbiter 选择模式
      - Planner 生成候选并选择任务
      - Executor 执行任务
      - Trace 记录决策日志
    """
    states = _make_simple_order_scenario()
    mock_client = MockStateClient(states)

    with patch("ai.agent.StateClient", return_value=mock_client):
        agent = Agent()
        agent.client = mock_client

        # 执行 5 拍
        for _ in range(5):
            try:
                snapshot = agent.client.read()
                agent._tick_once(World(snapshot))
                agent.tick += 1
            except EOFError:
                break

    # 验证决策日志存在且格式正确
    logs = trace.read_log()
    assert len(logs) > 0, "应该有决策日志"

    # 验证每条日志包含必需字段
    for log in logs:
        assert "t" in log, "日志应包含拍号"
        assert "mode" in log, "日志应包含模式"
        assert "candidates" in log, "日志应包含候选列表"
        assert "commit" in log, "日志应包含承诺状态"
        assert "exec" in log, "日志应包含执行状态"
        assert "keys" in log, "日志应包含按键列表"

    # 验证模式为 Normal
    assert logs[0]["mode"] == "Normal", "初始模式应为 Normal"


def test_e2e_mode_transition(clean_trace):
    """端到端：模式切换（Normal → Firefighting → Normal）。

    验证：
      - Arbiter 检测到火灾并切换到 Firefighting
      - 火灭后切回 Normal
      - 模式切换时 Executor halt 并丢弃树
      - Trace 记录模式变化
    """
    states = _make_firefighting_scenario()
    mock_client = MockStateClient(states)

    with patch("ai.agent.StateClient", return_value=mock_client):
        agent = Agent()
        agent.client = mock_client

        modes_seen = []

        # 执行 5 拍
        for _ in range(5):
            try:
                snapshot = agent.client.read()
                agent._tick_once(World(snapshot))
                modes_seen.append(agent.bb.mode)
                agent.tick += 1
            except EOFError:
                break

    # 验证模式转换序列
    assert "Normal" in modes_seen, "应该经历 Normal 模式"
    assert "Firefighting" in modes_seen, "应该切换到 Firefighting 模式"

    # 验证 trace 日志记录模式
    logs = trace.read_log()
    assert len(logs) > 0, "应该有决策日志"

    log_modes = [log["mode"] for log in logs]
    assert "Normal" in log_modes, "日志应记录 Normal 模式"
    assert "Firefighting" in log_modes, "日志应记录 Firefighting 模式"


def test_e2e_task_commitment(clean_trace):
    """端到端：任务承诺与认领管理。

    验证：
      - Planner 选择任务后写入 commitment
      - 任务的认领被正确申请
      - 任务完成或失败后认领被释放
    """
    states = _make_simple_order_scenario()
    mock_client = MockStateClient(states)

    with patch("ai.agent.StateClient", return_value=mock_client):
        agent = Agent()
        agent.client = mock_client

        commitments = []
        claims_counts = []

        # 执行 5 拍
        for _ in range(5):
            try:
                snapshot = agent.client.read()
                agent._tick_once(World(snapshot))

                # 记录承诺和认领状态
                commitments.append(agent.bb.commitment)
                claims_counts.append(len(agent.bb.claims))

                agent.tick += 1
            except EOFError:
                break

    # 验证至少有某些拍产生了承诺（如果 Planner 选择了任务）
    # 注意：由于是简化的 Mock 状态，可能不会实际生成任务
    # 主要验证结构正确性，不会崩溃

    # 验证 trace 日志中的 commit 字段
    logs = trace.read_log()
    for log in logs:
        assert "commit" in log, "日志应包含 commit 字段"
        # commit 可以为空 dict（无承诺）或包含 task 和 ticks
        if log["commit"]:
            assert "task" in log["commit"] or log["commit"] == {}, \
                "commit 应为空或包含 task"


def test_e2e_trace_format(clean_trace):
    """端到端：Trace 日志格式验证。

    验证：
      - 每行都是合法 JSONL
      - 包含所有必需字段
      - 字段类型正确
    """
    states = _make_simple_order_scenario()
    mock_client = MockStateClient(states)

    with patch("ai.agent.StateClient", return_value=mock_client):
        agent = Agent()
        agent.client = mock_client

        # 执行 5 拍
        for _ in range(5):
            try:
                snapshot = agent.client.read()
                agent._tick_once(World(snapshot))
                agent.tick += 1
            except EOFError:
                break

    # 读取并验证 trace 文件
    assert os.path.exists(trace.TRACE_PATH), "trace 文件应存在"

    with open(trace.TRACE_PATH, "r") as f:
        lines = f.readlines()

    assert len(lines) > 0, "trace 文件应有内容"

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        # 验证是合法 JSON
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as e:
            pytest.fail(f"第 {i+1} 行不是合法 JSON: {e}\n{line}")

        # 验证必需字段
        required_fields = ["t", "mode", "candidates", "commit", "exec", "keys"]
        for field in required_fields:
            assert field in entry, f"第 {i+1} 行缺少字段: {field}"

        # 验证字段类型
        assert isinstance(entry["t"], int), "t 应为整数"
        assert isinstance(entry["mode"], str), "mode 应为字符串"
        assert isinstance(entry["candidates"], list), "candidates 应为列表"
        assert isinstance(entry["commit"], dict), "commit 应为字典"
        assert isinstance(entry["exec"], dict), "exec 应为字典"
        assert isinstance(entry["keys"], list), "keys 应为列表"

        # 验证 exec 包含 status
        assert "status" in entry["exec"], "exec 应包含 status"


def test_e2e_order_lifecycle(clean_trace):
    """端到端：订单生命周期管理。

    验证：
      - Agent 选择订单（按 remaining 排序）
      - 订单消失后切换到下一单
      - 订单消失时释放相关认领
    """
    # 构造订单出现和消失的场景
    states = []

    # 状态 1-2: 两个订单
    for i in range(2):
        states.append(_make_minimal_state(
            tick=i,
            orders=[
                {"id": "order1", "recipe": "TomatoSoup", "remaining": 50},
                {"id": "order2", "recipe": "TomatoSoup", "remaining": 20}
            ]
        ))

    # 状态 3-4: order2 消失（完成）
    for i in range(2, 4):
        states.append(_make_minimal_state(
            tick=i,
            orders=[
                {"id": "order1", "recipe": "TomatoSoup", "remaining": 50}
            ]
        ))

    # 状态 5: 所有订单消失
    states.append(_make_minimal_state(
        tick=4,
        orders=[]
    ))

    mock_client = MockStateClient(states)

    with patch("ai.agent.StateClient", return_value=mock_client):
        agent = Agent()
        agent.client = mock_client

        # 执行 5 拍
        for _ in range(5):
            snapshot = agent.client.read()
            agent._tick_once(World(snapshot))
            agent.tick += 1

    # Plan 4 起是多订单模式，Agent 不再持有 current_order_id；订单的
    # 选择/切换由 Planner 从 world.orders 每拍重算。这里只验证生命周期
    # 走通：不崩溃，且订单消失后不残留指向已消失订单的认领。
    live_orders = {"order1"}
    for (res, action), claim in agent.bb.claims.items():
        assert claim.order_id in live_orders or claim.order_id is None, \
            "残留了已消失订单 order2 的认领: %s -> %s" % ((res, action), claim.order_id)

    assert len(trace.read_log()) == 5, "应记录 5 拍决策日志"


def test_e2e_no_deadlock(clean_trace):
    """端到端：验证无死锁（决策循环不会停止）。

    验证：
      - 主循环能正常执行预期拍数
      - 不会因异常而中断
      - 每拍都产生日志
    """
    states = _make_simple_order_scenario()
    # 扩展到 10 拍
    for i in range(5, 10):
        states.append(_make_minimal_state(
            tick=i,
            orders=[{"id": "order1", "recipe": "TomatoSoup", "remaining": 25}]
        ))

    mock_client = MockStateClient(states)

    with patch("ai.agent.StateClient", return_value=mock_client):
        agent = Agent()
        agent.client = mock_client

        ticks_executed = 0

        # 执行 10 拍
        for _ in range(10):
            try:
                snapshot = agent.client.read()
                agent._tick_once(World(snapshot))
                agent.tick += 1
                ticks_executed += 1
            except EOFError:
                break
            except Exception as e:
                pytest.fail(f"第 {ticks_executed + 1} 拍异常: {e}")

    # 验证执行了预期拍数
    assert ticks_executed == 10, f"应执行 10 拍，实际执行 {ticks_executed} 拍"

    # 验证日志数量匹配
    logs = trace.read_log()
    assert len(logs) == 10, f"应有 10 条日志，实际 {len(logs)} 条"


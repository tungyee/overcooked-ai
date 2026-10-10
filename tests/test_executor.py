# -*- coding: utf-8 -*-
"""Executor 执行层单测。

重点验证：
  1. task 身份相同时复用树（进度保留）。
  2. task 身份变化时 halt 并重建。
  3. 模式抢占时 halt 并丢弃树。
  4. SUCCESS / FAILURE 正确返回。
  5. 超时自动转 FAILURE。
"""
import pytest
from ai.executor import Executor
from ai.bt import Node, Status, Sequence
from ai.blackboard import Blackboard


class MockTask:
    """模拟 Task 用于测试。"""

    def __init__(self, task_id, tree_builder):
        # type: (tuple, callable) -> None
        self._task_id = task_id
        self._tree_builder = tree_builder
        self.build_count = 0

    @property
    def key(self):
        # type: () -> tuple
        return self._task_id

    def build(self):
        # type: () -> Node
        """构建树，计数以便验证是否重建。

        Task.build() 不接 ctx（树跨拍复用，构造时状态第二拍就过期），
        测试里的 tree_builder 也一律忽略入参。
        """
        self.build_count += 1
        return self._tree_builder(None)


class CountingNode(Node):
    """计数节点：记录 tick 和 halt 调用次数。"""

    def __init__(self, status_sequence):
        # type: (list) -> None
        self.status_sequence = status_sequence
        self.tick_count = 0
        self.halt_count = 0
        self._index = 0

    def tick(self, ctx):
        # type: (dict) -> str
        self.tick_count += 1
        if self._index < len(self.status_sequence):
            s = self.status_sequence[self._index]
            self._index += 1
            return s
        return Status.SUCCESS

    def halt(self):
        # type: () -> None
        self.halt_count += 1
        self._index = 0


def _make_ctx(tick=0):
    # type: (int) -> dict
    """构造测试用上下文。"""
    bb = Blackboard()
    return {
        "bb": bb,
        "tick": tick,
        "hands": None,
        "world": None,
    }


def test_executor_reuses_tree_when_task_identity_unchanged():
    """task 身份不变 → 复用树，进度保留。"""
    executor = Executor()

    # 第一次 tick：树返回 RUNNING
    node = CountingNode([Status.RUNNING, Status.RUNNING, Status.SUCCESS])
    task = MockTask(("ChopTask", "order1", ("board1",), "Cucumber"), lambda ctx: node)
    ctx = _make_ctx(tick=0)

    status = executor.tick(task, ctx)
    assert status == Status.RUNNING
    assert task.build_count == 1  # 第一次构建
    assert node.tick_count == 1
    assert node.halt_count == 0

    # 第二次 tick：相同 task，复用树
    ctx = _make_ctx(tick=1)
    status = executor.tick(task, ctx)
    assert status == Status.RUNNING
    assert task.build_count == 1  # 没有重建
    assert node.tick_count == 2  # 进度保留，继续 tick

    # 第三次 tick：完成
    ctx = _make_ctx(tick=2)
    status = executor.tick(task, ctx)
    assert status == Status.SUCCESS
    assert task.build_count == 1
    assert node.tick_count == 3


def test_executor_rebuilds_tree_when_task_identity_changes():
    """task 身份变化 → halt 旧树，构建新树。"""
    executor = Executor()

    # 第一个 task
    node1 = CountingNode([Status.RUNNING])
    task1 = MockTask(("ChopTask", "order1", ("board1",), "Cucumber"), lambda ctx: node1)
    ctx = _make_ctx(tick=0)

    status = executor.tick(task1, ctx)
    assert status == Status.RUNNING
    assert task1.build_count == 1
    assert node1.tick_count == 1
    assert node1.halt_count == 0

    # 第二个 task：身份不同
    node2 = CountingNode([Status.SUCCESS])
    task2 = MockTask(("ChopTask", "order2", ("board2",), "Tomato"), lambda ctx: node2)
    ctx = _make_ctx(tick=1)

    status = executor.tick(task2, ctx)
    assert status == Status.SUCCESS
    assert task2.build_count == 1  # 新树构建
    assert node1.halt_count == 1  # 旧树被 halt
    assert node2.tick_count == 1


def test_executor_halts_tree_on_mode_preemption():
    """模式被抢占 → halt 并丢弃树。"""
    executor = Executor()

    # 启动一个 task
    node = CountingNode([Status.RUNNING])
    task = MockTask(("FetchTask", "order1", ("pot1",), "Rice"), lambda ctx: node)
    ctx = _make_ctx(tick=0)

    status = executor.tick(task, ctx)
    assert status == Status.RUNNING
    assert node.tick_count == 1

    # 模式抢占：调用 halt_and_discard
    executor.halt_and_discard()
    assert node.halt_count == 1
    assert executor.active_tree is None
    assert executor.active_task_id is None
    assert executor.task_start_tick is None


def test_executor_returns_success_when_tree_completes():
    """树跑完 → 返回 SUCCESS。"""
    executor = Executor()

    node = CountingNode([Status.SUCCESS])
    task = MockTask(("DeliverTask", "order1", ("plate1",), None), lambda ctx: node)
    ctx = _make_ctx(tick=0)

    status = executor.tick(task, ctx)
    assert status == Status.SUCCESS
    assert node.tick_count == 1


def test_executor_returns_failure_on_timeout():
    """task 超时 → 返回 FAILURE（由 Timeout 装饰器处理）。"""
    executor = Executor()

    # 节点总是返回 RUNNING（永不完成）
    node = CountingNode([Status.RUNNING] * 400)
    task = MockTask(("StuckTask", "order1", (), None), lambda ctx: node)

    # 开始执行，跑 300 次应该都是 RUNNING
    ctx = _make_ctx(tick=0)
    for i in range(300):
        ctx = _make_ctx(tick=i)
        status = executor.tick(task, ctx)
        assert status == Status.RUNNING

    # 第 301 次应该超时（Timeout 装饰器的 max_ticks=300）
    ctx = _make_ctx(tick=300)
    status = executor.tick(task, ctx)
    assert status == Status.FAILURE


def test_executor_timeout_decorator_halts_on_timeout():
    """Timeout 装饰器超时时 halt 子树。"""
    from ai.primitives.stuck import Timeout

    # 创建一个会被 halt 的节点
    node = CountingNode([Status.RUNNING] * 100)
    timeout_node = Timeout(node, max_ticks=10)

    ctx = _make_ctx(tick=0)

    # tick 10 次应该都是 RUNNING
    for i in range(10):
        status = timeout_node.tick(ctx)
        assert status == Status.RUNNING

    # 第 11 次超时，应该 halt 并返回 FAILURE
    assert node.halt_count == 0
    status = timeout_node.tick(ctx)
    assert status == Status.FAILURE
    assert node.halt_count == 1


def test_executor_handles_task_switch_mid_execution():
    """执行中切换 task：旧树被 halt，新树从头开始。"""
    executor = Executor()

    # 第一个 task 跑了几拍
    node1 = CountingNode([Status.RUNNING, Status.RUNNING, Status.SUCCESS])
    task1 = MockTask(("TaskA", None, (), None), lambda ctx: node1)
    ctx = _make_ctx(tick=0)

    executor.tick(task1, ctx)
    ctx = _make_ctx(tick=1)
    executor.tick(task1, ctx)
    assert node1.tick_count == 2

    # 切换到第二个 task
    node2 = CountingNode([Status.SUCCESS])
    task2 = MockTask(("TaskB", None, (), None), lambda ctx: node2)
    ctx = _make_ctx(tick=2)

    status = executor.tick(task2, ctx)
    assert status == Status.SUCCESS
    assert node1.halt_count == 1  # 旧树被 halt
    assert node2.tick_count == 1  # 新树从头开始


def test_executor_preserves_progress_across_multiple_ticks():
    """多拍执行保留进度（模拟真实 task 的多步序列）。"""
    executor = Executor()

    # 模拟一个 3 步 task：每步需要 2 拍
    statuses = [
        Status.RUNNING, Status.RUNNING,  # 步骤 1
        Status.RUNNING, Status.RUNNING,  # 步骤 2
        Status.RUNNING, Status.SUCCESS,  # 步骤 3
    ]
    node = CountingNode(statuses)
    task = MockTask(("MultiStepTask", "order1", (), None), lambda ctx: node)

    # 连续 tick 6 次
    for i in range(6):
        ctx = _make_ctx(tick=i)
        status = executor.tick(task, ctx)
        if i < 5:
            assert status == Status.RUNNING
        else:
            assert status == Status.SUCCESS

    assert node.tick_count == 6
    assert task.build_count == 1  # 只构建一次


def test_executor_resets_timeout_counter_on_task_change():
    """切换 task 时重置超时计数器。"""
    executor = Executor()

    # 第一个 task 跑了 250 拍（接近超时但未超时）
    node1 = CountingNode([Status.RUNNING] * 500)
    task1 = MockTask(("TaskA", None, (), None), lambda ctx: node1)

    for i in range(250):
        ctx = _make_ctx(tick=i)
        status = executor.tick(task1, ctx)
        assert status == Status.RUNNING

    # 切换到第二个 task：Timeout 装饰器重新创建，计数器重置
    node2 = CountingNode([Status.RUNNING] * 500)
    task2 = MockTask(("TaskB", None, (), None), lambda ctx: node2)

    # task2 应该能再跑 300 拍才超时
    for i in range(300):
        ctx = _make_ctx(tick=250 + i)
        status = executor.tick(task2, ctx)
        assert status == Status.RUNNING

    # 第 301 次 tick 应该超时
    ctx = _make_ctx(tick=550)
    status = executor.tick(task2, ctx)
    assert status == Status.FAILURE

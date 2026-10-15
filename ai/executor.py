# -*- coding: utf-8 -*-
"""执行层包装（Executor）。

职责：
  管理 task 树的生命周期（复用 / 重建 / halt），每拍 tick 树产出按键。

与 oc/ai/ 的差异：
  - 旧实现：每个 Action 类内部手搓字符串状态机。
  - 新实现：用 bt.Sequence 管理顺序，Executor 只负责树的创建 / tick / 销毁。

Spec: §7 Executor、§7.6 树的生命周期
"""
from typing import Optional

from ai.bt import Node, Status
from ai.primitives.stuck import Timeout

# task 级超时（拍数）。20 Hz 下 300 拍 = 15 秒，足够完成任何正常 task。
# 超过说明卡住了，交还 Planner 重算（spec §7.2）。
MAX_TASK_TICKS = 300


class Executor:
    """执行层：管理 task 树的生命周期。

    每拍流程：
      1. 检查 task 身份是否变化（与上拍比较四元组）
      2. 身份变化 → halt 旧树并构建新树
      3. 身份不变 → 复用树（进度保留）
      4. tick 树，返回 Status（SUCCESS / FAILURE / RUNNING）
      5. 检查超时，超时 → 返回 FAILURE

    树的生命周期（spec §7.6）：
      - 相同 task → 复用 active_tree（跨拍对象，进度保留）
      - 不同 task → halt 旧树并丢弃，按新 task 构建新树
      - 模式抢占 → halt_and_discard() 丢弃树
    """

    def __init__(self):
        # type: () -> None
        self.active_tree = None  # type: Optional[Node]
        self.active_task_id = None  # type: Optional[tuple]
        self.active_task = None  # type: Optional[object]  当前树对应的 task
        self.task_start_tick = None  # type: Optional[int]
        self.baseline = None  # 出餐基线（建树时抓一次，DeliverPlate.done 用）

    def tick(self, task, ctx):
        # type: (object, dict) -> str
        """执行一拍。

        参数:
          task: Task 对象，有 key 属性（四元身份）和 build() 方法
          ctx: 执行上下文（w / chef / hands / bb / field / book / tick）

        返回: Status.SUCCESS / Status.FAILURE / Status.RUNNING

        超时由 Timeout 装饰器处理，不需要额外检查。
        """
        current_tick = ctx.get("tick", 0)
        task_id = task.key  # (TYPE, order_id, resources, item)

        # ① 检查 task 身份是否变化
        if task_id != self.active_task_id:
            self._rebuild_tree(task, ctx, current_tick)

        # 出餐基线：建树那拍抓一次，之后每拍塞回 ctx。DeliverPlate.done
        # 的两个信号都是"相对上一拍的变化"，基线必须固定为建树时那份。
        if self.baseline is not None:
            ctx["baseline"] = self.baseline

        # ② tick 树（Timeout 装饰器内部处理超时）
        status = self.active_tree.tick(ctx)

        if status != Status.RUNNING:
            # 终态：这棵树到此为止。留着它有两个坏处：
            #   1. 同一个 task 下一拍被重选时复用旧树，而树在 SUCCESS 后已把
            #      进度复位、且它所有 expect 都已成立 → 原地立刻再报一次
            #      SUCCESS，"什么都没做却一直成功"；
            #   2. Agent ⑨.5 靠 active_task 判断"这棵树还差一口气"，已终态的
            #      树不该再享受那个保护。
            self.active_tree = None
            self.active_task_id = None
            self.active_task = None
            self.task_start_tick = None
        return status

    def _rebuild_tree(self, task, ctx, current_tick):
        # type: (object, dict, int) -> None
        """halt 旧树并构建新树。

        每次重建都用 Timeout 装饰（spec §7.2 结构性安全网）：
        任何 task 异常久 → FAILURE → Planner 重算，防死循环的最后一道闸。
        """
        # halt 旧树（释放按键等资源）
        if self.active_tree:
            self.active_tree.halt()

        # 构建新树并装饰 Timeout。task.build() 不接 ctx——树跨拍复用，
        # 构造时绑定的每拍状态第二拍就过期；原语在 tick(ctx) 时现取。
        raw_tree = task.build()
        # 超时上限可由 task 自己声明：默认 MAX_TASK_TICKS，长差事（灭火
        # 要先端锅、再取灭火器，两趟）用 task.TIMEOUT_TICKS 调高。
        cap = getattr(task, "TIMEOUT_TICKS", MAX_TASK_TICKS)
        self.active_tree = Timeout(raw_tree, cap)
        self.active_task_id = task.key
        # 留住 Task 对象本身：Agent 要靠它判断"这棵树的后置条件是否已经
        # 成立"，从而决定这一拍能不能让 planner 换任务（见 agent ⑨.5）。
        self.active_task = task
        self.task_start_tick = current_tick

        # 抓出餐基线（DeliverPlate.done 的双信号之一）
        from ai.tasks.assembly import delivery_baseline_from
        self.baseline = delivery_baseline_from(ctx.get("w"))

    def halt_and_discard(self):
        # type: () -> None
        """被模式抢占时：halt 并丢弃树。

        场景：Normal 模式执行 Chop task 时，灶台着火，Arbiter 切到 Firefighting。
        此时需要 halt 当前树（释放移动按键）并清空状态，让 Firefighting 从头开始。
        """
        if self.active_tree:
            self.active_tree.halt()
        self.active_tree = None
        self.active_task_id = None
        self.active_task = None
        self.task_start_tick = None

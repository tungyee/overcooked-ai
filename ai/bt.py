"""行为树引擎：Node / Sequence / Selector / Action / Condition，支持 RUNNING 与 halt。

从 oc/ai/bt.py 移植。**唯一的语义改动：Sequence / Selector 改为"有记忆"**
（记当前 RUNNING 的子节点，已 SUCCESS 的子节点下一拍不再重 tick）。这是
spec §7.1 / §7.6 要求的语义：

  - §7.1："`self.sub` 字符串状态机整体删除，**顺序状态由 BT 的 RUNNING /
    halt 机制承载**"；
  - §7.6："task 身份与上拍相同 → 复用同一棵树（**进度自然保留**）"。

为什么必须改：legacy 的每个 action 自带一个 `sub` 字符串状态机记住"做到
哪一步"，所以引擎无记忆也无妨；新架构把 `sub` 删了（spec §7.1），进度就
只能由引擎承载。旧的无记忆语义把"我这一步做完没"的责任推给了每个原语
（靠 `expect` 短路 / 边沿触发），一旦某原语没有这种保护（如
`EnsureHandsFree`），重 tick 就会重复副作用——实机踩到：把它放到树首清手，
后面步骤把锅端起来，重 tick 又把它放下。

语义：
- Sequence（有记忆）：从当前子节点起依次 tick。
  RUNNING → 记住它并返回；SUCCESS → **前进**到下一个（子节点自己完成，
  不 halt，避免 spurious halt）；FAILURE → 回到起点并返回。
  全部 SUCCESS → 回到起点并返回 SUCCESS。
- Selector（有记忆）：从当前子节点起依次 tick。
  RUNNING → 记住并返回；FAILURE → 前进；SUCCESS → 回到起点并返回。
- Action / Condition：同前。
- halt：沿当前 RUNNING 子节点向下传播（按键释放依赖它），并把进度复位。

⚠️ 有记忆后，已 SUCCESS 的子节点在本趟不再被 tick，但原语仍需自己边沿
触发（见 primitives.Tap），以应对"同一原语被重新激活"的情形——例如
Selector 在候选失败后会从头再来、Sequence 整体成功后若被重 tick 会重跑。
"""


class Status(object):
    SUCCESS = "success"
    FAILURE = "failure"
    RUNNING = "running"


class Node(object):
    """行为树节点基类。"""

    def tick(self, ctx):
        # type: (dict) -> str
        """每帧执行一次，返回 Status 之一。ctx 是黑板（共享上下文）。"""
        raise NotImplementedError

    def halt(self):
        """RUNNING 节点被抢占/放弃时调用，用于释放资源（如按键）。"""
        pass


class Sequence(Node):
    """有记忆的顺序节点：已 SUCCESS 的子节点不重 tick（spec §7.1 / §7.6）。"""

    def __init__(self, children):
        # type: (list) -> None
        self.children = children
        self._i = 0            # 下一个要 tick 的子节点下标
        self._running = None   # 当前 RUNNING 的子节点（halt 用）

    def tick(self, ctx):
        # type: (dict) -> str
        while self._i < len(self.children):
            c = self.children[self._i]
            s = c.tick(ctx)
            if s == Status.RUNNING:
                self._running = c
                return Status.RUNNING
            if s == Status.FAILURE:
                self._running = None
                self._i = 0
                return Status.FAILURE
            # SUCCESS → 前进到下一个（不 halt：子节点自己完成，不是被抢占）
            self._running = None
            self._i += 1
        self._i = 0
        self._running = None
        return Status.SUCCESS

    def halt(self):
        if self._running is not None:
            self._running.halt()
            self._running = None
        self._i = 0


class Selector(Node):
    """有记忆的选择节点：RUNNING 的候选不重评估（spec §7.1 / §7.6）。"""

    def __init__(self, children):
        # type: (list) -> None
        self.children = children
        self._i = 0
        self._running = None

    def tick(self, ctx):
        # type: (dict) -> str
        while self._i < len(self.children):
            c = self.children[self._i]
            s = c.tick(ctx)
            if s == Status.RUNNING:
                self._running = c
                return Status.RUNNING
            if s == Status.SUCCESS:
                self._running = None
                self._i = 0
                return Status.SUCCESS
            # FAILURE → 试下一个
            self._running = None
            self._i += 1
        self._i = 0
        self._running = None
        return Status.FAILURE

    def halt(self):
        if self._running is not None:
            self._running.halt()
            self._running = None
        self._i = 0


class Action(Node):
    """执行函数，返回其结果（SUCCESS / FAILURE / RUNNING）。"""

    def __init__(self, fn):
        # type: (callable) -> None
        self.fn = fn

    def tick(self, ctx):
        # type: (dict) -> str
        return self.fn(ctx)


class Condition(Node):
    """执行谓词函数，返回 SUCCESS（真）或 FAILURE（假）。"""

    def __init__(self, fn):
        # type: (callable) -> None
        self.fn = fn

    def tick(self, ctx):
        # type: (dict) -> str
        return Status.SUCCESS if self.fn(ctx) else Status.FAILURE

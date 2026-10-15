"""Blackboard：唯一的可变状态容器，取代散落在 Agent 上的约 20 个跨拍字段。

写入权受控（spec §4.5）：
  Planner  → claims、commitment
  P1Model  → p1_model
  Executor → commitment.steps_done / retries、scratch（**不可写 claims**）

认领键是 (resource_id, action) 动作级粒度（spec §4.0）。按资源粒度会让
接力协作废掉：P1 往锅里放了生饭，若整锅标为"P1 占用"，AI 就不会去取
熟饭——而取熟饭恰恰是最有价值的接力。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

DEFAULT_TTL = 100          # 拍。20 Hz 下约 5 秒
WAIT_GIVEUP_TICKS = 60     # 等待超时让出（spec §4.3 规则 2）


@dataclass
class Claim:
    owner: str
    order_id: Any
    claimed_tick: int
    ttl: int = DEFAULT_TTL
    contents_expected: Any = None   # 盘/锅的预期内容，对账用（spec §4.1 触发点 3）
    missing_streak: int = 0         # 连续找不到的拍数，一拍宽限用（spec §4.4）
    # 这条认领在做"哪一样"熟料。recipe_graph 的在途抑制要按熟料区分：一单
    # 要两份熟料时，第一份下锅不该把第二份的候选也压掉（第 1 组 1.3）。
    # None 表示未记录（老认领），抑制退化回整单作用域。
    item: Any = None

    def is_expired(self, tick):
        # type: (int) -> bool
        return tick - self.claimed_tick > self.ttl


@dataclass
class Commitment:
    """当前承诺，防抖（spec §6.5）。"""
    active_task: Any = None
    started_tick: int = 0
    steps_done: int = 0
    infeasible_streak: int = 0
    retries: int = 0
    interrupt_failure_count: int = 0  # 中断 task 连续失败计数（Arbiter 用）
    target_resource_id: Any = None    # 当前目标资源 ID（用于失败降级拉黑）


@dataclass
class P1Model:
    """P1 推断（spec §6.7）。由 P1Model 层写，其他层只读。"""
    holding: Optional[str] = None
    p1_active_task: Any = None
    confidence: float = 0.0
    last_evidence_tick: int = 0


@dataclass
class Intents:
    """意图注入口（对话层预留，本轮为空壳）。"""
    assignments: List[Any] = field(default_factory=list)
    collab_mode: str = "relay"
    role_constraint: Any = None
    aggressiveness: float = 0.5


@dataclass
class StuckScratch:
    """脱困模式局部状态。模式进入时构造、退出即丢弃。

    bad_cells 的值是失效拍号（不是秒），混用两套时基会让测试必须 sleep
    （spec §3.4"拍统一指 Python 循环拍（50 ms）"）。
    """
    bad_cells: Dict[Tuple[int, int], int] = field(default_factory=dict)
    nudge_dir: Any = None
    last_cell: Optional[Tuple[int, int]] = None
    same_count: int = 0
    skip_ticks: int = 0
    # L3 放弃的目标 → 失效拍号（spec §7.2 的 AbandonTarget）。
    # 不直接清 bb.commitment：§4.5 规定 Executor 对 commitment 只有
    # steps_done / retries 的写权，Planner 在可行性过滤时读这里。
    abandoned: Dict[Any, int] = field(default_factory=dict)


@dataclass
class FireScratch:
    """灭火模式局部状态。模式进入时构造、退出即丢弃。"""
    ext_target: Any = None
    bad_ext: Set[Any] = field(default_factory=set)
    tries: int = 0


@dataclass
class Blackboard:
    claims: Dict[Tuple[Any, str], Claim] = field(default_factory=dict)
    waiters: Dict[Any, List[Tuple[Any, int]]] = field(default_factory=dict)
    order_assignment: Dict[Any, Optional[str]] = field(default_factory=dict)
    commitment: Commitment = field(default_factory=Commitment)
    p1_model: P1Model = field(default_factory=P1Model)
    intents: Intents = field(default_factory=Intents)
    scratch: Any = None
    tick: int = 0
    mode: str = "Normal"  # 当前模式（Arbiter 写入）
    blacklisted_resources: Dict[Any, int] = field(default_factory=dict)  # 拉黑资源 → 失效拍号
    paused_task: Optional[Dict] = None  # 暂停的任务 {"task_id": str, "steps_done": int}
    # 来不及做完的订单 id → 失效拍号（第 2 组 2.3）。值是拍号而非布尔，因为
    # slack 在 0 附近每拍抖动，判死必须有 TTL 迟滞、否则候选集会 flap。
    #
    # 放在 Blackboard 而**不是** StuckScratch：scratch 是「模式进入时构造、
    # 退出即丢弃」的局部状态，每次进出灭火/脱困都会被整体替换（arbiter.py
    # 两处、agent.py 一处、primitives/stuck.py 一处都会 new 一个）。判死必须
    # 跨模式存活——灭个火回来发现所有单又都"有救"了，这道闸就等于没有。
    doomed_orders: Dict[Any, int] = field(default_factory=dict)
    # 判死的**入口**确认计数：order id → 连续算出必死的拍数。攒够
    # planner.DOOM_CONFIRM_TICKS 才真的写进 doomed_orders。入口无迟滞而出口
    # 200 拍是不对称的，一次抖动就能把候选打翻（见 planner 的常数注释）。
    doom_streak: Dict[Any, int] = field(default_factory=dict)

    # ---------- 认领 ----------
    def claim(self, resource_id, action, owner, order_id, tick,
              ttl=DEFAULT_TTL, contents_expected=None, item=None):
        # type: (Any, str, str, Any, int, int, Any, Any) -> None
        self.claims[(resource_id, action)] = Claim(
            owner, order_id, tick, ttl, contents_expected, item=item
        )

    def release(self, resource_id, action):
        # type: (Any, str) -> None
        """释放。幂等——spec §4.1 的四个触发点可能重复命中同一个键。"""
        self.claims.pop((resource_id, action), None)

    def claim_of(self, resource_id, action):
        # type: (Any, str) -> Optional[Claim]
        return self.claims.get((resource_id, action))

    def is_claimed_by_other(self, resource_id, action, owner, tick):
        # type: (Any, str, str, int) -> bool
        """spec §4.2 的全局可行性前置用这个。过期认领不算阻塞。"""
        c = self.claims.get((resource_id, action))
        if c is None:
            return False
        if c.owner == owner:
            return False
        return not c.is_expired(tick)

    def release_order(self, order_id):
        # type: (Any) -> int
        """订单消失 → 该单所有认领释放（spec §4.1 触发点 2）。返回释放数。"""
        keys = [k for k, c in self.claims.items() if c.order_id == order_id]
        for k in keys:
            del self.claims[k]
        return len(keys)

    def release_expired(self, tick):
        # type: (int) -> int
        """TTL 兜底扫描（spec §4.1 触发点 4）。返回释放数。"""
        keys = [k for k, c in self.claims.items() if c.is_expired(tick)]
        for k in keys:
            del self.claims[k]
        return len(keys)

    def claims_of_owner(self, owner):
        # type: (str) -> List[Tuple[Any, str]]
        return [k for k, c in self.claims.items() if c.owner == owner]

    # ---------- 等待队列（spec §4.3）----------
    def add_waiter(self, resource_id, order_id, tick):
        # type: (Any, Any, int) -> None
        """登记等待。同一订单重复登记只保留首次时间（老化优先级要的是等待时长）。"""
        q = self.waiters.setdefault(resource_id, [])
        for oid, _ in q:
            if oid == order_id:
                return
        q.append((order_id, tick))

    def next_waiter(self, resource_id):
        # type: (Any) -> Optional[Tuple[Any, int]]
        """等得最久的那个优先（防饿死）。"""
        q = self.waiters.get(resource_id) or []
        if not q:
            return None
        return min(q, key=lambda e: e[1])

    def drop_waiter(self, resource_id, order_id):
        # type: (Any, Any) -> None
        q = self.waiters.get(resource_id)
        if not q:
            return
        self.waiters[resource_id] = [e for e in q if e[0] != order_id]

    def waited_ticks(self, resource_id, order_id, tick):
        # type: (Any, Any, int) -> Optional[int]
        """已等待拍数。未在队列里则 None。"""
        for oid, since in (self.waiters.get(resource_id) or []):
            if oid == order_id:
                return tick - since
        return None

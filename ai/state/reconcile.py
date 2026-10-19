"""认领对账（spec §4.6）：每拍用新快照校验认领。

这是"用户抢先出餐后 AI 自动改做下一单"的实现机制：订单被抢 → 认领失效
→ Planner 重分配。不需要在各处散落恢复逻辑。

ResourceView 把"世界里有什么"从 World 解耦出来，让对账可脱离游戏单测。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Tuple

MISSING_GRACE_TICKS = 1   # 一拍宽限（spec §4.4）：吸收盘被搬动时的瞬时缺失


@dataclass
class ResourceView:
    """对账所需的世界视图。由调用方从 World 构造。"""
    existing_ids: Set[Any] = field(default_factory=set)
    contents: Dict[Any, Any] = field(default_factory=dict)


@dataclass
class ReconcileReport:
    released_missing: List[Tuple[Any, str]] = field(default_factory=list)
    released_mismatch: List[Tuple[Any, str]] = field(default_factory=list)
    released_order_gone: List[Tuple[Any, str]] = field(default_factory=list)
    released_expired: List[Tuple[Any, str]] = field(default_factory=list)

    def total(self):
        # type: () -> int
        return (len(self.released_missing) + len(self.released_mismatch)
                + len(self.released_order_gone) + len(self.released_expired))


def reconcile_claims(view, bb, live_order_ids, tick):
    # type: (ResourceView, Any, Set[Any], int) -> ReconcileReport
    """校验全部认领，释放失效者。就地修改 bb，并返回报告供 trace 记录。

    四个释放触发点（spec §4.1）中的三个在这里（第四个"task 成功"由
    Planner 在 task 完成时调 bb.release 处理）：
      - 订单消失 → 全单释放
      - 资源消失（带一拍宽限）/ 内容不符 → 失效
      - TTL 兜底 → 强制释放
    """
    report = ReconcileReport()
    live_order_ids = set(live_order_ids or ())

    # 1) 订单消失。order_id 为 None 的认领（灭火、脱困）不参与。
    for key in list(bb.claims.keys()):
        c = bb.claims.get(key)
        if c is None or c.order_id is None:
            continue
        if c.order_id not in live_order_ids:
            del bb.claims[key]
            report.released_order_gone.append(key)

    # 2) 资源消失 / 内容不符
    for key in list(bb.claims.keys()):
        c = bb.claims.get(key)
        if c is None:
            continue
        resource_id = key[0]

        if resource_id not in view.existing_ids:
            c.missing_streak += 1
            if c.missing_streak > MISSING_GRACE_TICKS:
                del bb.claims[key]
                report.released_missing.append(key)
            continue

        c.missing_streak = 0
        if c.contents_expected is not None:
            actual = view.contents.get(resource_id)
            if actual is not None and actual != c.contents_expected:
                del bb.claims[key]
                report.released_mismatch.append(key)

    # 3) TTL 兜底
    for key in list(bb.claims.keys()):
        c = bb.claims.get(key)
        if c is not None and c.is_expired(tick):
            del bb.claims[key]
            report.released_expired.append(key)

    # 4) 顺手清理死订单的等待登记，否则 waiters 会无限增长
    for resource_id in list(bb.waiters.keys()):
        kept = [e for e in bb.waiters[resource_id] if e[0] in live_order_ids]
        if kept:
            bb.waiters[resource_id] = kept
        else:
            del bb.waiters[resource_id]

    return report

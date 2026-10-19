"""有界失败历史与动作级退避；订单和其他任务不能清掉同一故障。"""
from collections import Counter
from ai.state.blackboard import StuckScratch


def operation_key(task):
    # type: (object) -> tuple
    return (task.TYPE, task.resources, task.item)


class FailureHistory:
    def __init__(self, threshold=3, ttl=200):
        # type: (int, int) -> None
        self.threshold, self.ttl = threshold, ttl
        self.entries = {}

    def failed(self, task, tick):
        # type: (object, int) -> bool
        self.entries = {k: v for k, v in self.entries.items() if tick - v[1] < self.ttl}
        if getattr(task, 'NO_ABANDON', False):
            return False
        key = operation_key(task)
        count = self.entries.get(key, (0, tick))[0] + 1
        if count >= self.threshold:
            self.entries.pop(key, None)
            return True
        self.entries[key] = (count, tick)
        return False

    def succeeded(self, task):
        # type: (object) -> None
        self.entries.pop(operation_key(task), None)


def abandon_actions(bb, task, until):
    # type: (object, object, int) -> None
    if getattr(task, 'NO_ABANDON', False):
        return
    if not isinstance(bb.scratch, StuckScratch):
        bb.scratch = StuckScratch()
    actions = bb.scratch.abandoned_actions
    tick = getattr(bb, 'tick', 0)
    for key in list(actions):
        if actions[key] <= tick:
            del actions[key]
    for resource, action in task.claims():
        actions[(resource, action)] = until


def waiting_reason(candidates, orders):
    # type: (list, list) -> dict
    counts = Counter(c[4] for c in candidates if len(c) > 4 and not c[3])
    if counts['platform_moving']:
        reason = 'waiting_for_platform'
    elif counts['abandoned']:
        reason = 'recovery_cooldown'
    elif counts['unreachable'] or counts['unreachable_destination']:
        reason = 'waiting_for_route'
    elif not orders:
        reason = 'waiting_for_orders'
    else:
        reason = 'no_feasible_task'
    return dict(reason=reason, veto_counts=dict(counts))

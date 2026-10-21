"""站点交互的有限换位恢复：只封目标-站位，不把整条通道封死。"""
from ai.primitives.align import AlignToCell
from ai.primitives.base import target_cell
from ai.query import predicates as P

MAX_LOCAL_RETRIES = 2


class InteractionRecovery:
    def __init__(self):
        self.attempts = 0
        self.wait_frame = None

    def ready(self, ctx):
        if self.wait_frame is not None and ctx.get('frame_seq') == self.wait_frame:
            ctx['interaction'] = {'phase': 'wait_new_frame', 'attempt': self.attempts}
            return False
        self.wait_frame = None
        return True

    def retry(self, ctx, target, phase, reason=None, blacklist=True):
        """没有新观测编号时不能安全重按；正式运行最多换位两次。"""
        reason = reason or (ctx.get('motion_failure') or {}).get('reason') or 'stage_failed'
        cell = P.chef_cell(ctx['chef'], ctx['w'].to_grid)
        event = {'phase': phase, 'reason': reason, 'attempt': self.attempts,
                 'resource': P.resource_id(target), 'cell': cell}
        ctx['interaction_failure'] = event
        if (ctx.get('frame_seq') is None
                or not target or not target.get('n') or cell is None):
            return False
        if blacklist:
            AlignToCell._blacklist_pair(ctx, target, cell)
        if self.attempts >= MAX_LOCAL_RETRIES:
            return False
        self.attempts += 1
        self.wait_frame = ctx['frame_seq']
        ctx['interaction_recovery'] = dict(event, attempt=self.attempts,
                                           target=target_cell(target, ctx))
        ctx.pop('motion_failure', None)
        return True

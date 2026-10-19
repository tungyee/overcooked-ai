import pytest

from ai.state.blackboard import (
    DEFAULT_TTL,
    Blackboard,
    FireScratch,
    StuckScratch,
)


def test_claim_is_action_scoped_not_resource_scoped():
    """spec §4.0：锁住 fetch_to 不应锁住 take_cooked，否则接力废掉。"""
    bb = Blackboard()
    bb.claim("cooker1", "fetch_to", owner="P1", order_id="o1", tick=0)

    assert bb.is_claimed_by_other("cooker1", "fetch_to", owner="P2", tick=1) is True
    assert bb.is_claimed_by_other("cooker1", "take_cooked", owner="P2", tick=1) is False


def test_own_claim_is_not_blocking():
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)

    assert bb.is_claimed_by_other("plateA", "fill", owner="P2", tick=1) is False


def test_release_removes_claim():
    bb = Blackboard()
    bb.claim("board1", "chop", owner="P2", order_id="o1", tick=0)
    bb.release("board1", "chop")

    assert bb.claim_of("board1", "chop") is None


def test_release_is_idempotent():
    bb = Blackboard()
    bb.release("nope", "chop")  # 不应抛异常

    assert bb.claim_of("nope", "chop") is None


def test_release_order_frees_all_claims_of_that_order():
    """spec §4.1 触发点 2：订单消失（送达/超时/被 P1 抢先）→ 全部释放。"""
    bb = Blackboard()
    bb.claim("cooker1", "fetch_to", owner="P2", order_id="o1", tick=0)
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)
    bb.claim("board1", "chop", owner="P2", order_id="o2", tick=0)

    freed = bb.release_order("o1")

    assert freed == 2
    assert bb.claim_of("cooker1", "fetch_to") is None
    assert bb.claim_of("plateA", "fill") is None
    assert bb.claim_of("board1", "chop") is not None


def test_expired_claim_does_not_block():
    """spec §4.1 触发点 4：TTL 是防认领泄漏的最后一道闸。"""
    bb = Blackboard()
    bb.claim("cooker1", "take_cooked", owner="P1", order_id="o1", tick=0, ttl=10)

    assert bb.is_claimed_by_other("cooker1", "take_cooked", owner="P2", tick=5) is True
    assert bb.is_claimed_by_other("cooker1", "take_cooked", owner="P2", tick=11) is False


def test_release_expired_sweeps_stale_claims():
    bb = Blackboard()
    bb.claim("a", "act", owner="P2", order_id="o1", tick=0, ttl=10)
    bb.claim("b", "act", owner="P2", order_id="o1", tick=0, ttl=100)

    swept = bb.release_expired(tick=50)

    assert swept == 1
    assert bb.claim_of("a", "act") is None
    assert bb.claim_of("b", "act") is not None


def test_default_ttl_applied_when_not_given():
    bb = Blackboard()
    bb.claim("a", "act", owner="P2", order_id="o1", tick=0)

    assert bb.claim_of("a", "act").ttl == DEFAULT_TTL


def test_claims_of_owner_lists_keys():
    bb = Blackboard()
    bb.claim("a", "act", owner="P2", order_id="o1", tick=0)
    bb.claim("b", "act", owner="P1", order_id="o2", tick=0)

    assert bb.claims_of_owner("P2") == [("a", "act")]


def test_waiters_are_ordered_by_wait_duration():
    """spec §4.3 规则 1：老化优先级，防饿死。"""
    bb = Blackboard()
    bb.add_waiter("cooker1", "o2", tick=10)
    bb.add_waiter("cooker1", "o3", tick=5)

    assert bb.next_waiter("cooker1") == ("o3", 5)


def test_add_waiter_is_idempotent_per_order():
    bb = Blackboard()
    bb.add_waiter("cooker1", "o2", tick=10)
    bb.add_waiter("cooker1", "o2", tick=20)

    assert bb.waited_ticks("cooker1", "o2", tick=30) == 20


def test_waited_ticks_none_when_not_waiting():
    bb = Blackboard()

    assert bb.waited_ticks("cooker1", "o2", tick=30) is None


def test_drop_waiter_removes_entry():
    bb = Blackboard()
    bb.add_waiter("cooker1", "o2", tick=10)
    bb.drop_waiter("cooker1", "o2")

    assert bb.next_waiter("cooker1") is None


def test_scratch_types_are_independent():
    """spec §4 行 154：禁止跨模式复用字段。"""
    stuck = StuckScratch()
    fire = FireScratch()

    assert not hasattr(stuck, "ext_target")
    assert not hasattr(fire, "nudge_dir")


def test_scratch_defaults_are_not_shared_between_instances():
    a = StuckScratch()
    b = StuckScratch()
    a.bad_cells[(1, 1)] = 5

    assert b.bad_cells == {}


def test_fire_scratch_defaults_are_not_shared():
    a = FireScratch()
    b = FireScratch()
    a.bad_ext.add("ext1")

    assert b.bad_ext == set()


def test_blackboard_fresh_has_empty_state():
    bb = Blackboard()

    assert bb.claims == {}
    assert bb.commitment.active_task is None
    assert bb.scratch is None


def test_stuck_scratch_has_bad_pairs():
    """(资源,格) 二元拉黑：格对别的目标可能是好的，资源对别的格也是。"""
    from ai.state.blackboard import StuckScratch
    s = StuckScratch()
    assert s.bad_pairs == {}
    s.bad_pairs[(("GridManager", "counter_01"), (1, -4))] = 300
    assert len(s.bad_pairs) == 1

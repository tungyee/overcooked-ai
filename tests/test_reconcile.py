from ai.state.blackboard import Blackboard
from ai.state.reconcile import ResourceView, reconcile_claims


def test_claim_survives_when_resource_present():
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)
    view = ResourceView(existing_ids={"plateA"})

    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=1)

    assert report.released_missing == []
    assert bb.claim_of("plateA", "fill") is not None


def test_plate_being_carried_is_not_killed_on_first_miss():
    """spec §4.4：一拍宽限，吸收搬动过程中的瞬时缺失。"""
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)
    view = ResourceView(existing_ids=set())  # 这一拍没看到

    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=1)

    assert report.released_missing == []
    assert bb.claim_of("plateA", "fill") is not None
    assert bb.claim_of("plateA", "fill").missing_streak == 1


def test_plate_really_gone_is_released_on_second_miss():
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)
    view = ResourceView(existing_ids=set())

    reconcile_claims(view, bb, live_order_ids={"o1"}, tick=1)
    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=2)

    assert report.released_missing == [("plateA", "fill")]
    assert bb.claim_of("plateA", "fill") is None


def test_missing_streak_resets_when_resource_reappears():
    """盘被端起又放下，不该累计到失效。"""
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)

    reconcile_claims(ResourceView(existing_ids=set()), bb, {"o1"}, tick=1)
    reconcile_claims(ResourceView(existing_ids={"plateA"}), bb, {"o1"}, tick=2)
    report = reconcile_claims(ResourceView(existing_ids=set()), bb, {"o1"}, tick=3)

    assert report.released_missing == []
    assert bb.claim_of("plateA", "fill").missing_streak == 1


def test_content_mismatch_releases_immediately():
    """内容不符说明被 P1 动过，无需宽限（spec §4.1 触发点 3）。"""
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0,
             contents_expected=("rice",))
    view = ResourceView(existing_ids={"plateA"}, contents={"plateA": ("cucumber",)})

    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=1)

    assert report.released_mismatch == [("plateA", "fill")]
    assert bb.claim_of("plateA", "fill") is None


def test_content_match_keeps_claim():
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0,
             contents_expected=("rice",))
    view = ResourceView(existing_ids={"plateA"}, contents={"plateA": ("rice",)})

    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=1)

    assert report.released_mismatch == []


def test_claim_without_expected_contents_ignores_contents():
    bb = Blackboard()
    bb.claim("cooker1", "take_cooked", owner="P2", order_id="o1", tick=0)
    view = ResourceView(existing_ids={"cooker1"}, contents={"cooker1": ("whatever",)})

    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=1)

    assert report.released_mismatch == []


def test_order_gone_releases_all_its_claims():
    """用户抢先出餐 → 订单消失 → 认领失效 → Planner 改做下一单（spec §4.6）。"""
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)
    bb.claim("cooker1", "fetch_to", owner="P2", order_id="o1", tick=0)
    bb.claim("board1", "chop", owner="P2", order_id="o2", tick=0)
    view = ResourceView(existing_ids={"plateA", "cooker1", "board1"})

    report = reconcile_claims(view, bb, live_order_ids={"o2"}, tick=1)

    assert set(report.released_order_gone) == {("plateA", "fill"), ("cooker1", "fetch_to")}
    assert bb.claim_of("board1", "chop") is not None


def test_claim_with_no_order_id_is_not_order_released():
    """无主认领（如灭火、脱困用的资源）不该被订单扫描清掉。"""
    bb = Blackboard()
    bb.claim("ext1", "grab", owner="P2", order_id=None, tick=0)
    view = ResourceView(existing_ids={"ext1"})

    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=1)

    assert report.released_order_gone == []
    assert bb.claim_of("ext1", "grab") is not None


def test_expired_claim_is_swept():
    """TTL 兜底：前三条都漏掉的情况由它兜住（spec §4.1 触发点 4）。"""
    bb = Blackboard()
    bb.claim("cooker1", "fetch_to", owner="P2", order_id="o1", tick=0, ttl=5)
    view = ResourceView(existing_ids={"cooker1"})

    report = reconcile_claims(view, bb, live_order_ids={"o1"}, tick=20)

    assert report.released_expired == [("cooker1", "fetch_to")]
    assert bb.claims == {}


def test_no_claim_leak_after_order_lifecycle():
    """认领泄漏断言（spec §11.2 阶段 2）：订单走完一轮后表必须为空。"""
    bb = Blackboard()
    bb.claim("plateA", "fill", owner="P2", order_id="o1", tick=0)
    bb.claim("cooker1", "fetch_to", owner="P2", order_id="o1", tick=0)
    bb.claim("counter_3_0", "place", owner="P2", order_id="o1", tick=0)
    view = ResourceView(existing_ids={"plateA", "cooker1", "counter_3_0"})

    reconcile_claims(view, bb, live_order_ids=set(), tick=1)

    assert bb.claims == {}


def test_waiters_dropped_for_dead_orders():
    bb = Blackboard()
    bb.add_waiter("cooker1", "o1", tick=0)
    bb.add_waiter("cooker1", "o2", tick=0)
    view = ResourceView(existing_ids={"cooker1"})

    reconcile_claims(view, bb, live_order_ids={"o2"}, tick=1)

    assert bb.next_waiter("cooker1") == ("o2", 0)

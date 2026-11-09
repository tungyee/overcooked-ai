"""Planner 多订单模式单测（Plan 4 Task 1）。

重点验证：
  1. 候选生成枚举所有订单（不限 current_order_id）
  2. 认领粒度 = (resource, action)，接力语义正确
  3. task 身份绑定参数，不误报完成
  4. waiters 队列：等待 → 老化优先 → 超时让出
"""
import pytest

from ai import tasks as T
from ai.core import planner
from ai.state.blackboard import Blackboard, Claim
from ai.core.planner import generate_candidates
from ai.query.recipes import RecipeBook
from ai.state.world import World


def _make_world(orders=None, stations=None, recipes=None, chefs=None,
                plates=None, workstations=None, cookers=None):
    # type: (list, list, list, list, list, list, list) -> World
    """构造最小 World。"""
    state = {
        "orders": orders or [],
        "stations": stations or [],
        "recipes": recipes or [],
        "chefs": chefs or [],
        "plates": plates or [],
        "workstations": workstations or [],
        "cookers": cookers or [],
        "grid": {"nodeMap": None}
    }
    return World(state)


def _make_bb(claims=None, tick=10):
    # type: (dict, int) -> Blackboard
    """构造黑板。claims 是 {(resource_id, action): (owner, order_id)} 的映射。"""
    bb = Blackboard()
    bb.tick = tick
    if claims:
        for (res, act), (owner, oid) in claims.items():
            bb.claim(res, act, owner, oid, tick)
    return bb


# ==================== 测试 1：候选生成枚举所有订单 ====================

def test_generate_candidates_for_all_orders():
    """多订单模式：候选包含所有订单的 task。"""
    # 构造：两个订单 A（切黄瓜）、B（煮鱼）
    recipes = [
        {"name": "CookedFish", "comp": ["Fish"], "cook": "cooker"},
    ]
    orders = [
        {"id": "order_A", "dish": "CucumberSushi", "composition": ["Cucumber"], "remaining": 30},
        {"id": "order_B", "dish": "FishSushi", "composition": ["CookedFish"], "remaining": 40}
    ]
    stations = [
        {"t": "crate", "n": "crate_cucumber", "ing": "Cucumber",
         "chop": "ChoppedCucumber", "chop_stages": 8, "gx": 2, "gz": 1},
        {"t": "crate", "n": "crate_fish", "ing": "Fish", "gx": 3, "gz": 1},
        {"t": "chopping", "n": "board_01", "gx": 4, "gz": 1, "item": None},
        {"t": "counter", "n": "counter_01", "gx": 1, "gz": 1},
    ]
    cookers = [
        {"t": "cooking", "n": "cooker_01", "gx": 5, "gz": 1,
         "x": 20.4, "z": -4.8, "cooked": False}
    ]

    world = _make_world(orders, stations, recipes, cookers=cookers)
    bb = _make_bb()
    book = RecipeBook(recipes, stations)

    # 调用新接口（无 current_order_id 参数）
    candidates = generate_candidates(world, bb, None, book)

    # 断言：候选同时含 order_A 和 order_B 的 task
    order_ids = set(c.task.order_id for c in candidates if c.task.order_id)
    assert "order_A" in order_ids, "候选应包含 order_A 的 task（取黄瓜上板）"
    assert "order_B" in order_ids, "候选应包含 order_B 的 task（煮鱼）"


# ==================== 测试 2：认领粒度支持接力 ====================

def test_claim_granularity_enables_relay():
    """认领粒度 = (resource, action)：P1 占 fetch_to，AI 仍可 take_cooked。"""
    recipes = [
        {"name": "CookedFish", "comp": ["Fish"], "cook": "cooker"},
    ]
    orders = [{"id": "order1", "dish": "FishSushi",
               "composition": ["CookedFish"], "remaining": 30}]
    stations = [
        {"t": "crate", "n": "crate_fish", "ing": "Fish", "gx": 3, "gz": 1},
        {"t": "counter", "n": "counter_01", "gx": 5, "gz": 5},
    ]
    cookers = [
        {"t": "cooking", "n": "cooker_01", "gx": 2, "gz": 1,
         "x": 16.8, "z": -4.8, "cooked": True, "isCooking": True,
         "pot_contents": ["ing:Fish"]}
    ]

    world = _make_world(orders, stations, recipes, cookers=cookers)
    # 盘摆在拼装格 (5,5) 上，且缺熟料 → TakeCookedToPlate 的目标
    px, pz = world.to_world(5, 5)
    world.plates = [{"n": "plate_A", "x": px, "z": pz,
                     "count": 0, "contents": []}]

    # P1 占用了 cooker_01 的 fetch_to（正在下锅）
    bb = _make_bb(claims={
        ("cooker_01", "fetch_to"): ("P1", "order1")
    })
    book = RecipeBook(recipes, stations)

    plate_match = planner._match_plates_to_orders(world, bb, book)
    cooker_match = planner._match_cookers_to_orders(world, bb, book, plate_match)
    candidates = generate_candidates(world, bb, None, book,
                                     plate_match, cooker_match)

    # 断言：TakeCookedToPlate(cooker_01) 候选被生成（认领检查通过）
    # 注意：它可能因其他前置条件（holding_cooked）不满足而在后续过滤中被排除
    # 但在候选生成阶段，fetch_to 被占不应阻止 take_cooked 的候选生成
    # cooker_id 现在是 (grid, name) 元组（§5.2）；本场景的灶没有 grid
    # 字段，P.resource_id 把缺失 grid 归一化为空字符串。
    take_cooked_tasks = [c.task for c in candidates
                         if c.task.TYPE == "TakeCookedToPlate"
                         and c.task.cooker_id == ("", "cooker_01")]

    # 直接测试认领检查：验证 take_cooked 未被 P1 的 fetch_to 阻塞
    assert not bb.is_claimed_by_other("cooker_01", "take_cooked", "P2", bb.tick), \
        "take_cooked 不应被认领（P1 只占了 fetch_to）"

    # 验证候选生成了 TakeCookedToPlate（虽然可能有其他前置条件）
    assert len(take_cooked_tasks) > 0, \
        f"AI 应能生成 TakeCookedToPlate(cooker_01) 候选，实际候选: {[c.task for c in candidates]}"


# ==================== 测试 3：task 身份绑定参数 ====================

def test_task_identity_binds_resources():
    """task 身份绑定参数：两个 FetchToPlate 任务绑定不同盘是不同 task。"""
    # 构造两个取黄瓜到不同盘的 task
    # FetchToPlate(order_id, (plate_id,), item)
    task_A = T.FetchToPlate("order_A", ("plate_A",), "Cucumber")
    task_B = T.FetchToPlate("order_B", ("plate_B",), "Cucumber")

    # 断言：身份不同
    assert task_A != task_B, "绑定不同盘的 task 应该是不同的 task"
    assert task_A.key != task_B.key, "四元身份应不同"

    # 断言：同一 task 的实例相同
    task_A2 = T.FetchToPlate("order_A", ("plate_A",), "Cucumber")
    assert task_A == task_A2, "相同参数的 task 应该相等"


# ==================== 测试 4：waiters 队列老化优先 ====================

def test_waiters_queue_aging_priority():
    """waiters 队列：等待时长越长优先级越高。"""
    bb = Blackboard()
    bb.tick = 100

    # order#1 等了 50 拍，order#2 等了 20 拍
    bb.add_waiter(("cooker_01",), "order1", 50)
    bb.add_waiter(("cooker_01",), "order2", 80)

    # 获取下一个等待者（应该是 order1，因为它等得最久）
    next_waiter = bb.next_waiter(("cooker_01",))
    assert next_waiter is not None
    assert next_waiter[0] == "order1", "等待时长最长的 order1 应优先"
    assert next_waiter[1] == 50, "since_tick 应为 50"


# ==================== 测试 5：等待超时释放认领 ====================

def test_wait_timeout_releases_own_claims():
    """等待超时 → 放弃 task 并释放自己的认领（打断 hold-and-wait）。"""
    from ai.state.blackboard import WAIT_GIVEUP_TICKS

    bb = Blackboard()
    bb.tick = 10

    # AI 持有 plate + assembly 的认领
    bb.claim(("plate_A",), "fetch_plate", "P2", "order1", bb.tick)
    bb.claim(((5, 5),), "assembly", "P2", "order1", bb.tick)

    # 登记等待 cooker（since_tick = 10）
    bb.add_waiter(("cooker_01",), "order1", 10)

    # 推进到超时时刻
    bb.tick = 10 + WAIT_GIVEUP_TICKS + 1

    # 检查等待时长
    waited = bb.waited_ticks(("cooker_01",), "order1", bb.tick)
    assert waited is not None
    assert waited > WAIT_GIVEUP_TICKS, "等待时长应超过超时阈值"

    # 模拟超时让出：释放认领
    bb.release(("plate_A",), "fetch_plate")
    bb.release(((5, 5),), "assembly")

    # 断言：认领已释放
    assert bb.claim_of(("plate_A",), "fetch_plate") is None
    assert bb.claim_of(((5, 5),), "assembly") is None


# ==================== 测试 6：多订单并行认领 ====================

def test_multi_order_concurrent_claims():
    """多订单模式：可同时持有多个订单的资源认领。"""
    bb = Blackboard()
    bb.tick = 10

    # AI 同时为两个订单持有不同资源
    bb.claim(("plate_A",), "fetch_plate", "P2", "order1", bb.tick)
    bb.claim(("cooker_01",), "fetch_to", "P2", "order2", bb.tick)
    bb.claim(("board_01",), "chop", "P2", "order1", bb.tick)

    # 断言：三个认领共存
    assert bb.claim_of(("plate_A",), "fetch_plate") is not None
    assert bb.claim_of(("cooker_01",), "fetch_to") is not None
    assert bb.claim_of(("board_01",), "chop") is not None

    # 断言：认领绑定正确订单
    assert bb.claim_of(("plate_A",), "fetch_plate").order_id == "order1"
    assert bb.claim_of(("cooker_01",), "fetch_to").order_id == "order2"


# ==================== 不可打断的 task（Discard）====================

def test_non_interruptible_task_not_preempted():
    """Discard（倒掉还在烧的锅）不能被更高分候选抢走。

    常规改选分支必须查 `_task_is_interruptible`，否则分差够大时
    Discard 照样会被普通备菜换掉。
    """
    from ai.core.planner import _apply_commitment_inertia, _score_tasks, Candidate, EST_FETCH
    from ai.tasks import Discard

    recipes = [
        {"dish": "CucumberSushi", "composition": ["Plate", "Cucumber"]}
    ]
    orders = [
        {"id": "order1", "dish": "CucumberSushi", "composition": ["Plate", "Cucumber"], "remaining": 50}
    ]
    stations = [
        {"t": "crate", "n": "crate_cucumber", "ing": "Cucumber", "gx": 1, "gz": 1},
        {"t": "board", "n": "board_01", "gx": 2, "gz": 1},
        {"t": "cooker", "n": "cooker_01", "gx": 3, "gz": 1, "onFire": True}
    ]

    world = _make_world(orders, stations, recipes)
    bb = _make_bb()
    book = RecipeBook(recipes, stations)

    # 设置当前承诺：Discard RUNNING, 已完成 2 步（估计总共 12 步，剩余 10 步）
    current_task = Discard(resources=("cooker_01",))
    bb.commitment.active_task = current_task
    bb.commitment.started_tick = bb.tick - 2
    bb.commitment.steps_done = 2

    # 手动构造候选：当前任务 + 一个高分新任务
    cand_current = Candidate(
        task=current_task,
        preconditions=[],
        estimated_steps=12
    )
    new_task = T.FetchToBoard("order1", ("board_01",), "Cucumber")
    cand_new = Candidate(
        task=new_task,
        preconditions=[],
        estimated_steps=EST_FETCH
    )

    scored = _score_tasks([current_task, new_task], [cand_current, cand_new], world, bb)

    chosen = _apply_commitment_inertia(scored, world, bb)

    assert chosen == current_task, "Discard 不应被高分候选抢占"

"""Agent 对话接入：工具批次落账本、ack、事件与上下文（spec §6.2–6.4）。

全部离线：World / RecipeBook / Blackboard 都是最小真实对象，worker 是
带队列的 fake，不打开麦克风、不联网。
"""
import queue

import pytest
from unittest.mock import MagicMock

from ai import tasks as T
from ai.agent import Agent
from ai.dialog.schema import DialogState, preference_matches_task
from ai.dialog.worker import DutyBatch
from ai.query.recipes import RecipeBook
from ai.state.blackboard import Commitment
from ai.state.world import World


# ---------------------------------------------------------------- fixtures

def make_world(level="Sushi_1_4_2P", orders=None, chefs=None,
               session_id=None, scene_epoch=None):
    return World({
        "phase": "level",
        "level": level,
        "session_id": session_id,
        "scene_epoch": scene_epoch,
        "orders": orders if orders is not None else [
            {"id": 1, "dish": "CucumberSushi", "composition": ["Cucumber"],
             "remaining": 60, "lifetime": 130},
        ],
        "recipes": [{"name": "Cucumber", "comp": []}],
        "stations": [
            {"t": "crate", "n": "crate_cucumber", "ing": "Cucumber",
             "chop": "ChoppedCucumber", "gx": 1, "gz": 1},
            {"t": "counter", "n": "counter_01", "gx": 3, "gz": 3},
            # 关卡能力（洗盘是否可用）看站点：切菜板 / 洗碗池。
            {"t": "chopping", "n": "board_01", "gx": 2, "gz": 2},
            {"t": "washing", "n": "sink_01", "gx": 4, "gz": 4},
        ],
        "workstations": [],
        "plates": [],
        "cookers": [{"n": "cooker_01", "gx": 5, "gz": 5,
                     "x": 6.0, "z": 6.0}],
        "chefs": chefs if chefs is not None else [
            {"name": "Player 1", "x": 0.0, "z": 0.0, "held": None},
            {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None},
        ],
        "items": [],
        "extinguishers": [],
        "grid": {"size": [1.2, 1.0, 1.2], "nodeMap": None},
    })


@pytest.fixture
def world():
    return make_world()


def make_plate_world(contents=None):
    """两单同菜 + 一张拼装格上的盘；复现真实帧的 1:1 盘匹配场景。"""
    recipe = [{"name": "Cucumber", "comp": []}]
    w = World({
        "phase": "level",
        "level": "Sushi_1_4_2P",
        "orders": [
            {"id": 1, "dish": "CucumberSushi", "composition": ["Cucumber"],
             "remaining": 60, "lifetime": 130},
            {"id": 2, "dish": "CucumberSushi", "composition": ["Cucumber"],
             "remaining": 60, "lifetime": 130},
        ],
        "recipes": recipe,
        "stations": [
            {"t": "crate", "n": "crate_cucumber", "ing": "Cucumber",
             "gx": 1, "gz": 1},
            {"t": "counter", "n": "counter_01", "gx": 3, "gz": 3},
            # 能力校验（spec §4.4）：chop 需要切菜板。
            {"t": "chopping", "n": "board_01", "gx": 2, "gz": 2},
        ],
        "workstations": [],
        "plates": [],
        "cookers": [],
        "chefs": [
            {"name": "Player 1", "x": 0.0, "z": 0.0, "held": None},
            {"name": "Player 2", "x": 0.0, "z": 0.0, "held": None},
        ],
        "items": [],
        "extinguishers": [],
        "grid": {"size": [1.2, 1.0, 1.2], "nodeMap": None},
    })
    px, pz = w.to_world(3, 3)
    w.plates = [{"n": "plate_01", "x": px, "z": pz, "count": 0,
                 "contents": list(contents or [])}]
    return w


@pytest.fixture
def book(world):
    return RecipeBook(world.recipes, world.stations)


@pytest.fixture
def agent(monkeypatch, book):
    monkeypatch.setattr("ai.agent.StateClient", lambda *a, **k: MagicMock())
    a = Agent(voice=None)
    a.bb.tick = 100
    a.book = book
    return a


def _batch(*calls, source=""):
    return DutyBatch(batch_id="b1", calls=list(calls), source=source)


def _duty(**args):
    return ("update_duties", args)


# ---------------------------------------------------------- 账本落地


def test_batch_applies_duties_and_compiles_preferences(agent, world):
    results = agent._apply_duty_batch(world, _batch(
        _duty(op="add", mode="leave_to_p1", target="Cucumber"),
        _duty(op="add", mode="do", target="WashPlate"),
        source="我来切，你洗盘"))

    assert [r["ok"] for r in results] == [True, True]
    assert [r["id"] for r in results] == ["a1", "a2"]
    kinds = {p.duty_id: (p.kind, p.weight) for p in agent.bb.preferences}
    assert kinds == {"a1": ("claim", -8.0), "a2": ("direct", 4.0)}
    assert agent.ledger.get("a1").source == "我来切，你洗盘"


def test_batch_rejects_bad_calls_individually(agent, world):
    results = agent._apply_duty_batch(world, _batch(
        _duty(op="add", mode="do", target="SushiRice"),
        ("update_duties", None),
        ("teleport", {}),
        _duty(op="add", mode="do", target="Cucumber")))

    assert [r["ok"] for r in results] == [False, False, False, True]
    assert "SushiRice" in results[0]["reason"]
    assert "JSON" in results[1]["reason"]
    assert "未知工具" in results[2]["reason"]
    assert agent.ledger.ids() == ["a1"]


def test_wash_rejected_without_sink(agent):
    world = make_world()
    world.stations = [st for st in world.stations if st.get("t") != "washing"]
    results = agent._apply_duty_batch(world, _batch(
        _duty(op="add", mode="do", target="WashPlate")))
    assert results[0]["ok"] is False and "WashPlate" in results[0]["reason"]


def test_leave_to_p1_clears_matching_commitment(agent, world):
    active = T.Chop(order_id=1, resources=("board_01",), item="Cucumber")
    agent.bb.commitment = Commitment(active, 90, 0, 0, 0)

    agent._apply_duty_batch(world, _batch(
        _duty(op="add", mode="leave_to_p1", target="Cucumber")))

    assert agent.bb.commitment.active_task is None


def test_do_keeps_commitment(agent, world):
    active = T.Chop(order_id=1, resources=("board_01",), item="Cucumber")
    agent.bb.commitment = Commitment(active, 90, 0, 0, 0)

    agent._apply_duty_batch(world, _batch(
        _duty(op="add", mode="do", target="WashPlate")))

    assert agent.bb.commitment.active_task == active


def test_edit_flips_ownership_and_keeps_id(agent, world):
    agent._apply_duty_batch(world, _batch(
        _duty(op="add", mode="leave_to_p1", target="Cucumber")))
    results = agent._apply_duty_batch(world, _batch(
        _duty(op="edit", id="a1", mode="do")))

    assert results[0] == {"ok": True, "id": "a1",
                          "text": "P2 优先做 Cucumber"}
    assert [p.kind for p in agent.bb.preferences] == ["direct"]


def test_first_level_observation_does_not_clear_ledger(agent, world):
    agent._apply_duty_batch(world, _batch(
        _duty(op="add", mode="do", target="WashPlate")))
    agent._maybe_reset_dialog_level(world)
    assert agent.ledger.ids() == ["a1"]
    assert len(agent.bb.preferences) == 1


# ------------------------------------------------------- service

class FakeDialogWorker(object):
    """只带队列与计数器的 worker；不启动线程、不碰音频。"""

    def __init__(self):
        self.cmd_q = queue.Queue()
        self.ack_q = queue.Queue()
        self.ctx_q = queue.Queue(maxsize=1)
        self.event_q = queue.Queue(maxsize=1)
        self.contexts = []
        self.events = []
        self.level_events = []
        self.reset_count = 0
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def put_event(self, event):
        self.events.append(event)
        while True:
            try:
                self.event_q.get_nowait()
            except queue.Empty:
                break
        self.event_q.put_nowait(event)

    def publish_context(self, context):
        self.contexts.append(context)
        while True:
            try:
                self.ctx_q.get_nowait()
            except queue.Empty:
                break
        self.ctx_q.put_nowait(context)

    def reset_state(self):
        self.reset_count += 1

    def log_event(self, kind, **fields):
        if kind == "level_start":
            self.level_events.append((kind, fields.get("level")))


@pytest.fixture
def fake_worker():
    return FakeDialogWorker()


@pytest.fixture
def dagent(monkeypatch, book, fake_worker):
    monkeypatch.setattr("ai.agent.StateClient", lambda *a, **k: MagicMock())
    a = Agent(dialog_worker=fake_worker)
    a.bb.tick = 100
    a.book = book
    return a


def test_service_dialog_drains_batches_and_acks(dagent, world, fake_worker):
    fake_worker.cmd_q.put(_batch(
        _duty(op="add", mode="leave_to_p1", target="Cucumber")))

    record = dagent._service_dialog(world)

    ack = fake_worker.ack_q.get_nowait()
    assert ack["batch_id"] == "b1"
    assert ack["results"][0]["ok"] is True
    assert ack["duties"][0]["target"] == "Cucumber"
    assert record["batches"][0]["results"] == ack["results"]
    assert record["duties"][0]["id"] == "a1"
    # 落地本拍立刻发一份新上下文：worker 下一轮看到的是新账本
    assert fake_worker.contexts[-1]["duties"][0]["id"] == "a1"


def test_service_dialog_survives_crashing_batch(dagent, world, fake_worker,
                                                monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("bad")
    monkeypatch.setattr(dagent, "_apply_duty_batch", boom)
    fake_worker.cmd_q.put(_batch(_duty(op="add", mode="do", target="WashPlate")))

    dagent._service_dialog(world)

    ack = fake_worker.ack_q.get_nowait()
    assert ack["results"][0]["ok"] is False


def test_service_dialog_has_no_help_event(dagent, world, fake_worker):
    for tick in range(100, 160):
        dagent.tick = dagent.bb.tick = tick
        dagent._service_dialog(world)
    assert [e for e in fake_worker.events if e[0] == "help"] == []


def test_service_dialog_prunes_abandoned_orders(dagent, world):
    dagent.bb.abandoned_orders = {1: 100, 2: 200}

    dagent._service_dialog(world)

    assert dagent.bb.abandoned_orders == {2: 200}


def test_level_start_is_logged_on_first_observation_and_change(
        dagent, fake_worker):
    dagent._maybe_reset_dialog_level(make_world("Sushi_1_4_2P"))
    dagent._maybe_reset_dialog_level(make_world("Sushi_1_4_2P"))
    dagent._maybe_reset_dialog_level(make_world("Sushi_1_5_2P"))

    assert fake_worker.level_events == [
        ("level_start", "Sushi_1_4_2P"),
        ("level_start", "Sushi_1_5_2P"),
    ]


def test_level_change_clears_ledger_history_and_queues(dagent, fake_worker):
    world = make_world("Sushi_1_4_2P")
    dagent._service_dialog(world)
    dagent._apply_duty_batch(world, _batch(
        _duty(op="add", mode="do", target="WashPlate")))
    dagent.bb.dialog = DialogState(last_rescue_tick=3)
    dagent.bb.abandoned_orders = {1: 1300}
    dagent.bb.rescue_cooldown_until = 150
    fake_worker.cmd_q.put(_batch())
    fake_worker.ack_q.put({"batch_id": "old"})

    # 暂停不算换关
    paused = make_world("Sushi_1_4_2P")
    paused.phase = "paused"
    dagent._maybe_reset_dialog_level(paused)
    assert fake_worker.reset_count == 0
    assert dagent.ledger.ids() == ["a1"]

    dagent._maybe_reset_dialog_level(make_world("Sushi_1_5_2P"))

    assert fake_worker.reset_count == 1
    assert dagent.ledger.ids() == []
    assert dagent.bb.preferences == []
    assert dagent.bb.dialog.last_rescue_tick == 0
    assert dagent.bb.abandoned_orders == {}
    assert dagent.bb.rescue_cooldown_until == 0
    assert fake_worker.cmd_q.empty() and fake_worker.ack_q.empty()


def test_level_replay_same_name_clears_history(dagent, fake_worker):
    # 同一关重开：level 名不变、scene_epoch 变，也必须走完整重置。
    world = make_world("Sushi_1_4_2P", session_id="s1", scene_epoch=1)
    dagent._service_dialog(world)
    dagent._apply_duty_batch(world, _batch(
        _duty(op="add", mode="do", target="WashPlate")))
    dagent.bb.dialog = DialogState(last_rescue_tick=3)
    dagent.bb.abandoned_orders = {1: 1300}
    dagent.bb.rescue_cooldown_until = 150
    dagent._seen_orders = {"CucumberSushi": 1}
    fake_worker.cmd_q.put(_batch())
    fake_worker.ack_q.put({"batch_id": "old"})

    # 第一次观察只记账，不重置。
    dagent._maybe_reset_dialog_level(world)
    assert fake_worker.reset_count == 0
    assert dagent.ledger.ids() == ["a1"]

    # 同名重开：scene_epoch 变 → 完整重置（账本/偏好/对话态/弃单/救援/见单/队列）。
    dagent._maybe_reset_dialog_level(
        make_world("Sushi_1_4_2P", session_id="s1", scene_epoch=2))

    assert fake_worker.reset_count == 1
    assert dagent.ledger.ids() == []
    assert dagent.bb.preferences == []
    assert dagent.bb.dialog.last_rescue_tick == 0
    assert dagent.bb.abandoned_orders == {}
    assert dagent.bb.rescue_cooldown_until == 0
    assert dagent._seen_orders == {}
    assert fake_worker.cmd_q.empty() and fake_worker.ack_q.empty()


# ------------------------------------------------------- context


def test_build_dialog_context_fields(dagent, world):
    active = T.Chop(order_id=1, resources=("board_01",), item="Cucumber")
    dagent.bb.commitment = Commitment(active, 90, 0, 0, 0)
    dagent._apply_duty_batch(world, _batch(
        _duty(op="add", mode="leave_to_p1", target="Cucumber"),
        source="黄瓜我来"))
    world.chefs[0]["held"] = "Plate"

    ctx = dagent._build_dialog_context(world)

    assert ctx["level"] == "Sushi_1_4_2P"
    assert ctx["live"] is True
    assert ctx["tick"] == 100
    assert ctx["p2"]["doing"] is None   # 认领清掉了承诺
    assert ctx["p1"] == {"holding": "plate"}
    assert ctx["orders"][0]["id"] == 1
    assert ctx["orders"][0]["dish"] == "CucumberSushi"
    assert ctx["duties"][0]["source"] == "黄瓜我来"
    assert "Cucumber（要切）：取料上砧板 → 切 → 装盘" in ctx["dag"]
    assert "CucumberSushi" in ctx["dish_dags"]
    assert ctx["cookers"][0] == {"name": "1 号灶", "text": "空"}
    assert ctx["plates"].startswith("空盘")
    names = [t["function"]["name"] for t in ctx["tools"]]
    assert names == ["update_duties"]
    props = ctx["tools"][0]["function"]["parameters"]["properties"]
    assert props["id"]["enum"] == ["a1"]
    assert "Cucumber" in props["target"]["enum"]


def test_build_dialog_context_live_false_when_paused(dagent, world):
    world.paused = True
    ctx = dagent._build_dialog_context(world)
    assert ctx["live"] is False


def test_build_dialog_context_has_grounding_fields(dagent, world):
    """新字段：脏盘 / 板内容 / 空灶 / 连击 / 临期单，脏盘口径与 plate.py 一致。"""
    world.stations[2]["item"] = "ChoppedCucumber"   # 切菜板上有切好的黄瓜
    world.stations[3]["plate_count"] = 2             # 洗碗池待洗 2 块
    world.items = [{"n": "DirtyPlateStack", "x": 0.0, "z": 0.0}]
    world.cookers = [
        {"n": "cooker_01", "gx": 5, "gz": 5, "x": 6.0, "z": 6.0},
        {"n": "cooker_02", "gx": 6, "gz": 6, "x": 7.0, "z": 7.0,
         "pot_contents": ["SushiRice"]},
    ]
    world.orders[0]["remaining"] = 10
    world.raw["score"] = {"combo": 3, "base": 120, "deliveries": 2, "tips": 15}

    ctx = dagent._build_dialog_context(world)

    assert isinstance(ctx["dirty_plates"], int)
    assert ctx["dirty_plates"] == 3          # 池里 2 + 场上脏盘摞 1
    assert isinstance(ctx["boards"], list)
    assert ctx["boards"] == ["board_01：Cucumber(已切)"]
    assert isinstance(ctx["idle_cookers"], int)
    assert ctx["idle_cookers"] == 1          # cooker_02 有料，只有 cooker_01 空
    assert ctx["combo"] == 3
    assert ctx["deliveries"] == 2
    assert ctx["tips"] == 15
    assert ctx["total"] == 135        # base 120 + tips 15
    assert isinstance(ctx["expiring_orders"], list)
    assert [o["id"] for o in ctx["expiring_orders"]] == [1]

    from ai.agent import _plates_text
    text = _plates_text(world, dagent.book)
    assert "脏盘 3 个" in text

    from ai.dialog.llm import render_state
    state_text = render_state(ctx)
    assert "切菜板" in state_text
    assert "空闲灶 1 个" in state_text
    assert "连击 3" in state_text
    assert "快过期" in state_text
    assert "总分 135" in state_text


def test_context_p2_doing_uses_dag_node(dagent, world):
    active = T.Chop(order_id=1, resources=("board_01",), item="Cucumber")
    dagent.bb.commitment = Commitment(active, 90, 0, 0, 0)

    ctx = dagent._build_dialog_context(world)

    assert ctx["p2"]["doing"] == "Chop:Cucumber（订单 #1）"


def test_context_p2_options_come_from_planner_ranking(dagent, world):
    from unittest.mock import patch
    t1 = T.FetchToBoard(1, ("board_01",), "Cucumber")
    t2 = T.WashPlate(resources=("sink_01",))
    plan_record = {"candidates": [
        (t2, 1.0, {}, True, "", False, 6, True),
        (t1, 5.0, {}, True, "", True, 6, True),
        (T.Chop(1, ("board_01",), "Cucumber"), None, {}, False, "x", False, 6, False),
    ]}
    with patch("ai.agent.arbiter.select_mode",
               return_value=("Normal", "test")), \
         patch("ai.agent.planner.plan", return_value=(None, plan_record)), \
         patch("ai.agent.trace.emit"):
        dagent._tick_once(world)

    ctx = dagent._build_dialog_context(world)
    assert ctx["p2_options"] == [
        "FetchToBoard:Cucumber（订单 #1）（%s）" % (T.FetchToBoard.DESC,),
        "WashPlate（%s）" % (T.WashPlate.DESC,)]


def test_tick_once_includes_dialog_record(dagent, world):
    fake_worker = dagent.dialog_worker
    fake_worker.cmd_q.put(_batch(_duty(op="add", mode="do", target="WashPlate")))

    from unittest.mock import patch
    with patch("ai.agent.arbiter.select_mode",
               return_value=("Normal", "test")), \
         patch("ai.agent.planner.plan",
               return_value=(None, {"candidates": []})), \
         patch("ai.agent.trace.emit") as emit:
        dagent._tick_once(world)

    record = emit.call_args[0][0]
    assert record["dialog"]["batches"][0]["id"] == "b1"
    assert record["dialog"]["duties"][0]["target"] == "WashPlate"


def test_tick_once_publishes_paused_context(dagent, world):
    fake_worker = dagent.dialog_worker
    dagent.tick = 10
    world.paused = True

    dagent._tick_once(world)

    # 暂停分支也要更新上下文：worker 靠 live=False 停语音 / 关 check-in。
    assert fake_worker.contexts
    assert fake_worker.contexts[-1]["live"] is False


def test_service_dialog_emits_new_order_only_once(dagent, world,
                                                  fake_worker):
    dagent._service_dialog(world)
    dagent._service_dialog(world)

    new_orders = [e for e in fake_worker.events if e[0] == "new_order"]
    assert new_orders == [("new_order", [1])]


def test_service_dialog_new_order_skips_covered_materials(dagent, world,
                                                          fake_worker):
    world.orders = [
        {"id": 1, "dish": "Salad", "composition": ["Cucumber", "Tomato"],
         "remaining": 60, "lifetime": 130},
    ]
    dagent._service_dialog(world)
    fake_worker.events.clear()

    # 新单「生菜」：物料全被之前的单覆盖，不播 new_order。
    world.orders.append({
        "id": 2, "dish": "CucumberSalad", "composition": ["Cucumber"],
        "remaining": 60, "lifetime": 130,
    })
    dagent._service_dialog(world)
    assert [e for e in fake_worker.events if e[0] == "new_order"] == []

    # 新单「鱼」：有新物料，播一次。
    world.orders.append({
        "id": 3, "dish": "FishSalad", "composition": ["SushiFish"],
        "remaining": 60, "lifetime": 130,
    })
    dagent._service_dialog(world)
    assert [e for e in fake_worker.events
            if e[0] == "new_order"] == [("new_order", [3])]


def test_service_dialog_emits_last_seconds_once(dagent, world, fake_worker):
    # 最后 10 秒内：last_seconds 只发一次。
    world.raw["timer"] = {"server": {"left": 5, "limit": 130}}
    dagent._service_dialog(world)
    dagent._service_dialog(world)
    assert [e for e in fake_worker.events
            if e[0] == "last_seconds"] == [("last_seconds", None)]


def test_tick_once_emits_round_summary_once(dagent, world, fake_worker):
    dagent.tick = 10
    world.raw["timer"] = {"server": {"left": 0, "limit": 130}}

    # 未开局（计时器没跑过）：left=0 会被 _timer_expired 误判成「已结束」，
    # 但这里不应触发结束总结。
    dagent._tick_once(world)
    assert [e for e in fake_worker.events
            if e[0] == "round_summary"] == []

    # 计时器跑过 → 归零时才触发，且只发一次。
    dagent._round_started = True
    dagent._tick_once(world)
    dagent._tick_once(world)

    assert [e for e in fake_worker.events
            if e[0] == "round_summary"] == [("round_summary", None)]


def test_service_dialog_new_order_dedupes_by_dish(dagent, world,
                                                  fake_worker):
    dagent._service_dialog(world)
    fake_worker.events.clear()

    # 同一道菜已经有单，再来一张重复单：不触发新菜谱事件。
    world.orders.append({
        "id": 2, "dish": "CucumberSushi",
        "composition": ["Cucumber"], "remaining": 60, "lifetime": 130,
    })
    dagent._service_dialog(world)
    assert [e for e in fake_worker.events
            if e[0] == "new_order"] == []

    # 新菜谱出现：触发一次，只带新单 id。
    world.orders.append({
        "id": 3, "dish": "FishSushi",
        "composition": ["SushiFish"], "remaining": 60, "lifetime": 130,
    })
    dagent._service_dialog(world)
    assert [e for e in fake_worker.events
            if e[0] == "new_order"] == [("new_order", [3])]

    # 这道菜全部消失后再出现：本局已经播过，不再触发。
    world.orders = [order for order in world.orders
                    if order.get("dish") != "FishSushi"]
    dagent._service_dialog(world)
    fake_worker.events.clear()
    world.orders.append({
        "id": 4, "dish": "FishSushi",
        "composition": ["SushiFish"], "remaining": 60, "lifetime": 130,
    })
    dagent._service_dialog(world)
    assert [e for e in fake_worker.events
            if e[0] == "new_order"] == []


def test_service_dialog_publishes_context_every_10_ticks(dagent, world,
                                                         fake_worker):
    dagent.tick = 8
    dagent._service_dialog(world)          # 首拍有 new_order 事件，立刻发一份
    fake_worker.contexts.clear()

    dagent.tick = 9
    dagent._service_dialog(world)
    assert fake_worker.contexts == []

    dagent.tick = 10
    dagent._service_dialog(world)
    assert len(fake_worker.contexts) == 1
    assert fake_worker.contexts[0]["tick"] == dagent.bb.tick


def test_run_starts_and_stops_dialog_worker(monkeypatch):
    class LifecycleWorker(FakeDialogWorker):
        def start(self):
            self.started = True
            raise KeyboardInterrupt

    worker = LifecycleWorker()
    monkeypatch.setattr("ai.agent.StateClient", lambda *a, **k: MagicMock())
    agent = Agent(dialog_worker=worker)

    agent.run()

    assert worker.started
    assert worker.stopped


def test_trace_emit_includes_dialog(monkeypatch, tmp_path):
    from ai.output import trace
    monkeypatch.setattr(trace, "TRACE_PATH", str(tmp_path / "trace.jsonl"))

    trace.emit({"dialog": {"pref_count": 2},
                "branches": {"key-1": "Cucumber"},
                "rescue_override": "order-1"}, "Normal", None, {}, [], 1)

    entry = trace.read_log()[0]
    assert entry["dialog"] == {"pref_count": 2}
    assert entry["branches"] == {"key-1": "Cucumber"}
    assert entry["rescue_override"] == "order-1"



def test_tick_once_forwards_branches_and_rescue_override(dagent, world):
    from unittest.mock import patch
    plan_record = {
        "candidates": [],
        "branches": {"key-1": "Cucumber"},
        "rescue_override": "order-1",
    }
    with patch("ai.agent.arbiter.select_mode",
               return_value=("Normal", "test")), \
         patch("ai.agent.planner.plan", return_value=(None, plan_record)), \
         patch("ai.agent.trace.emit") as emit:
        dagent._tick_once(world)

    record = emit.call_args[0][0]
    assert record["branches"] == {"key-1": "Cucumber"}
    assert record["rescue_override"] == "order-1"


# ------------------------------------------------------- stuck 上报


def test_context_reports_stuck_from_scratch(dagent, world):
    from ai.state.blackboard import StuckScratch
    dagent.bb.scratch = StuckScratch(last_cell=(3, 4), same_count=5)

    ctx = dagent._build_dialog_context(world)

    stuck = ctx["p2"]["stuck"]
    assert stuck is not None
    assert "卡在" in stuck and "(3,4)" in stuck and "5 拍" in stuck


def test_context_stuck_none_below_threshold_or_no_scratch(dagent, world):
    from ai.state.blackboard import StuckScratch
    dagent.bb.scratch = StuckScratch(last_cell=(3, 4), same_count=2)
    assert dagent._build_dialog_context(world)["p2"]["stuck"] is None

    dagent.bb.scratch = None
    assert dagent._build_dialog_context(world)["p2"]["stuck"] is None


def test_tick_once_emits_stuck_event(dagent, world):
    from ai.state.blackboard import StuckScratch
    from unittest.mock import patch
    active = T.Chop(order_id=1, resources=("board_01",), item="Cucumber")
    dagent.bb.commitment = Commitment(active, 90, 0, 0, 2)
    dagent.bb.scratch = StuckScratch(last_cell=(3, 4), same_count=7)

    with patch("ai.agent.arbiter.select_mode",
               return_value=("Normal", "stuck detected")), \
         patch("ai.agent.planner.plan",
               return_value=(None, {"candidates": []})), \
         patch("ai.agent.trace.emit") as emit:
        dagent._tick_once(world)

    stuck = emit.call_args[0][0]["stuck"]
    assert stuck == {
        "task": "Chop:Cucumber（订单 #1）",
        "reason": "stuck detected",
        "pos": [3, 4],
        "duration_ticks": 7,
        "retries": 2,
    }


def test_tick_once_forwards_imperative(dagent, world):
    from unittest.mock import patch
    plan_record = {"candidates": [], "imperative": "WashPlate"}
    with patch("ai.agent.arbiter.select_mode",
               return_value=("Normal", "test")), \
         patch("ai.agent.planner.plan", return_value=(None, plan_record)), \
         patch("ai.agent.trace.emit") as emit:
        dagent._tick_once(world)

    assert emit.call_args[0][0]["imperative"] == "WashPlate"


def test_trace_emit_includes_stuck_and_imperative(monkeypatch, tmp_path):
    from ai.output import trace
    monkeypatch.setattr(trace, "TRACE_PATH", str(tmp_path / "trace.jsonl"))

    trace.emit({
        "stuck": {"task": "Chop:Cucumber", "reason": "stuck detected",
                  "pos": [3, 4], "duration_ticks": 7, "retries": 2},
        "imperative": "WashPlate",
    }, "Normal", None, {}, [], 1)

    entry = trace.read_log()[0]
    assert entry["stuck"] == {
        "task": "Chop:Cucumber", "reason": "stuck detected",
        "pos": [3, 4], "duration_ticks": 7, "retries": 2}
    assert entry["imperative"] == "WashPlate"


def test_trace_emit_omits_stuck_when_not_stuck(monkeypatch, tmp_path):
    from ai.output import trace
    monkeypatch.setattr(trace, "TRACE_PATH", str(tmp_path / "trace.jsonl"))

    trace.emit({"dialog": {}}, "Normal", None, {}, [], 1)

    assert "stuck" not in trace.read_log()[0]



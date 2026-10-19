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

def make_world(level="Sushi_1_4_2P", orders=None, chefs=None):
    return World({
        "phase": "level",
        "level": level,
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
    a = Agent(ctrl=MagicMock(), voice=None)
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
    a = Agent(ctrl=MagicMock(), dialog_worker=fake_worker)
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


def test_service_dialog_emits_new_order_only_once(dagent, world,
                                                  fake_worker):
    dagent._service_dialog(world)
    dagent._service_dialog(world)

    new_orders = [e for e in fake_worker.events if e[0] == "new_order"]
    assert new_orders == [("new_order", [1])]


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
    agent = Agent(ctrl=MagicMock(), dialog_worker=worker)

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



# -*- coding: utf-8 -*-
"""Agent 生命周期守卫：暂停 / 非关卡 / 断线时松键、不卡键。

对应 spec §8.1 的生命周期守卫，以及 legacy oc/ai/agent.py:280 的行为
（`if w.paused or w.phase != "level": release_all`）。

另外覆盖该守卫的 trace 心跳：进入暂停/非关卡时留一条日志（只一条，不逐拍），
以便事后区分"trace 是空的"和"AI 在暂停中没决策"。
"""
import pytest
from unittest.mock import patch

from ai.output import trace
from ai.core import planner
from ai.agent import Agent
from ai.state.blackboard import Blackboard
from ai.query.recipes import RecipeBook
from ai.state.world import World


class RecordingCtrl(object):
    """记录所有按键调用，不碰 Quartz。"""

    def __init__(self):
        self.calls = []

    def tap(self, name, hold=0.12):
        self.calls.append(("tap", name))

    def press(self, name):
        self.calls.append(("press", name))

    def release(self, name):
        self.calls.append(("release", name))

    def release_all(self):
        self.calls.append(("release_all",))


def _agent(ctrl):
    with patch("ai.agent.StateClient", return_value=object()):
        return Agent(ctrl=ctrl)


def _snapshot(phase="level", paused=False):
    return {
        "phase": phase,
        "paused": paused,
        "level": "Sushi_1_4_2P",
        "orders": [],
        "chefs": [{"name": "Player 2", "x": 1.0, "z": 1.0, "held": None}],
        "stations": [],
        "recipes": [],
        "plates": [],
        "workstations": [],
        "cookers": [],
        "items": [],
        "extinguishers": [],
        "grid": {"nodeMap": None},
    }


def test_paused_releases_keys_and_skips_decision():
    ctrl = RecordingCtrl()
    agent = _agent(ctrl)

    agent._tick_once(World(_snapshot(paused=True)))

    assert ("release_all",) in ctrl.calls, "暂停时应松键"
    assert agent.bb.commitment.active_task is None, "暂停时不应登记承诺"
    assert agent.bb.claims == {}, "暂停时不应写认领"


def test_non_level_phase_releases_keys():
    ctrl = RecordingCtrl()
    agent = _agent(ctrl)

    agent._tick_once(World(_snapshot(phase="menu")))

    assert ("release_all",) in ctrl.calls, "非关卡应松键"


@pytest.fixture
def trace_file(tmp_path, monkeypatch):
    """把 trace 指到临时文件，避免污染仓库。"""
    path = tmp_path / "trace.jsonl"
    monkeypatch.setattr(trace, "TRACE_PATH", str(path))
    return path


def test_paused_logs_one_heartbeat_not_every_tick(trace_file):
    """暂停期间只写一条 trace：区分"agent 没起来"和"AI 没决策"。"""
    ctrl = RecordingCtrl()
    agent = _agent(ctrl)

    for _ in range(5):
        agent._tick_once(World(_snapshot(paused=True)))
        agent.tick += 1

    logs = trace.read_log()
    assert len(logs) == 1, "暂停不逐拍刷屏，只记进入的那一条"
    assert logs[0]["mode_reason"] == "paused"
    assert logs[0]["exec"]["status"] == "skipped"
    assert logs[0]["commit"] == {}
    assert logs[0]["keys"] == []


def test_idle_heartbeat_resets_when_back_in_level(trace_file):
    """回到关卡内正常记拍；再次暂停时重新记一条。"""
    ctrl = RecordingCtrl()
    agent = _agent(ctrl)

    agent._tick_once(World(_snapshot(paused=True)))
    agent.tick += 1
    agent._tick_once(World(_snapshot(paused=True)))
    agent.tick += 1
    assert len(trace.read_log()) == 1

    agent._tick_once(World(_snapshot()))          # 关卡内：正常的一拍
    agent.tick += 1
    logs = trace.read_log()
    assert len(logs) == 2
    assert logs[1]["mode_reason"] != "paused"

    agent._tick_once(World(_snapshot(paused=True)))   # 再次暂停：再记一条
    agent.tick += 1
    logs = trace.read_log()
    assert len(logs) == 3
    assert logs[2]["mode_reason"] == "paused"


def test_non_level_phase_logs_one_heartbeat(trace_file):
    """非关卡（菜单/结算）同样留一条，mode_reason 带上是哪个阶段。"""
    ctrl = RecordingCtrl()
    agent = _agent(ctrl)

    for _ in range(3):
        agent._tick_once(World(_snapshot(phase="menu")))
        agent.tick += 1

    logs = trace.read_log()
    assert len(logs) == 1
    assert logs[0]["mode_reason"] == "phase=menu"
    assert logs[0]["exec"]["status"] == "skipped"


def test_run_releases_keys_on_disconnect():
    ctrl = RecordingCtrl()
    agent = _agent(ctrl)

    class EofClient(object):
        def read_latest(self):
            raise EOFError("socket closed")

    agent.client = EofClient()
    agent.run()          # 应立即返回，不阻塞

    assert ("release_all",) in ctrl.calls, "断线应松键"


def test_run_frame_sequence_advances_only_on_received_snapshots(monkeypatch):
    ctrl = RecordingCtrl()
    agent = _agent(ctrl)
    snapshots = iter([_snapshot(), None, None, _snapshot()])

    class Client:
        def read_latest(self):
            try:
                return next(snapshots)
            except StopIteration:
                raise EOFError()

    seen = []
    agent.client = Client()
    monkeypatch.setattr(agent, "_tick_once", lambda w: seen.append(agent._frame_seq))
    monkeypatch.setattr("ai.agent.time.sleep", lambda seconds: None)
    agent.run()
    assert seen == [1, 1, 1, 2]


def test_stale_stream_stops_keys_and_resumes_on_fresh_snapshot(monkeypatch, trace_file):
    ctrl = RecordingCtrl()
    agent = _agent(ctrl)
    agent._frame_seq, agent._frame_received_at = 1, 0.0
    agent.hands.set_move("right")
    monkeypatch.setattr("ai.agent.time.monotonic", lambda: 1.0)
    with patch("ai.agent.planner.plan") as plan:
        agent._tick_once(World(_snapshot()))
        plan.assert_not_called()
    assert agent.hands.move_dir is None
    assert trace.read_log()[-1]["mode_reason"] == "stale_frame"
    agent._frame_seq, agent._frame_received_at = 2, 1.0
    agent._tick_once(World(_snapshot()))
    assert trace.read_log()[-1]["mode_reason"] != "stale_frame"
    assert agent._exec_context(World(_snapshot()))["frame_seq"] == 2


def test_run_releases_keys_on_unexpected_read_error():
    ctrl = RecordingCtrl()
    agent = _agent(ctrl)

    class Client:
        def read_latest(self):
            raise OSError("read failed")

    agent.client = Client()
    agent.hands.set_move("right")
    with pytest.raises(OSError):
        agent.run()
    assert agent.hands.move_dir is None


def test_firefighting_task_is_never_abandoned():
    """灭火任务连续失败也不写 scratch.abandoned —— 火不能不管。

    Agent 的 L3 兜底（连续 3 次失败即拉黑认领资源）对声明 NO_ABANDON 的
    task 整条跳过；普通 task 照旧拉黑（第二段是同一装置里的对照）。
    """
    from ai import tasks as T

    agent = _agent(RecordingCtrl())

    fire = T.Extinguish()
    for _ in range(5):
        agent._note_failure(fire)
    assert agent.bb.scratch is None, "灭火失败不该建 StuckScratch"

    normal = T.ReturnPot(resources=("cooker_01",))
    for _ in range(3):
        agent._note_failure(normal)
    assert agent.bb.scratch.abandoned_actions[normal.claims()[0]] > agent.tick


def test_cooked_pot_tasks_are_never_abandoned():
    """端锅/盛饭这类停火关键任务不写也不读 abandoned 拉黑。

    拉黑期（200 拍）比烧糊倒计时还长：一旦把 RescueCooked / TakeCookedToPlate
    拉黑，熟锅必糊必着火（实机 trace-20261018-153802：盛饭失败把端锅一起
    拉黑 → 熟锅烧糊起火）。所以这两个 task 声明 NO_ABANDON，写侧与读侧
    都不受 abandoned 约束；bad_cells（格子级绕路）仍照常写。
    """
    from ai import tasks as T
    from ai.core.planner import _is_abandoned

    agent = _agent(RecordingCtrl())

    rescue = T.RescueCooked(resources=("cooker_01",))
    take = T.TakeCookedToPlate("order1", ("cooker_01", "plate_01"),
                               "BoiledSushiRice")

    # ① 写侧：连续失败不拉黑资源
    for _ in range(5):
        agent._note_failure(rescue)
        agent._note_failure(take)
    assert agent.bb.scratch is None, "端锅/盛饭失败不该建 StuckScratch"

    # ② 写侧：motion 失败也不拉黑资源，但 bad_cells 照写
    class CalmWorld(object):
        def chef_in_wind(self, chef):
            return False

    ctx = {"w": CalmWorld(),
           "chef": {"name": "Player 2", "x": 1.0, "z": 1.0, "held": None},
           "motion_failure": {"reason": "no_progress", "blocked_cell": (6, 1)}}
    agent._note_motion_failure(rescue, ctx)
    assert agent.bb.scratch is not None
    assert agent.bb.scratch.bad_cells[(6, 1)] > agent.tick
    assert not _is_abandoned(rescue, agent.bb), "端锅不该被拉黑"

    # ③ 读侧：即使资源已在 abandoned 表里，端锅/盛饭也不被否决
    agent.bb.scratch.abandoned["cooker_01"] = agent.tick + 200
    assert not _is_abandoned(rescue, agent.bb)
    assert not _is_abandoned(take, agent.bb)


def test_motion_obstruction_is_shared_across_order_ids():
    from ai import tasks as T
    from ai.core.planner import _is_abandoned

    agent = _agent(RecordingCtrl())
    first = T.FetchToCooker("1", ("cooker", ("SushiRice",)), "BoiledSushiRice")
    second = T.FetchToCooker("2", ("cooker", ("SushiRice",)), "BoiledSushiRice")
    agent._note_motion_failure(first, {"motion_failure": {
        "reason": "no_progress", "blocked_cell": (6, 1)}})
    assert _is_abandoned(second, agent.bb)
    assert agent.bb.scratch.bad_cells[(6, 1)] > agent.tick


def test_motion_failure_in_wind_does_not_blacklist_resource():
    """风区里被吹得动不了导致的 no_progress/observation_timeout 不拉黑资源。

    只写 bad_cells 让寻路绕开这一格，不写 abandoned——否则一阵风就把端料
    的整个资源判死刑（实机：端番茄时被风推、位置无法移动，料箱被拉黑）。
    """
    from ai import tasks as T
    from ai.core.planner import _is_abandoned

    agent = _agent(RecordingCtrl())
    task = T.FetchToCooker("1", ("cooker", ("SushiRice",)), "BoiledSushiRice")

    class WindyWorld(object):
        def chef_in_wind(self, chef):
            return True

    ctx = {"w": WindyWorld(),
           "chef": {"name": "Player 2", "x": 1.0, "z": 1.0, "held": None},
           "motion_failure": {"reason": "observation_timeout",
                              "blocked_cell": (6, 1)}}
    agent._note_motion_failure(task, ctx)
    assert agent.bb.scratch is not None
    assert agent.bb.scratch.bad_cells[(6, 1)] > agent.tick
    assert not _is_abandoned(task, agent.bb)


def test_motion_failure_outside_wind_still_blacklists():
    """不在风区里时，仍按旧逻辑拉黑资源。"""
    from ai import tasks as T
    from ai.core.planner import _is_abandoned

    agent = _agent(RecordingCtrl())
    task = T.FetchToCooker("1", ("cooker", ("SushiRice",)), "BoiledSushiRice")

    class CalmWorld(object):
        def chef_in_wind(self, chef):
            return False

    ctx = {"w": CalmWorld(),
           "chef": {"name": "Player 2", "x": 1.0, "z": 1.0, "held": None},
           "motion_failure": {"reason": "no_progress", "blocked_cell": (6, 1)}}
    agent._note_motion_failure(task, ctx)
    assert _is_abandoned(task, agent.bb)


def test_recipe_book_rebuilt_each_frame():
    """配方书必须按当前帧重建，不能被早帧/残帧污染（实机 bug 回归）。

    真实事故：进关卡第一帧 crate 的 chop 还没带上 → 整局以为"鱼不用切"，
    AI 拿生鱼直接去装盘、从不使用砧板。
    """
    ctrl = RecordingCtrl()
    agent = _agent(ctrl)

    f1 = _snapshot()
    f1["recipes"] = [{"name": "SushiFish", "comp": []}]
    f1["stations"] = [{"t": "crate", "n": "crate_fish",
                       "ing": "SushiFish", "gx": 1, "gz": 1}]      # 无 chop
    agent._tick_once(World(f1))
    assert agent.book.needs_chopping("SushiFish") is False

    f2 = _snapshot()
    f2["recipes"] = [{"name": "SushiFish", "comp": []}]
    f2["stations"] = [{"t": "crate", "n": "crate_fish",
                       "ing": "SushiFish", "chop": "ChoppedSushiFish",
                       "chop_stages": 8, "gx": 1, "gz": 1}]        # 带上 chop
    agent._tick_once(World(f2))
    assert agent.book.needs_chopping("SushiFish") is True
    assert agent.book.chop_target("SushiFish") == "ChoppedSushiFish"


# ---------- 盘↔订单 每拍 1:1 匹配（替代旧的观察式归属 + producing 持久绑定）----------

def _match_world(orders, plates, recipes=None):
    f = _snapshot()
    f["orders"] = orders
    f["plates"] = plates
    f["recipes"] = recipes or [{"name": "BoiledSushiRice", "comp": ["SushiRice"],
                                "cook": "Pot"}]
    # 盘↔订单匹配只认「已就位」的盘（落在 counter 拼装格上）。给每只盘补一个
    # 同格的 counter 站，让这些匹配单测贴近实机帧（P1 手里/沥碗架的盘不算）。
    stations = []
    for i, p in enumerate(plates):
        gx = int(round(p.get("x") / 1.2))
        gz = int(round(p.get("z") / 1.2))
        stations.append({"t": "counter", "n": "assembly_%d" % i,
                         "gx": gx, "gz": gz, "x": gx * 1.2, "z": gz * 1.2})
    f["stations"] = stations
    return World(f)


def _match(orders, plates, recipes=None):
    w = _match_world(orders, plates, recipes)
    return planner._match_plates_to_orders(w, Blackboard(),
                                           RecipeBook(w.recipes, w.stations))


_RICE = "BoiledSushiRice"


def test_match_plates_unique_fit_claims_one_order():
    """唯一兼容盘被匹配到一张单；同需求多单不会都认领同一只盘。"""
    got = _match(
        orders=[{"id": "1", "composition": ["Seaweed", _RICE, "Cucumber"],
                 "remaining": 30},
                {"id": "2", "composition": ["Seaweed", _RICE, "SushiFish"],
                 "remaining": 30}],
        plates=[{"n": "pl1", "x": 1.0, "z": 1.0, "count": 1,
                 "contents": ["CookedCompositeAssembledNode"]}])
    assert got == {"pl1": "1"}, got


def test_match_plates_skips_mismatched_and_fills_empty():
    """错料盘不匹配；空盘排最后、只填还没盘的订单。"""
    got = _match(
        orders=[{"id": "1", "composition": ["Seaweed", _RICE, "SushiFish"],
                 "remaining": 30}],
        plates=[{"n": "wrong", "x": 1.0, "z": 1.0, "count": 1,
                 "contents": ["Cucumber"]},
                {"n": "empty", "x": 2.0, "z": 1.0, "count": 0,
                 "contents": []}])
    assert "wrong" not in got, got
    assert got == {"empty": "1"}, got


def test_match_plates_strict_one_to_one():
    """两张同名菜的单 → 每单最多一只盘；多余的盘留空（1:1，不摊平到同单）。"""
    got = _match(
        orders=[{"id": "1", "composition": [_RICE], "remaining": 10},
                {"id": "3", "composition": [_RICE], "remaining": 90}],
        plates=[{"n": "p%d" % i, "x": float(i), "z": 1.0, "count": 1,
                 "contents": ["CookedCompositeAssembledNode"]}
                for i in range(1, 5)])
    assert sorted(set(got.values())) == ["1", "3"], got
    assert len(got) == 2, got


def test_match_plates_completion_aware():
    """完成度感知：更接近完成的盘配更紧的单（A 紧、C 缺 1、D 缺 2）。"""
    w = _match_world(
        orders=[{"id": "A", "composition": [_RICE, "SushiFish", "Seaweed"],
                 "remaining": 10},
                {"id": "B", "composition": [_RICE, "SushiFish", "Seaweed"],
                 "remaining": 90}],
        plates=[{"n": "C", "x": 1.0, "z": 1.0, "count": 2,
                 "contents": ["CookedCompositeAssembledNode", "SushiFish"]},
                {"n": "D", "x": 2.0, "z": 1.0, "count": 1,
                 "contents": ["CookedCompositeAssembledNode"]}])
    got = planner._match_plates_to_orders(w, Blackboard(),
                                          RecipeBook(w.recipes, w.stations))
    assert got == {"C": "A", "D": "B"}, got

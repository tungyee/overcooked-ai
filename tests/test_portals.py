"""传送门寻路（ai/space/portals.py）：误踩避让 + 过门跳边 + move 过门段。

真实帧 tests/fixtures/wizard/wizard55-20261101-145921-bun.ndjson.gz（Wizard_5_5，
插件 0.24.1，没有 portals 字段）叠上插件 0.24.3 实测的 [PORTALS] 日志
（trace-20261101-160703 那一局）：左高台北墙 (8.4,-1.9) ↔ 右高台北墙 (28.8,-1.9)，
门朝南、扇区 90°，落点在门前 (x,-2.4)。

这一帧右侧升降楼梯沉在地下（楼梯格不可走），不走门时左台去不了右台——正好验证
过门跳边把两块高台连起来。
"""
import gzip
import json
import os

import pytest

from ai.exec.bt import Status
from ai.primitives.move import GoToCell
from ai.space import navigation, portals
from ai.state.world import World

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "wizard",
                       "wizard55-20261101-145921-bun.ndjson.gz")


def _portal(pid, name, exit_id, x):
    return {
        "id": pid, "name": name, "exit": exit_id, "active": True, "server_enabled": True,
        "center": [x, -1.9, 3], "arc_dir": [0, -1], "arc_deg": 90,
        "receive_delay": 0, "cooldown_time": 0, "allow_reteleport": False,
        "trigger": {"type": "CapsuleCollider", "is_trigger": True, "enabled": True,
                    "minx": x - 0.44, "maxx": x + 0.44, "minz": -2.34, "maxz": -1.46,
                    "miny": 2, "maxy": 4},
        "point": [x, -2.4, 3], "point_fwd": [0, -1],
        "player_sender": True, "item_sender": True, "conveyor_in": False,
        "player_receiver": {"point": [x, -2.4, 3], "fwd": [0, -1], "ground": True},
        "item_receiver": {"point": [x, -2.4, 3], "fwd": [0, -1]},
        "teleporting": False, "cooldown": False, "receiving": False,
    }


RIGHT = "Teleportal#630920"
LEFT = "Teleportal (1)#634416"
PORTALS = [_portal(RIGHT, "Teleportal", LEFT, 28.8),
           _portal(LEFT, "Teleportal (1)", RIGHT, 8.4)]


@pytest.fixture(scope="module")
def frame():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        for line in fh:
            d = json.loads(line)
            if d.get("phase") == "level" and d.get("terrain"):
                return d
    raise AssertionError("fixture 没有关卡帧")


def _world(frame, with_portals=True):
    raw = dict(frame)
    if with_portals:
        raw["portals"] = PORTALS
    return World(raw)


def _field(w, start, blocked=None):
    return navigation.distance_field(start, belt_dir=w.belt_dir, blocked=blocked,
                                     **portals.nav_args(w, None, blocked))


LEFT_DECK = (8.4, -4.8)       # 左高台（砧板 / 生菜 / 面包）
RIGHT_RETURN = (30.0, -3.6)   # 右高台回盘口旁


def test_old_frame_has_no_portal_map(frame):
    assert _world(frame, with_portals=False).portal_map is None


def test_trigger_cells_and_hops(frame):
    pm = _world(frame).portal_map
    # 门口格（格心离触发盒 < 厨师半径）对寻路不可走
    assert pm.trigger_cells == {(-8, 2), (-8, 3), (9, 2), (9, 3)}
    # 左门入口邻格（门正南、西侧）→ 右门落地格 (9,1) 旁的格
    assert ((-8, 1), (9, 1)) in pm.hops
    assert ((-9, 2), (9, 1)) in pm.hops
    hop = pm.hops[((-8, 1), (9, 1))]
    assert hop["portal"] == LEFT and hop["push"] == [8.4, -1.9] and hop["land"] == [28.8, -2.4]
    # 反向也连
    assert ((9, 1), (-8, 1)) in pm.hops


def test_portal_links_left_and_right_decks(frame):
    w, w0 = _world(frame), _world(frame, with_portals=False)
    start, goal = w.to_grid(*LEFT_DECK), w.to_grid(*RIGHT_RETURN)
    assert _field(w0, start).get(goal) is None          # 不走门：这一帧去不了
    f = _field(w, start)
    # 进门 1 + 传送 TELEPORT_STEPS + 出门 1，加两头各走一步
    assert f.get(goal) == 2 + portals.TELEPORT_STEPS + 2
    path = navigation.trace_back(f, goal, w.belt_dir)
    assert path == [(-8, 0), (-8, 1), (9, 1), (10, 1)]


def test_field_never_steps_on_trigger_cells(frame):
    w = _world(frame)
    f = _field(w, w.to_grid(*LEFT_DECK))
    assert not set(f) & w.portal_map.trigger_cells


def test_start_on_trigger_cell_still_expands(frame):
    w = _world(frame)
    f = _field(w, (-8, 2))
    assert f[(-8, 2)] == 0 and f.get((-8, 1)) == 1


def test_blocked_trigger_cell_drops_that_portal(frame):
    """插件回 portal busy → 入口门中心格进 bad_cells，这扇门暂时不连。"""
    w = _world(frame)
    start, goal = w.to_grid(*LEFT_DECK), w.to_grid(*RIGHT_RETURN)
    assert _field(w, start, blocked={(-8, 2)}).get(goal) is None


def test_route_inserts_push_and_land_points(frame):
    w = _world(frame)
    path = [(-8, 0), (-8, 1), (9, 1), (10, 1)]
    points, gates, hops, extra = portals.route(w, path)
    assert gates == []
    assert hops == [{"enter": 2, "exit": 3, "portal": LEFT, "push": [8.4, -1.9],
                     "land": [28.8, -2.4], "max_wait": portals.PORTAL_WAIT_S}]
    assert points[2] == [8.4, -1.9] and points[3] == [28.8, -2.4]
    assert [round(v, 2) for v in points[1]] == [8.4, -3.6]
    assert [round(v, 2) for v in points[4]] == [28.8, -3.6]
    assert extra == pytest.approx(portals.PORTAL_HOP_S)


def test_route_without_hop_matches_seam_points(frame):
    w = _world(frame)
    points, gates, hops, extra = portals.route(w, [(-8, 0), (-8, 1)])
    assert hops == [] and gates == [] and extra == 0
    assert len(points) == 2


class _CaptureExec(object):
    def __init__(self):
        self.sent = []

    def issue(self, cmd):
        self.sent.append(cmd)
        return len(self.sent)

    def poll(self):
        return None


def test_goto_cell_sends_portal_segment(frame):
    w = _world(frame)
    chef = dict(next(c for c in w.chefs if c.get("name") == "Player 2"))
    chef["x"], chef["z"] = LEFT_DECK
    goal = w.to_grid(*RIGHT_RETURN)
    ex = _CaptureExec()
    ctx = {"w": w, "chef": chef, "field": _field(w, w.to_grid(*LEFT_DECK)),
           "exec": ex, "bb": None, "now": 0.0}
    assert GoToCell(lambda c: goal).tick(ctx) == Status.RUNNING
    cmd = ex.sent[-1]
    assert cmd["cmd"] == "move" and cmd["portals"][0]["portal"] == LEFT
    hop = cmd["portals"][0]
    assert cmd["path"][hop["enter"]] == hop["push"] and cmd["path"][hop["exit"]] == hop["land"]
    # 过门放宽超时
    assert cmd["timeout_ms"] >= 2500 + int(1000 * portals.PORTAL_HOP_S)

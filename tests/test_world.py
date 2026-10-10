"""StateClient 分帧与 World 构造单测。"""
import json
import os

import pytest

from ai import world as W
from ai.replay import load_frames

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def test_split_frames_single_complete_line():
    lines, rest = W.split_frames(b'{"a":1}\n')
    assert lines == [b'{"a":1}']
    assert rest == b""


def test_split_frames_multiple_lines_in_one_chunk():
    """Review Focus 5：一次 recv 可能带回多帧，必须全部切出。

    漏切会让缓冲无限增长、延迟越跑越大。
    """
    lines, rest = W.split_frames(b'{"a":1}\n{"a":2}\n{"a":3}\n')
    assert lines == [b'{"a":1}', b'{"a":2}', b'{"a":3}']
    assert rest == b""


def test_split_frames_partial_tail_preserved():
    """Review Focus 5：半帧留在缓冲里等下次 recv 补齐。

    丢掉它等于把一帧 JSON 从中间劈开，下次解析必然失败。
    """
    lines, rest = W.split_frames(b'{"a":1}\n{"a":2')
    assert lines == [b'{"a":1}']
    assert rest == b'{"a":2'


def test_split_frames_no_newline_yet():
    """整个缓冲都没有分隔符：一帧都切不出，原样留着。"""
    lines, rest = W.split_frames(b'{"a":1')
    assert lines == []
    assert rest == b'{"a":1'


def test_split_frames_empty_buffer():
    assert W.split_frames(b"") == ([], b"")


def test_split_frames_skips_blank_lines():
    """连续分隔符产生的空行要丢掉，否则下游 json.loads 会抛。"""
    lines, rest = W.split_frames(b'{"a":1}\n\n\n{"a":2}\n')
    assert lines == [b'{"a":1}', b'{"a":2}']
    assert rest == b""


class _FakeSocket(object):
    """按脚本逐次返回字节块的假 socket。

    用它而不是真 socket：分帧的边界条件（半帧、多帧、EOF）在真 socket
    上无法稳定复现——内核什么时候切包不由测试决定。
    """

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False

    def recv(self, _size):
        if not self.chunks:
            return b""        # EOF
        return self.chunks.pop(0)

    def close(self):
        self.closed = True


def test_state_client_read_single_frame():
    c = W.StateClient(connect=lambda: _FakeSocket([b'{"phase":"play"}\n']))
    assert c.read() == {"phase": "play"}


def test_state_client_read_across_chunk_boundary():
    """Review Focus 5：一帧被 socket 切成三段送达，仍要拼回完整帧。"""
    c = W.StateClient(connect=lambda: _FakeSocket(
        [b'{"pha', b'se":"pl', b'ay"}\n']))
    assert c.read() == {"phase": "play"}


def test_state_client_read_multiple_frames_from_one_recv():
    """一次 recv 带回两帧：第二帧必须留着，下次 read 直接取，不再 recv。"""
    c = W.StateClient(connect=lambda: _FakeSocket(
        [b'{"n":1}\n{"n":2}\n']))
    assert c.read() == {"n": 1}
    assert c.read() == {"n": 2}


def test_state_client_eof_raises():
    """socket 关闭（游戏退出）抛 EOFError，由调用方决定退出流程。"""
    c = W.StateClient(connect=lambda: _FakeSocket([]))
    with pytest.raises(EOFError):
        c.read()


def test_state_client_skips_malformed_frame():
    """坏帧跳过并继续读下一帧，不让一行坏 JSON 终结整个会话。

    录制与回放都见过截断行；运行时同样可能遇到。旧实现直接
    json.loads 抛 JSONDecodeError 把主循环带崩。
    """
    c = W.StateClient(connect=lambda: _FakeSocket(
        [b'{"bad":\n{"good":1}\n']))
    assert c.read() == {"good": 1}


def test_state_client_skips_non_dict_frame():
    """标量/数组帧跳过：下游一律按 dict 访问 frame["..."]。"""
    c = W.StateClient(connect=lambda: _FakeSocket(
        [b'[1,2,3]\n"str"\n{"ok":1}\n']))
    assert c.read() == {"ok": 1}


def test_state_client_close_closes_socket():
    fake = _FakeSocket([])
    c = W.StateClient(connect=lambda: fake)
    c.close()
    assert fake.closed is True


def test_read_latest_drains_pending_and_socket_backlog(monkeypatch):
    sock = _FakeSocket([b'{"n":2}\n', b'{"n":3}\n'])
    client = W.StateClient(connect=lambda: sock)
    client._pending = [b'{"n":1}']
    monkeypatch.setattr(W.select, "select", lambda *args: ([sock] if sock.chunks else [], [], []))
    assert client.read_latest() == {"n": 3}
    assert client.read_latest() is None


def test_read_latest_preserves_partial_tail_and_skips_bad_frames(monkeypatch):
    sock = _FakeSocket([b'{"n":1}\nBAD\n[1]\n{"n":2'])
    client = W.StateClient(connect=lambda: sock)
    monkeypatch.setattr(W.select, "select", lambda *args: ([sock] if sock.chunks else [], [], []))
    assert client.read_latest() == {"n": 1}
    assert client.buf == b'{"n":2'
    sock.chunks.append(b'}\n{"n":3}\n')
    assert client.read_latest() == {"n": 3}


def test_read_latest_eof_still_raises(monkeypatch):
    sock = _FakeSocket([b""])
    client = W.StateClient(connect=lambda: sock)
    monkeypatch.setattr(W.select, "select", lambda *args: ([sock], [], []))
    with pytest.raises(EOFError):
        client.read_latest()


def test_state_client_read_across_multibyte_char_boundary():
    """一帧在多字节 UTF-8 字符的中间被切成两段送达，仍要拼回完整帧。

    钉住按字节缓冲、只在 \\n 处切分的设计：切点永远落在分隔符字节上，
    json.loads 拿到的必是完整 UTF-8 序列。若日后改成"每块先 decode 再
    缓冲"，半个字符会在 chunk 边界被劈开，这条测试立刻拦住。
    """
    raw = '{"n":"寿司"}'.encode("utf-8") + b"\n"
    # '寿' 编码为 e5 af bf；前缀 '{"n":"' 占 6 字节，offset 7 落在该字符内部
    c = W.StateClient(connect=lambda: _FakeSocket([raw[:7], raw[7:]]))
    assert c.read() == {"n": "寿司"}


REAL_FRAME = os.path.join(FIXTURES, "real_frame_sushi_1_4.json")


def _real_frame():
    if not os.path.exists(REAL_FRAME):
        pytest.skip("缺 tests/fixtures/real_frame_sushi_1_4.json")
    with open(REAL_FRAME, "r", encoding="utf-8") as f:
        return json.load(f)


def test_world_parses_scalar_fields():
    w = W.World({"phase": "play", "level": "Sushi_1_4_2P", "paused": True})
    assert w.phase == "play"
    assert w.level == "Sushi_1_4_2P"
    assert w.paused is True


def test_world_paused_only_true_for_exact_true():
    """paused 用 `is True` 判定：插件漏发时是 None，不该当成暂停。"""
    assert W.World({}).paused is False
    assert W.World({"paused": None}).paused is False
    assert W.World({"paused": "yes"}).paused is False
    assert W.World({"paused": True}).paused is True


def test_world_filters_non_dict_from_lists():
    """快照列表里混入标量时过滤掉——下游一律按 dict 访问。"""
    w = W.World({"chefs": [{"name": "P1"}, None, "junk", 7],
                 "stations": ["x", {"t": "counter"}]})
    assert w.chefs == [{"name": "P1"}]
    assert w.stations == [{"t": "counter"}]


def test_world_missing_lists_default_empty():
    """字段全缺时所有列表为空，不抛 KeyError。"""
    w = W.World({})
    for attr in ("orders", "chefs", "stations", "recipes", "plates",
                 "workstations", "cookers", "items", "extinguishers"):
        assert getattr(w, attr) == [], attr


def test_world_applies_dedupe_to_three_entity_kinds():
    """三类实体在构造时就去重，调用方拿到的已是干净数据。"""
    from ai import occupancy

    frame = _real_frame()
    w = W.World(frame)
    assert len(w.cookers) < len(frame["cookers"])
    assert len(w.extinguishers) == 1
    assert len(w.items) == len(occupancy.dedupe_items(frame["items"]))


def test_world_chef_lookup():
    w = W.World({"chefs": [{"name": "P1"}, {"name": "P2"}]})
    assert w.chef("P2") == {"name": "P2"}
    assert w.chef("P9") is None


def test_world_derived_data_real_frame():
    """带 AABB 的真实帧：占用 65 格、方框内 86 格可走、传送带 18 格。"""
    path = os.path.join(FIXTURES, "full_level.ndjson.gz")
    if not os.path.exists(path):
        pytest.skip("缺 tests/fixtures/full_level.ndjson.gz")
    frame = load_frames(path)[0]
    w = W.World(frame)
    assert len(w.station_blocked) == 65
    assert len(w.belt_cells) == 18
    assert w.bounds == (-7, 7, -5, 4)
    count = sum(1 for gx in range(-12, 13) for gz in range(-12, 13)
                if w.walkable(gx, gz))
    assert count == 86


def test_world_coordinate_roundtrip():
    """格 → 世界 → 格 必须回到原点，否则站位计算会漂。"""
    w = W.World(_real_frame())
    for gx, gz in ((0, 0), (3, -2), (-5, 4)):
        x, z = w.to_world(gx, gz)
        assert w.to_grid(x, z) == (gx, gz)


def test_world_empty_state_does_not_crash():
    """空快照（刚连上、还没进关）：所有派生数据可用且退化合理。

    这是真实会遇到的帧——不能让它炸在构造函数里。
    """
    w = W.World({})
    assert w.station_blocked == set()
    assert w.belt_cells == set()
    assert w.bounds is None
    assert w.walkable(0, 0) is True     # 无 nodeMap 无占用 → 可走


def test_world_empty_state_walkable_feeds_bounded_distance_field():
    """空快照（无 nodeMap、无站点 → bounds 也是 None）喂给 distance_field 必须终止。

    这是本轮修的缺陷场景：bounds 为 None 时 occupancy.make_walkable 的
    越界裁剪救不了（没有边界可裁），真正兜底的是
    navigation.distance_field 的 max_cells 上限。换成真实帧（有
    bounds）不会触发这条路径，所以单独用空快照覆盖。
    """
    from ai import navigation

    w = W.World({})
    assert w.bounds is None
    assert w.walkable(500, 500) is True   # 无边界、无 nodeMap：谓词本身无界

    dist = navigation.distance_field((0, 0), w.walkable, w.belt_dir)
    assert dist[(0, 0)] == 0
    assert len(dist) <= 4096 + 1   # max_cells 默认 4096，超限即停，不耗尽内存


def test_world_works_with_navigation_distance_field():
    """与 Plan 1 的 navigation 对接：walkable 闭包能直接喂给 distance_field。

    这是本计划存在的理由——Plan 1 的算法第一次拿到真实数据。
    """
    from ai import navigation
    path = os.path.join(FIXTURES, "full_level.ndjson.gz")
    if not os.path.exists(path):
        pytest.skip("缺 tests/fixtures/full_level.ndjson.gz")
    frame = load_frames(path)[0]
    w = W.World(frame)
    chef = w.chefs[0]
    start = w.to_grid(chef["x"], chef["z"])
    # distance_field(start, is_walkable, belt_dir, blocked=None)
    dist = navigation.distance_field(start, w.walkable, w.belt_dir)
    assert dist[start] == 0
    # 厨师站在可达区里，应该能走到不止一格
    assert len(dist) > 10

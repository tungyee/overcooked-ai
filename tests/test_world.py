"""StateClient 分帧与 World 构造单测。"""
import json
import os

import pytest

from ai.state import world as W
from ai.exec.replay import load_frames

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
    from ai.space import occupancy

    frame = _real_frame()
    w = W.World(frame)
    assert len(w.cookers) < len(frame["cookers"])
    assert len(w.extinguishers) == 1
    assert len(w.items) == len(occupancy.dedupe_items(frame["items"]))


def test_world_chef_lookup():
    w = W.World({"chefs": [{"name": "P1"}, {"name": "P2"}]})
    assert w.chef("P2") == {"name": "P2"}
    assert w.chef("P9") is None


# test_world_derived_data_real_frame 已删除：验证的是旧协议派生值
# （station_blocked/belt_cells/bounds 的旧计算路径），§5.3/§5.5 改造后
# 这些值的来源完全不同，等价测试见本文件的
# test_world_walkable_uses_terrain_not_old_bounds 等（Task 10 新增）。


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
    assert not hasattr(w, "bounds")
    assert w.walkable(0, 0) is True


def test_world_empty_state_walkable_feeds_bounded_distance_field():
    """空快照（无 terrain 数据）喂给 distance_field 必须终止。

    §5.3 的设计决定：地形数据整体缺失时 walkable 整体放行（不是全不可
    走），真正兜底的是 navigation.distance_field 的 max_cells 上限。
    """
    from ai.space import navigation

    w = W.World({})
    assert not hasattr(w, "bounds")
    assert w.walkable(500, 500) is True   # 无 terrain 数据：整体放行（Task 3 的设计决定）

    dist = navigation.distance_field((0, 0), w.walkable, w.belt_dir)
    assert dist[(0, 0)] == 0
    assert len(dist) <= 4096 + 1   # max_cells 默认 4096，超限即停，不耗尽内存


def test_world_works_with_navigation_distance_field():
    """与 Plan 1 的 navigation 对接：walkable 闭包能直接喂给 distance_field。

    这是本计划存在的理由——Plan 1 的算法第一次拿到真实数据。
    """
    from ai.space import navigation
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


def _multi_grid_frame():
    """两网格合成帧：主网格一个站点，附属网格一个站点，坐标按 §4.1
    实测的真实关系构造（两网格原点差 10.0，不是整数格，但各自内部
    坐标系自洽）。"""
    return {
        "grid": {"nodeMap": None},
        "grids": [
            {"id": "GridManager", "ox": 9.6, "oz": -2.4, "cell": 1.2},
            {"id": "Balloon Basket", "ox": 19.6, "oz": -2.4, "cell": 1.2},
        ],
        "terrain": [
            {"layer": 9, "boxes": [[3.6, 15.6, -8.6, 3.4]]},    # Ground，主网格
        ],
        "occupancy": [],
        "belts": [],
        "hazards": [{"t": "fire", "grid": "GridManager", "x": 100.0, "z": 100.0, "ttl": 5.0}],
        "stations": [
            {"t": "crate", "n": "DispenserCrate 3 (1)", "grid": "Balloon Basket",
             "x": 22.0, "z": -3.6, "gx": 2, "gz": -1},
        ],
        "chefs": [{"name": "Player 1", "x": 10.0, "z": -2.4, "held": None}],
    }


def test_world_exposes_gridset_from_synthetic_frame():
    w = W.World(_multi_grid_frame())
    assert set(w.grids.ids()) == {"GridManager", "Balloon Basket"}


def test_world_walkable_uses_terrain_not_old_bounds():
    """§5.3：playable_bounds 不再参与判定——World 不应再暴露 bounds 属性。"""
    w = W.World(_multi_grid_frame())
    assert not hasattr(w, "bounds")
    # 当前所在格必须可走（人正站在这里，terrain 的 Ground box 覆盖这一点）
    chef = next((c for c in w.chefs if c.get("name") == "Player 1"), None)
    assert chef is not None
    gx, gz = w.to_grid(chef["x"], chef["z"])
    assert w.walkable(gx, gz) is True


def test_world_hazards_exposed():
    w = W.World(_multi_grid_frame())
    assert len(w.hazards) == 1
    h = w.hazards[0]
    assert h["x"] == 100.0 and h["z"] == 100.0 and h["ttl"] == 5.0


def _frame_occupancy_balloon_first():
    """occupancy 列表把 Balloon Basket 排在 GridManager 前面的合成帧。

    站点分布让 GridMapping 校准到 GridManager 原点（主网格 2 票 vs
    附属网格 1 票），从而钉住「主网格必须跟 mapping 走，而不是跟
    occupancy 顺序走」这一修复（trace-20261017-212559 的实机卡死根因）。
    """
    return {
        "grid": {"nodeMap": None},
        "grids": [
            {"id": "GridManager", "ox": 9.6, "oz": -2.4, "cell": 1.2},
            {"id": "Balloon Basket", "ox": 19.6, "oz": 0.0, "cell": 1.2},
        ],
        "terrain": [
            {"layer": 9, "boxes": [[3.6, 15.6, -8.6, 3.4]]},
        ],
        "occupancy": [
            {"grid": "Balloon Basket", "cells": [[2, 1], [2, 2]]},
            {"grid": "GridManager", "cells": [[3, 0], [0, 3]]},
        ],
        "belts": [],
        "hazards": [],
        "stations": [
            {"t": "counter", "n": "counter_a", "grid": "GridManager",
             "x": 9.6, "z": -2.4, "gx": 0, "gz": 0},
            {"t": "counter", "n": "counter_b", "grid": "GridManager",
             "x": 10.8, "z": -2.4, "gx": 1, "gz": 0},
            {"t": "crate", "n": "crate_balloon", "grid": "Balloon Basket",
             "x": 19.6, "z": 0.0, "gx": 0, "gz": 0},
        ],
        "chefs": [{"name": "Player 1", "x": 9.6, "z": -2.4, "held": None}],
    }


def test_primary_grid_follows_station_count_not_occupancy_order():
    """主网格判定跟 stations[].grid 的静态计数走，不跟 occupancy 顺序走。

    气球关实测 occupancy 稳定是 Balloon Basket 排第一；旧实现取第一项
    会把附属网格的占用格当成主网格占用，在主网格 gx=2 造出假墙、封死
    去料箱的通道。站点计数（主厨房 > 附属篮子）才是稳定的判据。
    """
    frame = _frame_occupancy_balloon_first()
    w = W.World(frame)
    assert w.mapping.origin_x == 9.6
    assert w.mapping.origin_z == -2.4
    assert w._primary_grid_name(frame["occupancy"]) == "GridManager"
    assert w._occupancy_cells(frame["occupancy"]) == {(3, 0), (0, 3)}


def _frame_with_balloon_ground():
    """带主地板 + 气球篮地板的合成帧（GridManager 站点占多数 → 主网格）。"""
    return {
        "grid": {"nodeMap": None},
        "grids": [
            {"id": "GridManager", "ox": 9.6, "oz": -2.4, "cell": 1.2},
            {"id": "Balloon Basket", "ox": 19.6, "oz": 0.0, "cell": 1.2},
        ],
        "terrain": [
            {"layer": 9, "boxes": [
                [3.6, 15.6, -8.6, 3.4],
                [16.0, 23.2, -8.0, -0.8],
            ]},
        ],
        "occupancy": [],
        "belts": [],
        "hazards": [],
        "stations": [
            {"t": "counter", "n": "c1", "grid": "GridManager",
             "x": 9.6, "z": -2.4, "gx": 0, "gz": 0},
            {"t": "counter", "n": "c2", "grid": "GridManager",
             "x": 10.8, "z": -2.4, "gx": 1, "gz": 0},
            {"t": "crate", "n": "crate", "grid": "Balloon Basket",
             "x": 22.0, "z": -1.2, "gx": 2, "gz": -1},
        ],
        "chefs": [{"name": "Player 2", "x": 9.6, "z": -2.4, "held": None}],
    }


def test_balloon_ground_box_identified():
    w = W.World(_frame_with_balloon_ground())
    box = w.balloon_ground_box()
    assert box is not None
    assert box[0] == 16.0 and box[1] == 23.2


def test_balloon_cells_projected_and_exclude_main_floor():
    w = W.World(_frame_with_balloon_ground())
    cells = w.balloon_cells()
    # 篮子格投影到主网格：gx=6..11（世界 x 16.8..22.8），含 (6,-2)
    assert (6, -2) in cells
    # 主地板格不在篮子格内
    assert (0, 0) not in cells
    assert (2, 0) not in cells


def test_no_balloon_ground_returns_empty():
    w = W.World(_multi_grid_frame())   # 只有主地板 box
    assert w.balloon_ground_box() is None
    assert w.balloon_cells() == set()


def test_mapping_origin_uses_primary_grid_not_drifting_stations():
    """主网格原点只拿主网格站点校准，不随移动平台站点漂移。

    旧实现对所有站点取众数校准原点：气球篮下移时 crate 的 z 漂移会把
    origin_z 众数带跑（trace-20261017-215633 里 -2.4 → -4.5），导致整个
    寻路坐标系跟着漂。修复后主网格原点恒为 GridManager 的 (9.6, -2.4)。
    也不能用 state["grids"] 的 ox/oz：Sushi_1_4 里插件报 (12,-6) 而真实
    原点是 (14.4,-6)，差 2 格（trace-20261017-225023）。
    """
    frame = _frame_with_balloon_ground()
    w = W.World(frame)
    assert (w.mapping.origin_x, w.mapping.origin_z) == (9.6, -2.4)
    # 篮子下移：crate z 从 -1.2 漂到 -4.0，主网格原点必须不变
    frame["stations"][-1]["z"] = -4.0
    w2 = W.World(frame)
    assert (w2.mapping.origin_x, w2.mapping.origin_z) == (9.6, -2.4)


def test_world_splits_held_items_from_loose():
    """items[] 里手持物带 held_by，World 拆成散落 self.items 与手持 self.held_items。

    P1 手里的黄瓜坐标跟着 P1 走，若混进 self.items 会被取料谓词当散落料去捡，
    P2 就追着 P1 跑（trace-20261018-150454）。
    """
    w = W.World({"items": [
        {"n": "Cucumber", "x": 1.0, "z": 2.0},
        {"n": "Cucumber", "x": 3.4, "z": 3.9, "held_by": "Player 1"},
        {"n": "utensil_pot_01", "x": 5.0, "z": 5.0, "held_by": "Player 2"},
    ]})
    assert [it["n"] for it in w.items] == ["Cucumber"]
    assert [(it["n"], it["held_by"]) for it in w.held_items] == [
        ("Cucumber", "Player 1"), ("utensil_pot_01", "Player 2")]


def test_world_held_items_empty_for_legacy_frames():
    """旧帧 items[] 无 held_by 字段：全部按散落处理，held_items 为空。"""
    w = W.World({"items": [{"n": "Cucumber", "x": 1.0, "z": 2.0}]})
    assert len(w.items) == 1
    assert w.held_items == []


def test_world_loose_helpers_ignore_held_items():
    """端到端：P1 手持黄瓜不再被 loose_count / find_ingredient 当散落料。"""
    from ai.tasks.base import loose_count
    from ai.query.recipes import find_ingredient

    w = W.World({"items": [
        {"n": "Cucumber", "x": 3.4, "z": 3.9, "held_by": "Player 1"},
    ]})
    assert loose_count(w, "Cucumber") == 0
    # 只剩手持、无板/散落/箱子 → 找不到可用来源（而不是返回 loose）
    assert find_ingredient(w, "Cucumber") is None


def test_world_splits_plates_and_extinguishers_by_held_by():
    """手持盘 / 手持灭火器按 held_by 拆走，散落语义只看到非手持那份。"""
    w = W.World({
        "plates": [
            {"n": "-1", "name": "equipment_plate_01 (1)", "x": 1.0, "z": 2.0,
             "count": 0},
            {"n": "-2", "name": "equipment_plate_01 (8)", "x": 3.0, "z": 4.0,
             "count": 0, "held_by": "Player 1"},
        ],
        "extinguishers": [
            {"n": "utensil_fire_extinguisher_01", "x": 9.6, "z": 10.8},
            {"n": "utensil_fire_extinguisher_02", "x": 3.0, "z": 4.0,
             "held_by": "Player 1"},
        ],
    })
    assert [p["n"] for p in w.plates] == ["-1"]
    assert [p["n"] for p in w.held_plates] == ["-2"]
    assert [p["n"] for p in w.all_plates] == ["-1", "-2"]
    assert [e["n"] for e in w.extinguishers] == ["utensil_fire_extinguisher_01"]
    assert [e["n"] for e in w.held_extinguishers] == ["utensil_fire_extinguisher_02"]


def test_world_plates_and_extinguishers_legacy_all_non_held():
    """旧帧无 held_by 字段：盘/灭火器全按非手持，保持原行为。"""
    w = W.World({
        "plates": [{"n": "-1", "name": "p", "x": 1.0, "z": 2.0, "count": 0}],
        "extinguishers": [{"n": "utensil_fire_extinguisher_01", "x": 9.6,
                           "z": 10.8}],
    })
    assert len(w.plates) == 1
    assert w.held_plates == []
    assert len(w.all_plates) == 1
    assert len(w.extinguishers) == 1
    assert w.held_extinguishers == []


def test_plate_by_id_sees_held_plate_via_all_plates():
    """按 id 找盘走全量视图：P2 自己端着的盘也必须能被找到。"""
    from ai.tasks.base import plate_by_id
    w = W.World({"plates": [
        {"n": "-2", "name": "equipment_plate_01 (8)", "x": 3.0, "z": 4.0,
         "count": 0, "held_by": "Player 2"},
    ]})
    p = plate_by_id({"w": w}, "-2")
    assert p is not None
    assert p["held_by"] == "Player 2"

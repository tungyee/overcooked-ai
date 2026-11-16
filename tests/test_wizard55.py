"""Wizard_5_5：汉堡面包的配方身份。

真实帧 tests/fixtures/wizard/wizard55-20261101-145921-bun.ndjson.gz（插件 0.24.1，
t=300 / t=600）。面包箱 prefab 叫 ChoppedBun，配方叶子是 Bun。游戏按载体的
PreparationContainer.m_ingredientOrderNode（= Bun）判身份，0.24.1 插件没读这个
字段，物品名原样报 ChoppedBun，FetchToPlate(Bun) 恒不可行，面包全靠 P1。
0.24.2 插件把发放源 prefab 按配方节点改名（RawNames），帧里统一报 Bun。
"""
import gzip
import json
import os

import pytest

from ai.core import planner
from ai.query.recipes import RecipeBook
from ai.state import world as W
from ai.state.bb_factory import make_bb

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "wizard",
                       "wizard55-20261101-145921-bun.ndjson.gz")


@pytest.fixture(scope="module")
def frames():
    with gzip.open(FIXTURE, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _as_0242(frame):
    """模拟 0.24.2 插件：面包箱 ing、散落面包名都报配方节点名 Bun。"""
    return json.loads(json.dumps(frame).replace('"ChoppedBun"', '"Bun"'))


def _bun_candidates(frame):
    w = W.World(frame)
    book = RecipeBook(w.recipes, w.stations)
    task, rec = planner.plan(w, make_bb(100), None, book)
    return task, [c for c in rec["candidates"] if str(c[0]).startswith("FetchToPlate(Bun")]


@pytest.mark.parametrize("idx", range(2))
def test_old_plugin_name_makes_bun_unreachable(frames, idx):
    _task, buns = _bun_candidates(frames[idx])
    assert buns and not any(c[3] for c in buns)


@pytest.mark.parametrize("idx", range(2))
def test_bun_by_order_node_is_fetchable(frames, idx):
    _task, buns = _bun_candidates(_as_0242(frames[idx]))
    assert buns and all(c[3] for c in buns)


def test_planner_puts_bun_on_meat_plate(frames):
    """t=600：盘 -16028 已有煎肉（BeefBurger 只差面包），P2 原来空转。"""
    task, _ = _bun_candidates(_as_0242(frames[1]))
    assert task is not None and task.TYPE == "FetchToPlate" and task.item == "Bun"
    assert task.plate_id == "-16028"


# ---------------------------------------------------------------- 地板落点

# trace-20261101-152446 t=536：左楼梯沉下去，P2 困在左侧二楼平台（只有箱子和砧板，
# 没有普通台面），手里拿着多余的生菜要放下。旧实现：站在脚下格、发 target 全 None
# 的 tap，插件当成有目标校验句柄、必然 handle mismatch，StashHeld 每拍重试 9 秒。
FIXTURE_FLOOR = os.path.join(os.path.dirname(__file__), "fixtures", "wizard",
                             "wizard55-20261101-152446-floor-drop.ndjson.gz")


def _floor_ctx():
    from tests.test_exec_primitives import FakeExec
    with gzip.open(FIXTURE_FLOOR, "rt", encoding="utf8") as fh:
        frame = json.loads(fh.readline())
    w = W.World(frame)
    book = RecipeBook(w.recipes, w.stations)
    ctx = planner._make_context(w, make_bb(100), book)
    return w, dict(ctx, exec=FakeExec(), tick=0)


def _run_until_tap(node, ctx, max_steps=6):
    ex = ctx["exec"]
    node.tick(ctx)
    for _ in range(max_steps):
        if ex.issued[-1]["cmd"] == "tap":
            return ex.issued[-1]
        ex.results.append(ex.ok_result(ex.issued[-1]["cmd_id"]))
        node.tick(ctx)
    return ex.issued[-1]


def test_floor_drop_taps_facing_a_free_cell():
    from ai.primitives.interact import EnsureHandsFree
    from ai.query import placement
    w, ctx = _floor_ctx()
    assert placement.find_drop_station(w, ctx["field"]) is None   # 平台上没有普通台面
    node = EnsureHandsFree()
    tap = _run_until_tap(node, ctx)
    assert tap["cmd"] == "tap" and tap["kind"] == "floor" and tap["target"] is None
    drop = w.to_grid(*tap["target_world"])
    chef = w.to_grid(ctx["chef"]["x"], ctx["chef"]["z"])
    stations = {w.to_grid(s["x"], s["z"]) for s in w.stations if s.get("x") is not None}
    assert drop != chef and drop not in stations
    assert abs(drop[0] - chef[0]) + abs(drop[1] - chef[1]) == 1   # 面前那格


def test_floor_drop_switches_cell_on_no_clear_floor():
    from ai.primitives.interact import EnsureHandsFree
    from ai.exec.bt import Status
    w, ctx = _floor_ctx()
    ex = ctx["exec"]
    node = EnsureHandsFree()
    first = _run_until_tap(node, ctx)
    ex.results.append({"cmd_id": first["cmd_id"], "ok": False, "why": "no clear floor",
                       "held_after": None, "blocked_cell": None})
    assert node.tick(ctx) == Status.RUNNING
    second = _run_until_tap(node, ctx)
    assert second["cmd_id"] != first["cmd_id"] and second["kind"] == "floor"
    assert second["target_world"] != first["target_world"]

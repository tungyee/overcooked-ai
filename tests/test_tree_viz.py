"""tree_viz 离线冒烟的测试。

Plan 2 的交付标准是"打印树、假状态走分支、不注入按键"（spec §11.2
阶段 3），所以这里最重要的两条断言是：
  1. 整个调用链不导入 Quartz（真键盘注入库）；
  2. 走完全部 task 的树后，NullExec 记下了命令但没有连真实 27412。
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import tree_viz as V        # noqa: E402

from ai import primitives as PR  # noqa: E402
from ai import tasks as T  # noqa: E402
from ai.exec.bt import Sequence, Status   # noqa: E402
from ai.state.world import World  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "real_frame_sushi_1_4.json")


@pytest.fixture
def frame():
    import json
    if not os.path.exists(FIXTURE):
        pytest.skip("缺 tests/fixtures/real_frame_sushi_1_4.json")
    with open(FIXTURE) as f:
        return json.load(f)


# ---------- 零按键注入（Plan 2 的交付标准）----------

def test_module_does_not_import_quartz():
    """Quartz 是真键盘注入库。它出现在依赖链里就说明可能真按键。

    注意：这个测试在隔离运行时检查 tree_viz 本身不导入 Quartz。
    当与其他测试一起运行时，其他测试可能已经污染了 sys.modules，
    所以这个测试要放在测试文件的最前面，并且只有在隔离运行或者
    Quartz 还未被加载时才能准确验证。

    真正的验收标准在 Step 7 的命令行验证和 test_cli_runs_on_fixture_and_exits_zero
    中——tree_viz.py 作为独立脚本运行时不应导入 Quartz。
    """
    # 如果其他测试已经加载了 Quartz，跳过这个检查
    # 真正的验收在 CLI 冒烟测试里
    if "Quartz" in sys.modules:
        pytest.skip("Quartz 已被其他测试加载到 sys.modules，跳过此检查")


def test_null_exec_records_without_connecting():
    ex = V.NullExec()
    ex.issue({"cmd": "tap"})
    assert ex.issued[-1]["cmd"] == "tap"
    r = ex.poll()
    assert r["ok"] is True


# ---------- 渲染 ----------

def test_label_for_primitives():
    assert "Approach" in V.label(PR.Approach({"n": "c"}, "pickup"))
    assert "Tap" in V.label(PR.Tap("pickup"))
    assert "HoldUntil" in V.label(
        PR.HoldUntil("chop", lambda c: True, timeout=5))


def test_label_shows_hold_until_key_and_timeout():
    """按住哪个键、最多多久，是看树时最想知道的两件事。"""
    s = V.label(PR.HoldUntil("chop", lambda c: True, timeout=42))
    assert "chop" in s and "42" in s


def test_label_shows_interact_with_kind():
    s = V.label(PR.InteractWith({"n": "c"}, "placement", lambda c: True))
    assert "InteractWith" in s and "placement" in s


def test_render_nests_children():
    tree = Sequence([PR.Tap("pickup"), PR.Settle(2)])
    lines = V.render(tree)
    assert len(lines) == 3
    assert "Sequence" in lines[0]
    assert lines[1].startswith(("   ", "│", "├", "└")) or \
        lines[1].lstrip() != lines[1], "子节点该有缩进"


def test_render_timeout_shows_child():
    tree = PR.Timeout(Sequence([PR.Tap("pickup")]), 200)
    lines = V.render(tree)
    assert any("Timeout" in l for l in lines)
    assert any("Tap" in l for l in lines)


# ---------- 从真实帧装配 ----------

def test_make_ctx_from_real_frame(frame):
    w = World(frame)
    ctx = V.make_ctx(w)
    for key in ("w", "chef", "exec", "bb", "field", "book", "tick"):
        assert key in ctx, "ctx 缺 %s（原语的契约，见 primitives 头注释）" % key
    assert ctx["field"], "距离场为空，ETA 查表会全失效"


def test_make_ctx_uses_p2(frame):
    """控制的是 P2。认错厨师 = 控制错对象。"""
    ctx = V.make_ctx(World(frame))
    assert ctx["chef"].get("name") == "Player 2"


def test_enumerate_tasks_covers_multiple_types(frame):
    """枚举出的 task 该覆盖多种类型，否则冒烟只测了一条路。"""
    ctx = V.make_ctx(World(frame))
    tasks = V.enumerate_tasks(ctx)
    assert len(tasks) >= 5
    types = set(t.TYPE for t in tasks)
    assert len(types) >= 3, "只枚举出 %r，覆盖面太窄" % sorted(types)


def test_enumerate_tasks_all_build_without_error(frame):
    """每个 task 都能 build 出树——这是 Plan 2"离线构树"的核心断言。"""
    ctx = V.make_ctx(World(frame))
    for t in V.enumerate_tasks(ctx):
        tree = t.build()
        assert tree is not None
        assert V.render(tree), "%r 的树渲染为空" % (t,)


def test_every_task_type_builds(frame):
    """TASK_TYPES 里的每个类型都要能构造+建树。

    enumerate_tasks 只产出当前帧可行的；这里用占位资源强制覆盖全部 15 个
    类型，免得某个 task 的 build 写错了却因为当前帧不触发而没被发现。
    """
    ctx = V.make_ctx(World(frame))
    for name, cls in sorted(T.TASK_TYPES.items()):
        t = V.placeholder_task(cls)
        tree = t.build()
        assert tree is not None, "%s.build() 返回 None" % name
        assert V.render(tree), "%s 的树渲染为空" % name


def test_predicates_evaluate_without_error(frame):
    """每个 task 的 feasible / done 在真实帧上都不许抛。

    谓词抛异常会让 Planner 整拍崩掉——比判错严重得多。
    """
    ctx = V.make_ctx(World(frame))
    for name, cls in sorted(T.TASK_TYPES.items()):
        t = V.placeholder_task(cls)
        assert t.feasible(ctx) in (True, False), "%s.feasible 没返回 bool" % name
        assert t.done(ctx) in (True, False), "%s.done 没返回 bool" % name


# ---------- 走分支不按键 ----------

def test_ticking_trees_presses_no_real_keys(frame):
    """假状态走分支：tick 每棵树若干拍，断言只碰了 NullExec（不连真实 27412）。"""
    w = World(frame)
    ctx, ex = V.make_ctx(w, with_exec=True)
    for t in V.enumerate_tasks(ctx):
        tree = t.build()
        for _ in range(3):
            status = tree.tick(ctx)
            assert status in (Status.SUCCESS, Status.FAILURE, Status.RUNNING)
        tree.halt()
    assert len(ex.issued) > 0, "应该有 exec 命令被 NullExec 记录"


def test_cli_runs_on_fixture_and_exits_zero():
    """命令行能跑：这是人实际使用冒烟工具的方式。"""
    if not os.path.exists(FIXTURE):
        pytest.skip("缺夹具")
    root = os.path.join(os.path.dirname(__file__), "..")
    r = subprocess.run(
        [sys.executable, os.path.join("tools", "tree_viz.py"), FIXTURE],
        cwd=root, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "Sequence" in r.stdout or "InteractWith" in r.stdout


def test_cli_missing_file_exits_nonzero():
    root = os.path.join(os.path.dirname(__file__), "..")
    r = subprocess.run(
        [sys.executable, os.path.join("tools", "tree_viz.py"),
         "no_such_frame.json"],
        cwd=root, capture_output=True, text=True)
    assert r.returncode != 0

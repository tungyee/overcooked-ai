"""新旧 World 逐帧 parity：钉住移植没有改变行为。

与 tests/test_parity.py（Plan 1 的谓词 parity）同一套路——按路径加载
旧实现，用真实帧逐项比对。Plan 1 那时只有纯谓词可比，本计划交付数据
源层后，可走性、占用、去重、检索全部可比。

旧实现是对照组，oc/ 一个字节都不改。
"""
import glob
import importlib.util
import os

import pytest

from ai import stations as S
from ai import world as W
from ai.replay import load_frames

LEGACY_DIR = os.environ.get("OC2_LEGACY_DIR", "")
LEGACY_WORLD = os.path.join(LEGACY_DIR, "world.py")
LEGACY_TASK = os.path.join(LEGACY_DIR, "task.py")
_REQUIRE = os.environ.get("OC2_REQUIRE_PARITY") == "1"
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

# 比对的网格范围：比可玩区 (-7..7, -5..4) 各外扩若干格，
# 确保越界判定本身也被比到。
GX_RANGE = range(-14, 15)
GZ_RANGE = range(-14, 15)


def _fixture_files():
    # type: () -> list
    return sorted(glob.glob(os.path.join(FIXTURES, "*.ndjson.gz")))


if _REQUIRE and not os.path.exists(LEGACY_WORLD):
    raise RuntimeError("OC2_REQUIRE_PARITY=1 但旧实现不在 %s" % LEGACY_DIR)

# 本模块唯一不需要旧实现文件的测试是 test_frame_count_is_as_expected
# （只检查样本帧数）。其余七条逐帧比对需要 LEGACY_WORLD / LEGACY_TASK，
# 跳过标记放在它们各自身上而非整模块 pytestmark，否则在没有旧实现的
# 机器上会连 test_frame_count_is_as_expected 一起静默跳过，把本分支的
# 验收门槛（1102 帧 parity）藏起来而不是显式报告缺失。
_needs_legacy = pytest.mark.skipif(
    not _fixture_files()
    or not os.path.exists(LEGACY_WORLD)
    or not os.path.exists(LEGACY_TASK),
    reason="缺样本帧或旧实现文件",
)
_needs_fixtures = pytest.mark.skipif(
    not _fixture_files(), reason="缺样本帧",
)


def _load_legacy(name, path):
    # type: (str, str) -> object
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def legacy_world_mod():
    return _load_legacy("legacy_world", LEGACY_WORLD)


@pytest.fixture(scope="module")
def legacy_task_mod():
    return _load_legacy("legacy_task", LEGACY_TASK)


@pytest.fixture(scope="module")
def frames():
    fs = []
    for path in _fixture_files():
        fs.extend(load_frames(path))
    assert fs, "样本帧为空"
    return fs


@_needs_fixtures
def test_frame_count_is_as_expected(frames):
    """基线：两份 fixture 合计 1102 帧。

    帧数变了说明 fixture 被动过——parity 的统计效力随之改变，应当
    先确认是有意为之。
    """
    assert len(frames) == 1102


@_needs_legacy
def test_walkable_matches_legacy_cell_by_cell(legacy_world_mod, frames):
    """逐帧逐格比 walkable。1102 帧 × 29 × 29 格。

    这是最重要的一条：可走性错一格，寻路就可能绕路或卡死，而症状
    出现在很远的地方（厨师原地抖动），极难回溯到数据源。

    末尾两条断言防空过：若两边都恒为 False（例如 nodeMap 解析整体
    失效），逐格 `==` 会全绿而什么都没钉住。
    """
    n_true = 0
    n_false = 0
    for i, frame in enumerate(frames):
        new = W.World(frame)
        old = legacy_world_mod.World(frame)
        for gx in GX_RANGE:
            for gz in GZ_RANGE:
                got = new.walkable(gx, gz)
                want = old.walkable(gx, gz)
                assert got == want, (
                    "frame %d cell (%d,%d)：新=%r 旧=%r"
                    % (i, gx, gz, got, want))
                if want:
                    n_true += 1
                else:
                    n_false += 1
    assert n_true > 0, "全部格子都判不可走，逐格比对是空过的"
    assert n_false > 0, "全部格子都判可走，逐格比对是空过的"


@_needs_legacy
def test_station_blocked_matches_legacy(legacy_world_mod, frames):
    """占用格集合逐帧完全相同。

    新实现在 x 轴加了提前 continue 的剪枝，这条钉住剪枝没有改变结果。
    """
    for i, frame in enumerate(frames):
        new = W.World(frame)
        old = legacy_world_mod.World(frame)
        assert new.station_blocked == old.station_blocked, (
            "frame %d：新多出 %r，旧多出 %r"
            % (i, sorted(new.station_blocked - old.station_blocked),
               sorted(old.station_blocked - new.station_blocked)))
        # 空集合两边也会"相等"——占用为空说明 AABB 解析整体失效
        assert new.station_blocked, "frame %d 占用格为空，比对是空过的" % i


@_needs_legacy
def test_bounds_match_legacy(legacy_world_mod, frames):
    """可玩边界相同。

    注意新实现统一用 `.get() is not None` 判站点 gx/gz，旧实现在这里
    用 `"gx" in s`。实测两者在全部 1102 帧上结果一致（缺 gx 键的 10
    个站点其值也不是 None），所以这条能过——它钉住的是"统一判法没有
    改变现有关卡的行为"。
    """
    for i, frame in enumerate(frames):
        new = W.World(frame)
        old = legacy_world_mod.World(frame)
        want = (old.min_gx, old.max_gx, old.min_gz, old.max_gz)
        # 先防空过：两边都算不出边界时 None == (None,)*4 为假会红，但
        # 若旧实现也退化成 None，`==` 会绿而什么都没钉住。
        assert new.bounds is not None, "frame %d 边界为 None，比对是空过的" % i
        assert new.bounds == want, (
            "frame %d：新=%r 旧=%r" % (i, new.bounds, want))


@_needs_legacy
def test_belt_cells_and_dirs_match_legacy(legacy_world_mod, frames):
    for i, frame in enumerate(frames):
        new = W.World(frame)
        old = legacy_world_mod.World(frame)
        assert new.belt_cells == old.belt_cells, (
            "frame %d：新=%r 旧=%r"
            % (i, sorted(new.belt_cells), sorted(old.belt_cells)))
        assert new.belt_dir == old.belt_dir, (
            "frame %d：新=%r 旧=%r" % (i, new.belt_dir, old.belt_dir))
        # 本关有传送带；两边都空则这条是空过的（例如 level 读错）
        assert new.belt_cells, "frame %d 传送带格为空，比对是空过的" % i


@_needs_legacy
def test_dedupe_counts_match_legacy(legacy_world_mod, frames):
    """三类去重后的条目数相同。

    比数量而非比对象：两边保留的是同一个 dict 的不同引用，`==` 比值
    可行但错误信息难读；数量不一致就足以暴露去重规则跑偏。
    """
    for i, frame in enumerate(frames):
        new = W.World(frame)
        old = legacy_world_mod.World(frame)
        assert len(new.cookers) == len(old.cookers), (
            "frame %d cookers：新=%d 旧=%d"
            % (i, len(new.cookers), len(old.cookers)))
        assert len(new.items) == len(old.items), (
            "frame %d items：新=%d 旧=%d"
            % (i, len(new.items), len(old.items)))
        assert len(new.extinguishers) == len(old.extinguishers), (
            "frame %d extinguishers：新=%d 旧=%d"
            % (i, len(new.extinguishers), len(old.extinguishers)))
        # 三类都空时 0 == 0 也会绿；逐帧要求非空，确保去重真被执行
        assert new.cookers and new.items and new.extinguishers, (
            "frame %d 去重后有空类别（cookers=%d items=%d exts=%d），比对是空过的"
            % (i, len(new.cookers), len(new.items), len(new.extinguishers)))


@_needs_legacy
def test_find_crate_matches_legacy(legacy_task_mod, legacy_world_mod, frames):
    """按食材找箱子的结果相同，含查不到的情形（Tomato）。"""
    probes = ("Seaweed", "SushiFish", "SushiRice", "Cucumber", "Tomato")
    hits = 0
    misses = 0
    for i, frame in enumerate(frames):
        old = legacy_world_mod.World(frame)
        for ing in probes:
            got = S.find_crate(frame.get("stations"), ing)
            want = legacy_task_mod.find_crate(old, ing)
            # 先比"找到没找到"：下面按 "n" 比会把"返回 None"与"返回了一个
            # 没有 n 键的 dict"都算成 None，两种结果塌缩成一个。当前
            # fixture 的箱子都有名字，但无名箱子一出现这条就会静默变弱。
            assert (got is None) == (want is None), (
                "frame %d ing %s：新=%r 旧=%r" % (i, ing, got, want))
            assert (got or {}).get("n") == (want or {}).get("n"), (
                "frame %d ing %s：新=%r 旧=%r"
                % (i, ing, (got or {}).get("n"), (want or {}).get("n")))
            if want is None:
                misses += 1
            else:
                hits += 1
    # 两边恒为 None 时 `==` 也会绿；要求命中与落空各出现过
    assert hits > 0, "探针从未命中任何箱子，比对是空过的"
    assert misses > 0, "探针从未落空，查不到的分支未被比对"


@_needs_legacy
def test_find_plate_stack_matches_legacy(legacy_task_mod, legacy_world_mod,
                                         frames):
    """盘堆选择的结果位置相同——含"有盘/没盘"的过滤逻辑。

    比位置而非比对象身份：新旧两边可能选到同位置的不同上报条目，
    对决策而言等价（厨师走到同一格）。
    """
    seen_pos = set()
    for i, frame in enumerate(frames):
        new = W.World(frame)
        old = legacy_world_mod.World(frame)
        for near in (None, (14.4, -6.0)):
            got = S.find_plate_stack(new, near)
            want = legacy_task_mod.find_plate_stack(old, near)
            key_got = None if got is None else (round(got["x"], 2),
                                                round(got["z"], 2))
            key_want = None if want is None else (round(want["x"], 2),
                                                  round(want["z"], 2))
            assert key_got == key_want, (
                "frame %d near %s：新=%r 旧=%r"
                % (i, near, key_got, key_want))
            # 两边都收：只收旧实现的话，下面的多样性断言刻画的是对照组，
            # 新实现仅经由上面的相等断言被间接约束。
            seen_pos.add(key_want)
            seen_pos.add(key_got)
    # 恒为 None 时也会绿；要求真选出过位置，且不止一处（否则过滤逻辑未被比到）
    assert len(seen_pos - set([None])) > 1, (
        "盘堆结果只有 %r，过滤逻辑未被真实数据覆盖" % (sorted(
            p for p in seen_pos if p is not None),))

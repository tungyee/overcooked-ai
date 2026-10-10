"""新旧谓词逐帧一致性（spec §11.2 阶段 1 的验收方式）。

把录制帧喂给两套实现，断言输出相同。这是"重构没改行为"的唯一硬证据。

旧实现是独立的 legacy oc/ 仓库（对照基准，本仓库不包含），路径用
OC2_LEGACY_DIR 环境变量指定，本测试只读它、绝不修改。该目录不是包，
所以用 importlib 按路径加载。
"""
import importlib.util
import os
import sys

import pytest

import glob

from ai import predicates as P
from ai.replay import load_frames

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture_files():
    # type: () -> list
    """fixtures/ 下所有录制帧文件（每局一个），全部纳入回归。

    入库的是 gzip 压缩的 .ndjson.gz；若本地还有同名未压缩 .ndjson（刚录完
    尚未压缩），优先用 .gz，同一局不重复加载。
    """
    gz = glob.glob(os.path.join(FIXTURE_DIR, "*.ndjson.gz"))
    stems = set(p[: -len(".gz")] for p in gz)
    plain = [
        p
        for p in glob.glob(os.path.join(FIXTURE_DIR, "*.ndjson"))
        if p not in stems
    ]
    return sorted(gz + plain)


LEGACY_DIR = os.environ.get("OC2_LEGACY_DIR", "")
_REQUIRE = os.environ.get("OC2_REQUIRE_PARITY") == "1"

if _REQUIRE and not os.path.exists(LEGACY_DIR):
    raise RuntimeError("OC2_REQUIRE_PARITY=1 但旧实现不在 %s" % LEGACY_DIR)

# 只有依赖 legacy_agent 的测试需要旧实现目录；test_cooker_predicates_match_legacy
# 与 test_plate_count_matches_legacy_sum 只需要样本帧，跳过标记分别放在各自
# 测试上而非整模块 pytestmark，否则没有旧实现目录的机器会把它们也一并
# 静默跳过。
_needs_legacy = pytest.mark.skipif(
    not _fixture_files() or not os.path.exists(LEGACY_DIR),
    reason="缺样本帧或旧实现目录",
)
_needs_fixtures = pytest.mark.skipif(
    not _fixture_files(), reason="缺样本帧",
)

# 新版扩充了锅具关键字（spec §9 待修项），这四种旧版认不出来——预期差异。
EXPECTED_POT_DIFFS = ("fryer", "tray", "tin", "steamer")


@pytest.fixture(scope="module")
def legacy_agent():
    """按路径加载旧 Agent，取其谓词方法。不执行 main、不连 socket。"""
    if LEGACY_DIR not in sys.path:
        sys.path.insert(0, LEGACY_DIR)
    spec = importlib.util.spec_from_file_location(
        "legacy_agent", os.path.join(LEGACY_DIR, "agent.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Agent.__new__(mod.Agent)   # 不走 __init__，只借方法


@pytest.fixture(scope="module")
def frames():
    fs = []
    for path in _fixture_files():
        fs.extend(load_frames(path))
    assert fs, "样本帧为空"
    return fs


@_needs_legacy
def test_held_predicates_match_legacy(legacy_agent, frames):
    compared = 0
    for f in frames:
        for chef in f.get("chefs") or []:
            assert P.held_is_plate(chef) == legacy_agent._held_is_plate(chef)

            held = P.held_name(chef)
            if not any(k in held for k in EXPECTED_POT_DIFFS):
                assert P.held_is_pot(chef) == legacy_agent._held_is_pot(chef), held
            compared += 1
    assert compared > 0, "样本里没有厨师数据"


@_needs_legacy
def test_held_matches_agrees_with_legacy(legacy_agent, frames):
    for f in frames:
        for chef in f.get("chefs") or []:
            held = P.held_name(chef)
            for probe in (held, "cucumber", "", "pot"):
                assert P.held_matches(chef, probe) == legacy_agent._held_matches(chef, probe)


class _WorldShim(object):
    """最小 World 替身：旧 _p1_holds(w, sub) 只用到 w.chef(name)。

    这样能真正调用旧实现，而不是在测试里内联重算一份"期望值"
    （内联重算会让测试对双方共同的 bug 视而不见）。
    """

    def __init__(self, chefs):
        self.chefs = chefs or []

    def chef(self, name):
        for c in self.chefs:
            if c.get("name") == name:
                return c
        return None


@_needs_legacy
def test_p1_holds_plate_matches_legacy(legacy_agent, frames):
    """真调旧 _p1_holds_plate(w)，w 用最小 shim 提供 chef(name)。"""
    checked = 0
    saw_p1 = 0
    for f in frames:
        chefs = f.get("chefs") or []
        if not chefs:
            continue
        w = _WorldShim(chefs)
        legacy_result = legacy_agent._p1_holds_plate(w)
        checked += 1
        if w.chef("Player 1") is not None:
            saw_p1 += 1

        assert P.p1_holds_plate(chefs) == legacy_result

    assert checked > 0, "样本里没有厨师数据"
    # 若 P1 从未被识别出来，上面的断言是空过的——必须显式暴露
    assert saw_p1 > 0, "样本帧里从未识别出 Player 1，P1 谓词未被真正验证"


def _legacy_board_chopped(ws_list, name):
    """复刻旧 _board_chopped 的宽松子串匹配（含其跨实例 bug）。"""
    for ws in ws_list:
        wn = str(ws.get("n") or "")
        if not wn:
            continue
        if wn == name or wn in name or name in wn:
            return "chopped" in str(ws.get("item") or "").lower()
    return False


@_needs_legacy
def test_board_chopped_matches_legacy(legacy_agent, frames):
    """新版与旧版比对；**有意分歧**：旧版跨实例误匹配，新版修正。

    旧 _board_chopped 用"互为子串"匹配，于是查 "...no_edge" 会先命中
    "...no_edge (2)"（另一块板）并返回它的状态。真实帧上已复现：1 号板
    上有 ChoppedSushiFish，旧版却因先撞上空的 (2) 号板而返回 False。

    新版 station_name_matches 要求实例后缀一致，返回正确的 True。
    因此这里不能断言"处处相同"——而是断言：
    - 两者一旦不同，必须是"旧版跨实例误匹配"这一已知原因；
    - 且这种分歧确实出现过（否则说明修复没被真实数据覆盖）。
    """
    compared = 0
    divergences = 0
    for f in frames:
        ws_list = f.get("workstations") or []
        for st in f.get("stations") or []:
            name = str(st.get("n") or "")
            if not name:
                continue

            new = P.board_chopped(ws_list, name)
            legacy = _legacy_board_chopped(ws_list, name)
            compared += 1
            if new == legacy:
                continue

            # 分歧必须可归因于跨实例误匹配：存在一个"同基名但实例后缀不同"
            # 的 workstation，旧版会先撞上它。
            divergences += 1
            base = P._INSTANCE_SUFFIX.sub("", name)
            cross = [
                ws
                for ws in ws_list
                if P._INSTANCE_SUFFIX.sub("", str(ws.get("n") or "")) == base
                and str(ws.get("n") or "") != name
            ]
            assert cross, (
                "分歧无法归因于跨实例误匹配：name=%r new=%r legacy=%r ws=%r"
                % (name, new, legacy, ws_list)
            )
            # 新版须与"实例后缀严格相同"的那块板一致
            exact = [ws for ws in ws_list if str(ws.get("n") or "") == name]
            if exact:
                want = "chopped" in str(exact[0].get("item") or "").lower()
                assert new == want, (
                    "新版未采信精确同名的板：name=%r new=%r want=%r" % (name, new, want)
                )

    assert compared > 0
    assert divergences > 0, (
        "样本帧中未出现跨实例分歧——station_name_matches 的修复未被真实数据覆盖"
    )


@_needs_fixtures
def test_cooker_predicates_match_legacy(frames):
    """旧版 _cooker_cooked / _cooker_burning / _cooker_has_pot 的判定约定。"""
    compared = 0
    for f in frames:
        for c in f.get("cookers") or []:
            assert P.cooker_cooked(c) == (c.get("cooked") is True)
            assert P.cooker_burning(c) == (
                c.get("burning") is True or c.get("onFire") is True
            )
            assert P.cooker_has_pot(c) == (c.get("cooked") is not None)
            compared += 1
    if compared == 0:
        pytest.skip("样本里没有灶台数据")


@_needs_fixtures
def test_plate_count_matches_legacy_sum(frames):
    """旧 _plate_total 对所有盘求 count 之和；新版用 P.plate_count 逐盘取。

    注意两者语义有意不同：新版优先 len(contents)，contents 为空才退回 count
    （count 在部分帧里不随内容更新）。本测试断言在真实帧上两者**不产生分歧**；
    一旦某帧 contents 与 count 不一致，这里会红，提示需要显式决定采信哪个。
    """
    checked = 0
    for f in frames:
        plates = f.get("plates") or []
        if not plates:
            continue
        legacy_total = sum(p.get("count") or 0 for p in plates)
        new_total = sum(P.plate_count(p) for p in plates)
        checked += 1

        assert new_total == legacy_total, (
            "plate 件数分歧：新=%d 旧=%d，plates=%r" % (new_total, legacy_total, plates)
        )
    assert checked > 0, "样本里没有盘数据"

"""测试夹具：构造假快照片段。

谓词只读 dict，所以不需要真 World——这正是 spec §6.6 说的工程红利。
"""
import glob
import os

import pytest

from ai.output import trace
from ai.exec.replay import load_frames

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture(scope="session", autouse=True)
def trace_to_tmp(tmp_path_factory):
    """trace 默认落盘在仓库根目录；测试一律改写到 pytest 的临时目录。

    session 级：必须早于测试自己的 fixture（如 test_trace 的 clean_log），
    否则那句 clear_log() 会在仓库里凭空建一个 trace-*.jsonl。
    """
    original = trace.TRACE_PATH
    trace.TRACE_PATH = str(tmp_path_factory.mktemp("trace") / "trace.jsonl")
    yield
    trace.TRACE_PATH = original


def fixture_files():
    # type: () -> list
    """入库为 .ndjson.gz；本地未压缩 .ndjson 若与某 .gz 同名则不重复加载。"""
    gz = glob.glob(os.path.join(FIXTURE_DIR, "*.ndjson.gz"))
    stems = set(p[: -len(".gz")] for p in gz)
    plain = [
        p
        for p in glob.glob(os.path.join(FIXTURE_DIR, "*.ndjson"))
        if p not in stems
    ]
    return sorted(gz + plain)


@pytest.fixture(scope="session")
def frames():
    """全部真实帧。scope=session：多个测试文件共用，1102 帧只解析一次。"""
    fs = []
    for path in fixture_files():
        fs.extend(load_frames(path))
    if not fs:
        pytest.skip("缺 tests/fixtures/*.ndjson.gz 真实帧样本（先 tools/record_frames.py 重录）")
    return fs


@pytest.fixture(scope="session")
def level_frame(frames):
    """取第一帧 phase==level 的（关卡内）帧。"""
    for f in frames:
        if f.get("phase") == "level" and f.get("stations"):
            return f
    pytest.skip("样本里没有关卡内的帧")


@pytest.fixture
def chef_empty():
    return {"slot": 1, "held": None, "x": 0.0, "z": 0.0}


@pytest.fixture
def chef_holding():
    def _make(held):
        return {"slot": 1, "held": held, "x": 0.0, "z": 0.0}
    return _make


@pytest.fixture
def cooker():
    def _make(cooked=None, burning=None, on_fire=None, is_cooking=None, name="Cooker_1"):
        return {
            "n": name,
            "cooked": cooked,
            "burning": burning,
            "onFire": on_fire,
            "isCooking": is_cooking,
        }
    return _make

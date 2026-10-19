import json

from ai.exec.record import FrameRecorder
from ai.exec.replay import iter_frames, load_frames


def test_load_frames_reads_ndjson(tmp_path):
    p = tmp_path / "frames.ndjson"
    p.write_text('{"tick": 1}\n{"tick": 2}\n', encoding="utf-8")

    frames = load_frames(str(p))

    assert frames == [{"tick": 1}, {"tick": 2}]


def test_load_frames_skips_blank_and_malformed_lines(tmp_path):
    p = tmp_path / "frames.ndjson"
    p.write_text('{"tick": 1}\n\nnot-json\n{"tick": 2}\n', encoding="utf-8")

    frames = load_frames(str(p))

    assert frames == [{"tick": 1}, {"tick": 2}]


def test_iter_frames_is_lazy(tmp_path):
    p = tmp_path / "frames.ndjson"
    p.write_text('{"tick": 1}\n{"tick": 2}\n', encoding="utf-8")

    it = iter_frames(str(p))

    assert next(it) == {"tick": 1}
    assert next(it) == {"tick": 2}


def test_load_frames_skips_non_dict_rows(tmp_path):
    """标量/数组行必须被拦掉：下游一律按 dict 访问 frame["..."]。"""
    p = tmp_path / "frames.ndjson"
    p.write_text(
        '{"tick": 1}\nnull\n123\n[1, 2]\n"str"\ntrue\n{"tick": 2}\n',
        encoding="utf-8",
    )

    frames = load_frames(str(p))

    assert frames == [{"tick": 1}, {"tick": 2}]


def test_load_frames_survives_invalid_utf8(tmp_path):
    """录制被 kill 时可能切断多字节字符，只能丢那一行，不能整个文件报废。"""
    p = tmp_path / "frames.ndjson"
    # 切在汉字"司"的 3 字节序列中间，造出真正非法的 UTF-8
    truncated = '{"n": "寿司"}'.encode("utf-8")[:-3]
    assert truncated.endswith(b"\xe5\x8f")  # 断言确实是残缺的多字节序列
    p.write_bytes(b'{"tick": 1}\n' + truncated + b'\n{"tick": 2}\n')

    frames = load_frames(str(p))

    assert frames == [{"tick": 1}, {"tick": 2}]


def test_recorder_writes_raw_lines(tmp_path):
    p = tmp_path / "out.ndjson"
    rec = FrameRecorder(str(p))
    rec.write(b'{"tick": 1}')
    rec.write(b'{"tick": 2}\n')
    rec.close()

    lines = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln]
    assert lines == [{"tick": 1}, {"tick": 2}]


def test_recorder_appends_not_truncates(tmp_path):
    """第二次打开同一路径必须追加，不能覆盖已录的帧。"""
    p = tmp_path / "f.ndjson"

    r1 = FrameRecorder(str(p))
    r1.write(b'{"tick": 1}')
    r1.close()

    r2 = FrameRecorder(str(p))
    r2.write(b'{"tick": 2}')
    r2.close()

    frames = load_frames(str(p))
    assert [f["tick"] for f in frames] == [1, 2]


def test_recorder_flushes_without_close(tmp_path):
    """write() 后不调 close() 也必须已落盘（崩溃前不丢帧）。"""
    p = tmp_path / "f.ndjson"

    r = FrameRecorder(str(p))
    r.write(b'{"tick": 1}')
    # 故意不 close()

    assert load_frames(str(p)) == [{"tick": 1}]

    r.close()

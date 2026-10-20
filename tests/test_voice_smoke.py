"""tools/voice_smoke.py 的离线测试（不连真实服务、不碰音频设备）。

只覆盖脚本自己的纯逻辑：.env 加载、重采样、WAV 读写、相似度和缺配置
时的退出码。真实 ASR/TTS/LLM 仍由 `OC2_LIVE_VOICE=1` 下的
`tests/test_live_voice.py` 与手工运行 `tools/voice_smoke.py` 覆盖。
"""
import os
import sys
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import voice_smoke as V  # noqa: E402


def test_load_env_file_parses_and_keeps_existing_values(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "# 注释\n"
        "VOICE_SMOKE_A=from-file\n"
        "VOICE_SMOKE_B='带 空格'\n"
        "export VOICE_SMOKE_C=3\n"
        "无等号的行\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VOICE_SMOKE_A", "from-env")
    for key in ("VOICE_SMOKE_B", "VOICE_SMOKE_C"):
        monkeypatch.delenv(key, raising=False)

    added = V.load_env_file(str(path))

    try:
        assert added == 2
        assert os.environ["VOICE_SMOKE_A"] == "from-env"
        assert os.environ["VOICE_SMOKE_B"] == "带 空格"
        assert os.environ["VOICE_SMOKE_C"] == "3"
    finally:
        os.environ.pop("VOICE_SMOKE_B", None)
        os.environ.pop("VOICE_SMOKE_C", None)


def test_resample_pcm16_keeps_constant_signal_and_ratio():
    import numpy as np

    samples = np.full(24000, 1000, dtype="<i2")
    pcm = samples.tobytes()

    out = V._resample_pcm16(pcm, 24000, 16000)
    resampled = np.frombuffer(out, dtype="<i2")

    assert len(resampled) == 16000
    assert set(resampled.tolist()) == {1000}
    assert V._resample_pcm16(pcm, 24000, 24000) == pcm


def test_read_wav_downmixes_and_resamples(tmp_path):
    import numpy as np

    path = tmp_path / "stereo.wav"
    left = np.full(2400, 500, dtype="<i2")
    right = np.full(2400, 1500, dtype="<i2")
    interleaved = np.column_stack((left, right)).reshape(-1)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(interleaved.tobytes())

    pcm = V._read_wav(str(path))
    samples = np.frombuffer(pcm, dtype="<i2")

    assert len(samples) == 1600          # 2400 帧 @24k → 1600 帧 @16k
    assert set(samples.tolist()) == {1000}   # (500 + 1500) / 2


def test_pcm16_roundtrip_through_wav(tmp_path):
    import numpy as np

    frames = [np.array([0.0, 0.5, -0.5], dtype=np.float32)]
    pcm = V._pcm16(frames)
    samples = np.frombuffer(pcm, dtype="<i2")
    assert samples.tolist() == [0, 16383, -16383]

    path = V._save_wav(str(tmp_path / "out.wav"), pcm, 16000)
    with wave.open(path, "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.readframes(wf.getnframes()) == pcm


def test_similarity_ignores_punctuation():
    assert V._similarity("我来切菜，你去煮饭。", "我来切菜 你去煮饭") == 1.0
    assert V._similarity("我来切菜", "今天天气不错") < 0.3
    assert V._similarity("", "随便") == 0.0


def test_single_mode_missing_key_exits_2(monkeypatch, capsys):
    for key in ("VOLC_ASR_API_KEY", "VOLC_TTS_API_KEY", "ARK_API_KEY",
                "ARK_MODEL"):
        monkeypatch.delenv(key, raising=False)

    code = V.main(["tts", "--no-env-file"])

    assert code == 2
    out = capsys.readouterr().out
    assert "VOLC_TTS_API_KEY" in out
    assert "your-volc-tts-api-key" not in out


def test_all_mode_without_keys_skips_instead_of_failing(monkeypatch):
    for key in ("VOLC_ASR_API_KEY", "VOLC_TTS_API_KEY", "ARK_API_KEY",
                "ARK_MODEL"):
        monkeypatch.delenv(key, raising=False)

    # doctor 会枚举音频设备；CI 无设备时也只是该项失败，其余仍应跳过。
    code = V.main(["all", "--no-env-file"])

    assert code in (0, 1)

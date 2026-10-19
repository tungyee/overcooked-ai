"""音频层单测：设备枚举、分帧、RMS VAD 与播放队列（spec §8）。

全部使用注入的 fake `sd`，不打开真实麦克风/扬声器。
"""
import numpy as np
import pytest

from ai.dialog import audio as A


class FakeInputStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.closed = False
        self._callback = kwargs["callback"]

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True

    def push(self, samples):
        data = np.asarray(samples, dtype=np.float32).reshape(-1, 1)
        self._callback(data, len(data), None, None)


class FakeOutputStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.aborted = False
        self.closed = False
        self._callback = kwargs["callback"]

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def abort(self):
        self.aborted = True

    def close(self):
        self.closed = True

    def pull(self, frames):
        out = np.zeros((frames, 1), dtype=np.float32)
        self._callback(out, frames, None, None)
        return out[:, 0].copy()


class FakeSD:
    def __init__(self, devices=None):
        self.devices = devices or []
        self.input_streams = []
        self.output_streams = []

    def query_devices(self, device=None, kind=None):
        # 与 sounddevice 一致：指定 device/kind 时返回单个 dict（默认设备），
        # 否则返回全部设备列表。
        if device is not None or kind is not None:
            if self.devices:
                return dict(self.devices[0])
            return {}
        return list(self.devices)

    def InputStream(self, **kwargs):
        stream = FakeInputStream(**kwargs)
        self.input_streams.append(stream)
        return stream

    def OutputStream(self, **kwargs):
        stream = FakeOutputStream(**kwargs)
        self.output_streams.append(stream)
        return stream


def test_list_input_devices_filters_by_channels():
    sd = FakeSD([
        {"name": "mic", "max_input_channels": 1, "max_output_channels": 0},
        {"name": "speaker", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "usb mic", "max_input_channels": 2, "max_output_channels": 0},
    ])
    devices = A.list_input_devices(sd=sd)
    assert [d["name"] for d in devices] == ["mic", "usb mic"]
    assert [d["index"] for d in devices] == [0, 2]


def test_list_output_devices_filters_by_channels():
    sd = FakeSD([
        {"name": "mic", "max_input_channels": 1, "max_output_channels": 0},
        {"name": "speaker", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "headset", "max_input_channels": 1, "max_output_channels": 2},
    ])
    devices = A.list_output_devices(sd=sd)
    assert [d["name"] for d in devices] == ["speaker", "headset"]
    assert [d["index"] for d in devices] == [1, 2]


def test_capture_yields_50ms_mono_frames():
    sd = FakeSD()
    cap = A.AudioCapture(device_index=3, sample_rate=16000, frame_ms=50, sd=sd)
    frames = cap.frames()
    stream = sd.input_streams[-1]

    assert stream.kwargs["samplerate"] == 16000
    assert stream.kwargs["channels"] == 1
    assert stream.kwargs["device"] == 3
    assert stream.started

    # 回调给出 400 + 400 样本，应拼成恰好一帧 800 样本（50 ms）。
    stream.push(np.full(400, 0.1, dtype=np.float32))
    stream.push(np.full(400, 0.2, dtype=np.float32))
    frame = next(frames)
    assert frame.shape == (800,)
    assert frame.dtype == np.float32
    assert frame[0] == pytest.approx(0.1)
    assert frame[-1] == pytest.approx(0.2)

    frames.close()
    assert stream.stopped and stream.closed


def test_capture_flush_discards_pending_frames_and_partial_buffer():
    sd = FakeSD()
    cap = A.AudioCapture(sample_rate=16000, sd=sd)
    frames = cap.frames()
    stream = sd.input_streams[-1]

    # 先消费 1000 样本，缓冲里留下 200 样本的半帧。
    stream.push(np.full(1000, 0.3, dtype=np.float32))
    assert next(frames).shape == (800,)
    assert cap._buffer.size == 200

    # 再积压一整帧待消费数据，然后 flush。
    stream.push(np.full(800, 0.4, dtype=np.float32))
    cap.flush()
    assert cap._queue.empty()
    assert cap._buffer.size == 0

    # flush 后只应产出之后采集的数据。
    stream.push(np.full(800, 0.7, dtype=np.float32))
    frame = next(frames)
    assert frame.shape == (800,)
    assert np.allclose(frame, 0.7)

    frames.close()


def test_capture_flush_preserves_close_sentinel():
    sd = FakeSD()
    cap = A.AudioCapture(sample_rate=16000, sd=sd)
    frames = cap.frames()
    stream = sd.input_streams[-1]
    stream.push(np.full(800, 0.2, dtype=np.float32))
    assert next(frames).shape == (800,)

    frames.close()  # 生成器 finally → close()，在队列里放入结束哨兵
    assert cap._queue.qsize() == 1

    cap.flush()
    assert cap._queue.qsize() == 1
    assert cap._queue.queue[0] is None
    # 哨兵仍在，消费端可以正常终止而不是永久阻塞。
    with pytest.raises(StopIteration):
        next(cap._iter_frames(stream))


def test_capture_flush_since_keeps_audio_before_playback():
    sd = FakeSD()
    cap = A.AudioCapture(sample_rate=16000, sd=sd)
    frames = cap.frames()
    stream = sd.input_streams[-1]

    # 播放前：1000 样本里消费一帧，缓冲留 200 个半帧样本。
    stream.push(np.full(1000, 0.3, dtype=np.float32))
    assert next(frames).shape == (800,)
    mark = cap.mark()
    # 播放窗口内：块必须被丢掉，但半帧缓冲是播放前的，不能清。
    stream.push(np.full(800, 0.4, dtype=np.float32))
    cap.flush_since(mark)

    assert cap._queue.empty()
    assert cap._buffer.size == 200
    assert np.allclose(cap._buffer, 0.3)

    stream.push(np.full(600, 0.5, dtype=np.float32))
    frame = next(frames)
    assert np.allclose(frame[:200], 0.3)
    assert np.allclose(frame[200:], 0.5)
    frames.close()


def test_vad_starts_on_third_loud_frame_and_ends_after_silence():
    vad = A.VAD()
    loud = np.full(800, 0.2, dtype=np.float32)
    quiet = np.zeros(800, dtype=np.float32)

    assert vad.feed(loud) is None
    assert vad.feed(loud) is None
    assert vad.feed(loud) == "start"

    # 400 ms（8 帧）静音后仍不切，第 9 帧确认收尾。
    for _ in range(8):
        assert vad.feed(quiet) is None
    assert vad.feed(quiet) == "end"


def test_vad_paused_ignores_loud_frames():
    vad = A.VAD()
    vad.paused = True
    for _ in range(5):
        assert vad.feed(np.full(800, 0.9, dtype=np.float32)) is None

    vad.paused = False
    loud = np.full(800, 0.9, dtype=np.float32)
    assert vad.feed(loud) is None
    assert vad.feed(loud) is None
    assert vad.feed(loud) == "start"


def test_continuous_game_noise_does_not_open_speech():
    vad = A.VAD(threshold_db=9.0, noise_floor_db=-40.0)
    flat = np.full(800, 0.05, dtype=np.float32)
    # 3 秒稳定游戏声：自适应噪声底会吸收它，不能被当成开口。
    assert [vad.feed(flat) for _ in range(60)] == [None] * 60


def test_audio_player_stop_clears_queue():
    sd = FakeSD()
    player = A.AudioPlayer(sample_rate=24000, sd=sd)
    assert player.is_playing is False

    player.play([b"\x00\x00" * 240])
    stream = sd.output_streams[-1]
    assert stream.kwargs["samplerate"] == 24000
    assert stream.kwargs["channels"] == 1
    assert stream.started
    assert player.is_playing is True
    assert player._queue.qsize() == 1

    player.stop()
    assert player.is_playing is False
    assert player._queue.empty()
    assert stream.aborted and stream.stopped


def test_audio_player_rearms_within_one_stream():
    sd = FakeSD()
    player = A.AudioPlayer(sample_rate=24000, sd=sd, volume=1.0)
    chunk_a = np.full(240, 8192, dtype=np.int16).tobytes()
    chunk_b = np.full(240, -8192, dtype=np.int16).tobytes()
    seen = []

    def chunks():
        yield chunk_a
        stream = sd.output_streams[-1]
        stream.pull(240)  # 回调排空 A，队列见底，is_playing 被置 False
        seen.append(player.is_playing)
        yield chunk_b  # 同一次 play() 内的后续块必须重新置位
        seen.append(player.is_playing)

    player.play(chunks())
    assert seen == [False, True]

    # B 也确实进入了输出回调。
    stream = sd.output_streams[-1]
    assert stream.pull(240)[0] == pytest.approx(-0.25)


def test_audio_player_marks_first_output_and_resets_per_play():
    sd = FakeSD()
    player = A.AudioPlayer(sample_rate=24000, sd=sd, volume=1.0)
    pcm = np.full(240, 8192, dtype=np.int16).tobytes()

    player.play([pcm])
    stream = sd.output_streams[-1]
    assert player.first_output_at is None

    stream.pull(240)
    first = player.first_output_at
    assert first is not None

    # 下一次播报必须清掉上一句的首帧时间，不能把旧值当成新句延迟。
    player.reset_output_mark()
    assert player.first_output_at is None
    player.play([pcm])
    assert player.first_output_at is None
    sd.output_streams[-1].pull(240)
    assert player.first_output_at is not None
    assert player.first_output_at >= first


def test_audio_player_converts_pcm_bytes_to_float():
    sd = FakeSD()
    player = A.AudioPlayer(sample_rate=24000, sd=sd, volume=1.0)
    pcm = np.array([16384, -16384] * 120, dtype=np.int16)
    player.play([pcm.tobytes()])
    stream = sd.output_streams[-1]

    out = stream.pull(240)
    assert out[0] == pytest.approx(0.5)
    assert out[1] == pytest.approx(-0.5)
    # 队列排空后回调把播放状态置回 False。
    assert player.is_playing is False


def test_audio_player_resamples_to_device_rate():
    sd = FakeSD([{
        "name": "speaker", "max_input_channels": 0,
        "max_output_channels": 2, "default_samplerate": 48000,
    }])
    player = A.AudioPlayer(sample_rate=24000, sd=sd, volume=1.0)
    pcm = np.full(240, 8192, dtype=np.int16)  # 10 ms @ 24 kHz

    player.play([pcm.tobytes()])

    stream = sd.output_streams[-1]
    assert stream.kwargs["samplerate"] == 48000
    queued = player._queue.get_nowait()
    assert queued.size == 480          # 10 ms @ 48 kHz
    assert queued.dtype == np.float32


def test_audio_player_keeps_tts_rate_without_device_info():
    sd = FakeSD()
    player = A.AudioPlayer(sample_rate=24000, sd=sd, volume=1.0)

    player.play([np.zeros(120, dtype=np.int16).tobytes()])

    assert sd.output_streams[-1].kwargs["samplerate"] == 24000
    assert player._queue.get_nowait().size == 120

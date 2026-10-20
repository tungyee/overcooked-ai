"""音频输入/输出层：设备枚举、定长分帧、RMS VAD 与流式播放（spec §8.1–8.3）。

设计要点：

- 采集与播放都用 ``sounddevice`` 的回调 + ``queue.Queue``。回调里只做拷贝/
  填充，不做网络或磁盘 I/O；worker 线程永远不会调用 ``sounddevice.wait()``。
- ``sd`` 参数用于测试注入；默认延迟导入 sounddevice，避免纯离线场景在
  import 阶段就加载 PortAudio。
- TTS 输出约定为 16 bit 小端单声道 PCM（16k/24k），播放前转成 float32。
"""
import logging
import math
import os
import queue
import time

import numpy as np


log = logging.getLogger(__name__)

# VAD 噪声底的跟随速度（每帧 EMA 系数）。取值需要同时满足：
# 稳定电平（游戏声）在 2–3 帧内被吸收，而人声起音能连续 3 帧超过阈值。
_FLOOR_ADAPT_RATE = 0.25
_SILENCE_DB = -200.0


def _load_sd():
    """延迟导入 sounddevice，缺失时给出可操作的错误信息。"""
    try:
        import sounddevice as sd
    except Exception as exc:  # pragma: no cover - 仅在缺依赖时触发
        raise RuntimeError(
            "音频依赖缺失：请先执行 python3 -m pip install -r requirements-voice.txt"
        ) from exc
    return sd


def _device_entries(sd):
    for index, device in enumerate(sd.query_devices()):
        entry = dict(device)
        entry["index"] = index
        yield entry


def list_input_devices(sd=None):
    # type: (object) -> list
    """返回有输入通道的设备，元素含 ``index`` / ``name`` 等字段。"""
    sd = sd if sd is not None else _load_sd()
    return [d for d in _device_entries(sd) if d.get("max_input_channels", 0) > 0]


def list_output_devices(sd=None):
    # type: (object) -> list
    """返回有输出通道的设备，元素含 ``index`` / ``name`` 等字段。"""
    sd = sd if sd is not None else _load_sd()
    return [d for d in _device_entries(sd) if d.get("max_output_channels", 0) > 0]


class AudioCapture:
    """16 kHz 单声道采集，``frames()`` 产出定长 float32 帧。

    TTS 播放期间 VAD 是暂停的，但麦克风仍在采集（会录到扬声器回声）。
    worker 在播放前调用 :meth:`mark`、播完调用 :meth:`flush_since`，只丢弃
    播放窗口内的块；TTS 前已排队、尚未处理的玩家语音保留。
    """

    def __init__(self, device_index=None, sample_rate=16000, frame_ms=50, sd=None):
        self.device_index = device_index
        self.sample_rate = int(sample_rate)
        self.frame_ms = int(frame_ms)
        self.frame_samples = int(self.sample_rate * self.frame_ms / 1000.0)
        if self.frame_samples <= 0:
            raise ValueError("frame_ms 太小，无法得到非空帧")
        self._sd = sd
        self._queue = queue.Queue()
        self._buffer = np.zeros(0, dtype=np.float32)
        self._stream = None
        # 每个回调块的单调序号：`flush_since(mark)` 只丢播放窗口内的块。
        self._block_seq = 0

    def _callback(self, indata, frames, time_info, status):
        # 回调运行在 PortAudio 线程：只拷贝，不做任何可能阻塞的事。
        data = np.asarray(indata, dtype=np.float32)
        if data.ndim > 1:
            data = data[:, 0]
        seq = self._block_seq
        self._block_seq += 1
        self._queue.put((seq, data.copy()))

    def frames(self):
        # type: () -> object
        """启动输入流并返回帧迭代器；迭代器关闭时自动停流。"""
        sd = self._sd if self._sd is not None else _load_sd()
        stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=0,
            device=self.device_index,
            callback=self._callback,
        )
        self._stream = stream
        stream.start()
        return self._iter_frames(stream)

    def _iter_frames(self, stream):
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                _seq, block = item
                if self._buffer.size:
                    self._buffer = np.concatenate((self._buffer, block))
                else:
                    self._buffer = block
                while self._buffer.size >= self.frame_samples:
                    # 先裁剪再 yield，保证生成器挂起时 _buffer 只含剩余样本。
                    frame = self._buffer[: self.frame_samples].copy()
                    self._buffer = self._buffer[self.frame_samples:]
                    yield frame
        finally:
            self.close()

    def mark(self):
        # type: () -> int
        """当前采集位置；与 `flush_since` 配对圈出 TTS 播放窗口。"""
        return self._block_seq

    def flush(self):
        """丢弃尚未消费的采集帧与不完整缓冲（TTS 结束、恢复 VAD 前调用）。"""
        saw_end = False
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                saw_end = True
        self._buffer = np.zeros(0, dtype=np.float32)
        if saw_end:
            # close() 的结束哨兵不能丢，否则阻塞在 get() 的迭代器无法退出。
            self._queue.put(None)

    def flush_since(self, mark):
        # type: (int) -> None
        """只丢弃 `mark` 之后采集的块，保留 TTS 前已排队的语音。

        与 `flush` 不同，这里**不清 `_buffer`**：半帧可能来自播放前的采样，
        清掉会把玩家刚说完的话截断。结束哨兵照常保留。
        """
        kept = []
        saw_end = False
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                saw_end = True
                continue
            seq, _block = item
            if seq >= mark:
                continue
            kept.append(item)
        for item in kept:
            self._queue.put(item)
        if saw_end:
            self._queue.put(None)

    def close(self):
        """停止并关闭输入流（幂等）。"""
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        self._queue.put(None)


class VAD:
    """RMS + 自适应噪声底的语音活动检测（spec §8.2）。

    ``feed`` 返回 ``"start"`` / ``"end"`` / ``None``。噪声底按 EMA 跟随输入：
    稳定电平（例如游戏背景声）会被吸收成底噪；只有明显高于底噪的快速上升
    才能连续 3 帧越过阈值，避免把一段恒定的游戏音当成开口。
    """

    def __init__(self, threshold_db=9.0, noise_floor_db=-40.0, frame_ms=50):
        self.threshold_db = float(threshold_db)
        self.noise_floor_db = float(noise_floor_db)
        self.frame_ms = int(frame_ms)
        self._floor_db = float(noise_floor_db)

        self._start_frames = 3
        # 400 ms 静音阈值；再等一帧确认，避免句尾短暂停顿被切断。
        self._end_frames = max(1, int(round(400.0 / self.frame_ms)))
        self._min_frames = max(1, int(round(300.0 / self.frame_ms)))
        self._max_frames = max(1, int(round(8000.0 / self.frame_ms)))

        self._paused = False
        self._speaking = False
        self._loud_run = 0
        self._quiet_run = 0
        self._utterance_frames = 0

    @property
    def paused(self):
        # type: () -> bool
        """TTS 播放期间置 True：VAD 完全忽略输入（半双工，spec §5.4）。"""
        return self._paused

    @paused.setter
    def paused(self, value):
        value = bool(value)
        if value:
            self._reset_state()
        self._paused = value

    @property
    def floor_db(self):
        # type: () -> float
        return self._floor_db

    def feed(self, frame):
        # type: (object) -> object
        if self._paused:
            return None

        level = self._level_db(frame)
        if not self._speaking:
            loud = level > self._floor_db + self.threshold_db
            self._loud_run = self._loud_run + 1 if loud else 0
            self._adapt_floor(level, allow_up=True)
            if self._loud_run >= self._start_frames:
                self._speaking = True
                self._quiet_run = 0
                self._utterance_frames = self._loud_run
                self._loud_run = 0
                return "start"
            return None

        self._utterance_frames += 1
        if level > self._floor_db + self.threshold_db:
            self._quiet_run = 0
        else:
            self._quiet_run += 1
        # 说话期间不让噪声底向上追（否则句中的响度变化会被误判成静音），
        # 只向下跟随环境安静。
        self._adapt_floor(level, allow_up=False)

        if self._utterance_frames >= self._max_frames:
            self._finish()
            return "end"
        if (self._quiet_run > self._end_frames
                and self._utterance_frames >= self._min_frames):
            self._finish()
            return "end"
        return None

    def _adapt_floor(self, level_db, allow_up):
        if not allow_up and level_db > self._floor_db:
            return
        target = self._floor_db + _FLOOR_ADAPT_RATE * (level_db - self._floor_db)
        # 初始底噪是已知的最安静环境，不继续向下漂。
        self._floor_db = max(self.noise_floor_db, target)

    def _finish(self):
        self._speaking = False
        self._loud_run = 0
        self._quiet_run = 0
        self._utterance_frames = 0

    def _reset_state(self):
        self._speaking = False
        self._loud_run = 0
        self._quiet_run = 0
        self._utterance_frames = 0

    @staticmethod
    def _level_db(frame):
        samples = np.asarray(frame, dtype=np.float32)
        if samples.size == 0:
            return _SILENCE_DB
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        return 20.0 * math.log10(max(rms, 1e-10))


def _resolve_volume(volume):
    if volume is None:
        try:
            volume = float(os.environ.get("OC2_VOICE_VOLUME", "0.7"))
        except ValueError:
            volume = 0.7
    return min(1.2, max(0.3, float(volume)))


class AudioPlayer:
    """流式播放 TTS PCM：输出回调消费队列，调用方线程不阻塞在设备上。

    TTS 固定输出 24 kHz，但设备默认采样率不一定是 24 kHz（Mac 扬声器常见
    48 kHz）。打开输出流前先查询默认设备的 `default_samplerate`，必要时用
    `numpy.interp` 重采样，避免以不支持的采样率开流（spec §8.1）。
    """

    def __init__(self, sample_rate=24000, sd=None, device=None, volume=None,
                 output_rate=None):
        self.sample_rate = int(sample_rate)
        self.volume = _resolve_volume(volume)
        self._sd = sd
        self._device = device
        self._output_rate = int(output_rate) if output_rate else None
        self._explicit_output_rate = output_rate is not None
        self._queue = queue.Queue()
        self._pending = None
        self._stream = None
        self._playing = False
        # 输出回调第一次真正写入非静音采样时的单调时钟；供延迟打点读取。
        self.first_output_at = None
        # stop() 递增代号；正在迭代流式 chunks 的 play() 看到代号变化后
        # 不再继续入队，避免“停下后又响”。
        self._generation = 0

    @property
    def is_playing(self):
        # type: () -> bool
        return self._playing

    def play(self, chunks):
        # type: (object) -> None
        """把 ``chunks``（16 bit 小端单声道 PCM bytes）依次送进播放队列。

        返回时音频可能仍在播放；调用方可轮询 ``is_playing``。首块入队后
        立即启动输出流，因此流式 TTS 不必等整句合成完。
        """
        generation = self._generation
        started = False
        self.first_output_at = None
        self._resolve_output_rate()
        for chunk in chunks:
            if generation != self._generation:
                return
            samples = self._resample(self._to_float(chunk))
            if samples.size == 0:
                continue
            self._queue.put(samples)
            # 每次入队都重新置位：流式合成期间队列可能短暂见底，回调会把
            # is_playing 置 False；后续块到达时必须重新变成“正在播放”。
            self._playing = True
            if not started:
                started = True
                self._ensure_stream()

    def reset_output_mark(self):
        # type: () -> None
        """清空首帧输出时间；每次播报前调用，避免读到上一句的时间。"""
        self.first_output_at = None

    def stop(self):
        """清空队列、中止输出流并复位播放状态（可在任意线程调用）。"""
        self._generation += 1
        self._playing = False
        self.first_output_at = None
        stream = self._stream
        self._stream = None
        if stream is not None:
            # 清理是尽力而为：任一步失败都不能阻止其余步骤和状态复位，
            # 但失败要留在 debug 日志里，方便排查设备卡死。
            try:
                stream.abort()
            except Exception:
                log.debug("AudioPlayer.stop: stream.abort() failed", exc_info=True)
            try:
                stream.stop()
            except Exception:
                log.debug("AudioPlayer.stop: stream.stop() failed", exc_info=True)
            try:
                stream.close()
            except Exception:
                log.debug("AudioPlayer.stop: stream.close() failed", exc_info=True)
        # 先停流再清队列，避免回调在清理过程中把旧块重新挂到 _pending。
        self._pending = None
        if not self._explicit_output_rate:
            # 下次播放重新查询设备：用户可能切换了默认输出设备。
            self._output_rate = None
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    close = stop

    def _resolve_output_rate(self):
        # type: () -> int
        """查询默认/指定输出设备的采样率；失败时退回 TTS 采样率。"""
        if self._output_rate is not None:
            return self._output_rate
        sd = self._sd if self._sd is not None else _load_sd()
        info = None
        try:
            try:
                info = sd.query_devices(self._device, "output")
            except TypeError:
                # 某些 fake / 旧 sounddevice 不接受 kind 参数。
                info = sd.query_devices()
        except Exception:                       # noqa: BLE001
            log.debug("查询输出设备采样率失败", exc_info=True)
        rate = None
        if isinstance(info, dict):
            rate = info.get("default_samplerate")
        elif isinstance(info, (list, tuple)) and info:
            rate = info[0].get("default_samplerate")
        try:
            rate = int(rate)
        except (TypeError, ValueError):
            rate = None
        self._output_rate = rate or self.sample_rate
        return self._output_rate

    def _resample(self, samples):
        # type: (object) -> object
        """TTS 采样率 → 设备采样率；一致时原样返回。"""
        rate = self._output_rate or self.sample_rate
        if rate == self.sample_rate or samples.size == 0:
            return samples
        target = int(round(len(samples) * float(rate) / self.sample_rate))
        if target <= 0:
            return samples
        old_axis = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
        new_axis = np.linspace(0.0, 1.0, num=target, endpoint=False)
        return np.interp(new_axis, old_axis, samples).astype(np.float32)

    def _ensure_stream(self):
        if self._stream is not None:
            return
        sd = self._sd if self._sd is not None else _load_sd()
        stream = sd.OutputStream(
            samplerate=self._output_rate or self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=0,
            device=self._device,
            callback=self._callback,
        )
        self._stream = stream
        stream.start()

    def _callback(self, outdata, frames, time_info, status):
        out = outdata[:, 0] if outdata.ndim > 1 else outdata
        out.fill(0.0)
        written = 0
        while written < len(out):
            chunk = self._pending
            if chunk is None or chunk.size == 0:
                try:
                    chunk = self._queue.get_nowait()
                except queue.Empty:
                    self._pending = None
                    break
                self._pending = chunk
            take = min(len(out) - written, chunk.size)
            out[written:written + take] = chunk[:take]
            written += take
            self._pending = chunk[take:] if take < chunk.size else None
        if written and self.first_output_at is None:
            # 第一次真正往设备写采样；比“首包入队”更接近用户听到的时间。
            self.first_output_at = time.monotonic()
        if self._pending is None and self._queue.empty():
            self._playing = False

    def _to_float(self, chunk):
        if isinstance(chunk, np.ndarray):
            if np.issubdtype(chunk.dtype, np.floating):
                samples = chunk.astype(np.float32, copy=True)
            else:
                samples = chunk.astype(np.float32) / 32768.0
        else:
            data = bytes(chunk)
            if len(data) % 2:
                data = data[:-1]
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        if self.volume != 1.0:
            samples *= self.volume
        return samples

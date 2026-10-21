"""对话 worker：把 VAD / ASR / LLM / TTS 串成一条后台流水线（spec §6.2）。

线程边界：
- worker 只读主循环发布的 context dict、只写 `cmd_q`；**不读 World、
  不写 Blackboard**。
- 两段式 function calling：LLM 的工具调用打包成 `DutyBatch` 投给主循环
  落地，worker 等 ack 拿到逐条结果、回填 tool 消息，再请求一次让模型按
  真实结果说最终那句话。
- TTS 播放期间 VAD 暂停（半双工，spec §5.4）；播放前记采集位置，播完只
  flush 播放窗口内的块（TTS 前已排队的语音保留），再恢复 VAD。
- 没有固定模板播报：火情 / 快糊由 planner 直接处理、不出声；需要向玩家
  解释的抢单走 rescue 事件，由 LLM 按事实说。TTS 失败时静默。
- `new_order` 不受主动开口 4 秒闸限制：1 秒合并窗口本身就是它的限频，
  丢一张单比连续两条订单播报更糟。`rescue` 仍受全局 4 秒闸。
"""
import dataclasses
import datetime
import json
import logging
import os
import queue
import random
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, List

import numpy as np

from ai.dialog.llm import (assistant_message, event_note, speech_message,
                           state_message, system_message, tool_message)
from ai.dialog.policy import (
    PROACTIVE_MIN_INTERVAL_TICKS,
    merge_new_orders,
    should_checkin,
    should_trigger,
)
from ai.dialog.schema import DialogState


log = logging.getLogger(__name__)

# 一批工具调用等 ack 的上限；主循环每拍开头 drain，正常远小于这个值。
ACK_TIMEOUT_S = 2.0
# 轮询播放状态的间隔与最长等待（兜底防止设备卡死拖住 worker 线程）。
SPEAK_POLL_S = 0.02
SPEAK_TIMEOUT_S = 30.0
# 超时后给 producer 线程的收尾时间：不能跟着卡住的网络流一起等。
TTS_JOIN_S = 0.1
# 本关对话历史上限（消息条数，含 tool 结果）。
HISTORY_MAX = 60
# VAD 暂停或等待音频空转时的休眠间隔。
IDLE_S = 0.02
# VAD 判定开口前的 onset 帧数：worker 把它们留作 pre-roll，避免首音被切。
PREROLL_FRAMES = 2
# PTT：按住录音。pre-roll 300ms 防止首音被切；松开后再收 200ms 尾音。
PTT_PREROLL_FRAMES = 6
PTT_TAIL_FRAMES = 4
PTT_MIN_FRAMES = 4
# 流式 ASR：录音结束后等最终文本的上限（服务端可能还在收尾）。
ASR_FINISH_TIMEOUT_S = 15.0
# LLM 文本按句号/问号/感叹号/分号/换行切句，逐句交给 TTS。
_SENTENCE_RE = re.compile(r"[。！？!?；;\n]+")
# 漏听兜底：ASR 空/失败或 LLM 第一段失败时，回一句而不是静默（spec §G.2）。
MISSED_SPEECH_TEXT = "没听清，再说一遍"
# 定时送单 check-in：每轮结束后随机排下一次主动开口的时间窗（秒）。
CHECKIN_MIN_S = 35
CHECKIN_MAX_S = 75


@dataclass
class TurnTiming(object):
    """一轮语音/事件的时间点（全部 `time.monotonic()`）。

    `started` 的定义随来源变化：
    - PTT：按键松开（之后还有 200 ms 尾音才送 ASR）；
    - VAD：VAD 判定句尾（已含约 400 ms 静音）；
    - 主动事件：worker 收到事件（new_order 从第一张单到达算起）。
    """
    source: str
    event: str
    started: float
    audio_ms: float = 0.0
    asr_started: float = None
    asr_done: float = None
    first_token_at: float = None
    llm_done_at: float = None
    tool_ack_ms: float = None
    tts_started: float = None
    tts_first_chunk: float = None
    first_audio: float = None
    playback_done: float = None


@dataclass
class _Utterance(object):
    """一段正在流式识别的录音。"""
    session: Any
    timing: TurnTiming
    texts: List[str]


class _ASRSession(object):
    """一段录音的流式 ASR：audio_q 持续喂 PCM，result_q 异步出 final 文本。

    ASR 客户端在独立线程里消费 audio_q（见 `ASRClient._pack_queue`），
    worker 线程只负责投入音频和轮询结果，不会阻塞采集。
    """

    def __init__(self, asr):
        # type: (Any) -> None
        self.audio_q = queue.Queue()
        self.result_q = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, args=(asr,), name="dialog-asr", daemon=True)
        self._thread.start()

    def feed(self, pcm):
        # type: (Any) -> None
        if pcm:
            self.audio_q.put(pcm)

    def finish(self):
        # type: () -> None
        self.audio_q.put(None)

    def cancel(self):
        # type: () -> None
        """放弃这段录音；ASR 客户端消费到哨兵后关闭连接。"""
        self.audio_q.put(None)

    def poll(self):
        # type: () -> list
        events = []
        while True:
            try:
                events.append(self.result_q.get_nowait())
            except queue.Empty:
                return events

    def _run(self, asr):
        # type: (Any) -> None
        try:
            for result in asr.stream(self.audio_q):
                if not isinstance(result, dict) or not result.get("definite"):
                    continue
                text = (result.get("text") or "").strip()
                if text:
                    self.result_q.put(("text", text))
        except Exception as exc:                # noqa: BLE001
            self.result_q.put(("error", exc))
        finally:
            self.result_q.put(("done", None))


class StreamingTTS(object):
    """把 LLM 的 text delta 按句切分、逐句合成并流式播放。

    一轮对话共享一个实例：第一段 / 第二段的 text 都喂进来，按句子先后
    排队播放；没有 text 的阶段自然不产生音频。首句合成完就开播，不
    等整段 LLM 输出结束。
    """

    def __init__(self, worker, timing):
        # type: (DialogWorker, TurnTiming) -> None
        self.worker = worker
        self.timing = timing
        self.buffer = ""
        self.text_seen = ""
        self.error = None
        self.sentences = queue.Queue()
        self.pcm = queue.Queue()
        self._cancelled = False
        self._vad_paused = False
        self._vad_mark = None
        self._producer_done = threading.Event()
        self._producer = threading.Thread(
            target=self._run_producer, name="dialog-tts-stream", daemon=True)
        self._producer.start()
        self._play_thread = threading.Thread(
            target=self._run_player, name="dialog-tts-play", daemon=True)
        self._play_thread.start()

    def feed(self, delta):
        # type: (str) -> None
        if self._cancelled or not delta:
            return
        self.text_seen += delta
        self.buffer += delta
        while True:
            match = _SENTENCE_RE.search(self.buffer)
            if not match:
                break
            end = match.end()
            sentence = self.buffer[:end].strip()
            self.buffer = self.buffer[end:]
            if sentence:
                self.sentences.put(sentence)

    def finish(self):
        # type: () -> None
        if self._cancelled:
            return
        tail = self.buffer.strip()
        self.buffer = ""
        if tail:
            self.sentences.put(tail)
        self.sentences.put(None)

    def cancel(self):
        # type: () -> None
        self._cancelled = True
        self.sentences.put(None)
        self.worker._shutdown(self.worker.clients.player, ("stop",))
        self._resume_capture()

    def wait(self, timeout=SPEAK_TIMEOUT_S):
        # type: (float) -> None
        self._producer_done.wait(timeout)
        if self.error is not None:
            self.worker._log_event("worker_error", where="tts",
                                   error=str(self.error))
        self._play_thread.join(timeout)
        deadline = time.monotonic() + timeout
        while (self.worker.clients.player.is_playing
               and not self.worker._stop.is_set()):
            self.worker._poll_first_audio(self.timing, self._snippet())
            if not self.worker._context_live():
                # 游戏暂停/退出时立刻停播，别把过期的播报念完。
                self.worker._shutdown(self.worker.clients.player, ("stop",))
                break
            if time.monotonic() >= deadline:
                self.worker._shutdown(self.worker.clients.player, ("stop",))
                break
            time.sleep(SPEAK_POLL_S)
        self.worker._poll_first_audio(self.timing, self._snippet())
        self._resume_capture()
        self.timing.playback_done = time.monotonic()

    def _snippet(self):
        # type: () -> str
        return self.text_seen[:40]

    def _pause_capture(self):
        # type: () -> None
        """首块音频入队后暂停 VAD，避免把自己的播报当成人声（半双工）。"""
        if self._vad_paused:
            return
        self._vad_paused = True
        self._vad_mark = self.worker._capture_mark()
        self.worker.clients.vad.paused = True

    def _resume_capture(self):
        # type: () -> None
        """播放结束 / 取消后恢复 VAD，只丢播放窗口内采集到的帧。"""
        if not self._vad_paused:
            return
        self._vad_paused = False
        self.worker._flush_capture_since(self._vad_mark)
        self.worker.clients.vad.paused = False

    def _run_player(self):
        # type: () -> None
        try:
            self.worker.clients.player.play(self._pcm_iter())
        except Exception as exc:                # noqa: BLE001
            self.error = exc

    def _pcm_iter(self):
        # type: () -> object
        while True:
            chunk = self.pcm.get()
            if chunk is None:
                return
            yield chunk

    def _run_producer(self):
        # type: () -> None
        try:
            while True:
                sentence = self.sentences.get()
                if sentence is None or self._cancelled:
                    break
                log.debug("播报: %r", sentence)
                if self.timing.tts_started is None:
                    self.timing.tts_started = time.monotonic()
                for chunk in self.worker.clients.tts.synthesize(sentence):
                    if self._cancelled:
                        break
                    if not chunk:
                        continue
                    if self.timing.tts_first_chunk is None:
                        self.timing.tts_first_chunk = time.monotonic()
                        self._pause_capture()
                        self.worker._log_event(
                            "tts_first_chunk", text=self._snippet(),
                            tts_first_chunk_ms=self.worker._ms_since(
                                self.timing.tts_started,
                                self.timing.tts_first_chunk),
                            end_to_first_chunk_ms=self.worker._ms_since(
                                self.timing.started,
                                self.timing.tts_first_chunk))
                    self.pcm.put(chunk)
        except Exception as exc:                # noqa: BLE001
            self.error = exc
        finally:
            self.pcm.put(None)
            self._producer_done.set()


@dataclass
class DialogClients(object):
    """worker 依赖的客户端；测试全部注入 fake。"""
    capture: Any
    vad: Any
    asr: Any
    tts: Any
    player: Any
    llm: Any
    # 可选 PTT 监听；None 时用 VAD 自动切句。
    ptt: Any = None


class DialogWorker(object):
    """语音对话后台 worker（spec §6.2）。"""

    def __init__(self, config, cmd_q, ack_q, ctx_q, event_q, clients=None):
        # type: (Any, Any, Any, Any, Any, Any) -> None
        self.config = config
        self.cmd_q = cmd_q
        self.ack_q = ack_q
        self.ctx_q = ctx_q
        self.event_q = event_q
        self.clients = clients if clients is not None else self._default_clients(config)
        self.state = DialogState()
        self.last_proactive_tick = 0
        self._stop = threading.Event()
        self._thread = None  # type: Any
        self._closed = False
        self._context = {}  # type: dict
        self._context_lock = threading.Lock()
        self._state_lock = threading.Lock()
        # 对话日志只由 worker / 生命周期线程写，主循环不碰；锁保护 append + flush。
        self._log_lock = threading.Lock()
        self._log_fh = None  # type: Any
        # 延迟打点的锚点；主循环写 level_start 时由 worker 线程读取。
        self._level_started = None
        self._pending_order_at = None
        # 下次定时 check-in 的单调时钟时间点；None 表示尚未排期。
        self._next_checkin_at = None
        # 首次 new_order 的触发时间与已发起的 check-in 次数：首次 check-in
        # 必须避开首次 new_order，避免开局连续发言。
        self._first_new_order_at = None
        self._checkins_fired = 0

    # -------------------------------------------------------------- logging

    def _log_event(self, kind, **fields):
        # type: (str, Any) -> None
        """追加一条 JSONL 对话日志；未配置 `log_path` 时是空操作。

        调用方只传复盘需要的字段；不记录密钥、完整 prompt 或原始音频。
        日志写入失败只降级为 debug，绝不拖垮语音链路。
        """
        with self._log_lock:
            fh = self._log_fh
            if fh is None:
                return
            record = {
                "ts": datetime.datetime.now(
                    datetime.timezone.utc).isoformat(timespec="milliseconds"),
                "kind": kind,
            }
            record.update(fields)
            try:
                fh.write(json.dumps(record, ensure_ascii=False,
                                    default=_json_default) + "\n")
                fh.flush()
            except Exception:
                log.debug("写对话日志失败", exc_info=True)

    def log_event(self, kind, **fields):
        # type: (str, Any) -> None
        """供主循环写生命周期事件（当前只有 level_start）。"""
        if kind == "level_start":
            # 关卡开始是「一进关分工讨论慢」的分母；之后每轮都带 since_level_ms。
            self._level_started = time.monotonic()
        self._log_event(kind, **fields)

    @staticmethod
    def _ms_since(start, end):
        # type: (Any, Any) -> Any
        if start is None or end is None:
            return None
        return int(round((end - start) * 1000.0))

    def _latency_fields(self, timing, outcome=None):
        # type: (TurnTiming, Any) -> dict
        """把 TurnTiming 压成一行可读的毫秒字段。"""
        fields = {
            "event": timing.event,
            "source": timing.source,
            "audio_ms": int(round(timing.audio_ms or 0)),
            "asr_ms": self._ms_since(timing.asr_started, timing.asr_done),
            "end_to_asr_ms": self._ms_since(timing.started, timing.asr_done),
            "llm_first_token_ms": self._ms_since(timing.started,
                                                 timing.first_token_at),
            "llm_done_ms": self._ms_since(timing.started,
                                          timing.llm_done_at),
            "tool_ack_ms": (int(round(timing.tool_ack_ms))
                            if timing.tool_ack_ms is not None else None),
            "tts_first_chunk_ms": self._ms_since(timing.started,
                                                 timing.tts_first_chunk),
            "first_audio_ms": self._ms_since(timing.started,
                                             timing.first_audio),
            "playback_done_ms": self._ms_since(timing.started,
                                               timing.playback_done),
            "since_level_ms": self._ms_since(self._level_started,
                                             timing.started),
        }
        if outcome:
            fields["outcome"] = outcome
        return fields

    def _open_log(self):
        # type: () -> None
        """创建父目录并以追加模式打开对话日志。"""
        path = self.config.log_path
        if not path:
            return
        try:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._log_fh = open(path, "a", encoding="utf-8")
        except OSError:
            log.exception("打开对话日志失败: %s", path)

    @staticmethod
    def _enable_debug_logging():
        # type: () -> None
        """`--voice-debug`：把 ai.dialog 日志开到 DEBUG，并只加一次 stderr handler。"""
        logger = logging.getLogger("ai.dialog")
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers:
            if getattr(handler, "_oc2_voice_debug", False):
                return
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(
            "[voice] %(levelname)s %(name)s: %(message)s"))
        handler._oc2_voice_debug = True
        logger.addHandler(handler)

    # ----------------------------------------------------------- lifecycle

    def start(self):
        # type: () -> None
        """启动 daemon 线程；重复调用无副作用。

        `stop()` 之后不支持重启：采集流已关闭，请为每局新建一个实例。
        """
        if self._closed:
            raise RuntimeError("DialogWorker 不支持 stop() 之后重启")
        if self._thread is not None and self._thread.is_alive():
            return
        if self.config.debug:
            self._enable_debug_logging()
            log.debug("VAD 开口阈值 %.1f dB", self.config.vad_threshold_db)
        if self.config.log_path and self._log_fh is None:
            self._open_log()
        self._log_event("start", debug=bool(self.config.debug),
                        log_path=self.config.log_path)
        # 预连 TTS：后台建好 WebSocket/StartConnection，等第一句文本一好就少一次握手。
        warmup = getattr(self.clients.tts, "warmup", None)
        if callable(warmup):
            try:
                warmup()
            except Exception:               # noqa: BLE001
                log.debug("TTS 预连失败（不影响后续逐句建连）", exc_info=True)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="dialog-worker", daemon=True
        )
        self._thread.start()

    def stop(self):
        # type: () -> None
        """停线程、停播放、关采集；可重复调用，停止是尽力而为。

        `player.play()` 可能阻塞在卡住的 TTS 流上，worker 线程未必能在
        join 上限内退出；daemon 线程随进程退出，`_speak` 也会在每次循环
        开头检查 stop。
        """
        self._stop.set()
        self._closed = True
        self._log_event("stop")
        # 先停客户端再 join：AudioCapture.frames() 阻塞在队列上，close()
        # 会推结束哨兵，否则 join 只能等超时。
        self._shutdown(self.clients.player, ("stop",))
        self._shutdown(self.clients.ptt, ("stop",))
        self._shutdown(self.clients.capture, ("stop", "close"))
        self._shutdown(self.clients.tts, ("close",))
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        with self._log_lock:
            fh = self._log_fh
            self._log_fh = None
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    log.debug("关闭对话日志失败", exc_info=True)

    @staticmethod
    def _shutdown(client, names):
        # type: (Any, tuple) -> None
        for name in names:
            fn = getattr(client, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    log.debug("关闭 %s.%s 失败", type(client).__name__, name,
                              exc_info=True)
                return

    def reset_state(self):
        # type: () -> None
        """关卡切换：清空跨回合对话状态（主循环调用，spec §4.7）。

        只替换 `DialogState` 对象，不在原地改字段：worker 线程可能在读旧
        对象，但 Python 的属性赋值是原子的，读到的要么是旧状态、要么是
        全新的空状态，不会看到改到一半的字段。`_state_lock` 让并发 reset
        串行化；`last_proactive_tick` 也归零，否则新关卡开局会被上一关的
        4 秒主动开口闸压住。
        """
        with self._state_lock:
            self.state = DialogState()
            self.last_proactive_tick = 0
            self._pending_order_at = None
            self._level_started = None
            self._next_checkin_at = None
            self._first_new_order_at = None
            self._checkins_fired = 0

    # ------------------------------------------------------------- queues

    def publish_context(self, context):
        # type: (dict) -> None
        """发布最新一份上下文；旧快照直接覆盖（队列 maxsize=1 语义）。"""
        context = dict(context or {})
        with self._context_lock:
            self._context = context
        while True:
            try:
                self.ctx_q.get_nowait()
            except queue.Empty:
                break
        try:
            self.ctx_q.put_nowait(context)
        except queue.Full:
            log.debug("上下文队列满，丢弃旧快照")

    def put_event(self, event):
        # type: (Any) -> None
        """投递主动事件；队列满时丢最旧的，主循环永不阻塞。"""
        try:
            self.event_q.put_nowait(event)
            return
        except queue.Full:
            pass
        try:
            self.event_q.get_nowait()
        except queue.Empty:
            pass
        try:
            self.event_q.put_nowait(event)
        except queue.Full:
            log.debug("事件队列满，丢弃事件: %r", event)

    def _latest_context(self):
        # type: () -> dict
        with self._context_lock:
            context = self._context
        if context:
            return context
        # 兼容调用方绕过 publish_context 直接投 ctx_q 的用法。
        try:
            context = self.ctx_q.get_nowait()
        except queue.Empty:
            return {}
        with self._context_lock:
            self._context = context
        return context

    # --------------------------------------------------------- thread loop

    def _new_utterance(self, source, speech_end=None):
        # type: (str, Any) -> _Utterance
        timing = TurnTiming(
            source=source, event="speech",
            started=speech_end if speech_end is not None else time.monotonic())
        timing.asr_started = time.monotonic()
        return _Utterance(_ASRSession(self.clients.asr), timing, [])

    def _feed_utterance(self, utterance, frame):
        # type: (_Utterance, Any) -> None
        pcm = _pcm16([frame])
        utterance.session.feed(pcm)
        utterance.timing.audio_ms += getattr(self.clients.vad, "frame_ms", 50)

    def _poll_utterance(self, utterance):
        # type: (_Utterance) -> bool
        """轮询 ASR 结果；整段结束时触发对话回合并返回 True。"""
        done = False
        error = None
        for kind, payload in utterance.session.poll():
            if kind == "text":
                utterance.texts.append(payload)
            elif kind == "error":
                error = payload
                done = True
            else:
                done = True
        if not done:
            return False
        utterance.timing.asr_done = time.monotonic()
        if error is not None:
            log.exception("流式 ASR 失败: %r", error)
            self._log_event("worker_error", where="asr", error=str(error))
            self._log_event(
                "turn_latency",
                **self._latency_fields(utterance.timing,
                                       outcome="asr_error"))
            return True
        merged = _merge_asr_texts(utterance.texts)
        if merged:
            self._handle_transcript(
                merged, self._latest_context(), timing=utterance.timing)
        else:
            self._log_event(
                "turn_latency",
                **self._latency_fields(utterance.timing, outcome="no_asr"))
            # 漏听主动提示：识别为空不再静默（spec §G.2）。
            self._speak(MISSED_SPEECH_TEXT, timing=utterance.timing)
        return True

    def _await_utterance(self, utterance):
        # type: (_Utterance) -> None
        """录音结束后等 ASR final；期间不再读新帧，避免阻塞在采集队列上。"""
        utterance.session.finish()
        deadline = time.monotonic() + ASR_FINISH_TIMEOUT_S
        while not self._poll_utterance(utterance):
            if self._stop.is_set() or time.monotonic() >= deadline:
                utterance.session.cancel()
                self._log_event("worker_error", where="asr",
                                error="等待 ASR final 超时/已停止")
                self._log_event(
                    "turn_latency",
                    **self._latency_fields(utterance.timing,
                                           outcome="asr_timeout"))
                return
            time.sleep(IDLE_S)

    def _run(self):
        # type: () -> None
        frames = None
        # 当前正在流式识别的录音；同一时间只允许一段。
        utterance = None
        vad_active = False
        # VAD 在连续 3 帧超阈值时才报 start；worker 一直保留最近 2 帧，
        # start 时一起送进 ASR，避免第一个音素被切掉（spec §8.2）。
        preroll = []
        frame_count = 0
        last_level_log = time.monotonic()
        # PTT：按住录音、松开发送；pre-roll 保留按键前的 300ms。
        ptt = self.clients.ptt
        ptt_ok = False
        ptt_recording = False
        ptt_tail = 0
        ptt_hold_frames = 0
        ptt_speech_end = None
        ptt_preroll = deque(maxlen=PTT_PREROLL_FRAMES)
        if ptt is not None:
            try:
                ptt.start()
                ptt_ok = True
                log.info("PTT 已启用：按键 %s",
                         getattr(self.config, "ptt_key", "?"))
            except Exception as exc:            # noqa: BLE001
                log.exception("PTT 启动失败，退回 VAD")
                self._log_event("worker_error", where="ptt", error=str(exc))
        try:
            while not self._stop.is_set():
                # 事件优先于音频：主动开口不能被采集阻塞。
                self._drain_events()
                # 单张新单只会投一次 `("new_order", [id])`，必须由 worker
                # 周期性用空 id 再调一次，1 秒合并窗口才会关窗触发。
                self._flush_pending_orders()
                if utterance is not None and self._poll_utterance(utterance):
                    utterance = None
                if self.clients.vad.paused:
                    preroll = []
                    vad_active = False
                    if utterance is not None:
                        # 主动播报/TTS 开始时，正在录的半段人声作废。
                        utterance.session.cancel()
                        utterance = None
                    if ptt_ok:
                        # 播放期间按住 PTT 不录音；清掉积压事件与半截缓冲。
                        ptt.poll()
                        ptt_recording = False
                        ptt_tail = 0
                        ptt_hold_frames = 0
                        ptt_preroll.clear()
                    self._stop.wait(IDLE_S)
                    continue
                self._maybe_checkin(utterance, vad_active, ptt_recording)
                if frames is None:
                    try:
                        frames = self.clients.capture.frames()
                        log.debug(
                            "音频采集已启动（rate=%s device=%s）",
                            getattr(self.clients.capture, "sample_rate", "?"),
                            getattr(self.clients.capture, "device_index", "?"))
                    except Exception as exc:
                        log.exception("启动音频采集失败")
                        self._log_event("worker_error", where="capture",
                                        error=str(exc))
                        self._stop.wait(0.5)
                        continue
                try:
                    frame = next(frames)
                except StopIteration:
                    frames = None
                    continue
                except Exception as exc:
                    log.exception("读取音频帧失败")
                    self._log_event("worker_error", where="frames",
                                    error=str(exc))
                    frames = None
                    self._stop.wait(0.1)
                    continue

                frame_count += 1
                level = floor = None
                if self.config.debug:
                    level_fn = getattr(self.clients.vad, "_level_db", None)
                    level = level_fn(frame) if callable(level_fn) else None
                    floor = getattr(self.clients.vad, "floor_db", None)
                    now = time.monotonic()
                    if now - last_level_log >= 2.0:
                        last_level_log = now
                        log.debug(
                            "音频帧 #%d level=%s floor=%s paused=%s",
                            frame_count,
                            "?" if level is None else "%.1f" % level,
                            "?" if floor is None else "%.1f" % floor,
                            self.clients.vad.paused)

                if ptt_ok:
                    for event in ptt.poll():
                        log.debug("PTT 事件: %s（帧 #%d）", event, frame_count)
                        if event == "down":
                            if not ptt_recording and utterance is None:
                                ptt_recording = True
                                ptt_tail = 0
                                ptt_hold_frames = 0
                                ptt_speech_end = None
                                utterance = self._new_utterance("ptt")
                                # 按键前的 300 ms 也送进去，避免首音被切。
                                for preroll_frame in ptt_preroll:
                                    self._feed_utterance(
                                        utterance, preroll_frame)
                        elif event == "up" and ptt_recording:
                            ptt_recording = False
                            ptt_speech_end = time.monotonic()
                            if utterance is not None:
                                utterance.timing.started = ptt_speech_end
                            if ptt_hold_frames >= 2:
                                ptt_tail = PTT_TAIL_FRAMES
                            else:
                                # 轻点一下不送 ASR，避免把 pre-roll 环境声
                                # 当成一句话。
                                if utterance is not None:
                                    utterance.session.cancel()
                                utterance = None
                                ptt_tail = 0
                                ptt_speech_end = None
                    if ptt_recording:
                        if utterance is not None:
                            self._feed_utterance(utterance, frame)
                        ptt_hold_frames += 1
                    elif ptt_tail > 0:
                        if utterance is not None:
                            self._feed_utterance(utterance, frame)
                        ptt_tail -= 1
                        if ptt_tail == 0:
                            if utterance is not None:
                                self._await_utterance(utterance)
                                utterance = None
                            ptt_speech_end = None
                    else:
                        ptt_preroll.append(frame)
                    if utterance is not None and self._poll_utterance(utterance):
                        utterance = None
                    continue

                try:
                    event = self.clients.vad.feed(frame)
                except Exception as exc:
                    log.exception("VAD 处理失败")
                    self._log_event("worker_error", where="vad",
                                    error=str(exc))
                    event = None
                if event:
                    log.debug(
                        "VAD 事件 %s（帧 #%d level=%s floor=%s）",
                        event, frame_count,
                        "?" if level is None else "%.1f" % level,
                        "?" if floor is None else "%.1f" % floor)
                if event == "start":
                    if utterance is None:
                        utterance = self._new_utterance("vad")
                        for preroll_frame in preroll:
                            self._feed_utterance(
                                utterance, preroll_frame)
                        self._feed_utterance(utterance, frame)
                        vad_active = True
                    else:
                        vad_active = False
                elif event == "end":
                    # VAD 的 end 已经包含约 400 ms 静音判定；started 从这里算。
                    speech_end = time.monotonic()
                    if vad_active and utterance is not None:
                        self._feed_utterance(utterance, frame)
                        utterance.timing.started = speech_end
                        self._await_utterance(utterance)
                        utterance = None
                    vad_active = False
                elif vad_active and utterance is not None:
                    self._feed_utterance(utterance, frame)
                if utterance is not None and self._poll_utterance(utterance):
                    utterance = None
                preroll.append(frame)
                if len(preroll) > PREROLL_FRAMES:
                    preroll = preroll[-PREROLL_FRAMES:]
        finally:
            if utterance is not None:
                utterance.session.cancel()
            if ptt_ok:
                try:
                    ptt.stop()
                except Exception:
                    log.debug("停止 PTT 失败", exc_info=True)
            close = getattr(frames, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    log.debug("关闭帧迭代器失败", exc_info=True)

    def _maybe_checkin(self, utterance, vad_active, ptt_recording):
        # type: (Any, bool, bool) -> None
        """定时 check-in：空闲且排期到点时，主动报一句送单局面。

        只在没有正在录的人声 / VAD 开口 / PTT 按住时开口（播放期 VAD 已暂停，
        调用点也在 `vad.paused` 分支之后）；排期由 `_run_llm` 每轮结束重置。
        """
        now = time.monotonic()
        context = self._latest_context() or {}
        live = bool(context.get("live"))
        if not live or context.get("round_finished"):
            # 暂停 / 退出关卡 / 本局已结束：清掉随机排期，恢复后再由下一轮
            # 正常回合重排。这样退出后不会再冒出 check-in。
            self._next_checkin_at = None
            return
        has_orders = bool(context.get("orders"))
        in_level = context.get("level") is not None
        if (not utterance and not vad_active and not ptt_recording
                and should_checkin(self._next_checkin_at, now,
                                   has_orders, in_level, live)):
            # 首次 check-in 必须和首次 new_order 保持间距，避免开局连续发言。
            if self._checkins_fired == 0 and self._first_new_order_at is not None:
                earliest = self._first_new_order_at + CHECKIN_MIN_S
                if now < earliest:
                    self._next_checkin_at = earliest
                    return
            # 先清排期再开口：正常结束会重排，LLM 失败等早退路径则停在
            # None，下一拍 `should_checkin` 返回 False，不会空转打 LLM。
            self._next_checkin_at = None
            self._checkins_fired += 1
            self._run_llm(context, "checkin", "",
                          timing=TurnTiming(source="event", event="checkin",
                                            started=now))

    def _context_live(self):
        # type: () -> bool
        """游戏是否在「关卡内且未暂停」；不满足时不再开口/继续播报。"""
        ctx = self._latest_context() or {}
        return bool(ctx.get("live"))

    def _drain_events(self):
        # type: () -> None
        while not self._stop.is_set():
            try:
                event = self.event_q.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle_event(event, self._latest_context())
            except Exception as exc:
                log.exception("处理对话事件失败: %r", event)
                self._log_event("worker_error", where="event",
                                event=event, error=str(exc))

    def _flush_pending_orders(self):
        # type: () -> None
        """推进新单合并窗口：没有待发批次时是空操作。"""
        if not self.state.pending_orders:
            return
        try:
            self._handle_event(("new_order", []), self._latest_context())
        except Exception as exc:
            log.exception("刷新新单批次失败")
            self._log_event("worker_error", where="new_order",
                            error=str(exc))

    def _process_utterance(self, frames, coalesce=False, source="vad",
                           speech_end=None, audio_ms=None):
        # type: (list, bool, str, Any, Any) -> None
        """一段人声：float32 帧 → 16 bit PCM → ASR → 对话回合。

        `coalesce=True`（PTT）时把同一次录音里的多个 definite 片段合并成
        一句，只触发一轮 LLM/TTS；否则按片段逐条处理（VAD 旧行为）。
        ASR 起止时间写进 `asr` / `turn_latency` 事件。
        """
        timing = TurnTiming(
            source=source,
            event="speech",
            started=speech_end if speech_end is not None else time.monotonic(),
            audio_ms=audio_ms or 0.0,
        )
        try:
            pcm = _pcm16(frames)
            if not pcm:
                return
            log.debug("处理一段人声：%d 帧 / %d 字节 PCM", len(frames), len(pcm))
            timing.asr_started = time.monotonic()
            texts = []
            for result in self.clients.asr.stream([pcm]):
                if not isinstance(result, dict) or not result.get("definite"):
                    continue
                text = result.get("text") or ""
                if not text.strip():
                    continue
                timing.asr_done = time.monotonic()
                if coalesce:
                    texts.append(text)
                else:
                    self._handle_transcript(
                        text, self._latest_context(), timing=timing)
            timing.asr_done = time.monotonic()
            if coalesce and texts:
                merged = _merge_asr_texts(texts)
                log.debug("ASR 合并文本: %r（片段 %r）", merged, texts)
                if merged:
                    self._handle_transcript(
                        merged, self._latest_context(), timing=timing)
        except Exception as exc:
            log.exception("ASR / 对话回合失败")
            self._log_event("worker_error", where="asr", error=str(exc))

    # ------------------------------------------------------------ commands

    def _handle_transcript(self, text, context, timing=None):
        # type: (str, dict, Any) -> None
        """一段 ASR final：去重 → LLM → 账本落地 → 播报。"""
        text = (text or "").strip()
        if not text:
            # 空文本 = 没识别到内容，主动提示（spec §G.2）。
            self._speak(MISSED_SPEECH_TEXT, timing=timing)
            return
        context = context or {}
        tick = int(context.get("tick", 0) or 0)
        if timing is None:
            # 直接调用（测试/回放）没有音频时间戳，从收到文本开始算。
            timing = TurnTiming(source="direct", event="speech",
                                started=time.monotonic())
        self._log_event(
            "asr", text=text, tick=tick, source=timing.source,
            audio_ms=int(round(timing.audio_ms or 0)),
            asr_ms=self._ms_since(timing.asr_started, timing.asr_done),
            end_to_asr_ms=self._ms_since(timing.started, timing.asr_done))
        log.debug("ASR 文本: %r（tick=%s）", text, tick)
        if not should_trigger("speech", self.state, tick, text):
            self._log_event("turn_latency",
                            **self._latency_fields(timing, outcome="suppressed"))
            return
        # 触发才记账；LLM 失败也不重试同一句话（spec §5.1 三道闸）。
        self.state.last_speech_tick = tick
        self._run_llm(context, "speech", text, timing=timing)

    def _handle_event(self, event, context):
        # type: (Any, dict) -> None
        """主循环投递的主动事件。

        当前只有 `("new_order", ids)`：新菜出现时由 LLM 定新物料分工。
        """
        if not isinstance(event, (tuple, list)):
            log.debug("忽略未知事件: %r", event)
            return
        if len(event) != 2:
            log.debug("忽略未知事件: %r", event)
            return
        name, payload = event
        context = context or {}
        tick = int(context.get("tick", 0) or 0)

        if name == "new_order":
            ids = payload or []
            if ids and self._pending_order_at is None:
                # 从第一张新单到达开始算，包含 1 秒合并窗口。
                self._pending_order_at = time.monotonic()
            trigger, batch = merge_new_orders(ids, self.state, tick)
            if not trigger:
                return
            # 首次 new_order 记下时间点，供首次 check-in 避让。
            if self._first_new_order_at is None:
                self._first_new_order_at = time.monotonic()
            # 1 秒合并窗口就是 new_order 的限频，不再套全局 4 秒闸。
            self.last_proactive_tick = tick
            timing = TurnTiming(
                source="event", event=name,
                started=(self._pending_order_at
                         if self._pending_order_at is not None
                         else time.monotonic()))
            self._pending_order_at = None
            self._run_llm(context, name, "", new_orders=batch,
                          timing=timing)
            return
        if name in ("last_seconds", "round_summary"):
            # 主循环一次性触发（各只发一次），直接说，不调工具、不再套限频闸。
            self.last_proactive_tick = tick
            timing = TurnTiming(source="event", event=name,
                                started=time.monotonic())
            self._run_llm(context, name, "", timing=timing)
            return
        log.debug("忽略未知事件: %r", name)

    def _run_llm(self, context, event, transcript, new_orders=None,
                 timing=None):
        # type: (dict, str, str, Any, Any) -> None
        """一次对话回合（标准两段式 function calling）。

        1. 第一段：speech / new_order 回合 `tool_choice="auto"`，模型决定调不调用
           工具；rescue 回合 `tool_choice="none"`（只说话）。两种都带 tools：
           历史里有 tool_calls，兼容端点要求 tools 有定义；
        2. 有调用 → 打包交主循环落地，等逐条结果，回填 tool 消息；
        3. 第二段 `tool_choice="none"`：模型看到真实结果**和落地后的账本**
           再说最终那句话。

        第一段 / 第二段的 text 都经 `StreamingTTS` 按句流式播放；没有 text
        的阶段不发声。第一段 text 直接播（换取更快体感，接受「先出口、
        随后第二段再确认/修正」的少量重复）。

        回合开始时钉住 `self.state`：回合中途若换关（`reset_state` 换了新
        对象），本轮结果作废，不把旧关历史写进新关。
        """
        state_obj = self.state
        context = context or {}
        if timing is None:
            timing = TurnTiming(source="event", event=event,
                                started=time.monotonic())
        else:
            timing.event = event
        head = [system_message(context.get("dag") or "")]
        new_dag = ""
        if event == "new_order":
            new_dag = _new_dishes_dag(context, new_orders)
        world = state_message(event, context, new_dag)
        opener = speech_message(transcript) if event == "speech" \
            else event_note(event)
        # 世界状态放在本轮开口之前：它描述「此刻」，模型读完状态再看 P1 的话。
        turn = [opener]
        history = list(state_obj.history)
        tools = context.get("tools") or None
        choice = "auto" if event in ("speech", "new_order") else "none"

        # 一整轮的流式 TTS：第一段 / 第二段的 text 都按句喂进去，按序播放。
        stream = StreamingTTS(self, timing)
        first = self._chat(head + history + [world] + turn,
                           tools, event, "first", tool_choice=choice,
                           timing=timing, on_text=stream.feed)
        if first is None or self.state is not state_obj:
            stream.cancel()
            if first is None:
                self._log_event(
                    "turn_latency",
                    **self._latency_fields(timing, outcome="llm_error"))
                # 漏听主动提示：第一段 LLM 失败时回一句，而不是静默（spec §G.2）。
                if event == "speech":
                    self._speak(MISSED_SPEECH_TEXT, timing=timing)
            return
        final = first
        if first.tool_calls:
            results, duties = self._submit_tools(
                first.tool_calls, event, transcript, timing=timing)
            if self.state is not state_obj:
                stream.cancel()
                return
            # 不改 LLM 返回的对象：只取它的调用，content 置空另建一份。
            calls = list(first.tool_calls)
            turn.append(assistant_message(
                dataclasses.replace(first, content="", tool_calls=calls)))
            for call, result in zip(calls, results):
                turn.append(tool_message(call.id, result))
            if duties is not None:
                # 第二段要看落地后的账本，否则世界状态与 tool 结果自相矛盾
                world = state_message(event, dict(context, duties=duties),
                                      new_dag)
            second = self._chat(head + history + [world] + turn,
                                tools, event, "final", tool_choice="none",
                                timing=timing, on_text=stream.feed)
            if self.state is not state_obj:
                stream.cancel()
                return
            if second is None:
                # 第二段失败：流式没喂进最终文本，用兜底模板播并收尾。
                stream.cancel()
                self._speak(_fallback_text(results), timing=timing)
                return
            content = second.content
            final = dataclasses.replace(first, content=content, tool_calls=[])
        turn.append(assistant_message(final))
        self._remember(state_obj, turn)
        timing.llm_done_at = time.monotonic()
        stream.finish()
        stream.wait()
        # 每轮正常结束后随机排下一次定时 check-in（失败早退不重排，沿用旧排期）。
        # 结束总结是本局最后一句，之后不再排 check-in，避免退出关卡还在说话。
        if event != "round_summary":
            self._next_checkin_at = (time.monotonic()
                                     + random.uniform(CHECKIN_MIN_S, CHECKIN_MAX_S))
        if stream.error is not None:
            outcome = "tts_error"
        elif stream.text_seen.strip():
            outcome = "ok"
        else:
            outcome = "empty_reply"
        self._log_event(
            "turn_latency",
            **self._latency_fields(timing, outcome=outcome))

    def _chat(self, messages, tools, event, stage, tool_choice="auto",
              timing=None, on_text=None):
        # type: (list, Any, str, str, str, Any, Any) -> Any
        """调一次 LLM 并记日志；失败返回 None（调用方静默或兜底）。"""
        started = time.monotonic()
        try:
            reply = self.clients.llm.chat(messages, tools,
                                          tool_choice=tool_choice,
                                          on_text=on_text)
        except Exception as exc:
            log.exception("LLM 回合失败（event=%s stage=%s）", event, stage)
            self._log_event("worker_error", where="llm", event=event,
                            stage=stage, error=str(exc))
            return None
        done = time.monotonic()
        if tool_choice == "none" and reply.tool_calls:
            # 个别模型无视 tool_choice=none：调用不落地，只取它的话
            log.debug("tool_choice=none 仍返回工具调用，已丢弃: %s",
                      reply.tool_calls)
            reply = dataclasses.replace(reply, tool_calls=[])
        first_token_ms = getattr(reply, "first_token_ms", None)
        end_first = None
        if timing is not None and first_token_ms is not None:
            end_first = int(round((started - timing.started) * 1000.0
                                  + first_token_ms))
            if timing.first_token_at is None:
                timing.first_token_at = started + first_token_ms / 1000.0
        self._log_event(
            "llm_reply", event=event, stage=stage, content=reply.content,
            tool_calls=[{"name": c.name, "arguments": c.arguments}
                        for c in reply.tool_calls],
            request_ms=(self._ms_since(timing.started, started)
                        if timing is not None else None),
            first_token_ms=(int(round(first_token_ms))
                            if first_token_ms is not None else None),
            elapsed_ms=int(round((done - started) * 1000.0)),
            end_to_first_token_ms=end_first,
            end_to_done_ms=(self._ms_since(timing.started, done)
                            if timing is not None else None))
        log.debug("LLM 回复（event=%s stage=%s）: %r tools=%s",
                  event, stage, reply.content, reply.tool_calls)
        return reply

    def _remember(self, state_obj, turn):
        # type: (Any, list) -> None
        """本轮消息并入历史并截断；不能以孤立的 tool / assistant 开头。"""
        with self._state_lock:
            if self.state is not state_obj:
                return
            history = (list(state_obj.history) + turn)[-HISTORY_MAX:]
            while history and history[0].get("role") in ("tool", "assistant"):
                history.pop(0)
            state_obj.history = history

    def _submit_tools(self, calls, event, transcript, timing=None):
        # type: (list, str, str, Any) -> tuple
        """把一轮的工具调用打包投给主循环，等一个 ack。

        返回 `(逐条结果, 落地后的账本快照或 None)`；超时时快照为 None。
        """
        batch = DutyBatch(
            batch_id=uuid.uuid4().hex,
            calls=[(call.name, call.args()) for call in calls],
            source=transcript or "",
            origin=event,
        )
        self.cmd_q.put(batch)
        tool_started = time.monotonic()
        self._log_event("duty_batch", event=event, batch_id=batch.batch_id,
                        calls=batch.calls)
        deadline = tool_started + ACK_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ack = self.ack_q.get(timeout=remaining)
            except queue.Empty:
                break
            if not isinstance(ack, dict) or ack.get("batch_id") != batch.batch_id:
                log.debug("忽略无关 ack: %r", ack)
                continue
            results = list(ack.get("results") or [])
            ack_ms = self._ms_since(tool_started, time.monotonic())
            if timing is not None:
                timing.tool_ack_ms = ack_ms
            self._log_event("duty_ack", batch_id=batch.batch_id,
                            results=results, ack_ms=ack_ms)
            if len(results) < len(calls):
                results += [{"ok": False, "reason": "主循环未处理"}] * (
                    len(calls) - len(results))
            return results[:len(calls)], ack.get("duties")
        ack_ms = self._ms_since(tool_started, time.monotonic())
        if timing is not None:
            timing.tool_ack_ms = ack_ms
        self._log_event("duty_ack", batch_id=batch.batch_id, results=None,
                        ack_ms=ack_ms)
        return [{"ok": False, "reason": "主循环超时未确认"}] * len(calls), None

    # ------------------------------------------------------------- speak

    def _speak(self, text, timing=None):
        # type: (Any, Any) -> None
        """半双工播放：暂停 VAD → 播放 → 只 flush 播放窗口 → 恢复 VAD。

        TTS 生成器在独立 producer 线程里消费，主线程有界等待
        `SPEAK_TIMEOUT_S`；超时/停止时调用 `player.stop()` 并关闭生成器，
        避免卡住的网络流把 worker 线程永远钉住（spec §5.4 / §7.4）。
        采集缓冲只丢播放窗口内的帧：TTS 前已排队、尚未处理的玩家语音必须
        保留，否则会把「刚说完的话」一起冲掉。

        TTS 失败时静默（只记日志），不在本机兜底朗读。
        `tts_first_chunk` / `first_audio` / `turn_latency` 记录分段耗时。
        """
        text = (text or "").strip()
        if not text or self._stop.is_set():
            return
        if timing is None:
            timing = TurnTiming(source="direct", event="speech",
                                started=time.monotonic())
        timing.tts_started = time.monotonic()
        self._log_event(
            "speak", text=text,
            from_end_ms=self._ms_since(timing.started, timing.tts_started))
        log.debug("播报: %r", text)
        vad = self.clients.vad
        mark = self._capture_mark()
        vad.paused = True
        generator = None
        errors = []  # type: list
        outcome = "ok"

        def consume():
            # producer 线程只做「生成器 → 播放队列」，异常交回主线程。
            try:
                self.clients.player.play(generator)
            except Exception as exc:            # noqa: BLE001
                errors.append(exc)

        try:
            base = self.clients.tts.synthesize(text)

            def timed_chunks():
                # 首块 PCM 是 TTS 首包；真正出声还要等输出流回调。
                for chunk in base:
                    if chunk and timing.tts_first_chunk is None:
                        timing.tts_first_chunk = time.monotonic()
                        self._log_event(
                            "tts_first_chunk", text=text,
                            tts_first_chunk_ms=self._ms_since(
                                timing.tts_started, timing.tts_first_chunk),
                            end_to_first_chunk_ms=self._ms_since(
                                timing.started, timing.tts_first_chunk))
                    yield chunk

            generator = timed_chunks()
            reset_mark = getattr(self.clients.player, "reset_output_mark", None)
            if callable(reset_mark):
                reset_mark()
            producer = threading.Thread(
                target=consume, name="dialog-tts", daemon=True)
            producer.start()
            deadline = time.monotonic() + SPEAK_TIMEOUT_S
            while producer.is_alive():
                self._poll_first_audio(timing, text)
                if (self._stop.is_set() or not self._context_live()
                        or time.monotonic() >= deadline):
                    break
                time.sleep(SPEAK_POLL_S)
            if producer.is_alive():
                log.warning("TTS 生产超时/已停止，强制结束")
                outcome = "timeout"
                self._shutdown(self.clients.player, ("stop",))
                self._close_generator(generator)
                producer.join(timeout=TTS_JOIN_S)
            elif errors:
                raise errors[0]
            else:
                # 音频已全部入队，等设备把队列放完；同样有界。
                while (self.clients.player.is_playing
                       and not self._stop.is_set()):
                    self._poll_first_audio(timing, text)
                    if not self._context_live():
                        self._shutdown(self.clients.player, ("stop",))
                        break
                    if time.monotonic() >= deadline:
                        log.warning("TTS 播放超时，强制恢复 VAD")
                        outcome = "timeout"
                        self._shutdown(self.clients.player, ("stop",))
                        break
                    time.sleep(SPEAK_POLL_S)
        except Exception as exc:
            outcome = "tts_error"
            log.exception("TTS 播放失败: %r", text[:40])
            self._log_event("worker_error", where="tts", error=str(exc))
        finally:
            timing.playback_done = time.monotonic()
            self._poll_first_audio(timing, text)
            self._log_event(
                "turn_latency",
                **self._latency_fields(timing, outcome=outcome))
            self._flush_capture_since(mark)
            vad.paused = False

    def _poll_first_audio(self, timing, text):
        # type: (TurnTiming, str) -> None
        """读取 AudioPlayer 的首帧输出时间；fake 播放器没有该属性。"""
        if timing is None or timing.first_audio is not None:
            return
        first = getattr(self.clients.player, "first_output_at", None)
        if first is None:
            return
        timing.first_audio = first
        self._log_event(
            "first_audio", text=text,
            tts_to_audio_ms=self._ms_since(timing.tts_started, first),
            end_to_first_audio_ms=self._ms_since(timing.started, first))

    def _capture_mark(self):
        # type: () -> Any
        """记录播放开始前的采集位置；fake 采集没有 mark 时返回 None。"""
        mark = getattr(self.clients.capture, "mark", None)
        if not callable(mark):
            return None
        try:
            return mark()
        except Exception:                       # noqa: BLE001
            log.debug("采集 mark 失败", exc_info=True)
            return None

    def _flush_capture_since(self, mark):
        # type: (Any) -> None
        """只丢弃播放窗口内采集的帧；没有 mark 能力时退回全量 flush。"""
        capture = self.clients.capture
        flush_since = getattr(capture, "flush_since", None)
        try:
            if mark is not None and callable(flush_since):
                flush_since(mark)
            else:
                capture.flush()
        except Exception:                       # noqa: BLE001
            log.debug("flush 采集缓冲失败", exc_info=True)

    @staticmethod
    def _close_generator(generator):
        # type: (Any) -> None
        """尽力关闭 TTS 生成器；producer 正在迭代时 close 会报错，忽略。"""
        close = getattr(generator, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:                       # noqa: BLE001
            log.debug("关闭 TTS 生成器失败", exc_info=True)

    # ------------------------------------------------------------ clients

    def _default_clients(self, config):
        # type: (Any) -> DialogClients
        """构造真实客户端；延迟导入，纯离线场景不加载 sounddevice/websockets。"""
        from ai.dialog.asr import ASRClient
        from ai.dialog.audio import AudioCapture, AudioPlayer, VAD
        from ai.dialog.llm import LLMClient
        from ai.dialog.tts import WarmTTSClient

        ptt = None
        if config.ptt_key:
            from ai.dialog.ptt import PushToTalk, keycode_for
            try:
                ptt = PushToTalk(keycode_for(config.ptt_key))
            except ValueError:
                log.exception("PTT 键名无效: %r，退回 VAD", config.ptt_key)

        return DialogClients(
            capture=AudioCapture(),
            vad=VAD(threshold_db=config.vad_threshold_db),
            # 流式 ASR 按住期间长连接；recv 超时要覆盖长录音，不能沿用 5 秒。
            asr=ASRClient(config.asr_key, config.asr_resource_id,
                          timeout=15.0),
            tts=WarmTTSClient(config.tts_key, config.tts_resource_id,
                              config.tts_voice, rate=config.rate),
            player=AudioPlayer(volume=config.volume),
            llm=LLMClient(config.ark_key, config.ark_base_url,
                          config.ark_model, timeout=config.llm_timeout),
            ptt=ptt,
        )


def _json_default(value):
    # type: (Any) -> Any
    """JSONL 日志兜底序列化：dataclass 展开，其余转字符串。"""
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return str(value)


def _merge_asr_texts(texts):
    # type: (list) -> str
    """按顺序合并 ASR 的 definite 片段，去掉重复/重叠部分。

    火山流式 ASR 对同一段录音可能返回多个 final 片段，且片段之间会有
    重叠（例如「你好」→「你好我」）。直接拼接会重复触发对话回合，这里
    用最长后缀/前缀重叠合并。
    """
    merged = ""
    for raw in texts:
        text = (raw or "").strip()
        if not text:
            continue
        if not merged:
            merged = text
            continue
        if text in merged:
            continue
        overlap = 0
        for size in range(min(len(merged), len(text)), 0, -1):
            if merged.endswith(text[:size]):
                overlap = size
                break
        merged += text[overlap:]
    return merged


def _pcm16(frames):
    # type: (list) -> bytes
    """float32 [-1, 1] 帧 → 16 bit 小端单声道 PCM。"""
    samples = [
        np.asarray(frame, dtype=np.float32).reshape(-1) for frame in frames
    ]
    samples = [item for item in samples if item.size]
    if not samples:
        return b""
    data = np.clip(np.concatenate(samples), -1.0, 1.0)
    return (data * 32767.0).astype("<i2").tobytes()


@dataclass
class DutyBatch(object):
    """一轮 LLM 的全部工具调用；主循环逐条落地后回一个 ack dict。

    `calls` 是 `[(工具名, 参数 dict 或 None)]`；`origin` 是事件名。
    ack 形如 `{"batch_id", "results": [...], "duties": [...]}`。
    """
    batch_id: str
    calls: List[Any]
    source: str = ""
    origin: str = "speech"


def _new_dishes_dag(context, order_ids):
    # type: (dict, Any) -> str
    """新单批次里各道菜的 DAG 文本（主循环在上下文里按菜名给出）。"""
    dags = (context or {}).get("dish_dags") or {}
    dishes = []
    for order in (context or {}).get("orders") or ():
        if order_ids and order.get("id") not in order_ids:
            continue
        dish = order.get("dish")
        if dish and dish not in dishes:
            dishes.append(dish)
    return "\n".join(dags[d] for d in dishes if d in dags)


def _fallback_text(results):
    # type: (list) -> str
    """第二段 LLM 失败时的兜底句：只按结果说，不编内容。"""
    if results and all((r or {}).get("ok") for r in results):
        return "好，记下了。"
    return "没弄成，再说一次？"
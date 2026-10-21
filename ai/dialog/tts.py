"""火山双向流式 TTS 客户端（spec §7.2）。

一句回复 = 一次连接、一个 session：StartConnection → StartSession →
TaskRequest（整句文本）→ FinishSession，然后收 AudioOnlyServer 帧并
yield 裸 PCM。协议参考官方双向流式 Demo 与 ``protocols.py``：头部 4 字节
+ event + session id + payload 长度。
"""
import asyncio
import contextlib
import json
import queue
import threading
import uuid

import websockets

from ai.dialog import volc_protocol as P
from ai.dialog._async_bridge import (
    DEFAULT_WS_TIMEOUT_S, async_queue_get, connect as open_ws, iter_async, recv as recv_ws)


TTS_ENDPOINT = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
_MAX_MESSAGE_SIZE = 10 * 1024 * 1024


class TTSClient:
    """火山流式 TTS 的同步封装；``ws_factory`` 默认 ``websockets.connect``。

    ``timeout`` 同时约束建连与每次 ``recv``：服务端不回帧时抛
    ``RuntimeError``，worker 的上层有界 producer 再兜底（spec §7.4）。
    """

    def __init__(self, api_key, resource_id, voice, sample_rate=24000,
                 rate=1.0, ws_factory=None, timeout=DEFAULT_WS_TIMEOUT_S):
        self.api_key = api_key
        self.resource_id = resource_id
        self.voice = voice
        self.sample_rate = int(sample_rate)
        self.rate = float(rate)
        self.ws_factory = ws_factory or websockets.connect
        self.timeout = float(timeout)

    def synthesize(self, text):
        # type: (str) -> object
        """返回 PCM 分片的迭代器；生成器关闭时连接随之关闭。"""
        return iter_async(lambda: self._synthesize_async(text))

    async def _synthesize_async(self, text):
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        session_id = str(uuid.uuid4())
        params = {
            "speaker": self.voice,
            "rate": self.rate,
            "audio_params": {
                "format": "pcm",
                "sample_rate": self.sample_rate,
            },
        }

        async with open_ws(
            self.ws_factory,
            TTS_ENDPOINT,
            timeout=self.timeout,
            label="TTS",
            additional_headers=headers,
            max_size=_MAX_MESSAGE_SIZE,
        ) as ws:
            try:
                await ws.send(
                    P.encode_tts_message(
                        P.FULL_CLIENT_REQUEST, P.EVENT_START_CONNECTION, b"{}"
                    )
                )
                await _wait_for(ws, P.EVENT_CONNECTION_STARTED, self.timeout)

                await ws.send(
                    P.encode_tts_message(
                        P.FULL_CLIENT_REQUEST,
                        P.EVENT_START_SESSION,
                        _json_bytes(
                            {
                                "event": P.EVENT_START_SESSION,
                                "req_params": dict(params, text=text),
                            }
                        ),
                        session_id=session_id,
                    )
                )
                await _wait_for(ws, P.EVENT_SESSION_STARTED, self.timeout)

                task_params = dict(params, text=text)
                await ws.send(
                    P.encode_tts_message(
                        P.FULL_CLIENT_REQUEST,
                        P.EVENT_TASK_REQUEST,
                        _json_bytes(
                            {
                                "event": P.EVENT_TASK_REQUEST,
                                "req_params": task_params,
                            }
                        ),
                        session_id=session_id,
                    )
                )
                await ws.send(
                    P.encode_tts_message(
                        P.FULL_CLIENT_REQUEST,
                        P.EVENT_FINISH_SESSION,
                        _json_bytes({"event": P.EVENT_FINISH_SESSION}),
                        session_id=session_id,
                    )
                )

                while True:
                    raw = await recv_ws(ws, self.timeout, "TTS")
                    if isinstance(raw, str):
                        raise RuntimeError("TTS 返回了文本帧：%r" % (raw[:200],))
                    msg = P.decode_tts_message(raw)
                    if msg.message_type == P.ERROR_RESPONSE:
                        raise RuntimeError(
                            "TTS 服务返回错误 %s：%s"
                            % (msg.error_code, _describe(msg.payload))
                        )
                    if msg.message_type == P.AUDIO_ONLY_SERVER:
                        if msg.payload:
                            yield msg.payload
                        continue
                    if msg.event == P.EVENT_SESSION_FAILED:
                        raise RuntimeError(
                            "TTS session 失败：%s" % (_describe(msg.payload),)
                        )
                    if msg.event in (
                        P.EVENT_SESSION_FINISHED,
                        P.EVENT_CONNECTION_FINISHED,
                    ):
                        break
            finally:
                # 尽力按官方流程结束连接；连接已断时忽略发送失败。
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await ws.send(
                        P.encode_tts_message(
                            P.FULL_CLIENT_REQUEST,
                            P.EVENT_FINISH_CONNECTION,
                            b"{}",
                        )
                    )


def _warm_pcm_iter(result_q):
    # type: (queue.Queue) -> object
    """把 WarmTTS 后台线程产出的 PCM 分片 / 结束哨兵转成同步迭代器。"""
    while True:
        item = result_q.get()
        if item is None:
            return
        if isinstance(item, tuple) and item and item[0] == "error":
            raise item[1]
        yield item


class WarmTTSClient(TTSClient):
    """带预连接的 TTS：后台线程持有长连接，`synthesize` 复用同一条连接。

    每次 `synthesize` 仍然是一个独立 session（StartSession → TaskRequest →
    FinishSession），但**不再重新建 WebSocket / StartConnection**，省掉
    TCP+TLS 握手与建连往返；空闲时连接保持打开，供下一句直接复用。

    `warmup()` 后台开始建连（幂等、不阻塞）；`close()` 结束连接并停线程。
    """

    def __init__(self, api_key, resource_id, voice, sample_rate=24000,
                 rate=1.0, ws_factory=None, timeout=DEFAULT_WS_TIMEOUT_S):
        super(WarmTTSClient, self).__init__(
            api_key, resource_id, voice, sample_rate=sample_rate,
            rate=rate, ws_factory=ws_factory, timeout=timeout)
        self._jobs = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self._closed = False

    def warmup(self):
        # type: () -> None
        """后台启动事件循环并建连；重复调用无副作用。"""
        with self._lock:
            if self._closed:
                raise RuntimeError('TTS 客户端已关闭')
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run_loop, name="dialog-tts-warm", daemon=True)
            self._thread.start()
            self._jobs.put(("warm", None))

    def synthesize(self, text):
        # type: (str) -> object
        self.warmup()
        result_q = queue.Queue()
        with self._lock:
            if self._closed:
                raise RuntimeError('TTS 客户端已关闭')
            self._jobs.put(("syn", text, result_q))
        return _warm_pcm_iter(result_q)

    def close(self):
        # type: () -> None
        """结束后台循环与连接（幂等）。"""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._jobs.put(None)
            thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            with self._lock:
                if not thread.is_alive() and self._thread is thread:
                    self._thread = None

    def _run_loop(self):
        # type: () -> None
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        finally:
            loop.close()

    async def _connect(self):
        # type: () -> object
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        cm = self.ws_factory(
            TTS_ENDPOINT,
            additional_headers=headers,
            max_size=_MAX_MESSAGE_SIZE,
        )
        ws = await asyncio.wait_for(cm.__aenter__(), self.timeout)
        await ws.send(P.encode_tts_message(
            P.FULL_CLIENT_REQUEST, P.EVENT_START_CONNECTION, b"{}"))
        await _wait_for(ws, P.EVENT_CONNECTION_STARTED, self.timeout)
        return ws

    async def _one_session(self, ws, text):
        # type: (object, str) -> object
        session_id = str(uuid.uuid4())
        params = {
            "speaker": self.voice,
            "rate": self.rate,
            "audio_params": {
                "format": "pcm",
                "sample_rate": self.sample_rate,
            },
        }
        await ws.send(P.encode_tts_message(
            P.FULL_CLIENT_REQUEST, P.EVENT_START_SESSION,
            _json_bytes({"event": P.EVENT_START_SESSION,
                         "req_params": dict(params, text=text)}),
            session_id=session_id))
        await _wait_for(ws, P.EVENT_SESSION_STARTED, self.timeout)
        await ws.send(P.encode_tts_message(
            P.FULL_CLIENT_REQUEST, P.EVENT_TASK_REQUEST,
            _json_bytes({"event": P.EVENT_TASK_REQUEST,
                         "req_params": dict(params, text=text)}),
            session_id=session_id))
        await ws.send(P.encode_tts_message(
            P.FULL_CLIENT_REQUEST, P.EVENT_FINISH_SESSION,
            _json_bytes({"event": P.EVENT_FINISH_SESSION}),
            session_id=session_id))
        while True:
            raw = await recv_ws(ws, self.timeout, "TTS")
            if isinstance(raw, str):
                raise RuntimeError("TTS 返回了文本帧：%r" % (raw[:200],))
            msg = P.decode_tts_message(raw)
            if msg.message_type == P.ERROR_RESPONSE:
                raise RuntimeError(
                    "TTS 服务返回错误 %s：%s"
                    % (msg.error_code, _describe(msg.payload)))
            if msg.message_type == P.AUDIO_ONLY_SERVER and msg.payload:
                yield msg.payload
                continue
            if msg.event == P.EVENT_SESSION_FAILED:
                raise RuntimeError(
                    "TTS session 失败：%s" % (_describe(msg.payload),))
            if msg.event == P.EVENT_SESSION_FINISHED:
                break

    async def _serve(self):
        # type: () -> None
        ws = None
        try:
            while True:
                job = await async_queue_get(self._jobs)
                if job is None:
                    break
                kind = job[0]
                if kind == "warm":
                    try:
                        if ws is None:
                            ws = await self._connect()
                    except Exception:           # noqa: BLE001
                        # 预连失败不影响后续；下一句 syn 会再次尝试建连。
                        ws = None
                    continue
                text, result_q = job[1], job[2]
                try:
                    if ws is None:
                        ws = await self._connect()
                    async for chunk in self._one_session(ws, text):
                        result_q.put(chunk)
                    result_q.put(None)
                except Exception as exc:        # noqa: BLE001
                    result_q.put(("error", exc))
                    if ws is not None:
                        with contextlib.suppress(Exception):
                            await ws.close()
                        ws = None
        finally:
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.send(P.encode_tts_message(
                        P.FULL_CLIENT_REQUEST,
                        P.EVENT_FINISH_CONNECTION, b"{}"))
                    await ws.close()


async def _wait_for(ws, event, timeout=DEFAULT_WS_TIMEOUT_S):
    # type: (object, int, float) -> object
    """等待指定事件，忽略与本步骤无关的下行帧（单帧读取有界）。"""
    while True:
        raw = await recv_ws(ws, timeout, "TTS")
        if isinstance(raw, str):
            raise RuntimeError("TTS 返回了文本帧：%r" % (raw[:200],))
        msg = P.decode_tts_message(raw)
        if msg.message_type == P.ERROR_RESPONSE:
            raise RuntimeError(
                "TTS 服务返回错误 %s：%s" % (msg.error_code, _describe(msg.payload))
            )
        if msg.event == event:
            return msg
        if msg.event in (P.EVENT_CONNECTION_FAILED, P.EVENT_SESSION_FAILED):
            raise RuntimeError(
                "TTS 建连/会话失败：%s" % (_describe(msg.payload),)
            )


def _json_bytes(payload):
    # type: (object) -> bytes
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _describe(payload):
    # type: (object) -> str
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, (dict, list)):
        return json.dumps(payload, ensure_ascii=False)
    return str(payload)

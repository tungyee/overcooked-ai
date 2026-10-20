"""火山「大模型流式语音识别」客户端（spec §7.1）。

一段人声 = 一次连接：VAD 切出语音后建连，发 full client request、按
200 ms 分包发送 PCM，最后发负包；服务端边收边返回识别结果。同步生成器
只 yield ``definite=true`` 的最终分句，中间结果丢弃。

协议参考官方 Demo（``bigmodel_async`` 的 ``RequestBuilder`` /
``ResponseParser``）：4 字节头 + 4 字节 sequence + 4 字节 payload 长度 +
gzip payload，整数大端。
"""
import asyncio
import contextlib
import json
import queue
import uuid

import websockets

from ai.dialog import volc_protocol as P
from ai.dialog._async_bridge import (
    DEFAULT_WS_TIMEOUT_S, connect as open_ws, iter_async, recv as recv_ws)


ASR_ENDPOINT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
_MAX_MESSAGE_SIZE = 10 * 1024 * 1024


class ASRClient:
    """火山流式 ASR 的同步封装。

    ``ws_factory`` 默认 ``websockets.connect``；测试传入 fake，避免网络。
    ``timeout`` 同时约束建连与每次 ``recv``，避免服务不回帧时 worker 永久
    阻塞（spec §7.4）。``pcm_chunks`` 应是已收集好的 16 bit 小端单声道 PCM
    分片；发送任务在事件循环线程里同步迭代它，因此不要在迭代中阻塞等待
    实时音频。
    """

    def __init__(self, api_key, resource_id, sample_rate=16000, ws_factory=None,
                 timeout=DEFAULT_WS_TIMEOUT_S):
        self.api_key = api_key
        self.resource_id = resource_id
        self.sample_rate = int(sample_rate)
        self.ws_factory = ws_factory or websockets.connect
        self.timeout = float(timeout)

    def stream(self, pcm_chunks):
        # type: (object) -> object
        """返回迭代器；每次迭代复用同一连接，迭代结束即关闭。"""
        return iter_async(lambda: self._stream_async(pcm_chunks))

    async def _stream_async(self, pcm_chunks):
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Connect-Id": str(uuid.uuid4()),
            # 官方 demo 在 WS 握手头里也带这个固定值。
            "X-Api-Sequence": "-1",
        }
        # 16 bit 单声道：sample_rate * 2 字节/秒；200 ms 一包。
        packet_size = max(2, self.sample_rate * 2 * 200 // 1000)

        async with open_ws(
            self.ws_factory,
            ASR_ENDPOINT,
            timeout=self.timeout,
            label="ASR",
            additional_headers=headers,
            max_size=_MAX_MESSAGE_SIZE,
        ) as ws:
            await ws.send(P.encode_asr_request(_request_payload(self.sample_rate)))
            sender = asyncio.ensure_future(
                _send_audio(ws, pcm_chunks, packet_size)
            )
            try:
                while True:
                    raw = await recv_ws(ws, self.timeout, "ASR")
                    if isinstance(raw, str):
                        raise RuntimeError("ASR 返回了文本帧：%r" % (raw[:200],))
                    msg = P.decode_asr_message(raw)
                    if msg.message_type == P.ERROR_RESPONSE:
                        raise RuntimeError(
                            "ASR 服务返回错误 %s：%s"
                            % (msg.error_code, _describe(msg.payload))
                        )
                    for text in _definite_texts(msg.payload):
                        yield {"text": text, "definite": True}
                    if msg.is_last_package:
                        # 服务端收尾帧可能先于发送任务结束（测试/极快响应）；
                        # 先把剩余音频送完，避免丢包。
                        if not sender.done():
                            await asyncio.wait_for(
                                asyncio.shield(sender), self.timeout)
                        break
            finally:
                if not sender.done():
                    sender.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender


def _request_payload(sample_rate):
    # type: (int) -> dict
    return {
        "user": {"uid": "oc2"},
        "audio": {
            "format": "pcm",
            "codec": "raw",
            "rate": int(sample_rate),
            "bits": 16,
            "channel": 1,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": True,
            "show_utterances": True,
            "enable_nonstream": False,
        },
    }


async def _send_audio(ws, pcm_chunks, packet_size):
    # type: (object, object, int) -> None
    seq = 2  # full client request 占用 1
    if isinstance(pcm_chunks, queue.Queue):
        # 实时麦克风：队列由 worker 线程持续投入；用 executor 等待，
        # 避免阻塞事件循环导致 ASR 下行帧读不到。
        packets = _pack_queue(pcm_chunks, packet_size)
    else:
        packets = _pack_pcm(pcm_chunks, packet_size)
    if hasattr(packets, "__aiter__"):
        async for packet in packets:
            await ws.send(P.encode_asr_audio(seq, packet))
            seq += 1
    else:
        for packet in packets:
            await ws.send(P.encode_asr_audio(seq, packet))
            seq += 1
    await ws.send(P.encode_asr_audio(seq, b"", is_last=True))


async def _pack_queue(chunks, packet_size):
    # type: (queue.Queue, int) -> object
    """从阻塞队列持续打包；None 表示录音结束。

    队列来自 worker 线程，不能在事件循环里直接 `get()`；交给默认
    executor 等待，音频包之间仍能正常收 ASR 的下行帧。
    """
    loop = asyncio.get_event_loop()
    buf = bytearray()
    while True:
        chunk = await loop.run_in_executor(None, chunks.get)
        if chunk is None:
            break
        if not chunk:
            continue
        buf.extend(bytes(chunk))
        while len(buf) >= packet_size:
            yield bytes(buf[:packet_size])
            del buf[:packet_size]
    if buf:
        yield bytes(buf)


def _pack_pcm(chunks, packet_size):
    # type: (object, int) -> object
    """把任意大小的 PCM 分片重新切成定长包；最后不足一包的照常发出。"""
    buf = bytearray()
    for chunk in chunks:
        if not chunk:
            continue
        buf.extend(bytes(chunk))
        while len(buf) >= packet_size:
            yield bytes(buf[:packet_size])
            del buf[:packet_size]
    if buf:
        yield bytes(buf)


def _definite_texts(payload):
    # type: (object) -> list
    """从一帧响应里按顺序取出 ``definite=true`` 的分句文本。

    兼容两种官方返回形态：bigmodel 的 ``result.utterances`` 对象，以及
    较早的 ``result`` 列表（列表项直接带 ``text``/``definite``）。
    """
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if isinstance(result, dict):
        entries = result.get("utterances")
        if not isinstance(entries, list):
            entries = [result]
    elif isinstance(result, list):
        entries = result
    else:
        return []

    texts = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("definite") is not True:
            continue
        text = entry.get("text")
        if isinstance(text, str) and text:
            texts.append(text)
    return texts


def _describe(payload):
    # type: (object) -> str
    if isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False)
    if isinstance(payload, bytes):
        return payload.decode("utf-8", "replace")
    return str(payload)

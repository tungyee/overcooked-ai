"""火山流式 ASR/TTS 客户端单测（spec §7.1–7.2）。

全部用 fake WebSocket：不连网络、不需要 API Key。fake 服务端帧用
``ai.dialog.volc_protocol`` 的编码函数构造，顺带验证二进制协议本身。
"""
import asyncio
import inspect
import json
import queue

import pytest
import websockets

from ai.dialog import asr, tts
from ai.dialog import volc_protocol as P


class FakeWS:
    """替身连接：预先排好服务端要发的二进制帧，记录客户端发出的帧。"""

    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False
        self.response = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        # 让出一次执行权，给发送任务机会先把音频帧发完（接近真实网络）。
        await asyncio.sleep(0)
        if not self.incoming:
            raise RuntimeError("fake websocket 没有更多帧")
        item = self.incoming.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeConnect:
    """记录 connect 调用参数；返回预置的 FakeWS。"""

    def __init__(self, ws):
        self.ws = ws
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.ws


def _asr_response(payload, sequence=1, is_last=False):
    return P.encode_asr_response(payload, sequence=sequence, is_last=is_last)


def _tts_frames(session_id="session-1"):
    return [
        P.encode_tts_message(
            P.FULL_SERVER_RESPONSE,
            P.EVENT_CONNECTION_STARTED,
            b"{}",
            connect_id="connect-1",
        ),
        P.encode_tts_message(
            P.FULL_SERVER_RESPONSE,
            P.EVENT_SESSION_STARTED,
            b"{}",
            session_id=session_id,
        ),
        P.encode_tts_message(
            P.AUDIO_ONLY_SERVER, P.EVENT_TTS_RESPONSE, b"\x01\x02", session_id=session_id
        ),
        P.encode_tts_message(
            P.AUDIO_ONLY_SERVER, P.EVENT_TTS_RESPONSE, b"\x03\x04", session_id=session_id
        ),
        P.encode_tts_message(
            P.FULL_SERVER_RESPONSE,
            P.EVENT_SESSION_FINISHED,
            b"{}",
            session_id=session_id,
        ),
    ]


# ---------------------------------------------------------------------------
# 二进制协议
# ---------------------------------------------------------------------------


def test_asr_protocol_roundtrip():
    request = {"audio": {"format": "pcm", "rate": 16000, "bits": 16, "channel": 1}}
    raw = P.encode_asr_request(request, sequence=7)
    msg = P.decode_asr_message(raw)
    assert msg.message_type == P.FULL_CLIENT_REQUEST
    assert msg.sequence == 7
    assert msg.is_last_package is False
    assert msg.payload == request

    audio = P.encode_asr_audio(8, b"\x01\x02\x03", is_last=True)
    amsg = P.decode_asr_message(audio)
    assert amsg.message_type == P.AUDIO_ONLY_CLIENT
    assert amsg.sequence == -8
    assert amsg.is_last_package is True
    assert amsg.payload == b"\x01\x02\x03"


def test_tts_protocol_roundtrip():
    raw = P.encode_tts_message(
        P.FULL_CLIENT_REQUEST,
        P.EVENT_START_SESSION,
        b'{"req_params":{}}',
        session_id="sid-42",
    )
    msg = P.decode_tts_message(raw)
    assert msg.message_type == P.FULL_CLIENT_REQUEST
    assert msg.event == P.EVENT_START_SESSION
    assert msg.session_id == "sid-42"
    assert msg.payload == b'{"req_params":{}}'

    opened = P.decode_tts_message(
        P.encode_tts_message(
            P.FULL_SERVER_RESPONSE,
            P.EVENT_CONNECTION_STARTED,
            b"{}",
            connect_id="cid",
        )
    )
    assert opened.event == P.EVENT_CONNECTION_STARTED
    assert opened.connect_id == "cid"
    assert opened.session_id == ""


# ---------------------------------------------------------------------------
# ASR
# ---------------------------------------------------------------------------


def test_asr_connect_headers_and_endpoint():
    ws = FakeWS([_asr_response({"result": {}}, is_last=True)])
    connect = FakeConnect(ws)
    client = asr.ASRClient("key", "volc.bigasr.sauc.duration", ws_factory=connect)

    list(client.stream([b"\x00\x00" * 1600]))

    url, kwargs = connect.calls[0]
    assert url == asr.ASR_ENDPOINT
    headers = kwargs["additional_headers"]
    assert headers["X-Api-Key"] == "key"
    assert headers["X-Api-Resource-Id"] == "volc.bigasr.sauc.duration"
    assert headers["X-Api-Connect-Id"]
    assert ws.closed


def test_asr_sends_full_request_and_200ms_packets():
    ws = FakeWS([_asr_response({"result": {}}, is_last=True)])
    client = asr.ASRClient("key", "volc.bigasr.sauc.duration", ws_factory=FakeConnect(ws))

    pcm = bytes(range(256)) * 50  # 12800 字节 = 16 kHz 16 bit 下的 400 ms
    list(client.stream([pcm]))

    msgs = [P.decode_asr_message(raw) for raw in ws.sent]
    assert msgs[0].message_type == P.FULL_CLIENT_REQUEST
    assert msgs[0].payload["audio"] == {
        "format": "pcm",
        "codec": "raw",
        "rate": 16000,
        "bits": 16,
        "channel": 1,
    }
    assert msgs[0].payload["request"]["model_name"] == "bigmodel"
    assert msgs[0].payload["request"]["show_utterances"] is True

    audio = [m for m in msgs if m.message_type == P.AUDIO_ONLY_CLIENT]
    # 两个 200 ms 包 + 一个空的负包。
    assert len(audio) == 3
    assert [len(m.payload) for m in audio[:2]] == [6400, 6400]
    assert b"".join(m.payload for m in audio[:2]) == pcm
    assert audio[-1].payload == b""
    assert audio[-1].is_last_package is True
    assert audio[-1].sequence < 0


def test_asr_accepts_live_queue_and_sends_same_packets():
    ws = FakeWS([_asr_response({"result": {}}, is_last=True)])
    client = asr.ASRClient("key", "volc.bigasr.sauc.duration",
                           ws_factory=FakeConnect(ws))

    pcm = bytes(range(256)) * 50  # 400 ms
    chunks = queue.Queue()
    chunks.put(pcm[:6400])
    chunks.put(pcm[6400:])
    chunks.put(None)

    list(client.stream(chunks))

    audio = [P.decode_asr_message(raw) for raw in ws.sent
             if P.decode_asr_message(raw).message_type == P.AUDIO_ONLY_CLIENT]
    assert [len(m.payload) for m in audio[:2]] == [6400, 6400]
    assert b"".join(m.payload for m in audio[:2]) == pcm
    assert audio[-1].is_last_package is True


def test_asr_yields_only_definite():
    ws = FakeWS(
        [
            _asr_response({"result": [{"text": "你", "definite": False}]}, sequence=1),
            _asr_response({"result": [{"text": "你好", "definite": True}]}, sequence=2, is_last=True),
        ]
    )
    client = asr.ASRClient("key", "volc.bigasr.sauc.duration", ws_factory=FakeConnect(ws))

    out = list(client.stream([b"\x00\x00" * 3200]))

    assert out == [{"text": "你好", "definite": True}]


def test_asr_reads_real_utterances_shape():
    ws = FakeWS(
        [
            _asr_response(
                {
                    "audio_info": {"duration": 2000},
                    "result": {
                        "text": "你好我洗",
                        "utterances": [
                            {"text": "你好", "definite": True},
                            {"text": "我洗", "definite": False},
                        ],
                    },
                },
                is_last=True,
            )
        ]
    )
    client = asr.ASRClient("key", "volc.bigasr.sauc.duration", ws_factory=FakeConnect(ws))

    out = list(client.stream([b"\x00\x00" * 3200]))

    assert out == [{"text": "你好", "definite": True}]


def test_asr_yields_repeated_definite_deltas():
    ws = FakeWS(
        [
            _asr_response({"result": [{"text": "好", "definite": True}]}, sequence=1),
            _asr_response(
                {"result": [{"text": "好", "definite": True}]},
                sequence=2,
                is_last=True,
            ),
        ]
    )
    client = asr.ASRClient("key", "volc.bigasr.sauc.duration", ws_factory=FakeConnect(ws))

    out = list(client.stream([b"\x00\x00" * 3200]))

    assert out == [
        {"text": "好", "definite": True},
        {"text": "好", "definite": True},
    ]


def test_asr_raises_on_error_response():
    ws = FakeWS([P.encode_asr_error(45000001, b'{"message":"bad"}')])
    client = asr.ASRClient("key", "volc.bigasr.sauc.duration", ws_factory=FakeConnect(ws))

    with pytest.raises(RuntimeError, match="ASR"):
        list(client.stream([b"\x00\x00" * 100]))
    assert ws.closed


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------


def test_tts_sends_text_and_yields_audio_chunks():
    ws = FakeWS(_tts_frames())
    connect = FakeConnect(ws)
    client = tts.TTSClient(
        "key", "seed-tts-2.0", "zh_female_qingxin", ws_factory=connect
    )

    chunks = list(client.synthesize("行，我洗。"))

    assert b"".join(chunks) == b"\x01\x02\x03\x04"

    url, kwargs = connect.calls[0]
    assert url == tts.TTS_ENDPOINT
    headers = kwargs["additional_headers"]
    assert headers["X-Api-Key"] == "key"
    assert headers["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert headers["X-Api-Connect-Id"]

    sent = [P.decode_tts_message(raw) for raw in ws.sent]
    events = [m.event for m in sent]
    assert events[:4] == [
        P.EVENT_START_CONNECTION,
        P.EVENT_START_SESSION,
        P.EVENT_TASK_REQUEST,
        P.EVENT_FINISH_SESSION,
    ]
    start = json.loads(sent[1].payload)
    assert start["req_params"]["speaker"] == "zh_female_qingxin"
    assert start["req_params"]["audio_params"] == {
        "format": "pcm",
        "sample_rate": 24000,
    }
    task = json.loads(sent[2].payload)
    assert task["req_params"]["text"] == "行，我洗。"
    assert task["req_params"]["speaker"] == "zh_female_qingxin"
    assert sent[2].session_id == sent[1].session_id
    assert ws.closed


def test_tts_control_bodies_carry_event_field():
    ws = FakeWS(_tts_frames())
    client = tts.TTSClient("key", "seed-tts-2.0", "voice", ws_factory=FakeConnect(ws))

    list(client.synthesize("行，我洗。"))

    sent = [P.decode_tts_message(raw) for raw in ws.sent]
    assert json.loads(sent[1].payload)["event"] == P.EVENT_START_SESSION
    assert json.loads(sent[2].payload)["event"] == P.EVENT_TASK_REQUEST
    assert json.loads(sent[3].payload)["event"] == P.EVENT_FINISH_SESSION
    assert [sent[1].event, sent[2].event, sent[3].event] == [
        P.EVENT_START_SESSION,
        P.EVENT_TASK_REQUEST,
        P.EVENT_FINISH_SESSION,
    ]


def test_default_websockets_connect_accepts_production_kwargs():
    parameters = inspect.signature(websockets.connect).parameters
    assert "additional_headers" in parameters
    assert "max_size" in parameters
    assert asr.ASRClient("k", "r").ws_factory is websockets.connect
    assert tts.TTSClient("k", "r", "v").ws_factory is websockets.connect


def test_tts_raises_on_session_failed_and_closes():
    ws = FakeWS(
        [
            P.encode_tts_message(
                P.FULL_SERVER_RESPONSE,
                P.EVENT_CONNECTION_STARTED,
                b"{}",
                connect_id="cid",
            ),
            P.encode_tts_message(
                P.FULL_SERVER_RESPONSE,
                P.EVENT_SESSION_FAILED,
                b'{"message":"nope"}',
                session_id="sid",
            ),
        ]
    )
    client = tts.TTSClient("key", "seed-tts-2.0", "voice", ws_factory=FakeConnect(ws))

    with pytest.raises(RuntimeError, match="TTS"):
        list(client.synthesize("测试"))
    assert ws.closed


def test_tts_close_on_early_stop():
    ws = FakeWS(_tts_frames())
    client = tts.TTSClient("key", "seed-tts-2.0", "voice", ws_factory=FakeConnect(ws))

    gen = client.synthesize("行，我洗。")
    assert next(gen) == b"\x01\x02"
    gen.close()

    assert ws.closed


# ---------------------------------------------------------------------------
# 建连/读取超时（B1，spec §7.4）
# ---------------------------------------------------------------------------


class BlockingWS(FakeWS):
    """``recv`` 永不返回（模拟服务端挂住）；用于验证读取超时。"""

    def __init__(self, delay=10.0):
        FakeWS.__init__(self, [])

    async def recv(self):
        await asyncio.sleep(10.0)
        return b""


class SlowConnectWS:
    """``__aenter__`` 永不完成；用于验证建连超时。"""

    async def __aenter__(self):
        await asyncio.sleep(10.0)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_asr_recv_timeout_raises():
    ws = BlockingWS()
    client = asr.ASRClient(
        "key", "rid", timeout=0.01, ws_factory=FakeConnect(ws))

    with pytest.raises(RuntimeError, match="ASR 读取超时"):
        list(client.stream([b"\x00\x00" * 100]))
    assert ws.closed


def test_asr_connect_timeout_raises():
    client = asr.ASRClient(
        "key", "rid", timeout=0.01,
        ws_factory=FakeConnect(SlowConnectWS()))

    with pytest.raises(RuntimeError, match="ASR 建连超时"):
        list(client.stream([b""]))


def test_tts_recv_timeout_raises():
    ws = BlockingWS()
    client = tts.TTSClient(
        "key", "seed-tts-2.0", "voice", timeout=0.01,
        ws_factory=FakeConnect(ws))

    with pytest.raises(RuntimeError, match="TTS 读取超时"):
        list(client.synthesize("测试"))
    assert ws.closed


def test_tts_connect_timeout_raises():
    client = tts.TTSClient(
        "key", "seed-tts-2.0", "voice", timeout=0.01,
        ws_factory=FakeConnect(SlowConnectWS()))

    with pytest.raises(RuntimeError, match="TTS 建连超时"):
        list(client.synthesize("测试"))


def test_clients_default_timeout_is_five_seconds():
    assert asr.ASRClient("k", "r").timeout == 5.0
    assert tts.TTSClient("k", "r", "v").timeout == 5.0


def test_invalid_status_message_includes_server_body():
    from ai.dialog import _async_bridge as B

    class Response(object):
        status_code = 403
        # websockets 的 response.body 实际是 bytearray。
        body = bytearray(b'{"error":"requested resource not granted"}')

    message = B._invalid_status_message("ASR", Response())

    assert "ASR 建连被拒绝" in message
    assert "HTTP 403" in message
    assert "requested resource not granted" in message


class WarmWS(object):
    """WarmTTSClient 的常驻连接替身：``send``/``recv``/``close`` 直接可用。"""

    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        await asyncio.sleep(0)
        if not self.incoming:
            raise RuntimeError("warm fake 没有更多下行帧")
        return self.incoming.pop(0)

    async def close(self):
        self.closed = True


def _warm_session(session_id):
    return [
        P.encode_tts_message(
            P.FULL_SERVER_RESPONSE, P.EVENT_SESSION_STARTED, b"{}",
            session_id=session_id),
        P.encode_tts_message(
            P.AUDIO_ONLY_SERVER, P.EVENT_TTS_RESPONSE, b"\x01\x02",
            session_id=session_id),
        P.encode_tts_message(
            P.FULL_SERVER_RESPONSE, P.EVENT_SESSION_FINISHED, b"{}",
            session_id=session_id),
    ]


def test_warm_tts_reuses_one_connection_for_multiple_sentences():
    incoming = [
        P.encode_tts_message(
            P.FULL_SERVER_RESPONSE, P.EVENT_CONNECTION_STARTED, b"{}",
            connect_id="c1"),
    ] + _warm_session("s1") + _warm_session("s2")
    ws = WarmWS(incoming)
    client = tts.WarmTTSClient(
        "key", "seed-tts-2.0", "voice", ws_factory=lambda url, **kw: ws)

    try:
        client.warmup()
        assert list(client.synthesize("你好")) == [b"\x01\x02"]
        assert list(client.synthesize("第二句")) == [b"\x01\x02"]

        events = [P.decode_tts_message(raw).event for raw in ws.sent]
        assert events.count(P.EVENT_START_CONNECTION) == 1
        assert events.count(P.EVENT_START_SESSION) == 2
    finally:
        client.close()

    assert ws.closed

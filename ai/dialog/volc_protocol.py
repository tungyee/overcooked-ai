"""火山引擎语音服务 v3 二进制协议（最小实现）。

协议来源（官方 Demo）：

- ASR ``bigmodel_async``：头部（4 字节）+ 4 字节 sequence + 4 字节 payload
  长度 + payload；payload 用 gzip 压缩，full client request 是 JSON，音频帧
  是裸 PCM。最后一包用负 sequence（``NEGATIVE_SEQUENCE``）。
- TTS 双向流式：头部（4 字节）+ 4 字节 event + session id + 4 字节 payload
  长度 + payload；控制帧是未压缩 JSON，音频帧是裸 PCM。

所有整数都是大端。这里只保留客户端实际用到的编码/解码，字段语义参考
官方 ``protocols.py`` 与 ASR demo 的 ``RequestBuilder``/``ResponseParser``。
"""
import gzip
import json
import struct
from dataclasses import dataclass


PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 1  # 以 4 字节为单位，即 4 字节

# message type
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_CLIENT = 0b0010
FULL_SERVER_RESPONSE = 0b1001
AUDIO_ONLY_SERVER = 0b1011
ERROR_RESPONSE = 0b1111

# message type specific flags
NO_SEQUENCE = 0b0000
POSITIVE_SEQUENCE = 0b0001
LAST_NO_SEQUENCE = 0b0010
NEGATIVE_SEQUENCE = 0b0011
WITH_EVENT = 0b0100

# serialization / compression
SERIALIZATION_RAW = 0b0000
SERIALIZATION_JSON = 0b0001
COMPRESSION_NONE = 0b0000
COMPRESSION_GZIP = 0b0001

# TTS 事件（上游 1–249，下游 50–399）
EVENT_START_CONNECTION = 1
EVENT_FINISH_CONNECTION = 2
EVENT_CONNECTION_STARTED = 50
EVENT_CONNECTION_FAILED = 51
EVENT_CONNECTION_FINISHED = 52
EVENT_START_SESSION = 100
EVENT_CANCEL_SESSION = 101
EVENT_FINISH_SESSION = 102
EVENT_SESSION_STARTED = 150
EVENT_SESSION_CANCELED = 151
EVENT_SESSION_FINISHED = 152
EVENT_SESSION_FAILED = 153
EVENT_USAGE_RESPONSE = 154
EVENT_TASK_REQUEST = 200
EVENT_UPDATE_CONFIG = 201
EVENT_TTS_SENTENCE_START = 350
EVENT_TTS_SENTENCE_END = 351
EVENT_TTS_RESPONSE = 352
EVENT_TTS_ENDED = 359

# 这些事件的帧里没有 session id 字段（官方 protocols.py 的约定）。
_EVENTS_WITHOUT_SESSION = frozenset(
    (
        EVENT_START_CONNECTION,
        EVENT_FINISH_CONNECTION,
        EVENT_CONNECTION_STARTED,
        EVENT_CONNECTION_FAILED,
        EVENT_CONNECTION_FINISHED,
    )
)
# 连接类下行事件额外带 connect id。
_EVENTS_WITH_CONNECT_ID = frozenset(
    (
        EVENT_CONNECTION_STARTED,
        EVENT_CONNECTION_FAILED,
        EVENT_CONNECTION_FINISHED,
    )
)


def _header(message_type, flags, serialization=SERIALIZATION_JSON,
            compression=COMPRESSION_NONE):
    # type: (int, int, int, int) -> bytes
    return bytes(
        (
            (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE,
            (message_type << 4) | flags,
            (serialization << 4) | compression,
            0,
        )
    )


def _json_bytes(payload):
    # type: (object) -> bytes
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _take(data, offset, size):
    # type: (bytes, int, int) -> tuple
    if offset + size > len(data):
        raise ValueError("二进制协议帧被截断：需要 %d 字节，实际 %d" % (size, len(data) - offset))
    return data[offset:offset + size], offset + size


def _uint32(data, offset):
    # type: (bytes, int) -> tuple
    raw, offset = _take(data, offset, 4)
    return struct.unpack(">I", raw)[0], offset


def _int32(data, offset):
    # type: (bytes, int) -> tuple
    raw, offset = _take(data, offset, 4)
    return struct.unpack(">i", raw)[0], offset


# ---------------------------------------------------------------------------
# ASR（bigmodel_async）
# ---------------------------------------------------------------------------


def encode_asr_request(payload, sequence=1):
    # type: (object, int) -> bytes
    """full client request：JSON + gzip，带正 sequence。"""
    body = gzip.compress(_json_bytes(payload))
    return (
        _header(FULL_CLIENT_REQUEST, POSITIVE_SEQUENCE, compression=COMPRESSION_GZIP)
        + struct.pack(">i", sequence)
        + struct.pack(">I", len(body))
        + body
    )


def encode_asr_audio(sequence, pcm, is_last=False):
    # type: (int, bytes, bool) -> bytes
    """audio only request：裸 PCM + gzip；最后一包用负 sequence。"""
    body = gzip.compress(bytes(pcm))
    if is_last:
        flags = NEGATIVE_SEQUENCE
        sequence = -abs(sequence)
    else:
        flags = POSITIVE_SEQUENCE
        sequence = abs(sequence)
    return (
        _header(AUDIO_ONLY_CLIENT, flags, compression=COMPRESSION_GZIP)
        + struct.pack(">i", sequence)
        + struct.pack(">I", len(body))
        + body
    )


def encode_asr_response(payload, sequence=1, is_last=False):
    # type: (object, int, bool) -> bytes
    """构造服务端 full server response（测试 fake 用）。"""
    body = gzip.compress(_json_bytes(payload))
    if is_last:
        flags = NEGATIVE_SEQUENCE
        sequence = -abs(sequence)
    else:
        flags = POSITIVE_SEQUENCE
        sequence = abs(sequence)
    return (
        _header(FULL_SERVER_RESPONSE, flags, compression=COMPRESSION_GZIP)
        + struct.pack(">i", sequence)
        + struct.pack(">I", len(body))
        + body
    )


def encode_asr_error(code, payload=b""):
    # type: (int, bytes) -> bytes
    """构造服务端错误帧（测试 fake 用）。"""
    return (
        _header(ERROR_RESPONSE, NO_SEQUENCE)
        + struct.pack(">i", code)
        + struct.pack(">I", len(payload))
        + payload
    )


@dataclass
class AsrMessage:
    message_type: int
    flags: int
    sequence: object  # Optional[int]
    is_last_package: bool
    event: int
    error_code: int
    payload: object  # dict/list（JSON）或 bytes（音频）


def decode_asr_message(data):
    # type: (bytes) -> AsrMessage
    """解析 ASR 上行/下行帧；payload 按消息类型返回 dict 或 bytes。"""
    if not isinstance(data, bytes):
        raise ValueError("ASR 只接受二进制帧")
    if len(data) < 4:
        raise ValueError("ASR 帧太短：%d 字节" % len(data))
    header_size = data[0] & 0x0F
    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    serialization = data[2] >> 4
    compression = data[2] & 0x0F
    offset = header_size * 4

    sequence = None
    is_last = False
    event = 0
    error_code = 0

    if flags & 0x01:
        sequence, offset = _int32(data, offset)
    if flags & 0x02:
        is_last = True
    if flags & 0x04:
        event, offset = _int32(data, offset)

    if message_type == ERROR_RESPONSE:
        error_code, offset = _int32(data, offset)
        size, offset = _uint32(data, offset)
        body, offset = _take(data, offset, size)
    elif message_type in (
        FULL_CLIENT_REQUEST,
        AUDIO_ONLY_CLIENT,
        FULL_SERVER_RESPONSE,
        AUDIO_ONLY_SERVER,
    ):
        size, offset = _uint32(data, offset)
        body, offset = _take(data, offset, size)
    else:
        raise ValueError("未知的 ASR message type：0x%x" % message_type)

    if compression == COMPRESSION_GZIP:
        body = gzip.decompress(body)

    payload = body
    if serialization == SERIALIZATION_JSON and message_type in (
        FULL_CLIENT_REQUEST,
        FULL_SERVER_RESPONSE,
        ERROR_RESPONSE,
    ):
        if body:
            payload = json.loads(body.decode("utf-8"))

    return AsrMessage(
        message_type=message_type,
        flags=flags,
        sequence=sequence,
        is_last_package=is_last,
        event=event,
        error_code=error_code,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# TTS（双向流式）
# ---------------------------------------------------------------------------


def encode_tts_message(message_type, event, payload=b"", session_id="",
                       connect_id=""):
    # type: (int, int, bytes, str, str) -> bytes
    """TTS 帧：头部 + event + session id + connect id + payload。"""
    out = bytearray(_header(message_type, WITH_EVENT))
    out += struct.pack(">i", event)
    if event not in _EVENTS_WITHOUT_SESSION:
        sid = session_id.encode("utf-8")
        out += struct.pack(">I", len(sid))
        out += sid
    if event in _EVENTS_WITH_CONNECT_ID:
        cid = connect_id.encode("utf-8")
        out += struct.pack(">I", len(cid))
        out += cid
    out += struct.pack(">I", len(payload))
    out += payload
    return bytes(out)


@dataclass
class TtsMessage:
    message_type: int
    event: int
    session_id: str
    connect_id: str
    error_code: int
    payload: bytes


def decode_tts_message(data):
    # type: (bytes) -> TtsMessage
    """解析 TTS 帧（客户端只需要读服务端下行的控制帧与音频帧）。"""
    if not isinstance(data, bytes):
        raise ValueError("TTS 只接受二进制帧")
    if len(data) < 4:
        raise ValueError("TTS 帧太短：%d 字节" % len(data))
    header_size = data[0] & 0x0F
    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    offset = header_size * 4

    event = 0
    session_id = ""
    connect_id = ""
    error_code = 0

    if flags & WITH_EVENT:
        event, offset = _int32(data, offset)
        if event not in _EVENTS_WITHOUT_SESSION:
            size, offset = _uint32(data, offset)
            raw, offset = _take(data, offset, size)
            session_id = raw.decode("utf-8")
        if event in _EVENTS_WITH_CONNECT_ID:
            size, offset = _uint32(data, offset)
            raw, offset = _take(data, offset, size)
            connect_id = raw.decode("utf-8")

    if message_type == ERROR_RESPONSE:
        error_code, offset = _int32(data, offset)

    size, offset = _uint32(data, offset)
    payload, offset = _take(data, offset, size)

    return TtsMessage(
        message_type=message_type,
        event=event,
        session_id=session_id,
        connect_id=connect_id,
        error_code=error_code,
        payload=payload,
    )

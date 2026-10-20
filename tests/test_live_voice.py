"""真实语音服务冒烟（env-gated，绝不进 CI）。

只有 `OC2_LIVE_VOICE=1` 且必需环境变量齐全时才执行；默认 skip。它覆盖
火山流式 ASR / TTS 与方舟 LLM 三条真实链路，用来在首次实跑时确认端点、
协议与音色默认值仍然有效（spec §10.1「真实 API 冒烟放在 env flag 后面」）。
"""
import os

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("OC2_LIVE_VOICE") != "1",
    reason="设置 OC2_LIVE_VOICE=1 才跑真实语音服务冒烟",
)


def _env(name, default=""):
    # type: (str, str) -> str
    return (os.environ.get(name) or default).strip()


def _require(name):
    # type: (str) -> str
    value = _env(name)
    if not value:
        pytest.skip("缺少环境变量 %s" % name)
    return value


def test_live_tts_returns_audio():
    from ai.dialog.tts import TTSClient

    api_key = _require("VOLC_TTS_API_KEY")
    resource_id = _env("VOLC_TTS_RESOURCE_ID", "seed-tts-2.0")
    voice = _env("VOLC_TTS_VOICE", "zh_female_vv_uranus_bigtts")
    client = TTSClient(api_key, resource_id, voice, timeout=10.0)

    chunks = list(client.synthesize("你好"))

    assert b"".join(chunks)


def test_live_ark_returns_json_text():
    from ai.dialog.llm import LLMClient

    api_key = _require("ARK_API_KEY")
    model = _require("ARK_MODEL")
    base_url = _env("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    client = LLMClient(api_key, base_url, model, timeout=10.0)

    text = client.query(
        '只输出 JSON：{"addressed": false, "reply": null, '
        '"commands": [], "confidence": 0.1}')

    assert text.strip()


def test_live_asr_accepts_silence_without_hanging():
    from ai.dialog.asr import ASRClient

    api_key = _require("VOLC_ASR_API_KEY")
    resource_id = _env("VOLC_ASR_RESOURCE_ID", "volc.seedasr.sauc.duration")
    client = ASRClient(api_key, resource_id, timeout=10.0)

    # 0.5 秒静音：服务通常不返回 definite 文本，但必须正常结束连接。
    results = list(client.stream([b"\x00\x00" * 8000]))

    assert isinstance(results, list)

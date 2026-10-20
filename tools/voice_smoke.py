#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立于游戏的语音链路冒烟：ASR / TTS / 方舟 LLM / 麦克风 / 扬声器。

不连 27411、不注入按键，也不读游戏状态，可单独验证 `ai/dialog` 的真实
客户端。首次拿到火山 key 时建议按下面顺序跑：

    python3 tools/voice_smoke.py doctor          # 依赖、配置、音频设备
    python3 tools/voice_smoke.py tone            # 扬声器（不需要 key）
    python3 tools/voice_smoke.py record --seconds 3  # 麦克风电平（不需要 key）
    python3 tools/voice_smoke.py tts             # TTS 合成 + 扬声器播放
    python3 tools/voice_smoke.py asr             # TTS → ASR 回环（不用麦）
    python3 tools/voice_smoke.py asr --source mic  # 麦克风 → VAD → ASR
    python3 tools/voice_smoke.py llm             # 方舟真实一轮 JSON 输出
    python3 tools/voice_smoke.py full            # 麦克风 → ASR → LLM → TTS
    python3 tools/voice_smoke.py all --with-mic  # 上面能自动跑的都跑一遍

注意：本工具的 `asr --source mic` / `full` 用 VAD 自动切句，不测 Agent 的
PTT；PTT 请在游戏内按 `OC2_VOICE_PTT_KEY`（如 F8）验证。

密钥只从环境变量读。脚本默认会加载仓库根的 `.env`（已存在时），
也可以用 `--env-file PATH` 指定；已有环境变量优先，不会覆盖。
所有输出都不会打印密钥内容。

退出码：0 通过（含按配置跳过）；1 有步骤失败；2 配置/依赖缺失。
"""
import argparse
import dataclasses
import difflib
import math
import os
import sys
import tempfile
import time
import traceback
import wave

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ai.dialog.env import load_env_file  # noqa: E402

TTS_SAMPLE_RATE = 24000
ASR_SAMPLE_RATE = 16000
DEFAULT_TTS_TEXT = "行，我来切菜，你去煮饭。"
DEFAULT_TRANSCRIPT = "我来切菜，你负责煮饭。"

# 与 run_agent.py / .env.example 保持一致。
DEFAULTS = {
    "VOLC_ASR_RESOURCE_ID": "volc.seedasr.sauc.duration",
    "VOLC_TTS_VOICE": "zh_female_vv_uranus_bigtts",
    "VOLC_TTS_RESOURCE_ID": "seed-tts-2.0",
    "ARK_BASE_URL": "https://ark.cn-beijing.volces.com/api/v3",
    "OC2_VOICE_VAD_DB": 9.0,
}
REQUIRED = {
    "asr": ("VOLC_ASR_API_KEY",),
    "tts": ("VOLC_TTS_API_KEY",),
    "llm": ("ARK_API_KEY", "ARK_MODEL"),
    "all": ("VOLC_ASR_API_KEY", "VOLC_TTS_API_KEY", "ARK_API_KEY", "ARK_MODEL"),
}

# 与 Agent._build_dialog_context 同形，方便直接看 LLM 在真实版式下的表现。
SAMPLE_CONTEXT = {
    "level": "Sushi_1_4_2P",
    "tick": 1200,
    "time_left": 180.0,
    "orders": [{"id": "1", "dish": "Sushi_Cucumber", "remaining": 60.0,
                "eta": 22.0}],
    "cookers": [{"name": "cooker (1)", "text": "在煮，40%；锅里 SushiRice"}],
    "plates": "空盘 2 个",
    "p1": {"holding": None},
    "p2": {"doing": "Chop:Cucumber（订单 #1）", "held": None},
    "p2_options": ["FetchToBoard:Cucumber（订单 #1）", "WashPlate"],
    "duties": [],
    "dag": ("Sushi_Cucumber\n"
            "  └ FetchToPlate:Seaweed\n"
            "  └ TakeCookedToPlate:BoiledSushiRice\n"
            "    └ FetchToCooker:BoiledSushiRice\n"
            "  └ FetchToPlate:Cucumber\n"
            "    └ Chop:Cucumber\n"
            "      └ FetchToBoard:Cucumber\n"
            "共享：WashPlate  PlaceCleanPlate  DeliverPlate"),
}


class ConfigError(RuntimeError):
    """缺少环境变量或依赖；单模式直接退出 2。"""


class SmokeError(RuntimeError):
    """某一项真实链路没有通过。"""


@dataclasses.dataclass
class Settings(object):
    asr_key: str
    asr_resource_id: str
    tts_key: str
    tts_voice: str
    tts_resource_id: str
    ark_key: str
    ark_base_url: str
    ark_model: str
    volume: float
    vad_threshold_db: float


# ---------------------------------------------------------------------------
# 环境与配置
# ---------------------------------------------------------------------------


def _env_text(name, default=""):
    # type: (str, str) -> str
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = str(raw).strip()
    return raw or default


def _env_float(name, default, low, high):
    # type: (str, float, float, float) -> float
    raw = _env_text(name, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value):
        return default
    return min(high, max(low, value))


def _settings():
    # type: () -> Settings
    return Settings(
        asr_key=_env_text("VOLC_ASR_API_KEY"),
        asr_resource_id=_env_text(
            "VOLC_ASR_RESOURCE_ID", DEFAULTS["VOLC_ASR_RESOURCE_ID"]),
        tts_key=_env_text("VOLC_TTS_API_KEY"),
        tts_voice=_env_text("VOLC_TTS_VOICE", DEFAULTS["VOLC_TTS_VOICE"]),
        tts_resource_id=_env_text(
            "VOLC_TTS_RESOURCE_ID", DEFAULTS["VOLC_TTS_RESOURCE_ID"]),
        ark_key=_env_text("ARK_API_KEY"),
        ark_base_url=_env_text("ARK_BASE_URL", DEFAULTS["ARK_BASE_URL"]),
        ark_model=_env_text("ARK_MODEL"),
        volume=_env_float("OC2_VOICE_VOLUME", 0.7, 0.3, 1.2),
        vad_threshold_db=_env_float(
            "OC2_VOICE_VAD_DB", DEFAULTS["OC2_VOICE_VAD_DB"], 2.0, 20.0),
    )


def _require(settings, kinds):
    # type: (Settings, tuple) -> None
    """按链路检查必需变量；缺一个就抛 ConfigError（只报变量名）。"""
    checks = {
        "asr": [("VOLC_ASR_API_KEY", settings.asr_key)],
        "tts": [("VOLC_TTS_API_KEY", settings.tts_key)],
        "llm": [("ARK_API_KEY", settings.ark_key),
                ("ARK_MODEL", settings.ark_model)],
    }
    missing = []
    for kind in kinds:
        for name, value in checks[kind]:
            if not value and name not in missing:
                missing.append(name)
    if missing:
        raise ConfigError(
            "缺少环境变量: %s（复制 .env.example 为 .env 并填写，"
            "或先 set -a; . ./.env; set +a）" % ", ".join(missing))


def _mask(value):
    # type: (str) -> str
    return "已设置（%d 字符）" % len(value) if value else "未设置"


# ---------------------------------------------------------------------------
# 音频工具
# ---------------------------------------------------------------------------


def _save_wav(path, pcm, sample_rate):
    # type: (str, bytes, int) -> str
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm)
    return path


def _read_wav(path):
    # type: (str) -> bytes
    """读 16 bit 单声道/多声道 WAV，必要重采样到 ASR 的 16 kHz。"""
    if not os.path.exists(path):
        raise ConfigError("输入文件不存在: %s" % path)
    try:
        with wave.open(path, "rb") as wf:
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            rate = wf.getframerate()
            data = wf.readframes(wf.getnframes())
    except wave.Error as exc:
        raise ConfigError("只支持 WAV 文件：%s" % exc)
    if width != 2:
        raise ConfigError("只支持 16 bit PCM WAV，当前 %d bit" % (width * 8))
    if channels > 1:
        import numpy as np
        samples = np.frombuffer(data, dtype="<i2")
        samples = samples.reshape(-1, channels).mean(axis=1)
        data = np.clip(np.round(samples), -32768, 32767).astype("<i2").tobytes()
    return _resample_pcm16(data, rate, ASR_SAMPLE_RATE)


def _resample_pcm16(pcm, src_rate, dst_rate):
    # type: (bytes, int, int) -> bytes
    if int(src_rate) == int(dst_rate) or not pcm:
        return pcm
    import numpy as np
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    target = int(round(len(samples) * float(dst_rate) / float(src_rate)))
    if target <= 0:
        return b""
    old_axis = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    new_axis = np.linspace(0.0, 1.0, num=target, endpoint=False)
    out = np.interp(new_axis, old_axis, samples)
    return np.clip(np.round(out), -32768, 32767).astype("<i2").tobytes()


def _pcm16(frames):
    # type: (list) -> bytes
    """float32 [-1, 1] 帧 → 16 bit 小端单声道 PCM（与 worker 同口径）。"""
    import numpy as np
    samples = [
        np.asarray(frame, dtype=np.float32).reshape(-1) for frame in frames
    ]
    samples = [item for item in samples if item.size]
    if not samples:
        return b""
    data = np.clip(np.concatenate(samples), -1.0, 1.0)
    return (data * 32767.0).astype("<i2").tobytes()


def _duration(pcm, sample_rate):
    # type: (bytes, int) -> float
    return len(pcm) / 2.0 / float(sample_rate)


def _save_dir(args):
    # type: (argparse.Namespace) -> str
    path = args.save_dir or os.path.join(
        tempfile.gettempdir(), "oc2-voice-smoke")
    os.makedirs(path, exist_ok=True)
    return path


def _timestamp():
    # type: () -> str
    return time.strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# 各链路
# ---------------------------------------------------------------------------


def _synthesize(settings, text, timeout):
    # type: (Settings, str, float) -> tuple
    """返回 (pcm, 首包秒数, 总秒数)；失败抛 SmokeError/RuntimeError。"""
    from ai.dialog.tts import TTSClient

    client = TTSClient(
        settings.tts_key, settings.tts_resource_id, settings.tts_voice,
        sample_rate=TTS_SAMPLE_RATE, timeout=timeout)
    started = time.monotonic()
    first_packet = None
    chunks = []
    for chunk in client.synthesize(text):
        if not chunk:
            continue
        if first_packet is None:
            first_packet = time.monotonic() - started
        chunks.append(bytes(chunk))
    elapsed = time.monotonic() - started
    pcm = b"".join(chunks)
    if not pcm:
        raise SmokeError("TTS 没有返回任何音频数据")
    return pcm, first_packet, elapsed


def _play_pcm(pcm, sample_rate, volume):
    # type: (bytes, int, float) -> float
    """用真实 AudioPlayer 播完；返回实际等待秒数。"""
    from ai.dialog.audio import AudioPlayer

    player = AudioPlayer(sample_rate=sample_rate, volume=volume)
    started = time.monotonic()
    try:
        player.play(iter([pcm]))
        deadline = started + _duration(pcm, sample_rate) + 10.0
        while player.is_playing and time.monotonic() < deadline:
            time.sleep(0.02)
        if player.is_playing:
            raise SmokeError("扬声器播放超过 %.1fs 仍未结束"
                             % (_duration(pcm, sample_rate) + 10.0))
        return time.monotonic() - started
    finally:
        player.stop()


def _transcribe(settings, pcm, timeout):
    # type: (Settings, bytes, float) -> tuple
    """返回 (definite 文本列表, 耗时秒数)。"""
    from ai.dialog.asr import ASRClient

    client = ASRClient(
        settings.asr_key, settings.asr_resource_id,
        sample_rate=ASR_SAMPLE_RATE, timeout=timeout)
    started = time.monotonic()
    texts = []
    for result in client.stream([pcm]):
        if not isinstance(result, dict):
            continue
        text = result.get("text") or ""
        if text and result.get("definite"):
            texts.append(text)
            print("  [asr] %s" % text)
    return texts, time.monotonic() - started


def _record_mic(max_seconds, fixed_seconds=None, threshold_db=9.0):
    # type: (float, float, float) -> tuple
    """录一段 16 kHz 单声道 PCM。

    默认用 VAD 切一句（与 DialogWorker 相同：3 帧起音 + 400 ms 静音）；
    `fixed_seconds` 非空时改为录固定时长，适合排查 VAD 不触发。
    """
    from ai.dialog.audio import AudioCapture, VAD

    capture = AudioCapture()
    vad = VAD(threshold_db=threshold_db)
    frames = capture.frames()
    collected = []
    preroll = []
    started = time.monotonic()
    reason = "达到最长录音时间"
    try:
        for frame in frames:
            now = time.monotonic()
            if fixed_seconds:
                collected.append(frame)
                if now - started >= fixed_seconds:
                    reason = "录满 %.1fs" % fixed_seconds
                    break
            else:
                event = vad.feed(frame)
                if event == "start":
                    collected = list(preroll) + [frame]
                    print("  [mic] 检测到开口，开始录音…")
                elif event == "end":
                    if collected:
                        collected.append(frame)
                    reason = "检测到句尾静音"
                    break
                elif collected:
                    collected.append(frame)
                preroll.append(frame)
                if len(preroll) > 2:
                    preroll = preroll[-2:]
                if now - started >= max_seconds:
                    break
    finally:
        close = getattr(frames, "close", None)
        if callable(close):
            close()
        capture.close()

    if not collected:
        raise SmokeError(
            "没有采集到语音：VAD 未触发（环境太吵/太轻），"
            "可用 --seconds 3 先录固定时长确认麦克风有电平")
    pcm = _pcm16(collected)
    levels = [VAD._level_db(frame) for frame in collected]
    print("  [mic] %s，共 %.2fs，峰值 %.1f dB / 均值 %.1f dB"
          % (reason, _duration(pcm, ASR_SAMPLE_RATE),
             max(levels), sum(levels) / len(levels)))
    if not fixed_seconds:
        print("  [mic] 当前噪声底 %.1f dB（开口阈值 = 底噪 + %.1f dB）"
              % (vad.floor_db, threshold_db))
    return pcm, collected


def _llm_call(settings, transcript, event, timeout):
    # type: (Settings, str, str, float) -> object
    """一轮真实方舟请求（带 update_duties 工具），返回 LLMReply。"""
    from ai.dialog.duties import tool_schemas
    from ai.dialog.llm import (LLMClient, speech_message, state_message,
                               system_message)

    client = LLMClient(
        settings.ark_key, settings.ark_base_url, settings.ark_model,
        timeout=timeout)
    keys = ["WashPlate", "PlaceCleanPlate", "DeliverPlate", "Chop",
            "FetchToBoard", "FetchToPlate", "FetchToCooker",
            "TakeCookedToPlate", "Chop:Cucumber", "FetchToBoard:Cucumber",
            "FetchToPlate:Cucumber", "FetchToPlate:Seaweed",
            "FetchToCooker:BoiledSushiRice",
            "TakeCookedToPlate:BoiledSushiRice"]
    messages = [system_message(SAMPLE_CONTEXT["dag"]),
                state_message(event, SAMPLE_CONTEXT)]
    if event == "speech":
        messages.append(speech_message(transcript))
    tools = tool_schemas(keys, [], ["1"]) if event == "speech" else None
    started = time.monotonic()
    reply = client.chat(messages, tools)
    elapsed = time.monotonic() - started
    print("  [llm] %.2fs，回复：%r" % (elapsed, reply.content))
    for call in reply.tool_calls:
        print("  [llm] 工具 %s %s" % (call.name, call.arguments))
    if not reply.content and not reply.tool_calls:
        raise SmokeError("方舟返回了空内容")
    return reply


def _similarity(a, b):
    # type: (str, str) -> float
    """忽略标点/空白的字符相似度，用来判断回环是否「基本对上」。"""
    keep = set("abcdefghijklmnopqrstuvwxyz"
               "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
               "0123456789"
               "你我他她它们这那有没去来洗切煮送盘菜饭锅肉")
    left = "".join(ch for ch in a if ch in keep)
    right = "".join(ch for ch in b if ch in keep)
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------


def cmd_doctor(settings, args):
    # type: (Settings, argparse.Namespace) -> None
    print("\n== 1. Python 与依赖 ==")
    print("  python %s (%s)"
          % (sys.version.split()[0], sys.platform))
    failed = False
    for module_name in ("numpy", "websockets", "requests", "sounddevice"):
        try:
            module = __import__(module_name)
            print("  [ok] %-12s %s"
                  % (module_name, getattr(module, "__version__", "?")))
        except Exception as exc:                # noqa: BLE001
            failed = True
            print("  [FAIL] %-12s %s" % (module_name, exc))
    if failed:
        raise ConfigError(
            "语音依赖缺失：python3 -m pip install -r requirements-voice.txt")

    print("\n== 2. 环境变量 ==")
    for name in ("VOLC_ASR_API_KEY", "VOLC_TTS_API_KEY", "ARK_API_KEY",
                 "ARK_MODEL"):
        value = _env_text(name)
        print("  %s %-22s %s"
              % ("[ok]" if value else "[--]", name,
                 _mask(value) if name.endswith("_KEY") else (value or "未设置")))
    for name, default in (
            ("VOLC_ASR_RESOURCE_ID", DEFAULTS["VOLC_ASR_RESOURCE_ID"]),
            ("VOLC_TTS_RESOURCE_ID", DEFAULTS["VOLC_TTS_RESOURCE_ID"]),
            ("VOLC_TTS_VOICE", DEFAULTS["VOLC_TTS_VOICE"]),
            ("ARK_BASE_URL", DEFAULTS["ARK_BASE_URL"])):
        print("  [i ] %-22s %s" % (name, _env_text(name, default)))

    print("\n== 3. 音频设备 ==")
    _device_report()

    print("\n提示：doctor 只检查本地条件；key 是否有效由 tts/asr/llm 实际请求验证。")


def _device_report():
    # type: () -> None
    try:
        import sounddevice as sd
        from ai.dialog.audio import list_input_devices, list_output_devices
    except Exception as exc:                    # noqa: BLE001
        raise ConfigError(
            "无法加载音频设备（sounddevice/PortAudio）：%s" % exc)
    try:
        default = tuple(getattr(sd.default, "device", (None, None)))
    except Exception:                           # noqa: BLE001
        default = (None, None)

    ok = True
    for label, lister, default_index in (
            ("输入（麦克风）", list_input_devices, default[0] if default else None),
            ("输出（扬声器）", list_output_devices,
             default[1] if len(default) > 1 else None)):
        try:
            devices = lister(sd)
        except Exception as exc:                # noqa: BLE001
            ok = False
            print("  [FAIL] 枚举%s设备失败：%s" % (label, exc))
            continue
        print("  %s设备：" % label)
        if not devices:
            ok = False
            print("    （没有可用设备）")
            continue
        for device in devices:
            index = device.get("index")
            print("    %s%2s  %-46s %sch  %.0f Hz"
                  % ("*" if index == default_index else " ", index,
                     str(device.get("name", "?"))[:46],
                     device.get("max_input_channels"
                                if "输入" in label else "max_output_channels"),
                     float(device.get("default_samplerate") or 0)))
    if not ok:
        raise SmokeError(
            "没有可用的输入/输出设备；用 `python3 -m sounddevice` 查看，"
            "并在 macOS「声音」里选择默认设备")


def cmd_tts(settings, args):
    # type: (Settings, argparse.Namespace) -> None
    _require(settings, ("tts",))
    text = args.text or DEFAULT_TTS_TEXT
    pcm, first_packet, elapsed = _synthesize(settings, text, args.timeout)
    duration = _duration(pcm, TTS_SAMPLE_RATE)
    path = _save_wav(
        os.path.join(_save_dir(args), "tts-%s.wav" % _timestamp()),
        pcm, TTS_SAMPLE_RATE)
    print("  文本：%s" % text)
    print("  首包 %.2fs，总耗时 %.2fs，音频 %.2fs（%d 字节）"
          % (first_packet, elapsed, duration, len(pcm)))
    print("  已保存：%s" % path)
    if args.no_play:
        print("  （--no-play：跳过扬声器播放）")
        return
    waited = _play_pcm(pcm, TTS_SAMPLE_RATE, settings.volume)
    print("  扬声器播放完成（等待 %.2fs）" % waited)


def cmd_tone(settings, args):
    # type: (Settings, argparse.Namespace) -> None
    """不依赖 key 的扬声器自检：播放一段带淡入淡出的正弦音。"""
    import numpy as np

    seconds = args.seconds if args.seconds else 1.0
    if seconds <= 0:
        raise ConfigError("--seconds 必须大于 0")
    samples = np.sin(
        2.0 * math.pi * float(args.frequency)
        * np.arange(int(TTS_SAMPLE_RATE * seconds)) / TTS_SAMPLE_RATE)
    samples = (samples * 0.25).astype(np.float32)
    fade = min(int(TTS_SAMPLE_RATE * 0.02), samples.size // 2)
    if fade:
        ramp = np.linspace(0.0, 1.0, num=fade, dtype=np.float32)
        samples[:fade] *= ramp
        samples[-fade:] *= ramp[::-1]
    pcm = np.clip(np.round(samples * 32767.0), -32768, 32767).astype(
        "<i2").tobytes()
    print("  %.0f Hz / %.2fs / 音量 %.1f"
          % (args.frequency, seconds, settings.volume))
    if args.no_play:
        print("  （--no-play：跳过扬声器播放）")
        return
    waited = _play_pcm(pcm, TTS_SAMPLE_RATE, settings.volume)
    print("  扬声器播放完成（等待 %.2fs）" % waited)


def cmd_record(settings, args):
    # type: (Settings, argparse.Namespace) -> None
    """不依赖 key 的麦克风自检：录一段并报告电平/保存 WAV。"""
    pcm, _frames = _record_mic(
        args.max_seconds, fixed_seconds=args.seconds,
        threshold_db=settings.vad_threshold_db)
    path = _save_wav(
        os.path.join(_save_dir(args), "record-%s.wav" % _timestamp()),
        pcm, ASR_SAMPLE_RATE)
    print("  已保存：%s（%.2fs，%d 字节）"
          % (path, _duration(pcm, ASR_SAMPLE_RATE), len(pcm)))
    if not any(pcm):
        raise SmokeError("录到的是全零（麦克风权限未授予或设备静音）")


def cmd_asr(settings, args):
    # type: (Settings, argparse.Namespace) -> None
    _require(settings, ("asr",))
    source = args.source
    if source == "tts":
        _require(settings, ("tts",))
        text = args.text or DEFAULT_TTS_TEXT
        pcm24, _first, _elapsed = _synthesize(settings, text, args.timeout)
        path = _save_wav(
            os.path.join(_save_dir(args), "tts-asr-%s.wav" % _timestamp()),
            pcm24, TTS_SAMPLE_RATE)
        pcm = _resample_pcm16(pcm24, TTS_SAMPLE_RATE, ASR_SAMPLE_RATE)
        print("  音源：TTS「%s」（已保存 %s）" % (text, path))
        expected = text
    elif source == "mic":
        seconds = args.seconds
        pcm, _frames = _record_mic(
            args.max_seconds, fixed_seconds=seconds,
            threshold_db=settings.vad_threshold_db)
        path = _save_wav(
            os.path.join(_save_dir(args), "mic-%s.wav" % _timestamp()),
            pcm, ASR_SAMPLE_RATE)
        print("  音源：麦克风（已保存 %s）" % path)
        expected = ""
    else:
        if not args.input:
            raise ConfigError("--source file 需要 --input PATH.wav")
        pcm = _read_wav(args.input)
        print("  音源：%s（已重采样到 %d Hz）"
              % (args.input, ASR_SAMPLE_RATE))
        expected = ""

    texts, elapsed = _transcribe(settings, pcm, args.timeout)
    if not texts:
        raise SmokeError(
            "ASR 没有返回 definite 文本（音频 %.2fs）；确认音频有清晰人声、"
            "16 kHz、资源 ID 与账号开通的版本一致"
            % _duration(pcm, ASR_SAMPLE_RATE))
    transcript = "".join(texts)
    print("  识别结果：%s（耗时 %.2fs）" % (transcript, elapsed))
    if expected:
        score = _similarity(expected, transcript)
        print("  与原文相似度：%.2f" % score)
        if score < 0.3:
            print("  [warn] 相似度偏低；先确认音色/音量正常，再检查 ASR 参数")


def cmd_llm(settings, args):
    # type: (Settings, argparse.Namespace) -> None
    _require(settings, ("llm",))
    transcript = args.text or DEFAULT_TRANSCRIPT
    print("  玩家说：%s" % transcript)
    _llm_call(settings, transcript, args.event, args.llm_timeout)


def cmd_full(settings, args):
    # type: (Settings, argparse.Namespace) -> None
    """麦克风 → ASR → LLM → TTS → 扬声器（不经过游戏/命令队列）。"""
    _require(settings, ("asr", "llm", "tts"))
    print("  请对麦克风说一句给 P2 的指令，例如：「你来煮饭」。")
    pcm, _frames = _record_mic(
        args.max_seconds, fixed_seconds=args.seconds,
        threshold_db=settings.vad_threshold_db)
    mic_path = _save_wav(
        os.path.join(_save_dir(args), "full-mic-%s.wav" % _timestamp()),
        pcm, ASR_SAMPLE_RATE)
    print("  [1/4] 麦克风已保存：%s" % mic_path)

    texts, asr_elapsed = _transcribe(settings, pcm, args.timeout)
    if not texts:
        raise SmokeError("ASR 没有返回 definite 文本")
    transcript = "".join(texts)
    print("  [2/4] ASR（%.2fs）：%s" % (asr_elapsed, transcript))

    reply = _llm_call(settings, transcript, "speech", args.llm_timeout)
    print("  [3/4] LLM 回复：%r" % (reply.content,))
    if not reply.content:
        raise SmokeError("LLM 没有给出可播报的回复；换一句明确指令再试")

    pcm24, first_packet, elapsed = _synthesize(
        settings, reply.content, args.timeout)
    tts_path = _save_wav(
        os.path.join(_save_dir(args), "full-reply-%s.wav" % _timestamp()),
        pcm24, TTS_SAMPLE_RATE)
    print("  [4/4] TTS 首包 %.2fs，总耗时 %.2fs，已保存：%s"
          % (first_packet, elapsed, tts_path))
    if args.no_play:
        print("  （--no-play：跳过扬声器播放）")
        return
    waited = _play_pcm(pcm24, TTS_SAMPLE_RATE, settings.volume)
    print("  扬声器播放完成（等待 %.2fs）" % waited)


# ---------------------------------------------------------------------------
# 运行与入口
# ---------------------------------------------------------------------------


def _auth_hint(message):
    # type: (str) -> str
    """把常见的鉴权失败翻译成可操作的下一步。"""
    text = (message or "")
    if "not granted" in text:
        return ("→ 账号尚未开通该资源：在对应控制台开通服务/资源包"
                "（ASR 服务 16、TTS 服务 8），并确认 key 属于该服务")
    if "401" in text or "403" in text or "Unauthorized" in text:
        return ("→ 凭据被拒绝：检查对应 API key、资源 ID 是否与账号"
                "开通的服务一致（ASR/TTS 各用各的 key）；方舟 plan/套餐 "
                "key 需要 ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/plan/v3")
    if "Name or service not known" in text or "Connection refused" in text:
        return "→ 网络不可达：检查代理/DNS，或确认端点地址填写正确"
    return ""


def _run_step(name, fn, args, results):
    # type: (str, object, argparse.Namespace, list) -> str
    print("\n=== %s ===" % name)
    try:
        fn()
    except ConfigError as exc:
        print("  [SKIP] %s" % exc)
        results.append((name, "skip"))
        return "skip"
    except KeyboardInterrupt:
        raise
    except Exception as exc:                    # noqa: BLE001
        message = str(exc)
        hint = _auth_hint(message)
        print("  [FAIL] %s: %s%s"
              % (type(exc).__name__, message, "  " + hint if hint else ""))
        if args.debug:
            traceback.print_exc()
        results.append((name, "fail"))
        return "fail"
    else:
        print("  [ok]")
        results.append((name, "ok"))
        return "ok"


def _run_all(settings, args):
    # type: (Settings, argparse.Namespace) -> int
    results = []
    _run_step("依赖 / 配置 / 设备", lambda: cmd_doctor(settings, args),
              args, results)
    _run_step("TTS 合成 + 扬声器", lambda: cmd_tts(settings, args),
              args, results)
    _run_step("TTS → ASR 回环", lambda: cmd_asr(settings, args),
              args, results)
    _run_step("方舟 LLM", lambda: cmd_llm(settings, args), args, results)
    if args.with_mic:
        mic_args = argparse.Namespace(**vars(args))
        mic_args.source = "mic"
        _run_step("麦克风 → ASR", lambda: cmd_asr(settings, mic_args),
                  args, results)

    print("\n=== 汇总 ===")
    failed = 0
    for name, status in results:
        print("  %-24s %s" % (name, {"ok": "通过", "skip": "跳过",
                                     "fail": "失败"}[status]))
        if status == "fail":
            failed += 1
    return 1 if failed else 0


def _parse_args(argv):
    # type: (object) -> argparse.Namespace
    parser = argparse.ArgumentParser(
        description="独立于游戏的 ASR/TTS/方舟语音链路冒烟")
    parser.add_argument(
        "mode", nargs="?", default="all",
        choices=("doctor", "tone", "record", "tts", "asr", "llm", "full",
                 "all"),
        help="要验证的链路（默认 all）")
    parser.add_argument(
        "--env-file", metavar="PATH", default=None,
        help="加载 .env 风格的 KEY=VALUE（默认尝试仓库根 .env）")
    parser.add_argument(
        "--no-env-file", action="store_true", help="不自动加载 .env")
    parser.add_argument(
        "--text", default=None,
        help="TTS 文本 / ASR 期望文本 / LLM 的玩家话术")
    parser.add_argument(
        "--source", choices=("tts", "mic", "file"), default="tts",
        help="ASR 音源（默认 tts，不需要麦克风）")
    parser.add_argument(
        "--input", metavar="WAV", default=None,
        help="--source file 时的 16 bit PCM WAV")
    parser.add_argument(
        "--seconds", type=float, default=None,
        help="麦克风固定录制 / tone 播放秒数（不设则用 VAD 切一句）")
    parser.add_argument(
        "--frequency", type=float, default=440.0,
        help="tone 模式的频率 Hz（默认 440）")
    parser.add_argument(
        "--max-seconds", type=float, default=10.0,
        help="VAD 模式下最长等待/录制秒数（默认 10）")
    parser.add_argument(
        "--timeout", type=float, default=10.0,
        help="ASR/TTS 建连与单次读取超时（默认 10s）")
    parser.add_argument(
        "--llm-timeout", type=float, default=15.0,
        help="方舟请求超时（默认 15s）")
    parser.add_argument(
        "--event", default="speech",
        help="喂给 LLM 的触发事件（默认 speech）")
    parser.add_argument(
        "--no-play", action="store_true", help="只合成，不播放到扬声器")
    parser.add_argument(
        "--save-dir", default=None,
        help="音频保存目录（默认系统临时目录/oc2-voice-smoke）")
    parser.add_argument(
        "--with-mic", action="store_true",
        help="all 模式额外跑一次麦克风 → ASR（会等待你说话）")
    parser.add_argument("--debug", action="store_true", help="失败时打印 traceback")
    return parser.parse_args(argv)


def main(argv=None):
    # type: (object) -> int
    args = _parse_args(argv)

    env_path = None
    if not args.no_env_file:
        env_path = args.env_file or os.path.join(_ROOT, ".env")
    if env_path and os.path.exists(env_path):
        try:
            added = load_env_file(env_path)
        except OSError as exc:
            print("[voice-smoke] 读取 %s 失败：%s" % (env_path, exc),
                  file=sys.stderr)
            return 2
        print("[voice-smoke] 已加载 %s（新增 %d 个变量，已有变量优先）"
              % (env_path, added))
    elif args.env_file:
        print("[voice-smoke] --env-file 指定的文件不存在：%s" % args.env_file,
              file=sys.stderr)
        return 2

    if args.debug:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    settings = _settings()
    try:
        if args.mode == "all":
            return _run_all(settings, args)
        commands = {
            "doctor": cmd_doctor,
            "tone": cmd_tone,
            "record": cmd_record,
            "tts": cmd_tts,
            "asr": cmd_asr,
            "llm": cmd_llm,
            "full": cmd_full,
        }
        results = []
        status = _run_step(
            args.mode, lambda: commands[args.mode](settings, args),
            args, results)
        # 单模式缺配置/依赖按约定退出 2；真实链路失败退出 1。
        if status == "skip":
            return 2
        return 0 if status == "ok" else 1
    except KeyboardInterrupt:
        print("\n[voice-smoke] 已取消（Ctrl-C）", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

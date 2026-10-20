# -*- coding: utf-8 -*-
"""run_agent 的 CLI / 环境变量解析测试（spec §6.3、§6.5）。

只测参数解析与 `VoiceConfig` 构造，不连 127.0.0.1:27411、不加载
sounddevice / websockets。
"""
import os

import pytest

import run_agent

REQUIRED = {
    "VOLC_ASR_API_KEY": "asr-key",
    "VOLC_TTS_API_KEY": "tts-key",
    "ARK_API_KEY": "ark-key",
    "ARK_MODEL": "doubao-lite",
}


def _env(**overrides):
    env = dict(REQUIRED)
    env.update(overrides)
    return env


# ---------------------------------------------------------------- .env 加载

def test_load_env_file_fills_only_missing(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "RUN_AGENT_ENV_A=from-file\n"
        "RUN_AGENT_ENV_B='带 空格'\n"
        "export RUN_AGENT_ENV_C=3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RUN_AGENT_ENV_A", "from-env")
    for key in ("RUN_AGENT_ENV_B", "RUN_AGENT_ENV_C"):
        monkeypatch.delenv(key, raising=False)

    run_agent._load_env(
        run_agent._parse_args(["--env-file", str(path)]))

    try:
        assert os.environ["RUN_AGENT_ENV_A"] == "from-env"
        assert os.environ["RUN_AGENT_ENV_B"] == "带 空格"
        assert os.environ["RUN_AGENT_ENV_C"] == "3"
    finally:
        os.environ.pop("RUN_AGENT_ENV_B", None)
        os.environ.pop("RUN_AGENT_ENV_C", None)


def test_no_env_file_skips_loading(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("RUN_AGENT_ENV_D=from-file\n", encoding="utf-8")
    monkeypatch.delenv("RUN_AGENT_ENV_D", raising=False)

    run_agent._load_env(
        run_agent._parse_args(["--env-file", str(path), "--no-env-file"]))

    assert "RUN_AGENT_ENV_D" not in os.environ


# ---------------------------------------------------------------- 参数解析

def test_voice_flag_parses():
    args = run_agent._parse_args(["--voice", "--voice-debug"])
    assert args.voice is True
    assert args.voice_debug is True


def test_no_voice_flag_parses():
    args = run_agent._parse_args(["--no-voice"])
    assert args.voice is False


def test_default_voice_decision_is_auto():
    args = run_agent._parse_args([])
    assert args.voice is None


def test_no_voice_overrides_env(monkeypatch):
    monkeypatch.setenv("VOLC_ASR_API_KEY", "x")
    args = run_agent._parse_args(["--no-voice"])
    assert args.voice is False


def test_voice_log_parses():
    args = run_agent._parse_args(["--voice-log", "/tmp/voice.jsonl"])
    assert args.voice_log == "/tmp/voice.jsonl"


def test_no_voice_wins_over_voice_flag():
    args = run_agent._parse_args(["--voice", "--no-voice"])
    assert args.voice is False


# --------------------------------------------------------- VoiceConfig 构造

def test_no_voice_returns_none():
    args = run_agent._parse_args(["--no-voice"])
    assert run_agent._build_voice_config(args, _env()) is None


def test_explicit_voice_with_full_env_builds():
    args = run_agent._parse_args(["--voice"])
    cfg = run_agent._build_voice_config(args, _env())
    assert cfg is not None
    assert cfg.asr_key == "asr-key"
    assert cfg.tts_key == "tts-key"
    assert cfg.ark_key == "ark-key"
    assert cfg.ark_model == "doubao-lite"


def test_explicit_voice_with_missing_keys_raises():
    args = run_agent._parse_args(["--voice"])
    env = {"VOLC_ASR_API_KEY": "asr-key"}
    with pytest.raises(run_agent.VoiceConfigError) as excinfo:
        run_agent._build_voice_config(args, env)
    message = str(excinfo.value)
    for key in ("VOLC_TTS_API_KEY", "ARK_API_KEY", "ARK_MODEL"):
        assert key in message
    assert "VOLC_ASR_API_KEY" not in message


def test_auto_voice_with_full_env_builds():
    cfg = run_agent._build_voice_config(run_agent._parse_args([]), _env())
    assert cfg is not None


def test_auto_voice_with_missing_keys_warns_and_disables(capsys):
    cfg = run_agent._build_voice_config(
        run_agent._parse_args([]), {"VOLC_ASR_API_KEY": "only-one"})
    assert cfg is None
    err = capsys.readouterr().err
    assert "VOLC_TTS_API_KEY" in err
    assert "ARK_API_KEY" in err
    assert "ARK_MODEL" in err


def test_oc2_voice_zero_disables_even_with_full_env():
    cfg = run_agent._build_voice_config(
        run_agent._parse_args([]), _env(OC2_VOICE="0"))
    assert cfg is None


def test_oc2_voice_one_enables_with_full_env():
    cfg = run_agent._build_voice_config(
        run_agent._parse_args([]), _env(OC2_VOICE="1"))
    assert cfg is not None


# ------------------------------------------------------------------ 默认值

def test_env_defaults():
    cfg = run_agent._build_voice_config(run_agent._parse_args([]), _env())
    assert cfg.asr_resource_id == "volc.seedasr.sauc.duration"
    assert cfg.tts_voice == "zh_female_vv_uranus_bigtts"
    assert cfg.tts_resource_id == "seed-tts-2.0"
    assert cfg.ark_base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert cfg.rate == pytest.approx(1.05)
    assert cfg.volume == pytest.approx(0.7)
    assert cfg.vad_threshold_db == pytest.approx(9.0)
    assert cfg.llm_timeout == pytest.approx(10.0)
    assert cfg.ptt_key is None
    assert cfg.log_path is None
    assert cfg.debug is False


def test_env_overrides_and_cli_log_debug():
    env = _env(
        VOLC_ASR_RESOURCE_ID="asr-res",
        VOLC_TTS_VOICE="voice-x",
        VOLC_TTS_RESOURCE_ID="tts-res",
        ARK_BASE_URL="https://example.test/v3",
        OC2_VOICE_RATE="0.95",
        OC2_VOICE_VOLUME="0.4",
        OC2_VOICE_VAD_DB="4",
        OC2_LLM_TIMEOUT="20",
        OC2_VOICE_PTT_KEY="f8",
        OC2_VOICE_LOG="/tmp/env-voice.jsonl",
    )
    args = run_agent._parse_args(
        ["--voice", "--voice-debug", "--voice-log", "/tmp/cli-voice.jsonl"])
    cfg = run_agent._build_voice_config(args, env)
    assert cfg.asr_resource_id == "asr-res"
    assert cfg.tts_voice == "voice-x"
    assert cfg.tts_resource_id == "tts-res"
    assert cfg.ark_base_url == "https://example.test/v3"
    assert cfg.rate == pytest.approx(0.95)
    assert cfg.volume == pytest.approx(0.4)
    assert cfg.vad_threshold_db == pytest.approx(4.0)
    assert cfg.llm_timeout == pytest.approx(20.0)
    assert cfg.ptt_key == "f8"
    assert cfg.log_path == "/tmp/cli-voice.jsonl"
    assert cfg.debug is True


def test_env_log_path_used_without_cli_flag():
    cfg = run_agent._build_voice_config(
        run_agent._parse_args([]), _env(OC2_VOICE_LOG="/tmp/env-voice.jsonl"))
    assert cfg.log_path == "/tmp/env-voice.jsonl"


def test_invalid_rate_falls_back_to_default(capsys):
    cfg = run_agent._build_voice_config(
        run_agent._parse_args([]), _env(OC2_VOICE_RATE="fast"))
    assert cfg.rate == pytest.approx(1.05)
    assert "OC2_VOICE_RATE" in capsys.readouterr().err


def test_non_finite_rate_and_volume_fall_back(capsys):
    cfg = run_agent._build_voice_config(
        run_agent._parse_args([]),
        _env(OC2_VOICE_RATE="nan", OC2_VOICE_VOLUME="inf"))
    assert cfg.rate == pytest.approx(1.05)
    assert cfg.volume == pytest.approx(0.7)
    err = capsys.readouterr().err
    assert "OC2_VOICE_RATE" in err
    assert "OC2_VOICE_VOLUME" in err


def test_rate_and_volume_are_clamped(capsys):
    low = run_agent._build_voice_config(
        run_agent._parse_args([]),
        _env(OC2_VOICE_RATE="0.1", OC2_VOICE_VOLUME="-1"))
    assert low.rate == pytest.approx(0.5)
    assert low.volume == pytest.approx(0.3)
    high = run_agent._build_voice_config(
        run_agent._parse_args([]),
        _env(OC2_VOICE_RATE="5", OC2_VOICE_VOLUME="9"))
    assert high.rate == pytest.approx(2.0)
    assert high.volume == pytest.approx(1.2)
    err = capsys.readouterr().err
    assert "OC2_VOICE_RATE" in err
    assert "OC2_VOICE_VOLUME" in err


def test_explicit_voice_overrides_oc2_voice_zero():
    args = run_agent._parse_args(["--voice"])
    cfg = run_agent._build_voice_config(args, _env(OC2_VOICE="0"))
    assert cfg is not None


# ---------------------------------------------------------------- main 退出码

def test_main_explicit_voice_missing_keys_returns_2(monkeypatch, capsys):
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)
    # --no-env-file：避免自动加载仓库根 .env 把 key 补回来。
    assert run_agent.main(["--dry-run", "--voice", "--no-env-file"]) == 2
    assert "VOLC_ASR_API_KEY" in capsys.readouterr().err

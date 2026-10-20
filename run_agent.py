#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 陪玩主进程启动入口。

`ai/` 下刻意不放 `__main__`（tools/tree_viz.py 的约定：ai/ 是纯库），
所以入口放在仓库根。legacy 的入口是 `cd oc/ai && python3 -u agent.py`
（`oc/ai/agent.py` 末尾 `if __name__ == "__main__": Agent().run()`），
这里对应地提供一个根级入口。

先决条件：
  1. 游戏（Overcooked! 2）已用 BepInEx（run_game.sh，见 AGENTS.md §5.1）启动并进入关卡
     Sushi_1_4_2P；插件把状态推到 127.0.0.1:27411。
  2. 本进程在 macOS 上运行，且终端已获「辅助功能」权限（CGEvent 注入）。

用法：
    python3 run_agent.py             # 正式：连游戏 + 注入按键；有 key 时默认开语音
    python3 run_agent.py --dry-run   # 不注入按键（NullController），只看决策
    python3 run_agent.py --no-voice  # 关闭语音对话
    python3 run_agent.py --voice --voice-debug  # 显式开语音并打印对话调试信息

语音配置全部走环境变量（spec §6.5），见 `.env.example` / `docs/dialog_system.md`。
启动时默认自动读取仓库根 `.env`（已有环境变量优先）；可用 `--env-file PATH`
指定其它文件，或 `--no-env-file` 关闭。

Ctrl-C / SIGTERM 退出时会松开所有按键。
"""
import argparse
import math
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from ai.agent import Agent
from ai.dialog.env import load_env_file
from ai.dialog.schema import VoiceConfig

# 语音链路必需的环境变量；缺任意一个都拼不出完整链路
# （VAD→ASR→LLM→TTS），所以要么显式报错（--voice），要么退回无语音。
VOICE_REQUIRED_KEYS = (
    "VOLC_ASR_API_KEY",
    "VOLC_TTS_API_KEY",
    "ARK_API_KEY",
    "ARK_MODEL",
)

# 非必需项的默认值（spec §6.5 / §12）。
VOICE_DEFAULTS = {
    # 2.0 小时版；1.0 的 volc.bigasr.sauc.duration 需要单独开通。
    "VOLC_ASR_RESOURCE_ID": "volc.seedasr.sauc.duration",
    # 2.0 音色；1.0 的 zh_female_qingxin 与 seed-tts-2.0 不匹配。
    "VOLC_TTS_VOICE": "zh_female_vv_uranus_bigtts",
    "VOLC_TTS_RESOURCE_ID": "seed-tts-2.0",
    "ARK_BASE_URL": "https://ark.cn-beijing.volces.com/api/v3",
    "OC2_VOICE_RATE": 1.05,
    "OC2_VOICE_VOLUME": 0.7,
    "OC2_VOICE_VAD_DB": 9.0,
    "OC2_LLM_TIMEOUT": 10.0,
    "OC2_VOICE_PTT_KEY": "",
}


class VoiceConfigError(RuntimeError):
    """显式 `--voice` 却缺少必需环境变量时的配置错误。"""


def _parse_args(argv):
    ap = argparse.ArgumentParser(description="Overcooked! 2 AI 陪玩")
    ap.add_argument("--dry-run", action="store_true",
                    help="不注入按键（用 NullController），只跑决策")
    ap.add_argument('--seconds', type=float, help='限时运行后自动松键退出')
    ap.add_argument('--stop-file', type=Path)
    ap.add_argument('--pickup-key', default='right_shift', choices=('right_shift','right_ctrl','slash'))
    ap.add_argument('--chop-key', default='right_ctrl', choices=('right_shift','right_ctrl','slash'))
    # default=None 用来区分「没给 flag」与显式关闭；--no-voice 恒优先。
    ap.add_argument("--voice", dest="voice", action="store_true", default=None,
                    help="显式启用语音；缺必需环境变量时报错退出（退出码 2）")
    ap.add_argument("--no-voice", action="store_true",
                    help="强制关闭语音，忽略环境变量")
    ap.add_argument("--voice-debug", action="store_true",
                    help="把 ai.dialog 日志开到 DEBUG（ASR/LLM/命令/ack/播报；"
                         "不含分段计时）")
    ap.add_argument("--voice-log", metavar="PATH", default=None,
                    help="对话日志路径（默认读 OC2_VOICE_LOG，未设置则不落盘）")
    ap.add_argument("--env-file", metavar="PATH", default=None,
                    help="加载 KEY=VALUE 配置（默认仓库根 .env，存在时自动加载）")
    ap.add_argument("--no-env-file", action="store_true",
                    help="不自动加载 .env")
    args = ap.parse_args(argv)
    if args.seconds is not None and args.seconds <= 0:
        ap.error('--seconds 必须大于 0')
    if args.no_voice:
        # 「--no-voice 永远生效」：即使同时给了 --voice 也以关闭为准。
        args.voice = False
    return args


def _env_text(env, key, default=""):
    # type: (dict, str, str) -> str
    """读环境变量并去掉首尾空白；空值视为未设置。"""
    raw = env.get(key)
    if raw is None:
        return default
    raw = str(raw).strip()
    return raw or default


def _env_float(env, key, default, min_value=None, max_value=None):
    # type: (dict, str, float, Optional[float], Optional[float]) -> float
    """读浮点环境变量；非法/非有限值回退默认，超范围收敛到边界。"""
    raw = _env_text(env, key, "")
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        print("[run] 忽略无效的 %s=%r，使用默认值 %s" % (key, raw, default),
              file=sys.stderr)
        return default
    if not math.isfinite(value):
        print("[run] 忽略非有限的 %s=%r，使用默认值 %s" % (key, raw, default),
              file=sys.stderr)
        return default
    if min_value is not None and value < min_value:
        print("[run] %s=%s 低于下限 %s，已收敛到 %s"
              % (key, raw, min_value, min_value), file=sys.stderr)
        return min_value
    if max_value is not None and value > max_value:
        print("[run] %s=%s 高于上限 %s，已收敛到 %s"
              % (key, raw, max_value, max_value), file=sys.stderr)
        return max_value
    return value


def _auto_voice_requested(env):
    # type: (dict) -> bool
    """无 CLI flag 时是否希望开语音；`OC2_VOICE=0` 可显式关掉。"""
    raw = _env_text(env, "OC2_VOICE", "").lower()
    if not raw:
        return True
    return raw not in ("0", "false", "off", "no")


def _build_voice_config(args, env):
    # type: (argparse.Namespace, dict) -> Optional[VoiceConfig]
    """从 CLI + 环境变量构造 `VoiceConfig`（spec §6.3、§6.5）。

    行为：
    - `--no-voice`（`args.voice is False`）：恒返回 None；
    - `--voice`（True）：优先于 `OC2_VOICE=0`；缺任一必需 key 抛
      `VoiceConfigError`；
    - 无 flag（None）：`OC2_VOICE=0` 时关闭；否则 key 齐才开启，
      缺 key 打印告警并返回 None，主循环照常跑（无语音）。
    """
    if args.voice is False:
        return None
    env = env or {}
    if args.voice is None and not _auto_voice_requested(env):
        return None

    missing = [key for key in VOICE_REQUIRED_KEYS if not _env_text(env, key)]
    if missing:
        if args.voice is True:
            raise VoiceConfigError(
                "显式 --voice 但缺少必需环境变量: %s。"
                "补齐后重试，或去掉 --voice 退回无语音模式。"
                % ", ".join(missing))
        print("[run] 语音未启用：缺少环境变量 %s；以无语音模式继续"
              "（可用 --no-voice 关闭该提示）。" % ", ".join(missing),
              file=sys.stderr)
        return None

    return VoiceConfig(
        asr_key=_env_text(env, "VOLC_ASR_API_KEY"),
        asr_resource_id=_env_text(
            env, "VOLC_ASR_RESOURCE_ID", VOICE_DEFAULTS["VOLC_ASR_RESOURCE_ID"]),
        tts_key=_env_text(env, "VOLC_TTS_API_KEY"),
        tts_voice=_env_text(
            env, "VOLC_TTS_VOICE", VOICE_DEFAULTS["VOLC_TTS_VOICE"]),
        tts_resource_id=_env_text(
            env, "VOLC_TTS_RESOURCE_ID", VOICE_DEFAULTS["VOLC_TTS_RESOURCE_ID"]),
        ark_key=_env_text(env, "ARK_API_KEY"),
        ark_base_url=_env_text(
            env, "ARK_BASE_URL", VOICE_DEFAULTS["ARK_BASE_URL"]),
        ark_model=_env_text(env, "ARK_MODEL"),
        rate=_env_float(
            env, "OC2_VOICE_RATE", VOICE_DEFAULTS["OC2_VOICE_RATE"],
            min_value=0.5, max_value=2.0),
        volume=_env_float(
            env, "OC2_VOICE_VOLUME", VOICE_DEFAULTS["OC2_VOICE_VOLUME"],
            min_value=0.3, max_value=1.2),
        vad_threshold_db=_env_float(
            env, "OC2_VOICE_VAD_DB", VOICE_DEFAULTS["OC2_VOICE_VAD_DB"],
            min_value=2.0, max_value=20.0),
        llm_timeout=_env_float(
            env, "OC2_LLM_TIMEOUT", VOICE_DEFAULTS["OC2_LLM_TIMEOUT"],
            min_value=1.0, max_value=60.0),
        ptt_key=(_env_text(
            env, "OC2_VOICE_PTT_KEY",
            VOICE_DEFAULTS["OC2_VOICE_PTT_KEY"]) or None),
        log_path=(args.voice_log
                  or _env_text(env, "OC2_VOICE_LOG", "")
                  or None),
        debug=bool(args.voice_debug),
    )


def _load_env(args):
    # type: (argparse.Namespace) -> None
    """加载 .env；`--no-env-file` 关闭，`--env-file` 指定路径。

    默认读取仓库根 `.env`。已存在的环境变量优先，不会被文件覆盖；
    只报告新增数量，不打印任何值。
    """
    if args.no_env_file:
        return
    path = args.env_file or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        if args.env_file:
            raise VoiceConfigError("--env-file 指定的文件不存在: %s" % path)
        return
    try:
        added = load_env_file(path)
    except OSError as exc:
        raise VoiceConfigError("读取 %s 失败: %s" % (path, exc))
    print("[run] 已加载 %s（新增 %d 个变量，已有环境变量优先）"
          % (path, added), file=sys.stderr)


def main(argv=None):
    args = _parse_args(argv)

    try:
        _load_env(args)
    except VoiceConfigError as exc:
        print("[run] %s" % exc, file=sys.stderr)
        return 2

    # 先解析语音配置：--voice 缺 key 要在连游戏、要辅助功能权限之前就失败。
    try:
        voice = _build_voice_config(args, os.environ)
    except VoiceConfigError as exc:
        print("[run] %s" % exc, file=sys.stderr)
        return 2

    ctrl = None
    if not args.dry_run:
        try:
            from ai.output.controller import Controller
            ctrl = Controller(bindings={'pickup': args.pickup_key, 'chop': args.chop_key})
        except Exception as exc:               # noqa: BLE001
            print("[run] 初始化输入 controller 失败: %s" % exc,
                  file=sys.stderr)
            print("[run] Windows 请使用启动器匹配游戏权限；macOS 需要 pyobjc/辅助功能；"
                  "或先用 --dry-run 只跑决策。", file=sys.stderr)
            return 2

    try:
        agent = Agent(ctrl=ctrl, voice=voice)
    except OSError as exc:
        if ctrl is not None:
            ctrl.release_all()
        print("[run] 连接游戏失败（插件没起？127.0.0.1:27411）: %s" % exc,
              file=sys.stderr)
        return 1

    def _stop(signum, frame):
        agent.hands.release_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    agent.require_live_protocol = True
    print("[run] 已连接；等待新版地形协议、双人关卡和游戏前台后开始决策",
          file=sys.stderr)
    if voice is not None:
        print("[run] 语音已启用：音色=%s 日志=%s"
              % (voice.tts_voice, voice.log_path or "未落盘"),
              file=sys.stderr)
    try:
        agent.run(max_seconds=args.seconds,
                  stop_requested=args.stop_file.exists if args.stop_file else None)
    finally:
        agent.hands.release_all()
        agent.client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

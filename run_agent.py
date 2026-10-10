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
    python3 run_agent.py             # 正式：连游戏 + 注入按键
    python3 run_agent.py --dry-run   # 不注入按键（NullController），只看决策

Ctrl-C / SIGTERM 退出时会松开所有按键。
"""
import argparse
import signal
import sys

from ai.agent import Agent


def _parse_args(argv):
    ap = argparse.ArgumentParser(description="Overcooked! 2 AI 陪玩")
    ap.add_argument("--dry-run", action="store_true",
                    help="不注入按键（用 NullController），只跑决策")
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    ctrl = None
    if not args.dry_run:
        try:
            from ai.controller import Controller
            ctrl = Controller()
        except Exception as exc:               # noqa: BLE001
            print("[run] 初始化 CGEvent controller 失败: %s" % exc,
                  file=sys.stderr)
            print("[run] 需要 macOS + pyobjc，并给终端「辅助功能」权限；"
                  "或先用 --dry-run 只跑决策。", file=sys.stderr)
            return 2

    try:
        agent = Agent(ctrl=ctrl)
    except OSError as exc:
        print("[run] 连接游戏失败（插件没起？127.0.0.1:27411）: %s" % exc,
              file=sys.stderr)
        return 1

    def _stop(signum, frame):
        agent.hands.release_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print("[run] 已连接 127.0.0.1:27411，开始决策（Ctrl-C 退出）",
          file=sys.stderr)
    agent.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

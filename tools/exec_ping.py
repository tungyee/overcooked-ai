#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测 27412 exec 通道是否活着（发 ping 等回包）。

只验证「对面有没有活插件处理命令」，不移动角色、不读游戏状态。
典型用途：`restart.sh` 就绪判断，以及 P2 不动时区分「AI 没决策」和
「端口被卡死的旧游戏进程占着」。

    python3 tools/exec_ping.py                 # 默认 127.0.0.1:27412，2s 超时
    python3 tools/exec_ping.py --timeout 5

退出码：0 有回包（通道活着，旧插件回 not implemented 也算）；
        1 连不上或无响应。
"""
import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ai.exec.client import ExecClient


def main(argv=None):
    ap = argparse.ArgumentParser(description="探测 27412 exec 通道是否活着")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=27412)
    ap.add_argument("--timeout", type=float, default=2.0)
    args = ap.parse_args(argv)

    where = "%s:%d" % (args.host, args.port)
    client = ExecClient(host=args.host, port=args.port)
    try:
        client.connect()
        if not client.connected:
            print("exec 通道 %s 连不上（插件没起，或端口没监听）" % where)
            return 1
        reply = client.ping(args.timeout)
    finally:
        client.close()

    if reply is None:
        print("exec 通道 %s 无响应（监听者没处理命令；可能是卡死的旧游戏进程占着端口）"
              % where)
        return 1
    if reply.get("ok"):
        print("exec 通道 %s 正常" % where)
    else:
        print("exec 通道 %s 有响应（why=%s；旧插件也会回包，说明进程活着）"
              % (where, reply.get("why")))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""录制插件推送的原始 NDJSON 帧。

只读 socket，不注入任何按键——人类正常游玩时可安全运行。
录下的帧是 spec §11.2 阶段 0 要求的回归基线。

用法：
    python3 tools/record_frames.py --out tests/fixtures/sample_frames.ndjson --seconds 60
"""
import argparse
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai.record import FrameRecorder


def record(out_path, seconds, host, port):
    # type: (str, float, str, int) -> int
    s = socket.create_connection((host, port), timeout=10)
    buf = b""
    n = 0
    deadline = time.time() + seconds
    with FrameRecorder(out_path) as rec:
        try:
            while time.time() < deadline:
                data = s.recv(65536)
                if not data:
                    print("socket 已关闭")
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        rec.write(line)
                        n += 1
                        if n % 25 == 0:
                            print("已录 %d 帧" % n, end="\r")
        except KeyboardInterrupt:
            print("\n手动中断")
        finally:
            s.close()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="输出 NDJSON 路径")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=27411)
    args = ap.parse_args()

    n = record(args.out, args.seconds, args.host, args.port)
    print("\n共录 %d 帧 → %s" % (n, args.out))
    return 0 if n > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

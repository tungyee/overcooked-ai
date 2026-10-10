#!/usr/bin/env python3
"""带实时诊断的录帧工具（人工录制用）。

和 tools/record_frames.py 一样只读 socket、把原始帧 tee 到 NDJSON，
额外每隔 --log-every 秒打印一次两名厨师的状态摘要（位置是否在动、
手持物、面向的可交互物），方便边玩边确认录到了什么。

用法：
    python3 tools/record_frames_verbose.py --out tests/fixtures/full_level.ndjson --seconds 160
"""
import argparse
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai.record import FrameRecorder


def _chef_summary(c):
    # type: (dict) -> str
    nm = c.get("name")
    x, z = c.get("x"), c.get("z")
    held = c.get("held") or "-"
    inter = c.get("interactions") or {}
    facing = inter.get("grid") or "-"
    pos = "(%.1f,%.1f)" % (x, z) if x is not None and z is not None else "(?,?)"
    return "%-9s %s held=%-28s facing=%s" % (nm, pos, held, facing)


def record(out_path, seconds, host, port, log_every):
    # type: (str, float, str, int, float) -> int
    s = socket.create_connection((host, port), timeout=10)
    buf = b""
    n = 0
    deadline = time.time() + seconds
    next_log = time.time()
    prev_pos = {}
    delivered_seen = None
    with FrameRecorder(out_path) as rec:
        try:
            while time.time() < deadline:
                data = s.recv(65536)
                if not data:
                    print("\nsocket 已关闭")
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    rec.write(line)
                    n += 1
                    now = time.time()
                    if now >= next_log:
                        next_log = now + log_every
                        try:
                            d = json.loads(line.decode("utf-8", "replace"))
                        except ValueError:
                            continue
                        chefs = d.get("chefs") or []
                        remain = int(deadline - now)
                        print("\n[剩 %3ds | 已录 %d 帧]" % (remain, n))
                        for c in chefs:
                            nm = c.get("name")
                            x, z = c.get("x"), c.get("z")
                            moved = ""
                            has_pos = x is not None and z is not None
                            if nm in prev_pos and has_pos:
                                dx = abs(x - prev_pos[nm][0]) + abs(z - prev_pos[nm][1])
                                moved = "  ← 移动中" if dx > 0.05 else "  (静止)"
                            if has_pos:
                                prev_pos[nm] = (x, z)
                            print("   " + _chef_summary(c) + moved)
                        sc = d.get("score") or {}
                        deliv = sc.get("deliveries")
                        if deliv is not None and deliv != delivered_seen:
                            delivered_seen = deliv
                            print("   >>> 送餐数 deliveries = %s" % deliv)
        except KeyboardInterrupt:
            print("\n手动中断")
        finally:
            s.close()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--seconds", type=float, default=160.0)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=27411)
    ap.add_argument("--log-every", type=float, default=5.0, help="每隔几秒打印一次摘要")
    args = ap.parse_args()

    n = record(args.out, args.seconds, args.host, args.port, args.log_every)
    print("\n共录 %d 帧 → %s" % (n, args.out))
    return 0 if n > 0 else 1


if __name__ == "__main__":
    sys.exit(main())

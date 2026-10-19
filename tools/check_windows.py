"""本机连接诊断与限时录帧；只读，不注入按键。"""
import argparse
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.state.world import World


def main():
    ap = argparse.ArgumentParser(description="OC2 plugin read-only diagnostic")
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    if args.seconds <= 0:
        ap.error("--seconds must be positive")
    count = 0
    phases = set()
    chefs = set()
    levels = set()
    started = time.monotonic()
    output = None
    latest = None
    try:
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            output = args.output.open("w", encoding="utf-8")
        with socket.create_connection(("127.0.0.1", 27411), timeout=3) as connection:
            connection.settimeout(0.5)
            buffer = b""
            while time.monotonic() - started < args.seconds:
                try:
                    chunk = connection.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    raise EOFError("Game plugin disconnected")
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    latest = json.loads(line)
                    world = World(latest)
                    count += 1
                    phases.add(world.phase)
                    levels.add(latest.get("level"))
                    chefs.update(c.get("name") for c in world.chefs)
                    if output:
                        output.write(line.decode("utf-8") + "\n")
    finally:
        if output:
            output.close()
    report = {"frames": count, "seconds": round(time.monotonic() - started, 2),
              "phases": sorted(phases), "levels": list(levels),
              "chefs": list(chefs), "latest_phase": latest.get("phase") if latest else None,
              "latest_paused": latest.get("paused") if latest else None,
              "latest_chef_count": len(latest.get("chefs", [])) if latest else 0,
              "output": str(args.output.resolve()) if args.output else None}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if count else 1


if __name__ == "__main__":
    sys.exit(main())

"""给 PowerShell 启动器提供只读的进程完整性级别诊断。"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.output.windows_privileges import integrity_report

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    print(json.dumps(integrity_report(parser.parse_args().pid)))

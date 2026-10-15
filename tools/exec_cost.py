# -*- coding: utf-8 -*-
"""按 task / 移动阶段归因执行耗时：一步为什么要 7~10 秒。

AGENTS §10.1 曾记「移动空转 51%」，但 trace-20261014-204822 已不成立
（cruise 2379 拍里 stall ≥0.3s 只有 13 拍 = 1%）。那笔时间去哪了需要按阶段
拆，而不是继续归咎移动 —— 这个工具回答那个问题。

注意 ai/eta.py 的 COST_* 常数是**含**旧损耗标定的（见该模块注释），所以这里
的实测中位若明显低于 COST_*，说明常数该重标。

用法: python3 -I tools/exec_cost.py <trace.jsonl>
"""
import json
import sys
from collections import defaultdict


def _median(xs):
    # type: (list) -> float
    if not xs:
        return 0.0
    s = sorted(xs)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def main(path):
    # type: (str) -> int
    # ① 按 task 实例切段，记每段时长与终态
    segs = []                       # (task_type, ticks, status)
    phase_ticks = defaultdict(int)  # 移动阶段 -> 拍数
    phase_by_type = defaultdict(lambda: defaultdict(int))
    keys_idle = 0                   # 有 task 但一个键都没按的拍
    total = 0

    cur_task = None
    cur_type = None
    cur_start = None
    last_status = None

    with open(path) as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            total += 1
            ex = rec.get("exec") or {}
            task = ex.get("task")
            tick = rec.get("t", 0)

            if task:
                ttype = task.split("(")[0]
                phase = ((ex.get("motion") or {}).get("phase")) or "none"
                phase_ticks[phase] += 1
                phase_by_type[ttype][phase] += 1
                if not (rec.get("keys") or ()):
                    keys_idle += 1

            if task != cur_task:
                if cur_task is not None:
                    segs.append((cur_type, tick - cur_start, last_status))
                cur_task, cur_start = task, tick
                cur_type = task.split("(")[0] if task else None
            last_status = ex.get("status")

    if cur_task is not None:
        segs.append((cur_type, total - cur_start, last_status))

    print("总拍 %d" % total)

    print("\n== 每类 task 的实测耗时（拍）==")
    print("%-22s %5s %7s %7s %7s  %s"
          % ("TYPE", "段数", "中位", "最大", "合计", "终态"))
    by_type = defaultdict(list)
    status_of = defaultdict(lambda: defaultdict(int))
    for ttype, dur, status in segs:
        if ttype is None:
            continue
        by_type[ttype].append(dur)
        status_of[ttype][status or "?"] += 1
    for ttype in sorted(by_type, key=lambda k: -sum(by_type[k])):
        ds = by_type[ttype]
        print("%-22s %5d %7.0f %7d %7d  %s"
              % (ttype, len(ds), _median(ds), max(ds), sum(ds),
                 dict(status_of[ttype])))

    print("\n== 移动阶段占比（有 task 的拍）==")
    busy = sum(phase_ticks.values())
    for phase, n in sorted(phase_ticks.items(), key=lambda x: -x[1]):
        print("  %-10s %6d  %4.0f%%" % (phase, n, 100.0 * n / max(busy, 1)))
    print("  有 task 但未按键: %d 拍 (%.0f%%)"
          % (keys_idle, 100.0 * keys_idle / max(busy, 1)))

    print("\n== 耗时最大的两类 task 的阶段分解 ==")
    for ttype in sorted(by_type, key=lambda k: -sum(by_type[k]))[:2]:
        ph = phase_by_type[ttype]
        tot = sum(ph.values())
        parts = ", ".join("%s %.0f%%" % (p, 100.0 * n / max(tot, 1))
                          for p, n in sorted(ph.items(), key=lambda x: -x[1]))
        print("  %-22s %s" % (ttype, parts))

    idle = total - busy
    print("\n无 task（idle）: %d 拍 (%.0f%%)" % (idle, 100.0 * idle / max(total, 1)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

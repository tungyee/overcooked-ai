"""打分项的决定力分析：每拍比较冠军与第二名，看分差由哪些 term 贡献。

用法: python3 tools/score_decomp.py trace-xxx.jsonl [最多读多少拍]

回答两个问题：
① 每项的「活跃率」——有多少拍它对任一候选非零（不活跃的项等于不存在）
② 每项的「决定力」——冠军与第二名的分差里，它贡献了多少（含反向贡献）
"""
import json
import sys
from collections import defaultdict


def main(path, limit):
    # type: (str, int) -> None
    active = defaultdict(int)        # 项 -> 非零的拍数
    swing = defaultdict(float)       # 项 -> |冠军-第二| 的绝对贡献累计
    against = defaultdict(int)       # 项 -> 它偏向第二名（即反对最终选择）的拍数
    flips = defaultdict(int)         # 项 -> 单独去掉它就会换人的拍数
    ticks = 0
    margins = []

    for line in open(path):
        if ticks >= limit:
            break
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        cands = rec.get("candidates") or []
        # 只比可行候选：infeasible 的 terms 是空的，没打过分
        scored = [c for c in cands if c.get("ok") and c.get("terms")]
        if len(scored) < 2:
            continue
        scored.sort(key=lambda c: c.get("score", 0.0), reverse=True)
        win, run = scored[0], scored[1]
        ticks += 1
        margin = win.get("score", 0.0) - run.get("score", 0.0)
        margins.append(margin)

        keys = set(win["terms"]) | set(run["terms"])
        for k in keys:
            for c in scored:
                if abs(c.get("terms", {}).get(k, 0.0)) > 1e-9:
                    active[k] += 1
                    break
            d = win["terms"].get(k, 0.0) - run["terms"].get(k, 0.0)
            swing[k] += abs(d)
            if d < -1e-9:
                against[k] += 1
            # 去掉这一项后冠军是否还赢
            if margin - d <= 0:
                flips[k] += 1

    if not ticks:
        print("没有可比较的拍（候选 < 2）")
        return

    print("可比较拍数: %d   平均分差: %.2f   分差 <0.5 的拍: %.0f%%" % (
        ticks, sum(margins) / ticks,
        100.0 * sum(1 for m in margins if m < 0.5) / ticks))
    print()
    print("%-22s %8s %8s %8s %8s" % ("term", "活跃率", "决定力", "反对率", "翻盘率"))
    print("-" * 60)
    for k in sorted(swing, key=lambda x: -swing[x]):
        print("%-22s %7.0f%% %8.2f %7.0f%% %7.0f%%" % (
            k,
            100.0 * active[k] / ticks,
            swing[k] / ticks,
            100.0 * against[k] / ticks,
            100.0 * flips[k] / ticks))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 100000)

#!/usr/bin/env python3
"""离线对照插件 walk_probe（物理测量）与 Python terrain 判定的可走性。

插件 0.23.3+ 在会动网格关卡（气球篮子）的帧里带 walk_probe：每个厨师周围
±2 格、每格 3×3 子点用厨师碰撞体做 OverlapCapsule 的结果。本工具读 trace
的 state.map（terrain + walk_probe），对每格比较：

  - py：occupancy.make_walkable_from_terrain 的格中心判定（只用 terrain，
    不含 hazard / 运行时占用，接缝误判出在 terrain 这一段）
  - phys：子点掩码，中心子点 + 9 个子点里站得下的个数

分歧类型：
  FN_center  py 不可走、物理格中心站得下（外扩 CHEF_CLEARANCE 过大/AABB 放大）
  FN_offset  py 不可走、格中心也站不下、但有偏心子点站得下（缝在格中心之外）
  FP         py 可走、物理格中心站不下

用法：
  python3 tools/walk_probe_report.py trace-XXXX.jsonl            # 汇总
  python3 tools/walk_probe_report.py trace-XXXX.jsonl --at 6206  # 某拍的格子图
"""
from __future__ import print_function

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai.space import occupancy  # noqa: E402
from ai.space.geometry import GridMapping  # noqa: E402

FREE = "."


def iter_probes(path):
    """逐拍产出 (t, map, probe)，跳过与上一条相同的 probe（20 Hz 复用 5 Hz 帧）。"""
    last = None
    with open(path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            m = (rec.get("state") or {}).get("map") or {}
            probe = m.get("walk_probe")
            if not probe:
                continue
            key = json.dumps(probe, sort_keys=True)
            if key == last:
                continue
            last = key
            yield rec.get("t"), m, probe


def py_walkable(m, probe):
    terrain = occupancy.parse_terrain(m.get("terrain"))
    level_bounds = occupancy.parse_level_bounds(m.get("level_bounds"))
    mapping = GridMapping(probe["cell"], 0, 0, probe["ox"], probe["oz"])
    return occupancy.make_walkable_from_terrain(
        terrain, [], set(), mapping, level_bounds=level_bounds)


def cells(chef, sub):
    """产出 (gx, gz, 子点串, 阻挡下标串)；子点串 dz 外层、dx 内层。"""
    n = sub * sub
    for r, (row, who) in enumerate(zip(chef.get("rows") or [], chef.get("who") or [])):
        gz = chef["gz0"] + r
        for c in range(len(row) // n):
            yield chef["gx0"] + c, gz, row[c * n:(c + 1) * n], who[c * n:(c + 1) * n]


def classify(walk, gx, gz, mask, sub):
    center = mask[(sub * sub) // 2]
    free = mask.count(FREE)
    py = walk(gx, gz)
    if not py and center == FREE:
        return "FN_center"
    if not py and free:
        return "FN_offset"
    if py and center == "#":
        return "FP"
    return None


def blocker_digest(b):
    rot = b.get("rot") or [0, 0, 0]
    tilted = any(abs(((v + 45) % 90) - 45) > 1 for v in rot)
    return "%s %s layer=%s(%s)%s%s aabb=%s" % (
        b.get("path"), b.get("type"), b.get("layer"), b.get("layer_name"),
        "" if b.get("terrain") else " NOT_IN_TERRAIN",
        " TILTED" if tilted else "", b.get("aabb"))


def summarize(path):
    kinds = Counter()
    examples = {}
    self_hits = []
    blockers = {}
    frames = 0
    ms = []
    for t, m, probe in iter_probes(path):
        frames += 1
        ms.append(probe.get("ms") or 0)
        walk = py_walkable(m, probe)
        sub = probe.get("sub") or 3
        for chef in probe.get("chefs") or ():
            if chef.get("self") is not None:
                self_hits.append((t, chef.get("name"), chef.get("self")))
            for gx, gz, mask, who in cells(chef, sub):
                k = classify(walk, gx, gz, mask, sub)
                if k:
                    kinds[k] += 1
                    examples.setdefault(k, []).append((t, chef.get("name"), gx, gz, mask))
        for b in probe.get("blockers") or ():
            blockers.setdefault(blocker_digest(b), 0)
            blockers[blocker_digest(b)] += 1
    print("probe 帧数: %d  平均耗时: %.2f ms  最大: %.2f ms" % (
        frames, sum(ms) / len(ms) if ms else 0, max(ms) if ms else 0))
    if not frames:
        print("trace 里没有 walk_probe（插件 <0.23.3 或该关没有会动网格）")
        return
    print("分歧计数:", dict(kinds) or "无")
    for k, ex in sorted(examples.items()):
        print("\n[%s] 前 10 例 (t, chef, gx, gz, 子点):" % k)
        for e in ex[:10]:
            print("  t=%s %s (%d,%d) %r" % e)
    print("\n原地自检被挡: %d 次%s" % (len(self_hits), "  例: %r" % (self_hits[:5],) if self_hits else ""))
    print("\n阻挡体（出现帧数）:")
    for d, c in sorted(blockers.items(), key=lambda kv: -kv[1])[:30]:
        print("  %4d  %s" % (c, d))


def show_at(path, tick):
    best = None
    for t, m, probe in iter_probes(path):
        if t is not None and t <= tick:
            best = (t, m, probe)
        elif best is not None:
            break
    if best is None:
        print("t<=%d 没有 walk_probe" % tick)
        return
    t, m, probe = best
    walk = py_walkable(m, probe)
    sub = probe.get("sub") or 3
    print("t=%s  每格: 左=物理子点(z 从上到下递减) 右上角=py 判定(o 可走/x 不可走)" % t)
    for chef in probe.get("chefs") or ():
        print("\n%s x=%s z=%s body=%s self=%s" % (
            chef.get("name"), chef.get("x"), chef.get("z"), chef.get("body"), chef.get("self")))
        grid = {}
        for gx, gz, mask, who in cells(chef, sub):
            grid[(gx, gz)] = (mask, who)
        if not grid:
            continue
        xs = sorted({g[0] for g in grid})
        zs = sorted({g[1] for g in grid}, reverse=True)
        print("      " + " ".join("gx=%-3d" % x for x in xs))
        for gz in zs:
            for sz in reversed(range(sub)):
                line = []
                for gx in xs:
                    mask, _ = grid[(gx, gz)]
                    seg = mask[sz * sub:(sz + 1) * sub]
                    tag = ("o" if walk(gx, gz) else "x") if sz == sub - 1 else " "
                    line.append("%s%s   " % (seg, tag))
                print("%5s " % ("gz=%d" % gz if sz == sub // 2 else "") + " ".join(line))
            print()
    for i, b in enumerate(probe.get("blockers") or ()):
        print("  [%s] %s" % ("0123456789abcdefghijklmnopqrstuvwxyz"[i] if i < 36 else "+", blocker_digest(b)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("trace")
    ap.add_argument("--at", type=int, help="打印该拍（或之前最近一条）的格子图")
    args = ap.parse_args()
    if args.at is not None:
        show_at(args.trace, args.at)
    else:
        summarize(args.trace)


if __name__ == "__main__":
    main()

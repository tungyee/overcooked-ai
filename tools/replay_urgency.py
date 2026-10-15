# -*- coding: utf-8 -*-
"""用 trace 里的 state 快照重放打分，检查 urgency 是否仍与到期顺序倒挂。

trace 的每条记录都带整帧 state，所以能离线重建 World 再跑 planner.plan()，
回答「同样世界状态下现在会选什么」。注意它**不**模拟物理执行（见 AGENTS §10.1
最后一条），所以只用来验证选择分布，不用来预测实机成绩。

用法: python3 -I tools/replay_urgency.py <trace.jsonl> [最多采样帧数]

AGENTS.md §10.2 规定的改权重三道闸之一（「urgency 是否仍单调，倒挂须为
0%」）。分层（spec §5）接进 plan() 之后，候选元组尾部多了 layer /
in_top_layer 两项，且只有最高层候选带非空 terms——可比候选组因此会显著
变少。样本不足时倒挂率会因「数据消失」而读作 0%，这正是 §10.2 自己警告
的「自欺的修复」：一个会因数据消失而变绿的闸比没有闸更危险。所以本工具
在样本不足时显式报结论无效，不输出一个好看但没有意义的百分比。
"""
import json
import re
import sys

from ai import planner
from ai.bb_factory import make_bb
from ai.recipes import RecipeBook
from ai.world import World

TASK_RE = re.compile(r"(\w+)\((.*)order#(\d+)\)")

# 样本量护栏：分层后只有最高层候选带 terms，可比候选组会显著减少。
# 若样本太少，倒挂率会因「数据消失」而读作 0% —— 那是 AGENTS.md §10.2
# 警告的「自欺的修复」：一个会因数据消失而变绿的闸比没有闸更危险。
MIN_GROUPS = 50   # 分层前本工具在一局 400 帧上能取到数千组；50 是宽松下限


def main(path, limit=400):
    # type: (str, int) -> int
    inverted = total = 0
    frames = 0
    with open(path) as fh:
        for line in fh:
            if frames >= limit:
                break
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            state = rec.get("state")
            if not state or len(state.get("orders") or ()) < 2:
                continue
            frames += 1
            world = World(state)
            book = RecipeBook(world.recipes, world.stations)
            bb = make_bb()
            bb.tick = rec.get("t", 0)
            _task, record = planner.plan(world, bb, None, book)

            remaining = {str(o["id"]): o["remaining"] for o in state["orders"]}
            # 按 (TYPE, 料) 分组，组内只有 order 不同 —— 其余打分项基本同值，
            # 于是 urgency 的方向可以被单独看出来。
            groups = {}
            for cand in record["candidates"]:
                # 变长解包：分层后候选元组是 8 项（尾部多 layer /
                # in_top_layer），plan_firefighting 仍发 6 项。只取前 6 项，
                # 与 ai/trace.py 的 _format_candidates 同款兼容。
                if len(cand) < 6:
                    continue
                task, _score, terms, ok, _veto, _chosen = cand[:6]
                if not ok or "urgency" not in terms:
                    continue
                m = TASK_RE.match(repr(task))
                if not m:
                    continue
                key = (m.group(1), getattr(task, "item", None))
                groups.setdefault(key, {})[m.group(3)] = terms["urgency"]

            for _key, by_order in groups.items():
                pairs = sorted((remaining[o], u)
                               for o, u in by_order.items() if o in remaining)
                if len(pairs) < 2 or pairs[0][0] == pairs[-1][0]:
                    continue
                total += 1
                # remaining 最小者的 urgency 应 >= remaining 最大者
                if pairs[0][1] < pairs[-1][1]:
                    inverted += 1

    if total < MIN_GROUPS:
        print("采样帧 %d，可比候选组仅 %d（下限 %d）。"
              "【结论无效】分层后只有最高层候选带 terms，样本不足时倒挂率"
              "无意义——不要把它当通过。"
              % (frames, total, MIN_GROUPS))
        return 0

    pct = inverted / total if total else 0.0
    print("采样帧 %d，可比候选组 %d，倒挂 %d (%.0f%%)"
          % (frames, total, inverted, pct * 100))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 400))

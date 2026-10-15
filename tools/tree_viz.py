#!/usr/bin/env python3
"""离线冒烟：从一帧快照构建全部 task 的树、打印 ASCII、走分支不按键。

用法:
  python3 tools/tree_viz.py tests/fixtures/real_frame_sushi_1_4.json
  python3 tools/tree_viz.py frames.ndjson.gz --frame 100
  python3 tools/tree_viz.py <帧文件> --tick 5      # 每棵树多走几拍

这是 Plan 2 的交付验证（spec §11.2 阶段 3）："打印树、假状态走分支、
**不注入按键**"。

与 oc/ai/tree_viz.py 的差异：旧版靠**猴补丁**绕开真实 controller
（import agent as A; A.Controller = NoopCtrl; A.StateClient = DummyClient）。
新版不需要——Hands 构造时注入 controller（见 ai/hands.py 的设计理由），
NullController 是正常参数。旧版还依赖 ag._build_tree(w, order) 这个
Agent 方法，而新架构里树由每个 task 自己 build()。

放 tools/ 而非 ai/：这是开发工具不是运行时模块，与 tools/record_frames.py
同列。spec §8.2 把 tree_viz.py 列在 ai/ 旁边，但那张表记的是职责归属
而非目录——ai/ 下不该有 __main__ 脚本。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))

from ai import primitives as PR            # noqa: E402
from ai import tasks as T                  # noqa: E402
from ai.blackboard import Blackboard       # noqa: E402
from ai.bt import Node, Status             # noqa: E402
from ai.hands import Hands                 # noqa: E402
from ai.navigation import distance_field   # noqa: E402
from ai.predicates import P2_NAME, chef_by_name   # noqa: E402
from ai.recipes import RecipeBook          # noqa: E402
from ai.world import World                 # noqa: E402


class NullController(object):
    """记录调用但不碰键盘。

    注意它**不是** oc/ai/tree_viz.py 的 NoopCtrl：那个是为了猴补丁
    Agent.Controller 而存在的空壳，这个是 Hands 的正常注入参数，并且
    记下调用序列供断言。
    """

    def __init__(self):
        # type: () -> None
        self.calls = []

    def tap(self, name, hold=0.12):
        self.calls.append(("tap", name))

    def press(self, name):
        self.calls.append(("press", name))

    def release(self, name):
        self.calls.append(("release", name))

    def release_all(self):
        self.calls.append(("release_all",))


# ---------- 渲染 ----------

def label(node):
    # type: (Node) -> str
    """节点的一行标签。

    每个原语最想知道的参数不同：Tap 是按哪个键，HoldUntil 还要知道
    最多等多久（timeout 写错会让切菜永远切不完），InteractWith 要知道
    是 pickup 还是 placement（放错句柄会把盘丢到出餐口）。
    """
    cls = type(node).__name__
    if isinstance(node, PR.Timeout):
        return "Timeout(%d)" % node.max_ticks
    if isinstance(node, PR.HoldUntil):
        return "HoldUntil(%s, timeout=%d)" % (node.key, node.timeout)
    if isinstance(node, PR.TapWhileHolding):
        return "TapWhileHolding(tap=%s)" % node.tap_key
    if isinstance(node, PR.Tap):
        return "Tap(%s)" % node.key
    if isinstance(node, PR.InteractWith):
        return "InteractWith(%s)" % (node.kind or "-")
    if isinstance(node, PR.Approach):
        return "Approach(%s)" % (node.kind or "-")
    if isinstance(node, PR.Settle):
        return "Settle(%d)" % node.ticks
    if isinstance(node, PR.Nudge):
        return "Nudge(%s, %d)" % (node.direction, node.ticks)
    return cls


def render(node, prefix="", is_last=True, lines=None):
    # type: (Node, str, bool, list) -> list
    """ASCII 树。Timeout 与 InteractWith 的内部子节点也展开。"""
    lines = lines if lines is not None else []
    connector = "└─ " if is_last else "├─ "
    lines.append(prefix + connector + label(node))
    child_prefix = prefix + ("   " if is_last else "│  ")

    children = list(getattr(node, "children", None) or ())
    if isinstance(node, PR.Timeout):
        children = [node.child]
    elif isinstance(node, PR.InteractWith):
        children = node._children()

    for i, c in enumerate(children):
        render(c, child_prefix, i == len(children) - 1, lines)
    return lines


# ---------- 装配 ----------

def make_ctx(world, chef=None, tick=0, with_controller=False):
    # type: (World, dict, int, bool) -> object
    """从真实 World 装配原语要的 ctx（契约见 ai/primitives.py 头注释）。

    控制的是 P2——认错厨师就是控制错对象（spec §11.2.1 已知缝隙表把
    "chefs[].slot 全为 null"记为 Plan 3 接入时要验的项，这里按 name 取）。
    """
    if chef is None:
        chef = chef_by_name(world.chefs, P2_NAME)
    if chef is None:
        # 没有 P2 的帧（菜单画面等）：取任一带坐标的厨师，纯为渲染
        for c in world.chefs:
            if c.get("x") is not None:
                chef = c
                break
    if chef is None:
        chef = {"name": P2_NAME, "x": 0.0, "z": 0.0, "held": None,
                "interactions": {}}

    ctrl = NullController()
    start = world.to_grid(chef.get("x") or 0.0, chef.get("z") or 0.0)
    ctx = {
        "w": world,
        "chef": chef,
        "hands": Hands(ctrl),
        "bb": Blackboard(),
        "field": distance_field(start, world.walkable, world.belt_dir),
        "book": RecipeBook(world.recipes, world.stations),
        "tick": tick,
    }
    if with_controller:
        return ctx, ctrl
    return ctx


def _first(seq, default=None):
    for x in seq or ():
        return x
    return default


def placeholder_task(cls):
    # type: (type) -> T.Task
    """用占位资源构造某类型的一个实例，只为验证 build() 与谓词不抛。

    enumerate_tasks 只产出当前帧可行的 task；这个函数强制覆盖全部 15 个
    类型，免得某个 build 写错却因当前帧不触发而逃过冒烟。
    """
    needs = {
        "PlaceCleanPlate": ("p0", (0, 0)),
        "ResetPlate": ("p0", (0, 0)),
        "WashPlate": ("sink0",),
        "FetchToCooker": ("cooker0",),
        "TakeCookedToPlate": ("cooker0", "p0"),
        "ReturnPot": ("cooker0",),
        "RescueCooked": ("cooker0",),
        "Discard": ("cooker0",),
        "RecoverLoosePot": ("cooker0",),
        "Extinguish": (),
        "FetchToBoard": ("board0",),
        "Chop": ("board0",),
        "FetchToPlate": ("p0",),
        "DeliverPlate": ("p0",),
        "StashHeld": (),
        "Prefetch": (),
    }
    return cls(order_id="0", resources=needs.get(cls.TYPE, ()),
               item="Cucumber")


def enumerate_tasks(ctx):
    # type: (dict) -> list
    """对当前帧枚举可构造的 task 实例（绑定真实资源）。

    这不是 Planner 的候选生成（那是 Plan 3，还要过可行性/协作/打分）。
    这里只求覆盖面：每类资源取一个真实实例，让冒烟能走到多种树。
    """
    w = ctx["w"]
    book = ctx["book"]
    out = []

    plate = _first(w.plates)
    plate_id = plate.get("n") if plate else None
    cooker = _first(w.cookers)
    cooker_id = cooker.get("n") if cooker else None
    board = _first([s for s in w.stations if s.get("t") == "chopping"])
    board_id = board.get("n") if board else None
    sink = _first([s for s in w.stations if s.get("t") == "washing"])
    sink_id = sink.get("n") if sink else None
    counter_cell = (0, 0)
    for s in w.stations:
        if s.get("t") == "counter" and s.get("gx") is not None:
            counter_cell = (s["gx"], s["gz"])
            break

    order = _first(w.orders) or {}
    oid = order.get("id")
    reqs = book.requirements(order)

    if plate_id is not None:
        out.append(T.PlaceCleanPlate(oid, (plate_id, counter_cell)))
        out.append(T.ResetPlate(None, (plate_id, counter_cell)))
        out.append(T.DeliverPlate(oid, (plate_id,)))
        for ing in reqs:
            out.append(T.FetchToPlate(oid, (plate_id,), ing))
    if sink_id is not None:
        out.append(T.WashPlate(resources=(sink_id,)))
    if cooker_id is not None:
        out.append(T.ReturnPot(resources=(cooker_id,)))
        out.append(T.RescueCooked(resources=(cooker_id,)))
        out.append(T.Discard(resources=(cooker_id,)))
        out.append(T.RecoverLoosePot(resources=(cooker_id,),
                                     item="utensil_pot_01"))
        for ing in reqs:
            if book.needs_cooking(ing):
                out.append(T.FetchToCooker(oid, (cooker_id,), ing))
                if plate_id is not None:
                    out.append(T.TakeCookedToPlate(
                        oid, (cooker_id, plate_id), ing))
    if board_id is not None:
        for ing in reqs:
            if book.needs_chopping(ing):
                out.append(T.FetchToBoard(oid, (board_id,), ing))
                out.append(T.Chop(oid, (board_id,), ing))
    out.append(T.StashHeld())
    for ing in reqs:
        out.append(T.Prefetch(item=ing))
    return out


# ---------- 命令行 ----------

def load_frame(path, index=0):
    # type: (str, int) -> dict
    """读一帧。.json 当单帧，.ndjson/.gz 按行取第 index 帧。"""
    if path.endswith(".json"):
        with open(path) as f:
            return json.load(f)
    from ai.replay import load_frames
    frames = load_frames(path)
    if not frames:
        raise ValueError("帧文件为空: %s" % path)
    return frames[min(index, len(frames) - 1)]


def main(argv=None):
    # type: (list) -> int
    ap = argparse.ArgumentParser(description="离线构树与冒烟（不注入按键）")
    ap.add_argument("frame", help="帧文件（.json / .ndjson / .ndjson.gz）")
    ap.add_argument("--frame-index", type=int, default=0,
                    help="ndjson 里取第几帧")
    ap.add_argument("--tick", type=int, default=0,
                    help="每棵树走几拍（默认 0，只打印不走）")
    args = ap.parse_args(argv)

    if not os.path.exists(args.frame):
        sys.stderr.write("帧文件不存在: %s\n" % args.frame)
        return 2

    world = World(load_frame(args.frame, args.frame_index))
    ctx, ctrl = make_ctx(world, with_controller=True)

    print("关卡 %s  phase=%s  订单 %r"
          % (world.level, world.phase,
             [o.get("dish") for o in world.orders]))
    print("P2 @ %r  held=%r"
          % (ctx["chef"].get("x"), ctx["chef"].get("held")))
    print("")

    tasks = enumerate_tasks(ctx)
    for t in tasks:
        ok = t.feasible(ctx)
        done = t.done(ctx)
        flag = "✓" if ok else ("·" if not done else "✔done")
        print("%s %r" % (flag, t))
        print("\n".join("    " + l for l in render(t.build())))
        if args.tick > 0:
            tree = t.build()
            for _ in range(args.tick):
                tree.tick(ctx)
                ctx["hands"].tick()
            tree.halt()
        print("")

    print("共 %d 个 task，可行 %d 个"
          % (len(tasks), sum(1 for t in tasks if t.feasible(ctx))))
    print("按键注入：%d 次调用，全部落在 NullController（零真实按键）"
          % len(ctrl.calls))
    if ctx["hands"].held_keys or ctx["hands"].move_dir:
        sys.stderr.write("⚠️ 有残留按键状态：held=%r move=%r\n"
                         % (ctx["hands"].held_keys, ctx["hands"].move_dir))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

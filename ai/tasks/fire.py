"""灭火 task：Extinguish（spec §6.1 第 6.5 号 / §6.5）。

本模块只放灭火这一组。**顺序是关键**：先把锅从灶台上端下来、就近放到
台面/地上，再去拿灭火器喷——锅留在灶上会一直烧，只喷不端灭不掉（用户
实机确认）。外部不直接 import 本模块，一律 `from ai import tasks as T`
（分组边界是实现细节，见 ai/tasks/__init__.py）。
"""
from typing import Dict, Optional

from ai import predicates as P
from ai import primitives as PR
from ai.bt import Condition, Guard, Node, RepeatUntil, Selector, Sequence
from ai.tasks.base import Task, _register, cooker_station

# 灭火喷射最长时长（拍）。喷射与切菜同键，但灭火器几秒就该扑灭一灶火，
# 超过这个时长说明没对准/被推离，交给 Sequence 重置重新 Approach。
EXTINGUISH_TIMEOUT_TICKS = 240

# 整棵灭火树的兜底上限（拍）。灭火是**两趟差事**（端锅 → 放锅 → 取灭火器
# → 回去喷），比普通 task 长：根目录旧 trace 里灭火那一段连续跑了 750+ 拍
# （默认 300 拍兜底、反复超时重建）。声明成 task 级 TIMEOUT_TICKS 让 Executor
# 用这个值兜底（见 ai/executor.py）；超时只是安全网，每一步都有 expect，
# 重建后从断点接着做。
FIREFIGHT_TIMEOUT_TICKS = 800


def _available_extinguisher(ctx):
    # type: (Dict) -> Optional[Dict]
    """找没被任何人拿着的灭火器（就近取第一个可用的）。"""
    w = ctx["w"]
    best = None
    cx, cz = ctx["chef"].get("x"), ctx["chef"].get("z")
    for e in w.extinguishers:
        held = str(e.get("held") or "").lower()
        if held not in ("", "player 2"):
            continue
        if best is None:
            best = e
            continue
        if cx is not None and cz is not None:
            bx, bz = e.get("x"), e.get("z")
            ox, oz = best.get("x"), best.get("z")
            if None not in (bx, bz, ox, oz) and \
                    (bx - cx) ** 2 + (bz - cz) ** 2 < (ox - cx) ** 2 + (oz - cz) ** 2:
                best = e
    return best


@_register
class Extinguish(Task):
    """第 6.5 号：灭火——全局 task，一棵树循环处理所有着火灶。

    一局火情 = 一个 task = 一棵树，不再按灶拆分：换灶不重建树、不丢灭火器
    （旧实现「一灶一 task」换目标就重建树，树首 EnsureHandsFree 会把端着的
    灭火器放下，实机复现）。

    顺序（用户实机确认的机制）：**先端所有带锅的着火灶，再逐灶喷**——
    锅留在灶上会一直烧、火不断复燃，只喷不端灭不掉。喷的过程中用 Guard
    每拍盯「有没有新着火的锅」，出现就中断喷、转去端锅。

    树结构：
      RepeatUntil(没有着火的灶了, Selector[
        带锅 → 清手 → 端锅 → 放锅（home_cooker=False 不回灶台）,
        有火 → 清手(keep 灭火器) → 取灭火器 → 走近 → 转向
               → Guard(没有新锅, HoldUntil(chop, 火全灭, target=下一灶)),
      ])

    喷射与切菜同键（P2 分屏 WorkstationInteract）。换下一灶由 HoldUntil 的
    target 重解析触发：当前灶一灭，target 指向下一灶、interact_ready 变假
    → 松键 FAILURE → 树重建 → 从 Selector 重新评估，去喷下一灶。
    """

    TYPE = "Extinguish"
    # 一局火情是循环树，比普通 task 长，用专属上限兜底（默认 200 拍会中途砍树）。
    TIMEOUT_TICKS = FIREFIGHT_TIMEOUT_TICKS
    # 火不能因为连续失败就被放弃——灭火没有"先去做别的任务"这个选项。
    # Agent 的 L3 兜底（连续失败即拉黑认领资源）碰到这个标记就跳过本 task。
    NO_ABANDON = True

    def claims(self):
        # 全局灭火 task 不按灶认领：火情协调靠 arbiter 的 _is_fire_unhandled
        # （P1 是否在旁 / 持灭火器），不靠 claims（spec §5.3）。
        return ()

    def done(self, ctx):
        # type: (Dict) -> bool
        return not any(P.cooker_burning(c) for c in ctx["w"].cookers)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        return any(P.cooker_burning(c) for c in ctx["w"].cookers)

    def build(self):
        # type: () -> Node
        def burning(c):
            # 所有着火 / 烧焦的灶，**按离 P2 的距离排序**。
            #
            # 排序是必须的，不是优化：下面 pot_station / fire_station 都取
            # 第 0 个，若用 world.cookers 的列表序，新灶着火就会把目标瞬间
            # 翻转到别处——实机 trace-20261014-011202 t=2404~2478：P2 端着
            # 灭火器已经走到灶 (2) 跟前（goal 已到 (6,1)），灶 (3) 跟着着火，
            # 目标立刻跳成 (7,-2)，人被拖去灶 (3)，喷了 50 拍两口火都没灭。
            # 按距离排序后「已经站在跟前的那口」天然最近，不会被抢走。
            out = [cc for cc in c["w"].cookers if P.cooker_burning(cc)]
            cx, cz = c["chef"].get("x"), c["chef"].get("z")
            if cx is None or cz is None:
                return out
            def d2(cc):
                x, z = cc.get("x"), cc.get("z")
                if x is None or z is None:
                    return float("inf")
                return (x - cx) ** 2 + (z - cz) ** 2
            return sorted(out, key=d2)

        def with_pot(c):
            # 只有「糊锅」（锅里的食物烧糊、pot.IsBurning()=True）才需要先端走；
            # 空锅/生锅的 IsBurning 是 false——即便灶台 onFire，直接喷灶火就能灭，
            # 不用端（实机：空锅不端火也灭；上一局卡在端空锅上导致火一直没灭）。
            return [cc for cc in burning(c) if cc.get("burning") is True]

        def any_fire(c):
            return bool(burning(c))

        def any_pot(c):
            return bool(with_pot(c))

        def pot_station(c):
            cc = with_pot(c)
            return cooker_station(c, cc[0]["n"]) if cc else None

        def fire_station(c):
            cc = burning(c)
            return cooker_station(c, cc[0]["n"]) if cc else None

        def ext(c):
            return _available_extinguisher(c)

        def holding_ext(c):
            return "extinguish" in P.held_name(c["chef"])

        def holding_pot(c):
            return P.held_is_cookware(c["chef"], c["w"].recipes)

        return PR.Timeout(RepeatUntil(lambda c: not any_fire(c), Selector([
            # 分支1：还有带锅的着火灶 → 端锅离灶 + 放下（不许回灶台）。
            # 端锅的 expect 取「手上有锅」：端下即成立，不绑定具体灶名，
            # 换灶不换树。
            Sequence([
                Condition(any_pot),
                PR.EnsureHandsFree(home_cooker=False),
                PR.InteractWith(pot_station, "pickup", holding_pot),
                PR.EnsureHandsFree(home_cooker=False),
            ]),
            # 分支2：没有带锅的了 → 取灭火器逐灶喷；喷时 Guard 每拍盯新锅，
            # 出现就 halt 喷、FAILURE 重建，回到分支1 去端锅。
            Sequence([
                Condition(any_fire),
                PR.EnsureHandsFree(keep=holding_ext),
                PR.InteractWith(ext, "pickup", holding_ext),
                PR.Approach(fire_station),
                PR.Face(fire_station),
                Guard(lambda c: not any_pot(c),
                      PR.HoldUntil("chop", lambda c: not any_fire(c),
                                   timeout=EXTINGUISH_TIMEOUT_TICKS,
                                   target=fire_station)),
            ]),
        ])), FIREFIGHT_TIMEOUT_TICKS)

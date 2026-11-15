"""灭火 task：Extinguish（spec §6.1 第 6.5 号 / §6.5）。

本模块只放灭火这一组。**顺序是关键**：先把锅从灶台上端下来、就近放到
台面/地上，再去拿灭火器喷——锅留在灶上会一直烧，只喷不端灭不掉（用户
实机确认）。外部不直接 import 本模块，一律 `from ai import tasks as T`
（分组边界是实现细节，见 ai/tasks/__init__.py）。
"""
from typing import Dict, List, Optional, Tuple

from ai.query import predicates as P
from ai import primitives as PR
from ai.exec.bt import Condition, Guard, Node, RepeatUntil, Selector, Sequence
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
    """找可用的灭火器（就近取第一个）。

    World 已把手持灭火器拆到 held_extinguishers，这里只遍历非手持的
    extinguishers（挂墙/地上），不会去追别人手里的。
    """
    w = ctx["w"]
    best = None
    cx, cz = ctx["chef"].get("x"), ctx["chef"].get("z")
    for e in w.extinguishers:
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


def _extinguisher_target(w, ext):
    # type: (Any, Optional[Dict]) -> Optional[Dict]
    """灭火器的 pick 目标：挂/放在台面上时用台面站点，否则用灭火器自己。

    灭火器挂在气球篮的 countertop 上时，游戏的 pickup 句柄是承载它的台面、
    不是灭火器自己（trace-20261115-213622 Balloon_2_3_2P：pickup=countertop(3)
    而非 extinguisher）。按位置就近解析到台面站点，pick 的目标名才跟句柄对得上；
    挂墙/放地上（没有同位置站点）就原样返回灭火器。
    """
    if not ext:
        return None
    x, z = ext.get("x"), ext.get("z")
    if x is None or z is None:
        return ext
    best = None
    best_d = 0.6 * 0.6  # 半格以内才算「挂在这台面上」
    for st in w.stations:
        sx, sz = st.get("x"), st.get("z")
        if sx is None or sz is None:
            continue
        d = (sx - x) ** 2 + (sz - z) ** 2
        if d < best_d:
            best_d = d
            best = st
    return best if best is not None else ext


def _burning_targets(world):
    # type: (Any) -> List[Dict]
    """所有在烧目标：烧灶（cookers 里 onFire/burning）+ 烧柜台/砧板/工作台。

    灶的着火走 cooker_burning（burning=糊锅 / onFire=灶火两个信号）；柜台没有锅，
    只有插件报的 onFire（见 station_burning）。灶在 stations[] 里也有 t="cooking"
    的条目，这里排除掉避免和 cookers 重复计数。
    """
    pts = [cc for cc in world.cookers if P.cooker_burning(cc)]
    pts += [s for s in world.stations
            if s.get("t") not in ("cooking",) and P.station_burning(s)]
    return pts


def _fire_centroid(points):
    # type: (List[Dict]) -> Optional[Tuple[float, float]]
    """起火点集合的团重心（x,z 均值）。空或全部缺坐标返回 None。"""
    xs = [p.get("x") for p in points if p.get("x") is not None]
    zs = [p.get("z") for p in points if p.get("z") is not None]
    if not xs or not zs:
        return None
    return (sum(xs) / len(xs), sum(zs) / len(zs))


@_register
class Extinguish(Task):
    """第 6.5 号：灭火——全局 task，一棵树循环处理所有着火目标（灶 + 柜台）。

    一局火情 = 一个 task = 一棵树，不再按灶拆分：换灶不重建树、不丢灭火器
    （旧实现「一灶一 task」换目标就重建树，树首 EnsureHandsFree 会把端着的
    灭火器放下，实机复现）。

    着火目标两种：灶（cookers 的 onFire/burning，带锅才要端）和普通柜台/砧板/
    工作台（stations 的 onFire，没有锅、直接喷）。`_burning_targets` 把它们合到
    一起；`burning()` 按「离团重心最近」排序，让 P2 站到整团火的中心旁喷，
    而不是站到最靠边那口。

    顺序（用户实机确认的机制）：**先端所有带锅的着火灶，再逐个起火目标喷**——
    锅留在灶上会一直烧、火不断复燃，只喷不端灭不掉。喷的过程中用 Guard
    每拍盯「有没有新着火的锅」，出现就中断喷、转去端锅。

    树结构：
      RepeatUntil(没有着火的灶了, Selector[
        带锅 → 清手 → 端锅 → 放锅（home_cooker=False 不回灶台）,
        有火 → 清手(keep 灭火器) → 取灭火器 → 走近 → 转向
               → Guard(没有新锅, HoldUntil(chop, 这口灭了, target=下一灶)),
      ])

    喷射与切菜同键（P2 分屏 WorkstationInteract），但 `kind="spray"`：端着
    灭火器时游戏的 use 句柄（m_interactable）是灭火器自己、不是灶，所以插件
    HoldCommand 的 UseMatches 门恒不中、按不出键；spray 改走朝向句柄
    （m_gridLocation，HandleMatches(kind=null)），正对灶才按键。

    换下一目标由 `spray_done` 谓词驱动：HoldUntil 用 `spray_target` 锁定离团
    重心最近的起火目标的名字，`spray_done` 判「这个目标还在不在燃烧列表里」。
    这口一灭 →
    提前 SUCCESS（不再等「全灭」）→ RepeatUntil 重新评估 Selector，走到
    下一口着火灶。否则 pred 若仍是「not any_fire」，多口火时会一直喷已经
    灭掉的那口、不换目标（实机 trace 里火反复蔓延、灭火器喷半天灭不完）。
    """

    TYPE = "Extinguish"
    # 给对话层 LLM 看的一句话说明（ai/dialog/catalog.py 渲染 DAG 时带出）。
    DESC = "用灭火器灭火"
    LABEL = "灭火"
    # 一局火情是循环树，比普通 task 长，用专属上限兜底（默认 200 拍会中途砍树）。
    TIMEOUT_TICKS = FIREFIGHT_TIMEOUT_TICKS
    # 火不能因为连续失败就被放弃——灭火没有"先去做别的任务"这个选项。
    # Agent 的 L3 兜底（连续失败即拉黑认领资源）碰到这个标记就跳过本 task。
    NO_ABANDON = True

    def claims(self):
        # 全局灭火 task 不按灶认领：火情协调靠 planner 的 _fire_needs_ai
        # （P1 是否在旁 / 持灭火器），不靠 claims（spec §5.3）。
        return ()

    def done(self, ctx):
        # type: (Dict) -> bool
        return not _burning_targets(ctx["w"])

    def feasible(self, ctx):
        # type: (Dict) -> bool
        return bool(_burning_targets(ctx["w"]))

    def build(self):
        # type: () -> Node
        def burning(c):
            # 所有在烧目标（烧灶 + 烧柜台），**按离起火团重心的距离排序**。
            #
            # 排序是必须的，不是优化：下面 pot_station / fire_station /
            # spray_target 都取第 0 个，若用 world 的列表序，新火点出现就会把
            # 目标瞬间翻转到别处（旧实机 trace-20261014-011202 t=2404~2478：
            # 新灶一着火目标就跳走、喷半天没灭）。按「团重心」排序后，第 0 个
            # 是整团火的中心，P2 站到团中心旁、朝团中心扫射，一记扫射尽量盖住
            # 整团（而不是旧实现按离 P2 距离排、站到最靠边那口）。
            pts = _burning_targets(c["w"])
            if not pts:
                return pts
            cen = _fire_centroid(pts)
            cx, cz = cen if cen is not None else (
                c["chef"].get("x"), c["chef"].get("z"))
            if cx is None or cz is None:
                return pts
            def d2(pt):
                x, z = pt.get("x"), pt.get("z")
                if x is None or z is None:
                    return float("inf")
                return (x - cx) ** 2 + (z - cz) ** 2
            return sorted(pts, key=d2)

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

        def station_of(c, pt):
            # 起火点 → 站点 dict：柜台/砧板等带 t 的站点直接用；灶（无 t）经
            # cooker_station 从 stations 里反查。Approach/HoldUntil 都要 gx/gz
            # （交互位）和 x/z（朝向/重瞄）。
            if pt.get("t"):
                return pt
            return cooker_station(c, pt["n"])

        # 「当前正在喷哪口」的可变标记（build 闭包共享）。喷完一口后
        # spray_done 用它判「这口灭了没」，让 HoldUntil 提前 SUCCESS、
        # RepeatUntil 重新评估去喷下一口。否则 pred 只能等「全灭」（not
        # any_fire），多口火时会一直喷已经灭掉的那口、不换目标（实机复现）。
        spray = {"n": None}

        def fire_station(c):
            # 供 Approach 走位用：解析离团重心最近的起火目标。顺便把上一口的
            # 喷标记清掉——Approach 在每次喷之前走位，走到新目标时「喷哪口」应
            # 从零开始，否则 spray_done 会拿上一口的旧名提前判 True。
            cc = burning(c)
            spray["n"] = None
            return station_of(c, cc[0]) if cc else None

        def spray_target(c):
            # 供 HoldUntil 喷用：锁定当前离团重心最近的起火目标，并记下名字。
            cc = burning(c)
            if not cc:
                spray["n"] = None
                return None
            spray["n"] = cc[0]["n"]
            return station_of(c, cc[0])

        def spray_done(c):
            # 当前锁定的这口灶灭了（或全灭）→ 本轮喷完，交 RepeatUntil 换下一口。
            cc = burning(c)
            if not cc:
                return True
            n = spray["n"]
            if n is None:
                return False
            return not any(cc0.get("n") == n for cc0 in cc)

        def ext(c):
            e = _available_extinguisher(c)
            return _extinguisher_target(c["w"], e) if e else None

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
                      PR.HoldUntil("chop", spray_done,
                                   timeout=EXTINGUISH_TIMEOUT_TICKS,
                                   kind="spray",
                                   target=spray_target)),
            ]),
        ])), FIREFIGHT_TIMEOUT_TICKS)

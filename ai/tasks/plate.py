"""盘管理三 task：PlaceCleanPlate / WashPlate / ResetPlate（spec §6.1 第 1/2/14 号）。

本模块只放盘管理这一组，共享的身份机制与跨组谓词在 base.py。
外部不直接 import 本模块，一律 `from ai import tasks as T`
（分组边界是实现细节，见 ai/tasks/__init__.py）。
"""
from typing import Any, Dict, List, Optional, Tuple

from ai.query import predicates as P
from ai import primitives as PR
from ai.query import stations as S
from ai.exec.bt import Condition, Node, Selector, Sequence
from ai.tasks.base import (TASK_TIMEOUT_TICKS, Task, _register, plate_by_id,
                           plate_cell, station_at_cell)

# 洗盘按住交互键的上限（拍）。done() 现在是「一池子全洗完」（见 WashPlate.done），
# 每块实测约 2.8 秒（~56 拍），一整摞 3~4 块要 168~224 拍，故给 240 拍（12 秒）。
# 这只是兜底：正常由 done()（池子清空）提前 SUCCESS，超时才 FAILURE 交还 Planner。
WASH_TIMEOUT_TICKS = 240

# ---------- 盘相关谓词（仅本组用） ----------


def any_order_wants(ctx, plate):
    # type: (Dict, Dict) -> bool
    """盘里的料还对得上某张现存订单吗（ResetPlate 的可行性）。

    判据：盘的 token 集 ⊆ 某单的需求 token 集。多放一样不属于该单的料就
    对不上——那张盘再加料也做不成那道菜。

    用 plate_tokens / order_want_tokens 的同一套装盘态词汇（含 rich
    cooked:{raw}:{cookware}），不能字面比较：盘里记的是装盘态词汇、订单写
    的是需求名，字面比较会让正常装配的盘被判成"对不上任何订单"，于是
    ResetPlate 把队友刚倒好的饭扣进垃圾桶。
    """
    have = ctx["book"].plate_tokens(plate)
    if not have:
        return True          # 空盘无所谓，交给 ResetPlate 自己判不可行
    for order in ctx["w"].orders:
        if have <= ctx["book"].order_want_tokens(order):
            return True
    return False


def clean_plate_exists(ctx):
    # type: (Dict) -> bool
    """快照里有没有可用的干净空盘。"""
    for p in ctx["w"].plates:
        if P.plate_count(p) == 0:
            return True
    return False


def sink_station(ctx, sink_id=None):
    # type: (Dict, Any) -> Optional[Dict]
    """取洗碗池站点；sink_id 给定时优先精确匹配（多池关卡），否则取第一个。"""
    washing = [s for s in ctx["w"].stations
               if isinstance(s, dict) and s.get("t") == "washing"]
    if not washing:
        return None
    if sink_id is not None:
        for s in washing:
            if str(s.get("n")) == str(sink_id):
                return s
    return washing[0]


def sink_plate_count(ctx, sink_id=None):
    # type: (Dict, Any) -> int
    """洗碗池里待洗的盘数（插件 washing 站点的 plate_count；缺失/异常按 0）。"""
    st = sink_station(ctx, sink_id)
    if st is None:
        return 0
    try:
        return int(st.get("plate_count") or 0)
    except (TypeError, ValueError):
        return 0


def clean_plate_at_source(ctx):
    # type: (Dict) -> bool
    """干净空盘是否已出现在取盘位（晾干架 / 盘堆）。

    这是旧 WashPlate.done 的判据；现在被 done() 复用，但要叠加
    「池子已清空」这一条件，见 WashPlate.done。
    """
    src_cells = set()
    for st in ctx["w"].stations:
        if not S._is_plate_source(st):
            continue
        gx, gz = st.get("gx"), st.get("gz")
        if gx is not None and gz is not None:
            src_cells.add((gx, gz))
    if not src_cells:
        return False
    for p in ctx["w"].plates:
        if P.plate_count(p) != 0:
            continue
        if plate_cell(ctx, p) in src_cells:
            return True
    return False


def dirty_plate_exists(ctx, sink_id=None):
    # type: (Dict, Any) -> bool
    """有要洗的盘：场上的 DirtyPlateStack，或洗碗池里待洗的盘（plate_count>0）。

    后者是「盘已放进池子、正在洗」的进行态：盘一入池就离开 items[]，
    若只认 items[]，feasible 会在入池那一拍翻脸，承诺惯性 3 拍后把正在
    洗的树抢走（实机 trace-20261017-182700 t=1678~1680）。
    """
    for it in ctx["w"].items:
        if "dirtyplate" in str(it.get("n") or "").lower():
            return True
    return sink_plate_count(ctx, sink_id) > 0


@_register
class PlaceCleanPlate(Task):
    """第 1 号：拿一块干净盘摆到拼装格。

    resources = (plate_id, counter_cell)。counter_cell 是 (gx, gz)，由
    Planner 的候选生成绑定——spec §9 记 PLATE_COUNTERS 的替代品是
    "Planner 的多拼装位候选生成 + 认领，不是一个 find 函数"（归 Plan 3）。
    """

    TYPE = "PlaceCleanPlate"
    # 给对话层 LLM 看的一句话说明（ai/dialog/duties.py 渲染 DAG 时带出）。
    DESC = "拿一个干净盘摆到拼装台上"
    LABEL = "摆盘"
    # 不分菜的共享节点（ai/dialog/duties.py）。
    SHARED = True

    @property
    def plate_id(self):
        return self._resources[0]

    @property
    def counter_cell(self):
        return self._resources[1]

    def claims(self):
        return ((self.plate_id, "place"), (self.counter_cell, "assemble"))

    def done(self, ctx):
        # type: (Dict) -> bool
        """认领盘的格 == 拼装格，且手已空（spec §6.1）。**绑定具体盘**求值。

        只查盘格不够：盘被端在手里、恰好路过拼装格时，其世界坐标也会落在
        目标格，于是 done 误判"已放好"（实机：端着盘在拼装格旁左右横跳）。
        手空是"真的放下去了"的必要信号——盘还攥在手里就绝不能算完成。
        """
        p = plate_by_id(ctx, self.plate_id)
        return (plate_cell(ctx, p) == tuple(self.counter_cell)
                and P.hands_free(ctx["chef"]))

    def feasible(self, ctx):
        # type: (Dict) -> bool
        if self.done(ctx):
            return False
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False                   # 盘被端走/消失
        return P.plate_count(p) == 0       # 必须是干净空盘

    def build(self):
        # type: () -> Node
        plate_id, cell = self.plate_id, self.counter_cell
        return PR.Timeout(Sequence([
            # 树首清手：被抢占时手里可能端着上一 task 的东西（spec §7.4）；
            # keep：手里已经是盘就别放下再捡
            PR.EnsureHandsFree(keep=lambda c: P.held_is_plate(c["chef"])),
            PR.InteractWith(
                lambda c: plate_by_id(c, plate_id), "pickup",
                lambda c: P.held_is_plate(c["chef"])),
            PR.InteractWith(
                lambda c: station_at_cell(c, cell), "placement",
                lambda c: (plate_cell(
                    c, plate_by_id(c, plate_id)) == tuple(cell)
                    and P.hands_free(c["chef"]))),
        ]), TASK_TIMEOUT_TICKS)


@_register
class WashPlate(Task):
    """第 2 号：洗脏盘。

    完成判定改成「一次性洗完」：洗碗池里待洗的盘清零（plate_count==0），
    且干净盘已出现在晾干架/盘堆。旧判定只等第一块干净盘，于是端一摞只洗
    一块就走，剩下的盘永远留在池子里没人管。

    spec §6.1 第 2 号注：洗完的盘从晾干架出来，不是盘堆。Plan 1.5 的
    stations._is_plate_source 已把 t=="plate_return" 且名含 drying 的
    站点算作可取盘位（实测帧确有 {'t':'plate_return','n':'DryingPart'}）。
    """

    TYPE = "WashPlate"
    # 给对话层 LLM 看的一句话说明（ai/dialog/duties.py 渲染 DAG 时带出）。
    DESC = "在洗碗池洗脏盘"
    LABEL = "洗盘"
    # 不分菜的共享节点；需要洗碗池这类站点才进目录（ai/dialog/duties.py）。
    SHARED = True
    STATION = "washing"

    @property
    def sink_id(self):
        return self._resources[0]

    def claims(self):
        return ((self.sink_id, "wash"),)

    def done(self, ctx):
        # type: (Dict) -> bool
        # 一次性洗完：池子里还有待洗的盘就不算完（plate_count>0）。
        # 不能只判「池子空了」——任务一开始（还没放盘进去）池子就是空的，
        # 那样 done 恒真、feasible 恒假，WashPlate 永远启动不了。所以必须
        # 叠加「已经洗出干净盘在取盘位」。
        if sink_plate_count(ctx, self.sink_id) > 0:
            return False
        return clean_plate_at_source(ctx)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        # done 已成立就不可行（上下游咬合，同 PlaceCleanPlate / Chop）。
        # 漏掉这一句的后果不是「多洗一趟」而是**彻底空转**：晾干架上已躺着
        # 干净盘时 done 恒真，树一建起来 `HoldUntil` 的谓词当拍就成立 → 整个
        # Sequence 直接 SUCCESS，一次洗盘键都不按。实机 trace-20261014-221306：
        # t=6117~6193 与 7228~7370 共 220 拍选 WashPlate、0 拍按键。
        if self.done(ctx):
            return False
        # 当前承诺就是洗这个池子 → 继续洗完，别中断。这里必须看「承诺」而
        # 不是只看「池子里有盘」：池子里有盘也可能是 P1 刚放进去的脏盘，那时
        # 场上还有干净盘/装配盘的话就不该去洗。只有 P2 自己已承诺在洗（含
        # 捡起脏盘往池子走的阶段）才放行续洗。
        bb = ctx.get("bb")
        commitment = getattr(bb, "commitment", None)
        active = getattr(commitment, "active_task", None) \
            if commitment is not None else None
        if active is not None and active == self:
            return True
        # 不在续洗态 → 只判物理可行性：有要洗的盘就可行。「要不要洗」的软闸
        # （场上还有干净盘 / 装配盘就别洗）已拆到 L4 层谓词（spec §A.3）——
        # feasible 这里若再拦，`now 洗盘` 这种硬指令就够不到 WashPlate 候选，
        # 因为 _select_imperative 只在 feasible 过滤后的候选里选。
        return dirty_plate_exists(ctx, self.sink_id)

    def build(self):
        # type: () -> Node
        sink_id = self.sink_id
        done = self.done

        def sink(c):
            return sink_station(c, sink_id)

        def sink_has_plates(c):
            return sink_plate_count(c, sink_id) > 0

        def dirty(c):
            """脏盘摞的取用目标：优先 stations 里的回盘台（有 gx/gz）。

            items[] 的 DirtyPlateStack 只有世界坐标，没有 gx/gz；同位置的
            stations 条目（实测 {'t':'plate_return','n':'workstation_plate_return',
            'gx':-1,'gz':-5}）才是带网格的权威目标，Approach / interact_ready
            都按它判。先确认脏盘摞真的存在（items），再把坐标换成站点那条。
            """
            stack = None
            for it in c["w"].items:
                if "dirtyplate" in str(it.get("n") or "").lower():
                    stack = it
                    break
            if stack is None:
                return None
            sx, sz = stack.get("x"), stack.get("z")
            if sx is None or sz is None:
                return stack
            for st in c["w"].stations:
                if not isinstance(st, dict) or st.get("t") != "plate_return":
                    continue
                if "drying" in str(st.get("n") or "").lower():
                    continue        # 晾干架是取干净盘的，不是脏盘摞
                if st.get("gx") is None or st.get("gz") is None:
                    continue
                # 同位置（回盘台与脏盘摞重合）才认
                if abs(float(st.get("x", 1e9)) - float(sx)) < 0.7 and \
                        abs(float(st.get("z", 1e9)) - float(sz)) < 0.7:
                    return st
            return stack

        return PR.Timeout(Sequence([
            Selector([
                Sequence([
                    # face=True：脏盘摞是**固定站点**，站对位置但朝向不对时
                    # `interactions.pickup` 为 null，不转身就永远按不到（实机
                    # trace-20261014-004742：WashPlate 11 次全部 failure，每次都
                    # 横穿整张地图到 (-1,-4) 再立刻失败。对比 t=3753 yaw=117 句柄
                    # 就绪 vs t=8421 yaw=270 句柄全 null）。
                    PR.InteractWith(dirty, "pickup",
                                    lambda c: not P.hands_free(c["chef"])),
                    PR.InteractWith(sink, "placement",
                                    lambda c: P.hands_free(c["chef"])),
                ]),
                # 起点 B：盘已经在池子里（没有可捡的脏盘摞）→ 走到池子并
                # 朝向它，不捡不放，直接开洗。
                Sequence([
                    Condition(sink_has_plates),
                    PR.Approach(sink, None),
                    PR.Face(sink),
                ]),
            ]),
            # target 让 HoldUntil 每拍校验还在水池前：被推走时松键 FAILURE，
            # Sequence 重置重新 Approach，而不是空按 wash 卡死（同 Chop）。
            PR.HoldUntil("chop", done, timeout=WASH_TIMEOUT_TICKS, target=sink),
        ]), TASK_TIMEOUT_TICKS)


@_register
class ResetPlate(Task):
    """第 14 号：清掉内容与任何现存订单都不匹配的盘。

    spec §6.1 原话："第 14 号 ResetPlate 是新增的必要能力（初版遗漏）。
    两个场景会楔死拼装格：① 盘里被放错料 → 对账只'失效认领'，那只错料
    盘仍停在拼装格上，PlaceCleanPlate 于是去放另一只盘，几轮后无格可用；
    ② AI 端着料齐的盘走向出餐口时订单过期 → 承诺被弃 → 手里一只满盘。
    没有这个 task，上述两种状态无法恢复。"

    清法：端起盘送到垃圾桶倒掉（盘里的料只能倒垃圾桶），空盘放回盘堆。
    完成判定放宽为"盘空 **或** 已移出拼装格"——后者已经解除了楔死。
    """

    TYPE = "ResetPlate"
    # 给对话层 LLM 看的一句话说明（ai/dialog/duties.py 渲染 DAG 时带出）。
    DESC = "把装错/无用的盘子清空复位"
    LABEL = "清盘"

    @property
    def plate_id(self):
        return self._resources[0]

    @property
    def counter_cell(self):
        return self._resources[1]

    def claims(self):
        return ((self.plate_id, "reset"), (self.counter_cell, "assemble"))

    def done(self, ctx):
        # type: (Dict) -> bool
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return True       # 盘不见了，楔死也解除了
        if P.plate_count(p) == 0:
            return True
        return plate_cell(ctx, p) != tuple(self.counter_cell)

    def feasible(self, ctx):
        # type: (Dict) -> bool
        p = plate_by_id(ctx, self.plate_id)
        if p is None:
            return False
        contents = P.plate_contents(p)
        if not contents:
            return False      # 空盘没什么要清的
        return not any_order_wants(ctx, p)

    def build(self):
        # type: () -> Node
        plate_id = self.plate_id

        def bin_st(c):
            return S.find_station(c["w"].stations, "bin",
                                  (c["chef"]["x"], c["chef"]["z"]))

        return PR.Timeout(Sequence([
            PR.InteractWith(lambda c: plate_by_id(c, plate_id), "pickup",
                            lambda c: P.held_is_plate(c["chef"])),
            PR.InteractWith(
                bin_st, "placement",
                lambda c: P.plate_count(plate_by_id(c, plate_id)) == 0),
        ]), TASK_TIMEOUT_TICKS)

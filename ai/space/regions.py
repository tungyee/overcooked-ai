"""连通区域：判断「料在哪一块地面」「消费站从哪一块可交互」。

Rapids_2_1 这类关卡地面分成互不相连的几块（静态厨房 + 漂流木筏），料只能
投过去（trace-20261028-234917：P2 木筏 / P1 厨房两个可达场交集为空）。投掷
规划只需要回答两个问题，不必枚举全部连通分量：
  - 某个站点/物品，P2 此刻能不能走过去交互（复用 reachability.can_interact）；
  - 某个站点/物品，是否在 P2 区域之外、却在别人（P1）的区域里。

区域定义 = 某厨师当前格出发的可达场（reachability.field_for，每帧缓存）。
木筏漂移只改投影，不改「两块」拓扑，所以每帧重算即可，不做跨帧推断。
"""
from typing import Dict, Optional

from ai.query import predicates as P
from ai.space import reachability


def field_of(world, bb=None, name=P.P2_NAME):
    # type: (object, object, str) -> Optional[Dict]
    """name 厨师的可达场；旧合成帧（无边界）返回 None = 拓扑未知。"""
    return reachability.field_for(world, bb, name)


def reachable_by(world, entry, name=P.P2_NAME, bb=None):
    # type: (object, Dict, str, object) -> bool
    """name 厨师此刻能否走到 entry 旁交互。拓扑未知时按可达处理（离线兼容）。"""
    chef = P.chef_by_name(world.chefs, name)
    ctx = dict(w=world, bb=bb, chef=chef, field=field_of(world, bb, name))
    return reachability.can_interact(ctx, entry)


def split(world, bb=None):
    # type: (object, object) -> bool
    """P2 与 P1 的可达场互不相交 = 地图被分割（只能投掷传料）。

    拓扑未知或缺任一厨师时返回 False：宁可当连通，不凭空发投掷候选。
    """
    f2 = field_of(world, bb, P.P2_NAME)
    f1 = field_of(world, bb, P.P1_NAME)
    if not f2 or not f1:
        return False
    return not (set(f2) & set(f1))


def on_far_side(world, entry, bb=None):
    # type: (object, Dict, object) -> bool
    """entry 在 P2 够不着、P1 够得着的那一侧。"""
    if field_of(world, bb, P.P2_NAME) is None:
        return False
    if reachable_by(world, entry, P.P2_NAME, bb):
        return False
    if P.chef_by_name(world.chefs, P.P1_NAME) is None:
        return False
    return reachable_by(world, entry, P.P1_NAME, bb)

"""跨区通道：分割地图上把不能投的东西交给对面的地方。

干净盘、脏盘、成品盘、生载体都走这里（具体 kind 与触发在
ai/revenue/handover.py）；能投的生料走 ThrowBatch。
地图被分成 P2 一块、P1 一块（两个可达场不相交）时，东西过去只有两条路：
  - 渡运通道（ai/space/shuttle.py）：人上不去的移动台面，篮子停靠时隔着围栏
    放 / 取。Balloon_2_2（游戏内 2-3），按渡运网格名区分，每个网格一条通道。
  - 静态通道：两个可达场**都能交互**的普通台面。Wizard_3_1（游戏内 3-1）中间
    一列台面两边都够得着（trace-20261101-002050 t=300：(-3,2) 台面 P2/P1 都
    can_interact，两场交集为空）。不按关卡配表：每帧按两个可达场现算，砧板
    整列滑走（t≈44s 起 x 7.2 → 13.2）这类地形变化下一帧自动跟上。

两边都够得着的**空砧板**也是静态通道的格（shared_boards），但只给载体用
（披萨饼本来就是在砧板上切出来、在砧板上放料的）：盘子放上去会占住切菜位。

传送带通道（CONVEYOR）：Rapids_4_2（双竹筏水巷）两边各一条 L 形台面传送带，
人过不去，加工好的东西放上自己这侧的传送带运到对面。传送带段是移动网格上
的 `ConveyorStation`（不是可走地板），用段名而不是格子认身份；放上去就算
「交给传送带」，不再追它此刻漂到哪。

通道 key：渡运 = 网格名；静态 = STATIC；传送带 = CONVEYOR。SendAcross 的
资源 id 第二位就是它。拓扑未知（旧合成帧无边界）或地图不分割时没有通道。
不写 World 以外的状态；每帧缓存在 world 上。
"""
from typing import Dict, List, Optional, Set, Tuple

from ai.query import predicates as P
from ai.space import occupancy
from ai.space import reachability
from ai.space import shuttle

STATIC = "@static"
CONVEYOR = "@conveyor"

# 传送带段没有 collider 字段（插件只报段位置），散落物靠 on_station 段名
# 或与段心的距离认领。格宽 1.2，半格 0.6 留一点余量。
CONVEYOR_SLOT_DIST = 0.75

# 每条通道至少留几格空着给对面往回送（成品 / 脏盘）。渡运台面一般 4~6 格，
# 留 1；静态共享台面常常只有一两格，留格会让它永远送不出去，不留。
KEEP_FREE = {STATIC: 0, CONVEYOR: 0}
SHUTTLE_KEEP_FREE = 1

# 传送带通道刚可用过的宽限（拍）：4-2 两筏相对位置会让段可达性逐帧翻转。
# 5Hz 一帧 = 4 拍；200 拍 = 10 秒，足够等到下一次接缝对齐。
CONVEYOR_GRACE_TICKS = 200
# 两筏带子「接上」的最近段距离（米）。实测：
#   - rapids42-own-belt（两筏漂开）最近 4.7~4.95m → 不接；
#   - rapids42-shared-belt / trace-20261111-005439 最近 2.47~2.75m → 接上。
# 3.0 留一点余量，又不会把漂开的带子误判成通路。
CONVEYOR_CONNECT_DIST = 3.0


def conveyor_waitable(bb):
    # type: (object) -> bool
    """传送带通道最近是否可用过（宽限期内）。

    `SendAcross` 用它把「接缝暂时漂开」从不可行变成可等：否则一帧可达性
    翻转就会 failure，下一棵树的 EnsureHandsFree 把成品盘放下
    （trace-20261111-001945 t=409/2524）。
    """
    if bb is None:
        return False
    until = (getattr(bb, "conveyor_open_until", None) or {}).get(CONVEYOR, 0)
    return until > getattr(bb, "tick", 0)


def _fields(world, bb=None):
    near = reachability.field_for(world, bb, P.P2_NAME)
    far = reachability.field_for(world, bb, P.P1_NAME)
    return near, far


def is_split(world, bb=None):
    # type: (object, object) -> bool
    """两个可达场都已知、非空且不相交（拓扑未知按不分割，不凭空发送件）。"""
    near, far = _fields(world, bb)
    return bool(near) and bool(far) and not (set(near) & set(far))


def _reach(world, field, entry, name):
    ctx = dict(w=world, chef=P.chef_by_name(world.chefs, name), field=field)
    return reachability.can_interact(ctx, entry)


def is_conveyor_station(world, st):
    # type: (object, Optional[Dict]) -> bool
    """台面传送带段（插件 stations[].t == "conveyor"）。"""
    return isinstance(st, dict) and st.get("t") == "conveyor"


def _on_conveyor(entry, st):
    # type: (Dict, Dict) -> bool
    """entry（料/盘）是否在这段传送带上。

    插件对台上的料给 `on_station="attach:<段名>"`；盘/旧帧没有该字段时按
    世界坐标就近认（段没有 AABB 可查，只能按段心距离）。
    """
    on = str(entry.get("on_station") or "")
    if ":" in on:
        name = on.split(":", 1)[1].strip()
        if name and name == str(st.get("n") or ""):
            return True
    x, z = entry.get("x"), entry.get("z")
    sx, sz = st.get("x"), st.get("z")
    if x is None or z is None or sx is None or sz is None:
        return False
    return ((float(sx) - float(x)) ** 2
            + (float(sz) - float(z)) ** 2) <= CONVEYOR_SLOT_DIST ** 2


def _station_by_name(world, name):
    # type: (object, str) -> Optional[Dict]
    for st in getattr(world, "stations", ()) or ():
        if is_conveyor_station(world, st) and str(st.get("n") or "") == str(name):
            return st
    return None


def plate_conveyor_station(world, plate):
    # type: (object, Optional[Dict]) -> Optional[Dict]
    """盘躺着的传送带段；认段口径与 plate_on_conveyor 一致（不按距离猜）。"""
    if not isinstance(plate, dict):
        return None
    on = str(plate.get("on_station") or "")
    if "conveyor" in on.lower() and ":" in on:
        st = _station_by_name(world, on.split(":", 1)[1].strip())
        if st is not None:
            return st
    name = str(plate.get("name") or "")
    x, z = plate.get("x"), plate.get("z")
    if not name or x is None or z is None:
        return None
    for it in getattr(world, "items", ()) or ():
        if not isinstance(it, dict) or str(it.get("n") or "") != name:
            continue
        on = str(it.get("on_station") or "")
        if "conveyor" not in on.lower() or ":" not in on:
            continue
        ix, iz = it.get("x"), it.get("z")
        if ix is None or iz is None:
            continue
        if abs(float(ix) - float(x)) < 0.6 and abs(float(iz) - float(z)) < 0.6:
            return _station_by_name(world, on.split(":", 1)[1].strip())
    return None


def plate_on_conveyor(world, plate):
    # type: (object, Optional[Dict]) -> bool
    """盘是否真的在传送带上。

    盘对象本身常没有 on_station（插件把段名报在同位置的 items[] 上）；
    只看盘心到段心的距离会把旁边柜台上的盘也误判成传送带，抢盘任务就会
    去拿一块根本没危险的盘（trace-20261110-231906 t=541）。优先按同名
    item 的 on_station 认，退路才看盘自身字段。
    """
    if not isinstance(plate, dict):
        return False
    if "conveyor" in str(plate.get("on_station") or "").lower():
        return True
    name = str(plate.get("name") or "")
    x, z = plate.get("x"), plate.get("z")
    if not name or x is None or z is None:
        return False
    for it in getattr(world, "items", ()) or ():
        if not isinstance(it, dict) or str(it.get("n") or "") != name:
            continue
        if "conveyor" not in str(it.get("on_station") or "").lower():
            continue
        ix, iz = it.get("x"), it.get("z")
        if ix is None or iz is None:
            continue
        if abs(float(ix) - float(x)) < 0.6 and abs(float(iz) - float(z)) < 0.6:
            return True
    return False


def belt_reached_by_player(world, st, bb=None):
    # type: (object, Optional[Dict], object) -> bool
    """这条传送带上有没有哪一段是玩家（P1）够得着的。

    传送带是整条 L 形带，人常常只站得到其中一段（P2 这侧的是带子开头，
    玩家站的是下一段）。只要玩家能站到任意一段，带上的东西就归玩家取，
    P2 不该抢——抢了会变成「放上去 → 抢下来 → 再放上去」
    （trace-20261110-235726 t=491~528：盘 -20214 在 Raft One 带子上，
    P1 在下一段接得到，P2 每拍都去拿）。
    """
    grid = str(st.get("grid") or "")
    if not grid or not is_split(world, bb):
        return False
    _near, far = _fields(world, bb)
    for other in getattr(world, "stations", ()) or ():
        if not is_conveyor_station(world, other):
            continue
        if str(other.get("grid") or "") != grid:
            continue
        if _reach(world, far, other, P.P1_NAME):
            return True
    return False


def plate_on_player_side_conveyor(world, plate, bb=None):
    # type: (object, Optional[Dict], object) -> bool
    """盘是否躺在玩家那侧的传送带上（=玩家会自己来取，不用抢救）。"""
    st = plate_conveyor_station(world, plate)
    if st is None:
        return False
    return belt_reached_by_player(world, st, bb)


def _chef_grid(world, chef):
    # type: (object, Optional[Dict]) -> Optional[str]
    """厨师所在的地面网格 id（只看会动的附属网格）。

    用 Ground 层的世界 AABB 认：厨师所在的盒子与某个网格原点的盒子相同，
    就说明人在这个网格上。不能用「离哪个原点近」——两筏交错时原点距离会
    说反（trace-20261111-005439 t=906，P2 在 Raft Two，离 Raft One 原点更近）。
    """
    if not isinstance(chef, dict) or chef.get("x") is None:
        return None
    boxes = (getattr(world, "terrain", None) or {}).get(
        occupancy.LAYER_GROUND) or []

    def box_of(x, z):
        for b in boxes:
            if b[0] <= x <= b[1] and b[2] <= z <= b[3]:
                return b
        return None

    chef_box = box_of(chef["x"], chef["z"])
    if chef_box is None:
        return None
    best = None  # type: Optional[Tuple[float, str]]
    for gid in getattr(getattr(world, "grids", None), "ids", lambda: [])():
        if str(gid) == "GridManager":
            continue
        origin = world.grids.to_world(gid, 0, 0)
        if origin is None:
            continue
        if box_of(*origin) is not chef_box:
            continue
        d = (origin[0] - chef["x"]) ** 2 + (origin[1] - chef["z"]) ** 2
        if best is None or d < best[0]:
            best = (d, str(gid))
    return best[1] if best else None


def conveyor_slots(world, bb=None):
    # type: (object, object) -> List[Dict]
    """P2 自己这侧的送件带段；只在两筏的带子接上时才算可用。

    4-2 的两条 L 形带分属 Raft Two / Raft One，玩家够不着 P2 这条的
    **同一 grid**，但两筏靠近时带子的端头会接上。之前按「同一 grid 有段
    玩家够得着」筛，P2 这侧的带子永远进不来，只剩对面 grid 的段被投影
    坐标误判成可达——AI 于是走到缝边等 P1 的木筏（trace-20261111-005439
    t=363/906）。
    现在：
      - 只取 chef 所在 grid 的段（`_chef_grid`）；
      - 该段到任一 P1 可达段的最近距离 ≤ `CONVEYOR_CONNECT_DIST` 才算接上；
      - 接不上就返回空，让 SendAcross 在宽限期内端盘等，而不是选对面段。
    """
    cache = getattr(world, "_conveyor_slots", None)
    if cache is not None:
        if cache and bb is not None:
            bb.conveyor_open_until[CONVEYOR] = (
                getattr(bb, "tick", 0) + CONVEYOR_GRACE_TICKS)
        return cache
    out = []  # type: List[Dict]
    if is_split(world, bb):
        near, far = _fields(world, bb)
        chef = P.chef_by_name(getattr(world, "chefs", ()) or (), P.P2_NAME)
        own_grid = _chef_grid(world, chef)
        if own_grid is not None:
            # —— 4-2 竹筏：两条 L 形带分属两筏，临时对接 ——
            player_segs = [
                st for st in (getattr(world, "stations", ()) or ())
                if is_conveyor_station(world, st)
                and _reach(world, far, st, P.P1_NAME)]
            player_grids = {str(st.get("grid") or "") for st in player_segs}
            # 比较整条玩家带，而不是只看 P1 此刻够得着的那一段：玩家带从接缝
            # 一路走到取件段，接缝段本身 P1 够不着不代表带子没接上
            # （rapids42-shared-belt：接缝段 p1=False，但它后面那段 p1=True）。
            player_all = [
                st for st in (getattr(world, "stations", ()) or ())
                if is_conveyor_station(world, st)
                and str(st.get("grid") or "") in player_grids]
            # 自己这侧整条带都够得着：P2 就站在同一块竹筏地板上。不用 _reach
            # 逐段判——主网格投影会随竹筏亚格漂移闪断，把明明能站到的段判成
            # 不可达（trace-20261111-115549 t=367 等，7 段只有 4 段被认可达）。
            near_segs = [
                st for st in (getattr(world, "stations", ()) or ())
                if is_conveyor_station(world, st)
                and str(st.get("grid") or "") == own_grid]
            if player_all and near_segs:
                best = min(
                    ((a.get("x", 0.0) - b.get("x", 0.0)) ** 2
                     + (a.get("z", 0.0) - b.get("z", 0.0)) ** 2) ** 0.5
                    for a in near_segs for b in player_all)
                if best <= CONVEYOR_CONNECT_DIST:
                    out = near_segs
        else:
            # —— 4-5 静态：传送带是「竖带把料送向中点 (0,0)、开关再分左右」。
            # 竖带（gx=0、gz!=0）两半都朝中点流，P2 放自己可达的那半即可；
            # 中点横带（gz=0）是送进两边的桶、不能当放料点（trace-20261112-122718
            # 空盘放 gx=5 一路进右桶）。按可达场筛，P2/P1 谁在南谁在北都能自适应。
            near_segs = [
                st for st in (getattr(world, "stations", ()) or ())
                if is_conveyor_station(world, st)
                and st.get("gx") == 0 and (st.get("gz") or 0) != 0
                and _reach(world, near, st, P.P2_NAME)]
            player_segs = [
                st for st in (getattr(world, "stations", ()) or ())
                if is_conveyor_station(world, st)
                and st.get("gx") == 0 and (st.get("gz") or 0) != 0
                and _reach(world, far, st, P.P1_NAME)]
            if near_segs and player_segs:
                out = near_segs
    if out and bb is not None:
        # 记下「这条通道刚开过」，供 SendAcross 在下一帧闪断时继续等。
        bb.conveyor_open_until[CONVEYOR] = (
            getattr(bb, "tick", 0) + CONVEYOR_GRACE_TICKS)
    world._conveyor_slots = out
    return out


def switch_direction(world, bb=None):
    # type: (object, object) -> Optional[str]
    """4-5 传送带方向开关当前的开关格方向：'>'（右）或 '<'（左）；拿不到返回 None。

    开关格是传送带系统中点 (0,0) 那格 conveyor——按钮（switch 站点）挂在
    (1,1)/(-1,-1)，但随按钮翻转的是中点格的 vx（trace-20261111-233924 按钮把
    (0,0) 从 '<' 翻成 '>'）。读该格的 vx 符号。
    """
    if not any(isinstance(st, dict) and st.get("t") == "switch"
               for st in getattr(world, "stations", ()) or ()):
        return None
    for st in getattr(world, "stations", ()) or ():
        if not isinstance(st, dict) or st.get("t") != "conveyor":
            continue
        if st.get("gx") == 0 and st.get("gz") == 0:
            vx = st.get("vx") or 0.0
            if abs(vx) > 0.01:
                return ">" if vx > 0 else "<"
    return None


def _bin_reachable_by(world, bb, name, gx):
    # type: (object, object, str, int) -> Optional[bool]
    """name 是否够得着 gx 那端的传送带桶（gz=0）。拿不到（无厨师/无桶/拓扑未知）返回 None。"""
    try:
        ctx = reachability.context(world, bb, name)
    except Exception:  # noqa: BLE001 单测 fake world 没有 chefs 等字段
        return None
    for st in getattr(world, "stations", ()) or ():
        if isinstance(st, dict) and st.get("t") == "bin" \
                and st.get("gx") == gx and st.get("gz") == 0:
            return reachability.can_interact(ctx, st)
    return None


def conveyor_direction_ok(world, bb=None):
    # type: (object, object) -> bool
    """4-5 传送带方向是否「正确」（把料送向 P1 够得着的接收桶，而非另一头）。

    开关格 (0,0) 的 raw vx>0 = 实际向左 → 左桶(gx=-6)；raw vx<0 = 实际向右 →
    右桶(gx=6)。正确的方向 = 把料送向 P1 够得着的那端——按 P1 可达场判，谁在
    南谁在北都能自适应。判不出（拓扑未知/两端都够得着）回退「左端收」。没有
    开关（其它关卡）恒 True。
    """
    d = switch_direction(world, bb)
    if d is None:
        return True
    left = _bin_reachable_by(world, bb, P.P1_NAME, -6)
    right = _bin_reachable_by(world, bb, P.P1_NAME, 6)
    if left is True and right is not True:
        return d == ">"   # P1 只在左端收 → 需要 raw vx>0（实际向左）
    if right is True and left is not True:
        return d == "<"   # P1 只在右端收 → 需要 raw vx<0（实际向右）
    return d == ">"       # 未知/两端都可 → 回退左端收


def conveyor_interact_cells(world, st):
    # type: (object, Dict) -> Optional[Set[Tuple[int, int]]]
    """移动网格（竹筏）上 conveyor 段的交互站位：用该网格本地邻格映射回主网格。

    静态主网格的 conveyor 走常规 interaction_footprint；移动网格的段投影到
    主网格后不是可行走格、四邻也不在 P2 可达场里（trace-20261111-121429
    t=640：盘 -18832 投影格 (4,2) in_field=False），所以要用段的本地 gx/gz
    的邻格，映射回主网格后取可行走的那些。
    非移动网格（grid 缺失 / GridManager）返回 None，调用方退回 footprint。
    """
    gid = str(st.get("grid") or "")
    if not gid or gid == "GridManager":
        return None
    gx, gz = st.get("gx"), st.get("gz")
    if gx is None or gz is None:
        return None
    grids = getattr(world, "grids", None)
    if grids is None:
        return None
    try:
        gx, gz = int(gx), int(gz)
    except (TypeError, ValueError):
        return None
    cells = set()  # type: Set[Tuple[int, int]]
    for dx, dz in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
        point = grids.to_world(gid, gx + dx, gz + dz)
        if point is None:
            continue
        cell = world.to_grid(point[0], point[1])
        if cell is not None and world.walkable(*cell):
            cells.add(cell)
    return cells or None


def terminal_conveyor_segment(world, grid):
    # type: (object, str) -> Optional[Dict]
    """该 grid 上贴 Bin 的传送带末端段（离 Bin 最近的那段）。

    4-2 每条 L 形带一端接着自己的 Bin：Raft Two Bin(-1,-2) 贴段 (0,-2)，
    Raft One Bin(1,2) 贴段 (0,2)。抢救/拦截就站在这一段旁等东西到末端，
    不追它当前所在段（盘子单向走，追当前段会永远追不上）。
    """
    gid = str(grid or "")
    if not gid:
        return None
    bins = [st for st in getattr(world, "stations", ()) or ()
            if st.get("t") == "bin" and str(st.get("grid") or "") == gid]
    if not bins:
        return None
    segs = [st for st in getattr(world, "stations", ()) or ()
            if is_conveyor_station(world, st)
            and str(st.get("grid") or "") == gid]
    if not segs:
        return None

    def cell_dist(st, b):
        try:
            return (int(st.get("gx") or 0) - int(b.get("gx") or 0)) ** 2 \
                + (int(st.get("gz") or 0) - int(b.get("gz") or 0)) ** 2
        except (TypeError, ValueError):
            return 1e9

    return min(segs, key=lambda st: min(cell_dist(st, b) for b in bins))


def conveyor_start_rank(st):
    # type: (Dict) -> Tuple[int, int]
    """传送带段的「开头」排序键：先大 gz、再大 gx。

    双竹筏的 L 形带从柱子顶端（gx 最大、gz 最大）绕到工作区那一端：
    反着放会把东西送到远端死胡同（4-2 实测成品盘停在 `gx=0,gz=-2`，
    玩家够不着）。这里只按局部格排，不猜方向，简单稳定。
    """
    try:
        gx = int(st.get("gx") or 0)
        gz = int(st.get("gz") or 0)
    except (TypeError, ValueError):
        return (0, 0)
    return (-gz, -gx)


def conveyor_station_of(world, entry, slots=None):
    # type: (object, Optional[Dict], Optional[List[Dict]]) -> Optional[Dict]
    """entry 所在的传送带段；不在传送带上返回 None。"""
    if not isinstance(entry, dict) or entry.get("held_by"):
        return None
    pool = slots if slots is not None else [
        st for st in getattr(world, "stations", ()) or ()
        if is_conveyor_station(world, st)]
    for st in pool:
        if _on_conveyor(entry, st):
            return st
    return None


def slot_occupied(world, st):
    # type: (object, Dict) -> bool
    """通道格上已经有东西（盘/锅/料/板）。传送带按段名/段心认，不走投影格。"""
    if is_conveyor_station(world, st):
        groups = (getattr(world, "plates", None), getattr(world, "items", None))
        for arr in groups:
            for it in arr or ():
                if not isinstance(it, dict) or it.get("held_by"):
                    continue
                if _on_conveyor(it, st):
                    return True
        return False
    from ai.query.placement import counter_key, counter_occupied
    return counter_occupied(world, counter_key(world, st))


def static_slots(world, bb=None):
    # type: (object, object) -> List[Dict]
    """两边都能交互的普通台面（不含渡运台面、砧板等功能站）。"""
    cache = getattr(world, "_static_slots", None)
    if cache is not None:
        return cache
    out = []  # type: List[Dict]
    if is_split(world, bb):
        near, far = _fields(world, bb)
        for st in getattr(world, "stations", ()) or ():
            if not isinstance(st, dict) or st.get("t") != "counter":
                continue
            if shuttle.is_shuttle_station(world, st):
                continue
            if _reach(world, near, st, P.P2_NAME) and _reach(world, far, st, P.P1_NAME):
                out.append(st)
    world._static_slots = out
    return out


def shared_boards(world, bb=None):
    # type: (object, object) -> List[Dict]
    """两边都能交互的砧板（不管空不空）；地图不分割时为空。"""
    cache = getattr(world, "_shared_boards", None)
    if cache is not None:
        return cache
    out = []  # type: List[Dict]
    if is_split(world, bb):
        near, far = _fields(world, bb)
        for st in getattr(world, "stations", ()) or ():
            if isinstance(st, dict) and st.get("t") == "chopping" \
                    and _reach(world, near, st, P.P2_NAME) and _reach(world, far, st, P.P1_NAME):
                out.append(st)
    world._shared_boards = out
    return out


def _board_free(world, st):
    # type: (object, Dict) -> bool
    for ws in getattr(world, "workstations", ()) or ():
        if P.station_name_matches(str(ws.get("n") or ""), str(st.get("n") or "")):
            return not ws.get("item")
    return False


def free_carrier_slots(world, bb=None):
    # type: (object, object) -> List[Dict]
    """静态通道上能放载体的空格：共享台面在前，空的共享砧板在后。"""
    return free_slots(world, STATIC, bb) + [
        st for st in shared_boards(world, bb) if _board_free(world, st)]


def channel_keys(world, bb=None):
    # type: (object, object) -> List[str]
    """此刻可用的通道（渡运网格按名排序在前，传送带随后，静态通道在后）。"""
    keys = sorted(shuttle.shuttle_grids(world))
    # 刚开过的传送带也在 keys 里：候选仍要生成，SendAcross 会端着东西等
    # 接缝重开。否则一帧 flash 就让所有送件候选消失，AI 直接停摆
    # （trace-20261111-001945 t=2934）。
    # 4-5 的方向开关不在这里闸：方向不对由 SendAcross 树里的 _ensure_direction_ok
    # 前置节点去翻，不把候选掐掉。
    if conveyor_slots(world, bb) or conveyor_waitable(bb):
        keys.append(CONVEYOR)
    if static_slots(world, bb):
        keys.append(STATIC)
    return keys


def slots(world, key, bb=None):
    # type: (object, str, object) -> List[Dict]
    """某条通道的全部台面（传送带通道返回可用的传送带段）。"""
    if key == STATIC:
        return list(static_slots(world, bb))
    if key == CONVEYOR:
        return list(conveyor_slots(world, bb))
    return [st for st in shuttle.shuttle_stations(world)
            if str(st.get("grid") or "") == str(key)]


def slot_of(world, entry, bb=None):
    # type: (object, Optional[Dict], object) -> Optional[Dict]
    """entry（盘 / 料）此刻放在哪条通道的台面上；手持 / 不在通道上返回 None。"""
    if not isinstance(entry, dict) or entry.get("held_by"):
        return None
    st = conveyor_station_of(world, entry)
    if st is not None:
        return st
    st = shuttle.station_of(world, entry)
    if st is not None:
        return st
    shared = static_slots(world, bb) + shared_boards(world, bb)
    if not shared:
        return None
    from ai.space import occupancy as O
    support = O._support_station(entry, shared)
    if support is None:
        return None
    return support


def channel_of_slot(world, st, bb=None):
    # type: (object, Dict, object) -> Optional[str]
    if is_conveyor_station(world, st):
        return CONVEYOR
    if shuttle.is_shuttle_station(world, st):
        return str(st.get("grid"))
    if any(s is st for s in static_slots(world, bb) + shared_boards(world, bb)):
        return STATIC
    return None


def keep_free(key):
    # type: (str) -> int
    return KEEP_FREE.get(key, SHUTTLE_KEEP_FREE)


def free_slots(world, key=None, bb=None):
    # type: (object, Optional[str], object) -> List[Dict]
    """通道上空着的台面/传送带段（没盘、没料、没锅）；key=None 时全部通道。"""
    keys = [key] if key is not None else channel_keys(world, bb)
    out = []  # type: List[Dict]
    for k in keys:
        for st in slots(world, k, bb):
            if not slot_occupied(world, st):
                out.append(st)
    return out


def has_capacity(world, key, bb=None):
    # type: (object, str, object) -> bool
    """这条通道还能再放一份（空格数 > 留给回程的格数）。"""
    return len(free_slots(world, key, bb)) > keep_free(key)

"""网格寻路：全图距离场 + 交互位计算。

与 oc/ai/world.py 的区别：原代码对每个目标单独跑一次 Dijkstra
（`bfs_to_station`），候选 task 一多就是 O(候选数 × 图)。这里改为每拍
从厨师位置 flood-fill 一次得到全图距离，ETA 查表 O(1)（spec §6.4）。

代价模型与原 `_step_cost` 保持一致：逆传送带方向走要长按，代价 8。
所以这是 Dijkstra，不是均匀代价 BFS——若用 BFS 会把逆带路径算便宜。
"""
import heapq
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

BELT_REVERSE_COST = 8

_NEIGHBORS = ((-1, 0), (1, 0), (0, -1), (0, 1))


def step_cost(belt_dir, cur, nxt):
    # type: (Dict, Tuple[int, int], Tuple[int, int]) -> int
    """cur → nxt 的移动代价。逆着传送方向走要长按，代价高。"""
    d = (nxt[0] - cur[0], nxt[1] - cur[1])
    bd = belt_dir.get(cur)
    if bd and d[0] * bd[0] + d[1] * bd[1] < 0:
        return BELT_REVERSE_COST
    return 1


def cross_grid_bridges(primary_to_world, other_cells_world, max_gap=0.5):
    # type: (Callable[[int,int], Tuple[float,float]], List[Tuple[Tuple[int,int],Tuple[float,float]]], float) -> Dict[Tuple[int,int], Tuple[int,int]]
    """主网格边界格与其它网格格之间的虚拟邻接（spec §4.3/§7）。

    两个网格世界坐标相距 < max_gap 即算相邻——§4.3 实测两网格间隙 0.4，
    远小于格宽 1.2，物理上贴着。扫描范围只需主网格附近有限格（调用方
    传入的 primary 候选格集合，不在这里枚举全图——避免 O(全图×全图)）。

    返回值只含"确实找到桥"的格，调用方（distance_field）据此在
    Dijkstra 展开时把它们当四邻的第五个方向处理。
    """
    bridges = {}  # type: Dict[Tuple[int, int], Tuple[int, int]]
    # 候选主网格格：对每个附属格世界坐标，反推可能邻接的主网格格范围
    # （世界坐标差 < max_gap 的格，格宽通常 1.2，候选最多 3x3）。
    for other_cell, (ox, oz) in other_cells_world:
        # 简化：由调用方保证 other_cells_world 已经是"值得检查"的边界候选，
        # 这里只需要在主网格坐标系里反推最近格并验证距离。
        best = None
        best_d = None
        for gx in range(-50, 51):           # 实际使用中由调用方传入更紧的范围
            for gz in range(-50, 51):
                px, pz = primary_to_world(gx, gz)
                d = ((px - ox) ** 2 + (pz - oz) ** 2) ** 0.5
                if best_d is None or d < best_d:
                    best, best_d = (gx, gz), d
        if best is not None and best_d <= max_gap:
            bridges[best] = other_cell
    return bridges


def distance_field(start, is_walkable, belt_dir, blocked=None, max_cells=4096,
                   soft_cost=None, bridges=None, edge_blocked=None):
    # type: (Tuple[int, int], Callable[[int, int], bool], Dict, Optional[Set], int, Optional[Dict], Optional[Dict], Optional[Callable[[Tuple[int,int], Tuple[int,int]], bool]]) -> Dict[Tuple[int, int], int]
    """从 start 做 Dijkstra，返回 {格子: 最小代价}。

    start 本身总在结果里（代价 0），即使它被判为不可走——厨师可能正站在
    被站点占用兜底误判的格子上，直接返回空会让所有 ETA 失效。

    soft_cost: P1 周边软代价图，{cell: extra_cost}（Plan 4 Task 2）。

    bridges（新增，spec §4.3/§7）：{主网格格: 附属网格格} 的虚拟邻接表。
    展开到桥格时代价记 1（与普通一步相同），桥对面的格子不再受
    is_walkable 约束（附属网格的可走性由另一次独立调用覆盖——当前实现
    不递归处理"桥接链"，一次只跨一层，气球关只有两网格，够用）。

    edge_blocked: 可选。cur → nxt 这一步是否被薄墙挡住（occupancy.
    make_edge_blocker 返回）。薄墙夹在两格中心之间、点判漏掉，但角色物理
    上跨不过去；这里在邻接层把它拦下，两格各自仍可走。
    """
    blocked = set(blocked or ())
    soft_cost = soft_cost or {}
    bridges = bridges or {}
    dist = {start: 0}
    pq = [(0, start)]
    while pq:
        if len(dist) > max_cells:
            break          # 谓词无界（例如无 nodeMap 帧），停在上限而非耗尽内存
        cost, cur = heapq.heappop(pq)
        if cost > dist.get(cur, 1 << 60):
            continue
        for dx, dz in _NEIGHBORS:
            nxt = (cur[0] + dx, cur[1] + dz)
            if nxt in blocked:
                continue
            if not is_walkable(nxt[0], nxt[1]):
                continue
            if edge_blocked is not None and edge_blocked(cur, nxt):
                continue
            # 基础代价 + P1 软代价
            ncost = cost + step_cost(belt_dir, cur, nxt) + soft_cost.get(nxt, 0)
            if ncost < dist.get(nxt, 1 << 60):
                dist[nxt] = ncost
                heapq.heappush(pq, (ncost, nxt))
        bridge_target = bridges.get(cur)
        if bridge_target is not None and bridge_target not in blocked:
            ncost = cost + 1
            if ncost < dist.get(bridge_target, 1 << 60):
                dist[bridge_target] = ncost
                heapq.heappush(pq, (ncost, bridge_target))
    return dist


def _interact_cells(gx, gz, is_walkable, belt_cells):
    # type: (int, int, Callable[[int, int], bool], Set[Tuple[int, int]]) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]
    """四邻里可走的格子，按 (非传送带, 全部) 两组返回。"""
    preferred = []  # type: List[Tuple[int, int]]
    fallback = []   # type: List[Tuple[int, int]]
    for dx, dz in _NEIGHBORS:
        cell = (gx + dx, gz + dz)
        if not is_walkable(cell[0], cell[1]):
            continue
        fallback.append(cell)
        if cell not in belt_cells:
            preferred.append(cell)
    return preferred, fallback


def interact_cells(gx, gz, is_walkable, belt_cells=None):
    # type: (int, int, Callable[[int, int], bool], Optional[Set]) -> List[Tuple[int, int]]
    """站点 (gx,gz) 的可站立交互位（四邻）。

    默认排除传送带格：站上面会被推走，放/取料不稳。但若四邻全是传送带，
    则放行它们——否则该站点永远不可达。
    """
    preferred, fallback = _interact_cells(
        gx, gz, is_walkable, set(belt_cells or ()))
    return preferred if preferred else fallback


def interact_cells_with_belts(gx, gz, is_walkable, belt_cells=None):
    # type: (int, int, Callable[[int, int], bool], Optional[Set]) -> List[Tuple[int, int]]
    """交互位含传送带格（非传送带在前）。

    供「非传送带位全不可达」时兜底：可达性检查在调用方用距离场做，这里
    只负责把被 interact_cells 排除的传送带位放回来。站在传送带上交互要靠
    TapWhileHolding 顶住反向键（spec §7.2）。
    """
    _, fallback = _interact_cells(
        gx, gz, is_walkable, set(belt_cells or ()))
    return fallback


def interact_cells_multi(cells, is_walkable, belt_cells=None, include_belts=False):
    # type: (Iterable[Tuple[int, int]], Callable[[int, int], bool], Optional[Set], bool) -> List[Tuple[int, int]]
    """多格站点（出餐口 1×2）的可站立交互位：各占地格的四邻并集。

    出餐口这类多格站点，游戏接受站在它任一占地格的邻侧交互（placement
    锚点仍报单一交互格）。只用交互格单格的四邻会漏掉另一半占地旁边的
    交互位，角色已站在能送餐的位置却还要多挪一格。并集去重；传送带规则
    与 interact_cells / interact_cells_with_belts 一致（include_belts=True
    放行传送带位，供「非传送带位全不可达」时兜底）。
    """
    preferred = set()  # type: Set[Tuple[int, int]]
    fallback = set()   # type: Set[Tuple[int, int]]
    belts = set(belt_cells or ())
    for gx, gz in cells:
        p, f = _interact_cells(gx, gz, is_walkable, belts)
        preferred.update(p)
        fallback.update(f)
    if include_belts:
        return list(fallback)
    return list(preferred) if preferred else list(fallback)


def nearest_cost(field, cells):
    # type: (Dict, Iterable[Tuple[int, int]]) -> Optional[Tuple[Tuple[int, int], int]]
    """cells 中在距离场里代价最小的格子及其代价。都不可达则 None。"""
    best = None
    for cell in cells:
        cost = field.get(cell)
        if cost is None:
            continue
        if best is None or cost < best[1]:
            best = (cell, cost)
    return best


def _predecessor(field, cur, belt_dir, soft_cost=None, edge_blocked=None):
    # type: (Dict, Tuple[int, int], Dict, Optional[Dict]) -> Optional[Tuple[int, int]]
    """cur 在距离场里的合法前驱。

    判据是**代价账对得上**：field[n] + step_cost(n → cur) == field[cur]，
    而不是"field 值最小的邻居"。

    为什么不能用梯度下降：step_cost 按**离开**的格子计代价，是非对称的——
    step_cost(belt={(0,0):(1,0)}, (0,0)→(-1,0)) == 8，而反方向
    step_cost(..., (-1,0)→(0,0)) == 1。同一条边两个方向代价不同，所以
    "最小邻居"会选出距离场里根本没走过的边。spec §11.2.1 实测真实帧
    60 个可达格里有 2 个（均在传送带排）被梯度下降选错。
    """
    want = field.get(cur)
    extra = (soft_cost or {}).get(cur, 0)
    if want is None:
        return None
    for dx, dz in _NEIGHBORS:
        n = (cur[0] + dx, cur[1] + dz)
        c = field.get(n)
        if c is None:
            continue
        # 同代价邻居未必是建场时走过的边：薄墙两侧的格可能绕路可达。
        if edge_blocked is not None and edge_blocked(n, cur):
            continue
        # 软代价按进入的格子收费，必须与 distance_field 的账一致。
        if c + step_cost(belt_dir, n, cur) + extra == want:
            return n
    return None


def trace_back(field, goal, belt_dir, soft_cost=None, edge_blocked=None):
    # type: (Dict, Tuple[int, int], Dict, Optional[Dict]) -> Optional[List[Tuple[int, int]]]
    """从距离场回溯 start → goal 的格子序列（含两端）。

    goal 不在 field 里（不可达）返回 None。start 自身返回 [start]。
    """
    if goal not in field:
        return None
    path = [goal]
    cur = goal
    # 上限 len(field)：每格最多进路径一次，断链/环都在这里止住而不是死循环
    for _ in range(len(field)):
        if field[cur] == 0:
            path.reverse()
            return path
        prev = _predecessor(field, cur, belt_dir, soft_cost, edge_blocked)
        if prev is None:
            return None          # 距离场被破坏，交还调用方（原语会转 FAILURE）
        path.append(prev)
        cur = prev
    return None


def first_step(field, goal, belt_dir, soft_cost=None, edge_blocked=None):
    # type: (Dict, Tuple[int, int], Dict, Optional[Dict]) -> Optional[Tuple[int, int]]
    """从 start 出发朝 goal 的下一格。

    已站在 goal 上，或 goal 不可达，都返回 None——调用方据此区分
    "到了"（配合自己的到位判定）与"去不了"。
    """
    path = trace_back(field, goal, belt_dir, soft_cost, edge_blocked)
    if not path or len(path) < 2:
        return None
    return path[1]

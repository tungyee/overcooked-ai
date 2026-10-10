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


def distance_field(start, is_walkable, belt_dir, blocked=None, max_cells=4096, soft_cost=None):
    # type: (Tuple[int, int], Callable[[int, int], bool], Dict, Optional[Set], int, Optional[Dict]) -> Dict[Tuple[int, int], int]
    """从 start 做 Dijkstra，返回 {格子: 最小代价}。

    start 本身总在结果里（代价 0），即使它被判为不可走——厨师可能正站在
    被站点占用兜底误判的格子上，直接返回空会让所有 ETA 失效。

    soft_cost: P1 周边软代价图，{cell: extra_cost}（Plan 4 Task 2）。
    """
    blocked = set(blocked or ())
    soft_cost = soft_cost or {}
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
            # 基础代价 + P1 软代价
            ncost = cost + step_cost(belt_dir, cur, nxt) + soft_cost.get(nxt, 0)
            if ncost < dist.get(nxt, 1 << 60):
                dist[nxt] = ncost
                heapq.heappush(pq, (ncost, nxt))
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


def _predecessor(field, cur, belt_dir, soft_cost=None):
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
        # 软代价按进入的格子收费，必须与 distance_field 的账一致。
        if c + step_cost(belt_dir, n, cur) + extra == want:
            return n
    return None


def trace_back(field, goal, belt_dir, soft_cost=None):
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
        prev = _predecessor(field, cur, belt_dir, soft_cost)
        if prev is None:
            return None          # 距离场被破坏，交还调用方（原语会转 FAILURE）
        path.append(prev)
        cur = prev
    return None


def first_step(field, goal, belt_dir, soft_cost=None):
    # type: (Dict, Tuple[int, int], Dict, Optional[Dict]) -> Optional[Tuple[int, int]]
    """从 start 出发朝 goal 的下一格。

    已站在 goal 上，或 goal 不可达，都返回 None——调用方据此区分
    "到了"（配合自己的到位判定）与"去不了"。
    """
    path = trace_back(field, goal, belt_dir, soft_cost)
    if not path or len(path) < 2:
        return None
    return path[1]

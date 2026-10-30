"""规划与食材查询共用当前帧的可达性；未知拓扑保留离线兼容。"""
from ai.space import navigation, occupancy, seams
from ai.query import predicates as P


def field_for(world, bb=None, name=P.P2_NAME, soft_cost=None):
    # type: (object, object, str, object) -> object
    terrain = getattr(world, 'terrain', {})
    bounded = isinstance(terrain, dict) and bool(terrain.get(occupancy.LAYER_GROUND))
    raw = getattr(world, 'raw', {})
    if not bounded and not occupancy.parse_level_bounds(raw.get('level_bounds')):
        return None  # 无边界的旧合成帧不能做完整可达性证明。
    chef = P.chef_by_name(world.chefs, name)
    start = P.chef_cell(chef, world.to_grid)
    if start is None:
        return {}
    scratch = getattr(bb, 'scratch', None)
    tick = getattr(bb, 'tick', 0)
    blocked = {tuple(cell) for cell, until in (getattr(scratch, 'bad_cells', None) or {}).items()
               if until > tick}
    # 移动篮子护栏（已按用户要求注释）：篮子移动且 P2 不在篮子上时，把篮子
    # 格子拉黑，避免跨网格踩空掉落（trace-20261017-215633 t=1121 respawn 5s）。
    # 注释后 planner 不再因篮子移动而把料箱/洗碗池判 unreachable。
    # if getattr(bb, 'balloon_moving', False) and hasattr(world, 'balloon_cells'):
    #     platform = world.balloon_cells()
    #     if start not in platform:
    #         blocked |= platform
    cache = getattr(world, '_reach_fields', None)
    if cache is None:
        cache = world._reach_fields = {}
    # 门洞时间表（bb.seam_clock）参与寻路：同一帧里 now 不变，按 now 进缓存键。
    clock = getattr(bb, 'seam_clock', None)
    key = (start, frozenset(blocked), tuple(sorted((soft_cost or {}).items())),
           getattr(clock, 'now', None))
    if key not in cache:
        # 寻路谓词叠了平台接缝门洞（ai/space/seams.py），站位判定仍用 world.walkable。
        walkable, edge_blocked = seams.nav_predicates(world, bb)
        cache[key] = navigation.distance_field(start, walkable, world.belt_dir,
            blocked, soft_cost=soft_cost, edge_blocked=edge_blocked,
            wait_cost=seams.wait_cost_of(world, bb), jumps=seams.nav_jumps(world, bb)[0])
    return cache[key]


def context(world, bb=None, name=P.P2_NAME):
    # type: (object, object, str) -> dict
    return dict(w=world, bb=bb, chef=P.chef_by_name(world.chefs, name),
                field=field_for(world, bb, name))


def cell_of(world, entry):
    # type: (object, dict) -> object
    if entry.get('x') is not None and entry.get('z') is not None:
        return world.to_grid(entry['x'], entry['z'])
    if entry.get('gx') is not None and entry.get('gz') is not None:
        grids = getattr(world, 'grids', None)
        if entry.get('grid') and grids is not None:
            point = grids.to_world(str(entry['grid']), entry['gx'], entry['gz'])
            if point is not None:
                return world.to_grid(*point)
        return (entry['gx'], entry['gz'])
    return None


def can_interact(ctx, entry):
    # type: (dict, dict) -> bool
    field = ctx.get('field')
    if field is None:
        return True
    goals = _interact_goals(ctx, entry)
    if goals is True:
        return True
    return any(goal in field for goal in goals or ())


def interact_wait(ctx, entry):
    # type: (dict, dict) -> float
    """走到 entry 旁交互的最短路上要在门洞口等几秒（不可达 / 不用等返回 0）。"""
    field = ctx.get('field')
    waits = getattr(field, 'waits', None)
    if not waits:
        return 0.0
    goals = _interact_goals(ctx, entry)
    if goals is True or not goals:
        return 0.0
    reach = [(field[g], waits.get(g, 0)) for g in goals if g in field]
    if not reach:
        return 0.0
    return min(reach)[1] * seams.SEC_PER_STEP


def task_wait_seconds(task, ctx):
    # type: (object, dict) -> float
    """task 要碰的资源里，最晚那个的等门时间（秒）。与 task_veto 同一份资源表。"""
    field = ctx.get('field')
    if not getattr(field, 'waits', None):
        return 0.0
    worst = 0.0
    for rid in _task_resource_ids(task):
        if rid == 'bin':
            continue
        target = resource(ctx['w'], rid)
        if target is not None:
            worst = max(worst, interact_wait(ctx, target))
    destination = _DESTINATIONS.get(task.TYPE)
    if destination:
        waits = [interact_wait(ctx, s) for s in ctx['w'].stations
                 if s.get('t') == destination and can_interact(ctx, s)]
        if waits:
            worst = max(worst, min(waits))
    return worst


def _interact_goals(ctx, entry):
    # type: (dict, dict) -> object
    """entry 的可用交互站位集合；手持物是本人拿着返回 True（无需走）。"""
    if not isinstance(entry, dict):
        return set()
    chef = ctx.get('chef') or {}
    holder = entry.get('held_by')
    if holder:
        return True if holder == chef.get('name') else set()
    world = ctx['w']
    cell = cell_of(world, entry)
    if cell is None:
        return set()
    cells = {cell}
    if entry.get('t') == 'plate_station' and hasattr(world, 'mapping'):
        cells = occupancy.station_block_cells(entry, world.terrain, world.mapping)
    elif entry.get('t') == 'washing':
        from ai.query.stations import sink_place_cells
        cells = sink_place_cells(entry, world.stations, world.to_grid)
    else:
        # 与执行层 move._station_interact_cells 同一口径：按占地（含承载台面）
        # 取交互位，移动篮亚格偏移时盘不会因量化到台面后一格而判不可达。
        cells = occupancy.interaction_footprint(entry, cell, world.stations,
                                                world.to_grid, world.to_world,
                                                world.walkable)
    goals = set(navigation.interact_cells_multi(cells, world.walkable, world.belt_cells, include_belts=True))
    if world.walkable(*cell):
        goals.add(cell)
    # 执行层认定「该目标在此站位交互失败」后，规划层也必须读取同一事实。
    # 否则 NO_ABANDON 的盛饭任务会每拍重建，在唯一坏站位上失败数百次。
    bb = ctx.get('bb')
    scratch = getattr(bb, 'scratch', None)
    tick = getattr(bb, 'tick', 0)
    rid = P.resource_id(entry)
    bad = {tuple(stance) for (resource, stance), until
           in (getattr(scratch, 'bad_pairs', None) or {}).items()
           if resource == rid and until > tick}
    return goals - bad


def resource(world, rid):
    # type: (object, object) -> object
    if isinstance(rid, tuple) and len(rid) == 2 and all(isinstance(v, (int, float)) for v in rid):
        return dict(gx=rid[0], gz=rid[1])
    groups = (P.all_plates(world), world.stations, world.cookers, world.workstations,
              world.items, getattr(world, 'extinguishers', ()))
    for group in groups:
        for entry in group:
            if isinstance(rid, tuple):
                if P.resource_id(entry) == rid:
                    return entry
            elif str(entry.get('n')) == str(rid):
                return entry
    return None


_DESTINATIONS = {'DeliverPlate': 'plate_station', 'ResetPlate': 'bin', 'Discard': 'bin'}


def _task_resource_ids(task):
    # type: (object) -> list
    ids = [rid for rid, _ in task.claims()]
    plate = getattr(task, 'plate_id', None)
    if plate is not None and plate not in ids:
        ids.append(plate)
    return ids


def task_veto(task, ctx):
    # type: (object, dict) -> str
    # ThrowBatch 的消费站本就可能在对面（分割地图）：来源与出手位的可达性
    # 由它自己的 feasible 判（throw_supply.sources / launch_plan）。
    if ctx.get('field') is None or task.TYPE == 'ThrowBatch':
        return ''
    for rid in _task_resource_ids(task):
        if rid == 'bin' and task.TYPE == 'Discard':
            continue  # 逻辑共享资源，下面按站点类型解析真实垃圾桶。
        target = resource(ctx['w'], rid)
        if target is None:
            return 'missing_resource'
        if not can_interact(ctx, target):
            return 'unreachable'
    destination = _DESTINATIONS.get(task.TYPE)
    if destination and not any(s.get('t') == destination and can_interact(ctx, s)
                               for s in ctx['w'].stations):
        return 'unreachable_destination'
    return ''

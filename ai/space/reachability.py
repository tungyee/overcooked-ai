"""规划与食材查询共用当前帧的可达性；未知拓扑保留离线兼容。"""
from ai.space import navigation, occupancy
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
    key = (start, frozenset(blocked), tuple(sorted((soft_cost or {}).items())))
    if key not in cache:
        cache[key] = navigation.distance_field(start, world.walkable, world.belt_dir,
            blocked, soft_cost=soft_cost, edge_blocked=getattr(world, 'edge_blocked', None))
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
    if not isinstance(entry, dict):
        return False
    chef = ctx.get('chef') or {}
    holder = entry.get('held_by')
    if holder:
        return holder == chef.get('name')
    world = ctx['w']
    cell = cell_of(world, entry)
    if cell is None:
        return False
    cells = {cell}
    if entry.get('t') == 'plate_station' and hasattr(world, 'mapping'):
        cells = occupancy.station_block_cells(entry, world.terrain, world.mapping)
    elif entry.get('t') == 'washing':
        from ai.query.stations import sink_place_cells
        cells = sink_place_cells(entry, world.stations, world.to_grid)
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
    return any(goal in field and goal not in bad for goal in goals)


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


def task_veto(task, ctx):
    # type: (object, dict) -> str
    if ctx.get('field') is None or task.TYPE == 'Throw':
        return ''
    ids = [rid for rid, _ in task.claims()]
    plate = getattr(task, 'plate_id', None)
    if plate is not None and plate not in ids:
        ids.append(plate)
    for rid in ids:
        if rid == 'bin' and task.TYPE == 'Discard':
            continue  # 逻辑共享资源，下面按站点类型解析真实垃圾桶。
        target = resource(ctx['w'], rid)
        if target is None:
            return 'missing_resource'
        if not can_interact(ctx, target):
            return 'unreachable'
    destination = {'DeliverPlate': 'plate_station', 'ResetPlate': 'bin', 'Discard': 'bin'}.get(task.TYPE)
    if destination and not any(s.get('t') == destination and can_interact(ctx, s)
                               for s in ctx['w'].stations):
        return 'unreachable_destination'
    return ''

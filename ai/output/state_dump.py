# -*- coding: utf-8 -*-
"""Trace 状态快照：把一帧 World 压成可 JSON 序列化的精简字典。

复盘「为什么熟了不拿去装盘」时，只有 task/keys 不够——得看到盘里有什么、
锅在哪、灶熟没熟、传送带在哪、P2 卡在哪一格。本模块把 World 里各类实体
（厨师 / 订单 / 盘 / 板 / 灶 / 散落物 / 站点 / 地图）压成紧凑 dict，供
trace.emit 的 state 字段逐拍落盘。

设计取舍：
  - 只保留排查会看、且可 JSON 序列化的字段；nodeMap 位串不逐拍落，只留
    行列数（World 已解析过）。
  - 所有元组转 list、set 转排序 list，保证 json.dumps 不炸。
  - 实体 dict 直接浅拷贝原样透传：它们来自插件 JSON，本就是 JSON 原生值。
"""
from typing import Any, Dict, List, Optional

from ai.query import predicates as P


def snapshot(world):
    # type: (Any) -> Dict
    """把一帧 World 压成精简 dict。

    Args:
        world: ai.state.world.World 实例（只读视图）。

    Returns:
        可直接 json.dumps 的 dict。字段：
          level / phase / paused / score / timer
          chefs / orders / plates / workstations / cookers / items
          extinguishers / stations / recipes / map
    """
    raw = getattr(world, "raw", None) or {}
    return {
        "level": getattr(world, "level", None),
        "phase": getattr(world, "phase", None),
        "paused": bool(getattr(world, "paused", False)),
        "plugin_version": raw.get('plugin_version'),
        "protocol_version": raw.get('protocol_version'),
        "session_id": raw.get('session_id'),
        "scene_epoch": raw.get('scene_epoch'),
        "score": raw.get("score"),
        "timer": raw.get("timer"),
        "chefs": [_chef(world, c) for c in (getattr(world, "chefs", None) or [])],
        "orders": [_copy(o) for o in (getattr(world, "orders", None) or [])],
        "plates": [_copy(p) for p in (getattr(world, "plates", None) or [])],
        "held_plates": [_copy(p) for p in (getattr(world, "held_plates", None) or [])],
        "workstations": [_copy(s) for s in (getattr(world, "workstations", None) or [])],
        "cookers": [_copy(c) for c in (getattr(world, "cookers", None) or [])],
        "items": [_copy(i) for i in (getattr(world, "items", None) or [])],
        "extinguishers": [_copy(e) for e in (getattr(world, "extinguishers", None) or [])],
        "stations": [_copy(s) for s in (getattr(world, "stations", None) or [])],
        "recipes": [_recipe(r) for r in (getattr(world, "recipes", None) or [])],
        "map": _map(world),
    }


def _copy(d):
    # type: (Dict) -> Dict
    """浅拷贝 dict，避免 trace 落盘时与 World 内部结构共享引用。"""
    return dict(d) if isinstance(d, dict) else {}


def _chef_cell(world, chef):
    # type: (Any, Dict) -> Optional[List[int]]
    """厨师当前所在网格格 (gx, gz)，拿不到返回 None。"""
    if not isinstance(chef, dict):
        return None
    x, z = chef.get("x"), chef.get("z")
    if x is None or z is None:
        return None
    try:
        return list(world.to_grid(x, z))
    except Exception:                     # noqa: BLE001
        return None


def _chef(world, c):
    # type: (Any, Dict) -> Dict
    d = _copy(c)
    d["cell"] = _chef_cell(world, c)
    return d


def _recipe(r):
    # type: (Dict) -> Dict
    """配方只留排查会用到的字段（配方本身是静态大表，全量太吵）。"""
    if not isinstance(r, dict):
        return {}
    out = {
        "name": r.get("name"),
        "comp": r.get("comp"),
        "cook": r.get("cook"),
        "plate": r.get("plate"),
    }
    # 切配跟着食材走（插件 0.19+ 随配方下发）：回放从 trace 重建 RecipeBook
    # 时少了它，传送带供料、无箱子的食材会被当成不用切。旧帧没有就不写，
    # 让 RecipeBook 照旧退回箱子。
    for key in ("chop", "chop_stages"):
        if key in r:
            out[key] = r.get(key)
    return out


def _raw_grid(world):
    # type: (Any) -> Optional[Dict]
    raw = getattr(world, "raw", None) or {}
    g = raw.get("grid")
    return g if isinstance(g, dict) else None


def _map(world):
    # type: (Any) -> Dict
    """地图 / 导航相关：多网格、传送带、落火、厨师所在格（§5.3/§5.7）。

    还落 terrain / occupancy：§5.3 之后可走性判定依据是层号地形 AABB +
    运行时占用格（belt/hazard 之外的两段），复盘「为什么这格被判成可走/
    不可走」必须看得到它们。trace-20261017-191650 的 no_progress 排查时
    快照正缺这两段，只能靠猜薄墙点判的根因——补上后可直接定位。
    """
    belt_cells = getattr(world, "belt_cells", None) or set()
    belt_dir = getattr(world, "belt_dir", None) or {}
    hazards = getattr(world, "hazards", None) or []
    chefs = getattr(world, "chefs", None) or []
    p2 = P.chef_by_name(chefs, P.P2_NAME)
    p1 = P.chef_by_name(chefs, P.P1_NAME)
    grid = _raw_grid(world) or {}
    raw = getattr(world, "raw", {}) or {}
    return {
        # 直接从 world.raw["grids"] 透传，不是从 world.grids 的 GridSet 对象取——
        # GridSet 没有可序列化成 list 的公开方法，原始协议字段本身就是 trace
        # 想要的形状，不需要转换一轮再转回去。
        "grids": raw.get("grids") or [],
        "grid": {
            "origin": grid.get("origin"),
            "size": grid.get("size"),
            "rows": getattr(world, "_nm_rows", 0),
            "cols": getattr(world, "_nm_cols", 0),
        },
        "hazards": [_copy(h) for h in hazards],
        # terrain / occupancy 原样透传：它们已是插件 JSON 原生值（terrain 是
        # {layer, boxes}，occupancy 是 {grid, cells}），World 不缓存解析结果，
        # 只能从 world.raw 读。
        "terrain": raw.get("terrain") or [],
        "occupancy": raw.get("occupancy") or [],
        "level_bounds": raw.get('level_bounds'),
        "belts": raw.get('belts') or [],
        "wind_zones": raw.get('wind_zones') or [],
        "belt_cells": sorted([list(c) for c in belt_cells]),
        "belt_dir": _belt_dir(belt_dir),
        "p2_cell": _chef_cell(world, p2) if p2 else None,
        "p1_cell": _chef_cell(world, p1) if p1 else None,
    }


def _belt_dir(belt_dir):
    # type: (Dict) -> Dict
    """传送带方向表：{(gx,gz): (dx,dz)} → {"gx,gz": [dx,dz]}。"""
    out = {}
    for cell, d in belt_dir.items():
        if isinstance(cell, tuple) and len(cell) == 2:
            key = "%d,%d" % (cell[0], cell[1])
        else:
            key = str(cell)
        out[key] = list(d) if isinstance(d, (tuple, list)) else d
    return out

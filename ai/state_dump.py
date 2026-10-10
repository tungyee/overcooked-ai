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

from ai import predicates as P


def snapshot(world):
    # type: (Any) -> Dict
    """把一帧 World 压成精简 dict。

    Args:
        world: ai.world.World 实例（只读视图）。

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
        "score": raw.get("score"),
        "timer": raw.get("timer"),
        "chefs": [_chef(world, c) for c in (getattr(world, "chefs", None) or [])],
        "orders": [_copy(o) for o in (getattr(world, "orders", None) or [])],
        "plates": [_copy(p) for p in (getattr(world, "plates", None) or [])],
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
    return {
        "name": r.get("name"),
        "comp": r.get("comp"),
        "cook": r.get("cook"),
        "plate": r.get("plate"),
    }


def _raw_grid(world):
    # type: (Any) -> Optional[Dict]
    raw = getattr(world, "raw", None) or {}
    g = raw.get("grid")
    return g if isinstance(g, dict) else None


def _map(world):
    # type: (Any) -> Dict
    """地图 / 导航相关：可玩区、传送带、厨师所在格。"""
    bounds = getattr(world, "bounds", None)
    belt_cells = getattr(world, "belt_cells", None) or set()
    belt_dir = getattr(world, "belt_dir", None) or {}
    chefs = getattr(world, "chefs", None) or []
    p2 = P.chef_by_name(chefs, P.P2_NAME)
    p1 = P.chef_by_name(chefs, P.P1_NAME)
    grid = _raw_grid(world) or {}
    return {
        "bounds": list(bounds) if bounds else None,
        "grid": {
            "origin": grid.get("origin"),
            "size": grid.get("size"),
            "rows": getattr(world, "_nm_rows", 0),
            "cols": getattr(world, "_nm_cols", 0),
        },
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

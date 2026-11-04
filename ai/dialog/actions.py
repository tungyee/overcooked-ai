# -*- coding: utf-8 -*-
"""动作级分工的元数据与作用域编译（spec §5.1 / §7.6）。

分工单位 = 「动作」（= 站点 = 边）。本模块给出两条静态映射：

- `ACTION_TASK_TYPES`：6 个**可分工动作** → 一组 task TYPE。LLM 的 `action`
  枚举只含这 6 个值；投掷/渡运（交接）、救援、housekeeping 都不在此。
- `FETCH_TASK_TYPES` / `OTHER_TASK_TYPES`：取料（planner 按料源可达性自动
  归位，不进枚举）与 auto_only/交接/救援（不开放认领）。

作用域编译：`action_scope(action)` 产出 `{(task_type, None)}`（动作级，命中
任意 item）；`action_scope(action, item)` 产出 `{(task_type, item.lower())}`
（精确到一个节点，供 `now`/`stop` 选对 task）。与 `ai/dialog/schema.py` 的
`scope_matches` 口径一致。

本模块纯数据 + 纯函数，不 import `ai.tasks`（避免反向依赖）；task TYPE 用
字符串字面量写死，由 `tests/test_actions.py::test_action_partition_covers_all_task_types`
钉住「映射恰好覆盖全部 TASK_TYPES、无重叠、无遗漏」。
"""
from typing import Dict, FrozenSet, Optional, Tuple

# 6 个可分工动作 → task TYPE（顺序即目录渲染顺序）。
ACTION_TASK_TYPES = {
    "切": ("Chop",),
    # 「下锅」= 把料送进灶：含 FetchToCooker（取料下锅这一步）。
    "下锅": ("FetchToCooker", "FillCooker", "PlaceCarrierInCooker"),
    "拌": ("Mix",),
    "装盘": ("FetchToPlate", "TakeCookedToPlate", "PlaceCleanPlate"),
    "送餐": ("DeliverPlate",),
    "洗盘": ("WashPlate",),
}  # type: Dict[str, Tuple[str, ...]]

# 取料/搬运：planner 按料源可达性自动归位，不进 LLM 枚举。
FETCH_TASK_TYPES = ("FetchToBoard", "FetchToCarrier", "Prefetch")  # type: Tuple[str, ...]

# auto_only / 跨边交接 / 救援：不开放认领。
OTHER_TASK_TYPES = (
    "ReturnPot", "RescueCooked", "MergePlates", "SendAcross", "ThrowBatch",
    "Discard", "RecoverLoosePot", "StashHeld", "ResetPlate", "Extinguish",
)  # type: Tuple[str, ...]

# 每个可分工动作绑定的站点类型（frame stations[].t），供「判」做粗粒度校验
# （站点在本关存在、P2 是否够得着）。装盘/送餐都归到 plate_station（盘/出餐
# 口一侧），精确站点由 planner 每拍选。
ACTION_STATION_TYPES = {
    "切": "chopping",
    "下锅": "cooking",
    "拌": "mixing",
    "装盘": "plate_station",
    "送餐": "plate_station",
    "洗盘": "washing",
}  # type: Dict[str, str]


def action_scope(action, item=None):
    # type: (str, Optional[str]) -> FrozenSet[Tuple[Optional[str], Optional[str]]]
    """动作 → 作用域。`item` 为 None 时是动作级 `{(type, None)}`，否则精确到
    节点 `{(type, item.lower())}`。"""
    types = ACTION_TASK_TYPES[action]
    if item is None:
        return frozenset((t, None) for t in types)
    item_l = str(item).lower()
    return frozenset((t, item_l) for t in types)

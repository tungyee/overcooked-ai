# -*- coding: utf-8 -*-
"""动作级作用域映射：ai/dialog/actions.py 的单元测试。"""
from ai import tasks as T
from ai.dialog.actions import (ACTION_TASK_TYPES, FETCH_TASK_TYPES,
                               OTHER_TASK_TYPES, action_scope)


def test_action_partition_covers_all_task_types():
    assert set(ACTION_TASK_TYPES) == {"切", "下锅", "拌", "装盘", "送餐", "洗盘"}
    covered = set()
    for types in ACTION_TASK_TYPES.values():
        covered |= set(types)
    covered |= set(FETCH_TASK_TYPES) | set(OTHER_TASK_TYPES)
    assert covered == set(T.TASK_TYPES), set(T.TASK_TYPES) - covered


def test_action_scope_without_item_is_type_level():
    assert action_scope("切") == frozenset({("Chop", None)})


def test_action_scope_with_item_is_node_level():
    assert action_scope("切", "Cucumber") == frozenset({("Chop", "cucumber")})

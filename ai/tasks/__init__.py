"""16 个 task 的身份、可行性、完成判定与树构建（spec §6.1 / §7.3）。

本包是 tasks 的唯一对外入口。调用方一律：

    from ai import tasks as T
    T.Chop("9", ("board_01",), "Cucumber")
    T.TASK_TYPES          # 全部 16 个 task 类型

**不要直接 import 子模块**——plate / cook / chop / assembly / cleanup
的分组边界是实现细节（为满足 spec §11.5 的 400 行上限而拆），不是 API。

下面的 `from ... import *` 看着粗糙，但它承担一个具体职责：**导入子模块
即触发其中的 @_register，TASK_TYPES 才完整**。少一行就静默少一组 task，
而 tree_viz 以 TASK_TYPES 为入口枚举——漏掉的 task 不会报错，只是看不到。
test_task_types_registry_is_complete 用计数钉住这一点。
"""
from ai.tasks.base import (OWNER, TASK_TYPES, TASK_TIMEOUT_TICKS, Task,
                           claims_available, ingredient_available,
                           ingredient_source)
from ai.tasks.plate import *        # noqa: F401,F403
from ai.tasks.cook import *         # noqa: F401,F403
from ai.tasks.chop import *         # noqa: F401,F403
from ai.tasks.assembly import *     # noqa: F401,F403
from ai.tasks.cleanup import *      # noqa: F401,F403
from ai.tasks.mix import *          # noqa: F401,F403
from ai.tasks.assembly import delivery_baseline_from


__all__ = [
    "OWNER",
    "TASK_TYPES",
    "TASK_TIMEOUT_TICKS",
    "Task",
    "claims_available",
    "ingredient_available",
    "ingredient_source",
    "delivery_baseline_from",
]


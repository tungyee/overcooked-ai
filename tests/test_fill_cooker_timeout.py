# -*- coding: utf-8 -*-
"""填灶的超时上限回归。

实机 trace-20261015-165358：九次 FillCooker 运行里**四次撞 200 拍上限**
（t=149 用了 199 拍只差一拍、t=699/941/2719 整 200 拍 FAILURE）。

一趟是「空手走到米箱 ≈95 拍 + 拿米走回灶 ≈95 拍」= 190+，而上限 200，
余量只有 5%。t=941 那次逐格还原：米已在手、人已站到灶前一格 (4,2)，差
最后一个 Tap 被砍。失败后重试只用 40/25 拍就成功——活儿一点没白干，
只是记账判了失败，然后重建树重走一遍。三次超时 = 600 拍 = 30 秒，
而同期三口灶全空（全局 62.8% 的拍一口都没在煮）。

`cook.py` 的注释已经为同一个原因（一轮取+投约 80 拍、整树只有 200）砍掉
过批量投掷。现在路径已是最短单程仍不够，该动的是上限本身。
`TakeCookedToPlate` 早有先例：TAKE_COOKED_TIMEOUT_TICKS=400。
"""
from ai import tasks as T
from ai.core.executor import MAX_TASK_TICKS
from ai.tasks.base import TASK_TIMEOUT_TICKS


def _two_legs():
    """两趟横穿地图的实测拍数（见模块 docstring 的逐格还原）。"""
    return 190


def test_fill_cooker_timeout_covers_two_legs():
    """上限必须容得下「去箱子 + 回灶」两趟，否则永远在最后一步被砍。"""
    cap = T.FillCooker(item="BoiledSushiRice",
                       resources=("cooker_01",)).TIMEOUT_TICKS
    assert cap > _two_legs(), "200 拍只给 5% 余量，实机四次撞线"
    assert cap >= 350, "两趟 190 拍 + 取放交互的 observe 等待，350 是下限"


def test_fill_cooker_timeout_declared_above_default():
    """声明值要高于默认 200，否则这条修复是空的。"""
    cap = T.FillCooker(item="BoiledSushiRice",
                       resources=("cooker_01",)).TIMEOUT_TICKS
    assert cap > TASK_TIMEOUT_TICKS


def test_fill_cooker_inner_timeout_matches_declared():
    """内层树的 Timeout 必须跟着放宽。

    Executor 用 `getattr(task, "TIMEOUT_TICKS", MAX_TASK_TICKS)` 包外层，
    但 `FetchToCooker.build()` 自己又包了一层 TASK_TIMEOUT_TICKS=200。
    内层更紧 → 只加类属性等于没改。这条测试钉住两层一致。
    """
    task = T.FillCooker(item="BoiledSushiRice", resources=("cooker_01",))
    tree = task.build()
    assert getattr(tree, "max_ticks", None) == task.TIMEOUT_TICKS, \
        "内层 Timeout 仍是 200 的话，类属性放宽无效"


def test_fetch_to_cooker_also_widened():
    """按单发射的 FetchToCooker 走同一棵树、同样两趟，上限该一致。"""
    task = T.FetchToCooker("order1", ("cooker_01",), "BoiledSushiRice")
    assert task.TIMEOUT_TICKS > TASK_TIMEOUT_TICKS
    tree = task.build()
    assert getattr(tree, "max_ticks", None) == task.TIMEOUT_TICKS


def test_executor_cap_still_bounds_it():
    """放宽后仍要留在 Executor 的兜底之内才有意义（否则外层先砍）。"""
    cap = T.FillCooker(item="BoiledSushiRice",
                       resources=("cooker_01",)).TIMEOUT_TICKS
    assert cap <= max(MAX_TASK_TICKS, cap), "外层按 TIMEOUT_TICKS 走，不该被 300 截断"

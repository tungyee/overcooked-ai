# -*- coding: utf-8 -*-
"""收益函数：送达一单到底值多少分。

**全部常数来自实测**，不是推演：从 trace-20261013-* / trace-20261014-*
里抽出 52 次「送达前后 score 的差分」回归得到，样本与断言见
`tests/test_economy.py::SAMPLES`。打分权重要有可比的量纲，就不能让这些
数字散落在 planner 里当魔法数。

实测结论三条：

1. `base` 固定 60/单（PlainPrawn 20），**与送达早晚无关**。
2. `tips` = 新鲜度档位 × 连击系数。档位只有三档（8 / 5 / 3），按
   `remaining / lifetime` 落在 >2/3、1/3~2/3、<1/3。
3. 连击系数在 combo < 3 时恒为 1 —— trace-20261014-120406 整局最好只爬到
   combo 2，所以 16 的 tips 里**一分连击红利都没有**。

还有一条反直觉的：**超时不扣分**。社区攻略（Steam）说超时罚 -10 coin，但
trace-20261014-120406 t=2771 order#3 过期时 base/tips 都没动，只有 combo
从 2 砸到 0。超时的真实代价是「连击归零 + 已投入的工时白费」，不是负分。
`EXPIRY_POINT_PENALTY = 0` 就是在钉这个事实，别再按罚金建模。

量级参考（给权重定标用）：新鲜度最多差 5 分（3→8，相对 base 60 只有 8%），
而 combo 从 2 到 3 系数翻倍。所以优先级是
**不丢单 >> 按序送 >> 送得新鲜**，新鲜度是顺带收，不是目标。
"""
from typing import Any, Optional

# 新鲜度三档小费（实测值）
TIP_FRESH = 8     # remaining > 2/3 lifetime
TIP_MID = 5       # 1/3 ~ 2/3
TIP_LATE = 3      # < 1/3

# 档位边界（lifetime 的比例）
FRESH_RATIO = 2.0 / 3.0
MID_RATIO = 1.0 / 3.0

# 超时的分数罚金：实测为 0（见模块注释）。代价在 combo 归零。
EXPIRY_POINT_PENALTY = 0

# 连击系数：combo >= 3 才起效，之后每级 +1（实测 3→2x、4→3x、5→4x）
COMBO_THRESHOLD = 3


def tip_tier(remaining, lifetime):
    # type: (Optional[float], Optional[float]) -> int
    """按剩余寿命比例给出新鲜度档位小费（未计连击系数）。

    残帧/缺字段时保守退到最低档：宁可低估收益，也不要凭空给高分把
    planner 骗去抢一个并不存在的档位。
    """
    if remaining is None or not lifetime:
        return TIP_LATE
    try:
        ratio = float(remaining) / float(lifetime)
    except (TypeError, ZeroDivisionError):
        return TIP_LATE
    if ratio > FRESH_RATIO:
        return TIP_FRESH
    if ratio > MID_RATIO:
        return TIP_MID
    return TIP_LATE


def combo_multiplier(combo):
    # type: (Any) -> int
    """连击系数。combo < 3 恒为 1；之后 combo-1（3→2、4→3、5→4）。

    >5 没有实测样本，按同一线性规律外推（单调不减，不会让高连击反而更差）。
    """
    try:
        c = int(combo)
    except (TypeError, ValueError):
        return 1
    if c < COMBO_THRESHOLD:
        return 1
    return c - 1


def tip_for(remaining, lifetime, combo):
    # type: (Optional[float], Optional[float], Any) -> int
    """送达这一单能拿到的小费 = 档位 × 连击系数。"""
    return tip_tier(remaining, lifetime) * combo_multiplier(combo)


def freshness_headroom(remaining, lifetime):
    # type: (Optional[float], Optional[float]) -> Optional[float]
    """距「掉到下一档」还有几秒。用于打分的边界效应。

    档位是台阶而非斜坡：`remaining` 刚好在边界上方时，「现在送 vs 晚 3 秒
    送」差整整一档，值得抢；离边界远则早送晚送没区别。所以打分要读这个
    余量，而不是线性的 remaining。

    已在最低档（没有下一档可掉）或缺字段时返回 None。
    """
    if remaining is None or not lifetime:
        return None
    try:
        rem = float(remaining)
        life = float(lifetime)
    except TypeError:
        return None
    for ratio in (FRESH_RATIO, MID_RATIO):
        edge = life * ratio
        if rem > edge:
            return rem - edge
    return None

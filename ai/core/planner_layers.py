# -*- coding: utf-8 -*-
"""层判定（spec §5）：把候选分到 8 个优先层。

**为什么分层**：扁平加权和里 17 项有 O(n²) 对交互，单组测试结构上查不出
任何一对。AGENTS.md §10.2 记的那次——urgency 改读 slack 后七个「灶空转该
填灶」的拍全部回退、982 个测试一个都没报警——就是这个性质的后果。分层后
跨类优先级由控制流隔离保证：有高层候选时，低层候选压根不参与排序。

**层序判据**：有倒计时的压过没倒计时的，倒计时短的压过长的。新 task 问
一句「它在抢救什么正在流失的东西」就知道该进哪层。

**层是派生值，不是状态**：每拍从 feasible 重新算。所以「高层动作不可达」
自然表现为该候选从 feasible 消失、top_layer 自己掉下来——不需要降层动作、
不需要失效检测（spec §8.4 记了被否掉的三个机制及理由）。
"""
from typing import Any, Callable, Optional, Tuple

# 数字小 = 层高。min() 依赖这个约定。
L_FIRE = 0           # 灶在烧，会蔓延
L_IRREVERSIBLE = 1   # 锅快糊/已糊：料会毁、还要灭火
L_RESCUE = 2         # 刚熟的锅：12 秒不可压缩的时钟在走
L_REDEEM = 3         # 订单寿命 + 立即到账的 60 分 base
L_UNBLOCK = 4        # 拼装链无落点（但洗完就恢复，没东西被毁）
# L_CAPACITY 现在是**空层**。它原先装 FillCooker，那是个设计错误：
# 本层序的判据是「有倒计时的压过没倒计时的」，而**空灶没有倒计时** ——
# 灶空着不会变质、不会毁料、不会蔓延。「灶空转的机会成本」是期望收益，
# 不是正在流失的时钟，期望收益之间的比较该用数值（那正是 L_PREP 的事）。
#
# 实机代价（trace-20261015-165358）：FillCooker 拿到 top_layer 的 1043 拍
# **全部**有 L6 备菜可做却被结构性压掉，一次例外都没有；而它被选中时
# **77% 的拍缺口只有 1 份**（cooker_idle 仅 1.9 分，本该输给中段备菜的
# 1.5~4）。L5 里只有 FillCooker 自己，打分形同虚设 —— 1.9 分无需跟任何
# 东西比较就赢了。1002 拍的填灶工时只换来 4 锅米。
#
# 换句话说，分层把 `W_COOKER_IDLE = 5.6` 那一整套标定旁路掉了（那个数字
# 是按「压过备菜真实合计 4.97、留 0.6 余量」定的，且饱和点为缺 3 份）。
# 放回 L_PREP 后这套关系自动重新生效，不需要新常数。
#
# 留着空壳而不删枚举值：照 L_FIRE 白名单为空的先例，删了会动层号连续性与
# TERM_WHITELIST 的完整性检查，收益为零。将来若有「有倒计时的产能类动作」
# （例如会过期的预制半成品），它该进这一层。
L_CAPACITY = 5       # 空层，见上
L_PREP = 6           # 无时钟
L_CLEANUP = 7        # 无时钟

# 层阈值。复用 _time_pressure 的实测曲线（0=生、1.0=刚熟、2.0=快糊）：
# 1.5 对应 ratio 0.75（烧糊路程走了四分之三）；1.0 正是 planner.py:1542
# 今天跳过承诺惯性用的同一个阈值，语义一致。
PRESSURE_IRREVERSIBLE = 1.5
PRESSURE_RESCUE = 1.0

# 备菜层的 task 类型。TakeCookedToPlate 在**快熟阶段**（pressure < 1.0）也
# 落这里：锅还没熟时它与备菜同层竞争，靠 pressure 项（快熟渐变）提前去端盘
# 等熟；一旦 pressure >= 1.0（刚熟）它仍先命中 L1/L2。
# **不是** planner._COMBO_PREP_TYPES——后者还含 PlaceCleanPlate（分层后在 L4）。
LAYER_PREP_TYPES = ("FetchToPlate", "FetchToBoard", "Chop",
                    "FetchToCooker", "FillCooker", "Mix", "PlaceToRelay",
                    "TakeCookedToPlate")

_PLATE_TYPES = ("WashPlate", "PlaceCleanPlate")


def build_rules(pressure_fn, plate_pressure_fn):
    # type: (Callable, Callable) -> Tuple
    """构造层序规则表。

    表可替换（重排 / 删条），谓词不可替换——谓词里的曲线是实测定标过的，
    不开放给策略层改（spec §5.1）。两条曲线由调用方注入，让本模块不 import
    planner（避免环），也让单测能喂固定值。
    """
    return (
        (L_FIRE,         lambda t, w: t.TYPE == "Extinguish"),
        (L_IRREVERSIBLE, lambda t, w: pressure_fn(t, w) >= PRESSURE_IRREVERSIBLE),
        (L_RESCUE,       lambda t, w: pressure_fn(t, w) >= PRESSURE_RESCUE),
        (L_REDEEM,       lambda t, w: t.TYPE == "DeliverPlate"),
        (L_UNBLOCK,      lambda t, w: (t.TYPE in _PLATE_TYPES
                                       and plate_pressure_fn(w) > 0)),
        # L_CAPACITY 现在没有成员，见下方 L_CAPACITY 常量处的说明。规则表里
        # 保留这一行（谓词恒假）而不是删掉，是为了让层号与表的行一一对应 ——
        # 将来若真有「有倒计时的产能类动作」，它该插在这里。
        (L_CAPACITY,     lambda t, w: False),
        (L_PREP,         lambda t, w: t.TYPE in LAYER_PREP_TYPES),
    )


def layer_of(task, world, pressure_fn=None, plate_pressure_fn=None, rules=None):
    # type: (Any, Any, Optional[Callable], Optional[Callable], Optional[Tuple]) -> int
    """这个候选属于哪一层。遍历规则表取第一个命中，无命中则 L_CLEANUP。

    纯函数，**不读 bb**：策略覆盖由调用方决定传哪张 rules，层判定本身只认
    (task, world)。这样它能脱离黑板单测，也不会让「当前策略」悄悄渗进一个
    看起来是纯查询的函数（spec §5.1）。
    """
    if rules is None:
        rules = build_rules(pressure_fn, plate_pressure_fn)
    for layer, pred in rules:
        if pred(task, world):
            return layer
    return L_CLEANUP


def layer_of_task(task, world):
    # type: (Any, Any) -> int
    """生产路径的层判定入口：接上 planner 的两条实测曲线。

    import 放在函数内是**防御性**的：今天 `ai/planner.py` 并不 import 本模块，
    所以模块级 import 也能过；但 planner 将来若在模块级消费本模块（Task 5 的
    消费点目前也在 `plan()` 函数内），两边就会成环。这两条曲线是纯函数、调用
    开销可忽略，保持局部 import 最省心。
    """
    from ai.core.planner import _plate_pressure, _time_pressure
    return layer_of(task, world, rules=build_rules(
        _time_pressure, lambda w: _plate_pressure(w)))


def layer_filter(candidates, world, layer_fn=None):
    # type: (Any, Any, Optional[Callable]) -> Tuple[Optional[int], list, dict]
    """把候选分层，返回 (最高层, 该层候选, 全部候选的层表)。

    `layers` 含**全部**候选而不只是 in_layer：trace 要靠它区分「这个候选
    被否决了」与「它可行但层不够高」——这是分层最大的可观测性收益。

    in_layer 保留 candidates 的原顺序：下游打分后的排序必须稳定（分层收窄
    候选集后同分概率显著上升，Python 的 sorted 是稳定排序，所以输入顺序
    决定同分时的胜者）。

    空候选返回 (None, [], {})：min() 对空序列抛 ValueError，调用方若没包
    在 `if candidates:` 里就会炸。

    **同 key 必同层**是个不变量，不是巧合：`Task.key` 就是四元身份
    （ai/tasks/base.py），而 layer_of_task 只读 TYPE 与 cooker_id，两者都由身份
    决定。但这里仍取 min 而不是让后写的覆盖——万一将来有调用方违反这个不变量，
    失败模式应该是「保守地当高层处理」，而不是「高层候选被静默降级混进低层」
    （实测后写覆盖会让 top_layer 算错且高层候选混入 in_layer）。
    """
    if layer_fn is None:
        layer_fn = layer_of_task
    layers = {}
    for cand in candidates:
        key = cand.task.key
        layer = layer_fn(cand.task, world)
        prev = layers.get(key)
        layers[key] = layer if prev is None else min(prev, layer)
    if not layers:
        return None, [], {}
    top = min(layers.values())
    in_layer = [c for c in candidates if layers[c.task.key] == top]
    return top, in_layer, layers


# 每层参与打分的 term（spec §7）。
#
# **关键性质：W_* 之间不再需要跨类可比。** W_COOKER_IDLE 只跟 W_ETA 比
# （同在 L5），W_ASYNC_RESOURCE 只在 L6 内部有意义。原来那些「必须压过
# 备菜的实际合计、但不能压过端刚熟的锅」式的约束，由层序承担。
#
# 不在任何白名单里的 term 不参与打分，但仍进 trace（便于复盘「这一项算
# 出来是多少、只是没被用」）。
TERM_WHITELIST = {
    # L0 单候选，不打分
    L_FIRE: frozenset(),
    # L1/L2 只看锅：越接近糊越先救。只留一项 → 退化成纯词典序，最可预测。
    # 不含 completion_ratio——锅的时钟不管盘满不满（spec §7 定过）。
    # pref 是对话偏好（spec §4.5）。L1/L2 只给**正分**开口：负分在
    # planner._matching_preferences 里已被免疫规则跳过，所以「我来煮」的
    # 负 claim 不会把「饭要糊了」压下去，而「你去煮」的 direct 仍能生效。
    L_IRREVERSIBLE: frozenset(["pressure", "pref"]),
    L_RESCUE: frozenset(["pressure", "pref"]),
    # L3 兑现：送哪种菜。combo 是这层的主角（它只能选菜、不能选单，
    # 见 planner._credited_order_id 的 55/0 实测）。
    L_REDEEM: frozenset(["freshness", "combo_head", "combo_break", "urgency",
                         "pref"]),
    # L4 解卡：0 盘 > 无盘就位 > 1 盘
    L_UNBLOCK: frozenset(["plate_pressure", "no_plate_placed", "pref"]),
    # L5 空层（见 L_CAPACITY 常量处）。白名单保留原内容：层里没有候选，
    # 它永远不会被 filter_terms 用到；留着是为了「将来往这层放东西」时
    # 不必同时想起登记白名单（未登记的层会 raise）。
    L_CAPACITY: frozenset(["cooker_idle", "eta", "pref"]),
    # L6 备菜：现有大部分项都在这层。deadline_lookahead 是「锅烧糊前瞻」
    # （订单到期前瞻那支已删，见 AGENTS.md §10.1/§10.2 的双计算说明）。
    # pressure 只对 TakeCookedToPlate 非零（快熟阶段 pressure < 1.0 时它落
    # 本层），别的备菜 task _time_pressure 恒 0，加了不影响它们。
    L_PREP: frozenset([
        "pressure",
        "urgency", "completion_ratio", "async_resource", "ingredient_reuse",
        "combo_head", "combo_break", "deadline_lookahead", "hands",
        "move_penalty", "eta", "relay_1_3_bonus",
        # 对话偏好（spec §4.5）在备菜层与既有项直接相加：direct 的 +4
        # 改同层先后，claim 的 -8 在层内被截止闸兜底（spec §4.6）。
        "pref",
        # cooker_idle 随 FillCooker 一起从 L5 迁来。**少了它填灶在本层净分
        # 只剩 eta 的负数**，会从「永远赢」直接翻到「永远输」、灶又全空 ——
        # 正是 FillCooker 当初被造出来要解决的问题（AGENTS.md §4 产能：
        # 59% 的拍三口灶一个都没在煮、三单全过期）。只给 FillCooker 算，
        # 按单发射的 FetchToCooker 走 async_resource，不重复给。
        "cooker_idle",
    ]),
    # L7 清理：就近。pref 让「去洗碗」这类账本约定在清理层也生效。
    L_CLEANUP: frozenset(["eta", "hands", "pref"]),
}


def filter_terms(terms, layer):
    # type: (dict, Optional[int]) -> Tuple[float, dict]
    """按层白名单筛 term，返回 (分数, 留下的项)。

    分数是留下项的**和**——不要改回手写加法。planner 为此踩过一次：旧版
    把 16 项重抄一遍，漏掉求和那处的症状是「trace 里有这一项但它不影响
    选择」，而单测断言的是 terms、一个都不会报警（见 cf2fc64）。
    推论：凡进 terms 的都必须是已带符号的最终贡献（惩罚项存负数）。

    layer 为 None（无候选）时返回 (0.0, {})。
    """
    if layer is None:
        return 0.0, {}
    allowed = TERM_WHITELIST.get(layer)
    if allowed is None:
        # 未登记的层 → **立刻炸**，不要退回扁平求和。
        #
        # 这里曾经写 `return sum(terms.values()), dict(terms)` 并注释「别静默
        # 丢分」，方向是反的：静默**不隔离**比静默丢分危险得多——未登记的层会
        # 悄悄退回扁平加权和（正是分层要消灭的东西），而 trace 里看不出区别。
        # 隔离是本模块存在的全部理由，失去它就该炸，不该降级运行。
        #
        # 用 raise 而不是 assert：assert 在 python -O 下会被跳过。
        # 结构上到不了这里（L0~L7 全部登记，layer_of 的兜底是已登记的
        # L_CLEANUP），所以这条分支是给「将来有人加了新层却忘了登记白名单」的。
        raise KeyError(
            "层 %r 没有登记 TERM_WHITELIST——加新层时必须同时登记白名单，"
            "否则该层会退回扁平求和（分层失效）" % (layer,))
    kept = dict((k, v) for k, v in terms.items() if k in allowed)
    return sum(kept.values()), kept

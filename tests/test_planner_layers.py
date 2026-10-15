# -*- coding: utf-8 -*-
"""层判定（spec §5）。

层是派生值：每拍从 feasible 算出来即用，不是被维护的状态。所以这里只测
「给定 (task, world) 落在哪层」这个纯函数，没有状态迁移要测。
"""
import pytest

from ai import planner_layers as PL


class _T(object):
    """最小 task 替身：层判定只读 TYPE。"""

    def __init__(self, type_name):
        # type: (str) -> None
        self.TYPE = type_name


def _rules(pressure=0.0, plate_pressure=0.0):
    # type: (float, float) -> tuple
    """固定返回值的规则表，让层判定与两条曲线的实现解耦。"""
    return PL.build_rules(lambda t, w: pressure, lambda w: plate_pressure)


def test_layer_constants_are_ordered_high_to_low():
    """数字小 = 层高。这个约定被 min() 依赖，钉死。"""
    assert PL.L_FIRE < PL.L_IRREVERSIBLE < PL.L_RESCUE < PL.L_REDEEM
    assert PL.L_REDEEM < PL.L_UNBLOCK < PL.L_CAPACITY < PL.L_PREP < PL.L_CLEANUP


def test_extinguish_is_top_layer():
    assert PL.layer_of(_T("Extinguish"), None, rules=_rules()) == PL.L_FIRE


def test_pressure_splits_same_task_across_three_layers():
    """同一个 TakeCookedToPlate 按锅的进度落在不同层——这是「按局面分层」
    的全部意义。静态按类型分层做不到这件事，而「锅快糊了压过一切」正是它
    存在的理由。

    低 pressure 时落 **L7 清理**而不是 L6 备菜：spec §4.2 的 L6 列表不含
    TakeCookedToPlate，因为锅还没熟时「端盘去盛饭」没有任何东西在倒计时。
    （_time_pressure 的 0.25~0.5 渐变段是为「提前去端盘等着」设的；若将来
    确认那个提前量有价值，要把本 task 加进 L6 或给它单独一层。）
    """
    t = _T("TakeCookedToPlate")
    assert PL.layer_of(t, None, rules=_rules(pressure=1.9)) == PL.L_IRREVERSIBLE
    assert PL.layer_of(t, None, rules=_rules(pressure=1.2)) == PL.L_RESCUE
    assert PL.layer_of(t, None, rules=_rules(pressure=0.3)) == PL.L_CLEANUP


def test_pressure_thresholds_are_inclusive():
    """边界值归高层：恰好 1.5 算不可逆、恰好 1.0 算救料。"""
    t = _T("RescueCooked")
    assert PL.layer_of(t, None, rules=_rules(pressure=1.5)) == PL.L_IRREVERSIBLE
    assert PL.layer_of(t, None, rules=_rules(pressure=1.0)) == PL.L_RESCUE


def test_deliver_is_redeem_layer():
    assert PL.layer_of(_T("DeliverPlate"), None, rules=_rules()) == PL.L_REDEEM


def test_plate_tasks_move_between_unblock_and_cleanup():
    """盘充足时洗盘掉到 L7：符合「没有正在流失的东西」，也避免盘够用时
    还去洗盘占住解卡层。"""
    for name in ("WashPlate", "PlaceCleanPlate"):
        t = _T(name)
        assert PL.layer_of(t, None, rules=_rules(plate_pressure=2.0)) == PL.L_UNBLOCK
        assert PL.layer_of(t, None, rules=_rules(plate_pressure=0.0)) == PL.L_CLEANUP


def test_fill_cooker_is_capacity_layer():
    assert PL.layer_of(_T("FillCooker"), None, rules=_rules()) == PL.L_CAPACITY


def test_prep_types_land_in_prep_layer():
    for name in PL.LAYER_PREP_TYPES:
        assert PL.layer_of(_T(name), None, rules=_rules()) == PL.L_PREP


def test_unknown_and_cleanup_types_fall_to_cleanup():
    """无命中 → L7。新加的 task 忘了登记也不会崩，只是优先级最低。"""
    for name in ("StashHeld", "ReturnPot", "RecoverLoosePot", "ResetPlate",
                 "Prefetch", "Discard", "SomeFutureTask"):
        assert PL.layer_of(_T(name), None, rules=_rules()) == PL.L_CLEANUP


def test_prep_types_is_not_combo_prep_types():
    """两张表必须不同：_COMBO_PREP_TYPES 含 TakeCookedToPlate 与
    PlaceCleanPlate，而它们分层后在 L2 / L4，不在备菜层。混用会把救料
    和摆盘错划进 L6。"""
    from ai import planner
    assert "TakeCookedToPlate" in planner._COMBO_PREP_TYPES
    assert "TakeCookedToPlate" not in PL.LAYER_PREP_TYPES
    assert "PlaceCleanPlate" in planner._COMBO_PREP_TYPES
    assert "PlaceCleanPlate" not in PL.LAYER_PREP_TYPES


def test_rules_table_is_replaceable_by_remapping_layers():
    """层序调整是策略层的旋钮之一（spec §9）。

    注意机制：`layer_of` 返回的是**命中规则自带的层号**，不是规则在表里的
    位置——所以重排行顺序不改变任何结果（谓词互斥时尤其明显）。要让填灶
    压过送餐，得传一张把它映射到更高层号的表。

    这也修正了 spec §5.1「表可替换（重排/删条）」的措辞：可替换的是
    **层号映射**与**规则集合**，不是行顺序。
    """
    base = _rules()
    by_layer = dict((layer, pred) for layer, pred in base)
    # 把 FillCooker 的谓词映射到 L_RESCUE（比 L_REDEEM 高），送餐不动
    remapped = (
        (PL.L_FIRE,    by_layer[PL.L_FIRE]),
        (PL.L_RESCUE,  by_layer[PL.L_CAPACITY]),   # 填灶改挂到更高层号
        (PL.L_REDEEM,  by_layer[PL.L_REDEEM]),
    )
    fill = _T("FillCooker")
    deliver = _T("DeliverPlate")
    assert PL.layer_of(fill, None, rules=remapped) < PL.layer_of(deliver, None, rules=remapped)
    # 原表里是反的（兑现 > 产能，spec §4.3）
    assert PL.layer_of(deliver, None, rules=base) < PL.layer_of(fill, None, rules=base)


def test_rules_table_is_replaceable_by_deletion():
    """「别管脏盘了我来洗」= 删掉 L_UNBLOCK 那条，WashPlate 落到 L7。"""
    kept = tuple((l, p) for l, p in _rules(plate_pressure=2.0)
                 if l != PL.L_UNBLOCK)
    assert PL.layer_of(_T("WashPlate"), None, rules=kept) == PL.L_CLEANUP


def test_layer_of_does_not_read_blackboard():
    """layer_of 的签名里没有 bb，这条由签名本身保证；这里钉住它不被
    悄悄加回来（spec §5.1：策略覆盖走 rules 参数，不走黑板）。"""
    import inspect
    params = inspect.signature(PL.layer_of).parameters
    assert "bb" not in params
    assert "blackboard" not in params


def _cooker(n="cooker1", progress=None, ctime=12.0, cooked=False,
            burning=False, on_fire=False):
    # type: (str, Any, float, bool, bool, bool) -> dict
    """灶状态条目。字段语义按真实帧实测：cooking_time 是常量容量（本关恒
    12）、cooking_progress 是进度（单位≈秒）。熟点 = progress 达 ctime，
    糊点 = 2*ctime。"""
    c = {"n": n, "cooking_time": ctime, "cooked": cooked,
         "burning": burning, "onFire": on_fire}
    if progress is not None:
        c["cooking_progress"] = progress
    return c


class _W(object):
    """最小 world 替身：层判定只读 cookers / plates。"""

    def __init__(self, cookers=(), plates=()):
        # type: (Any, Any) -> None
        self.cookers = list(cookers)
        self.plates = list(plates)


class _CookTask(object):
    """带 cooker_id 的 task 替身——_time_pressure 要读它。"""

    def __init__(self, type_name, cooker_id):
        # type: (str, str) -> None
        self.TYPE = type_name
        self.cooker_id = cooker_id
        self.key = (type_name, cooker_id)


def test_layer_of_task_reads_real_pressure_curve():
    """生产入口：progress 19.2/12 → ratio 0.8 → pressure 1.6 → L1。
    （曲线：ratio = progress/(2*ctime)，0.5=刚熟、1.0=快糊；
    pressure 从刚熟 1.0 线性升到快糊 2.0。）"""
    w = _W(cookers=[_cooker(progress=19.2)])
    t = _CookTask("TakeCookedToPlate", "cooker1")
    assert PL.layer_of_task(t, w) == PL.L_IRREVERSIBLE


def test_cooking_progress_maps_to_three_layers():
    """走一遍一个烹饪周期：生 → 快熟 → 刚熟 → 快糊。

    progress 在周期内**单调递增**，所以 L6→L2→L1 是单向的，一个周期内
    不会来回跳——这是 spec §8.4 否掉「降层迟滞」的依据之一。

    注意 progress=2.0（刚下锅）落 **L_CLEANUP** 而不是 L_PREP：spec §4.2 的
    L6 备菜层列表不含 TakeCookedToPlate，因为锅还没熟时「端盘去盛饭」没有任何
    东西在倒计时（同 Task 1 的 test_pressure_splits_same_task_across_three_layers）。
    """
    t = _CookTask("TakeCookedToPlate", "cooker1")
    cases = [
        (2.0,  PL.L_CLEANUP),       # ratio 0.08，刚下锅
        (12.0, PL.L_RESCUE),        # ratio 0.50，刚熟 → pressure 1.0
        (18.0, PL.L_IRREVERSIBLE),  # ratio 0.75，pressure 1.5
        (24.0, PL.L_IRREVERSIBLE),  # ratio 1.00，快糊 → pressure 2.0
    ]
    for progress, expected in cases:
        w = _W(cookers=[_cooker(progress=progress)])
        assert PL.layer_of_task(t, w) == expected, "progress=%s" % progress


def test_burning_cooker_is_irreversible():
    """已糊/着火：_time_pressure 返回 2.0。"""
    t = _CookTask("Discard", "cooker1")
    w = _W(cookers=[_cooker(progress=30.0, burning=True)])
    assert PL.layer_of_task(t, w) == PL.L_IRREVERSIBLE


def test_task_without_cooker_id_never_enters_cook_layers():
    """层判定只认 _time_pressure 认的那三个 task（TakeCookedToPlate /
    RescueCooked / Discard）。别的 task 该函数返回 0.0，自然落不进
    L1/L2——即使场上有一口快糊的锅。这是现有行为，分层不改它。"""
    w = _W(cookers=[_cooker(progress=23.0)])
    assert PL.layer_of_task(_T("Chop"), w) == PL.L_PREP
    assert PL.layer_of_task(_T("DeliverPlate"), w) == PL.L_REDEEM


def test_plate_pressure_three_tiers():
    """_plate_pressure：0 块=2.0、1 块=1.0、>=2 块=0.0。前两档进 L4，
    第三档掉 L7。"""
    clean = {"n": "plate1", "contents": []}
    t = _T("WashPlate")
    assert PL.layer_of_task(t, _W(plates=[])) == PL.L_UNBLOCK
    assert PL.layer_of_task(t, _W(plates=[clean])) == PL.L_UNBLOCK
    assert PL.layer_of_task(
        t, _W(plates=[clean, dict(clean, n="plate2")])) == PL.L_CLEANUP


def test_missing_cooking_progress_falls_back_to_binary():
    """旧帧/残帧没有 cooking_progress 时，_time_pressure 退回二进制
    （cooked 或 burning → 1.0）。于是「熟锅」进 L2 而不是 L6。

    这是现有行为，但**分层放大了它的后果**：原来是几分之差，现在是整层
    隔离——残帧下一口标记为 cooked 的锅会把所有备菜候选挡在门外。记在
    这里，便于残帧问题复现时第一时间想到分层这一环。
    """
    t = _CookTask("TakeCookedToPlate", "cooker1")
    w = _W(cookers=[{"n": "cooker1", "cooked": True}])   # 无 progress/time
    assert PL.layer_of_task(t, w) == PL.L_RESCUE

    w_raw = _W(cookers=[{"n": "cooker1"}])               # 空灶，什么都没有
    assert PL.layer_of_task(t, w_raw) == PL.L_CLEANUP


def test_unknown_cooker_id_does_not_crash():
    """task 绑的灶已不在 world.cookers（被 P1 端走等）→ _time_pressure
    返回 0.0 → 落 L7。层判定不负责发现「资源没了」，那由 feasible 负责
    （spec §8.1：候选消失本身就是信号）。"""
    t = _CookTask("TakeCookedToPlate", "ghost_cooker")
    assert PL.layer_of_task(t, _W(cookers=[_cooker(progress=20.0)])) == PL.L_CLEANUP


class _Cand(object):
    """最小 candidate 替身：分层只读 .task。"""

    def __init__(self, task):
        # type: (Any) -> None
        self.task = task


class _KeyedTask(object):
    """带 key 的 task 替身——layers dict 以 key 为键。"""

    def __init__(self, type_name, key=None):
        # type: (str, Any) -> None
        self.TYPE = type_name
        self.key = key or (type_name,)


def test_layer_filter_on_empty_candidates_returns_none():
    """空候选 → top_layer 为 None，不抛 ValueError。

    min() 对空序列抛的是 ValueError 而不是返回 None。plan() 在
    `if feasible:` 内部调用所以碰不到，但本函数会被单独调用（测试、
    以及将来的 trace 工具），必须自己兜住。
    """
    top, in_layer, layers = PL.layer_filter([], None)
    assert top is None
    assert in_layer == []
    assert layers == {}


def _static_layer_fn(mapping):
    # type: (dict) -> Any
    """按 TYPE 查表的层函数，让过滤测试与曲线解耦。"""
    return lambda t, w: mapping[t.TYPE]


def test_only_top_layer_candidates_pass():
    """这是分层的全部机制：低层候选压根不进打分。

    §10.2 那次回退的 case——灶空转时 planner 选了备菜——在这里被结构性
    地排除：FillCooker 在 L5、备菜在 L6，备菜不进 in_layer。
    """
    cands = [_Cand(_KeyedTask("FetchToPlate")),
             _Cand(_KeyedTask("FillCooker")),
             _Cand(_KeyedTask("Chop"))]
    fn = _static_layer_fn({"FetchToPlate": PL.L_PREP,
                           "FillCooker": PL.L_CAPACITY,
                           "Chop": PL.L_PREP})
    top, in_layer, layers = PL.layer_filter(cands, None, fn)
    assert top == PL.L_CAPACITY
    assert [c.task.TYPE for c in in_layer] == ["FillCooker"]
    # 但层表含全部候选——trace 要据此说明「备菜可行但层不够高」
    assert len(layers) == 3


def test_layers_dict_covers_all_candidates_not_just_top():
    """层表是 trace 的数据源，必须全量。"""
    cands = [_Cand(_KeyedTask("DeliverPlate")), _Cand(_KeyedTask("Chop"))]
    fn = _static_layer_fn({"DeliverPlate": PL.L_REDEEM, "Chop": PL.L_PREP})
    _, in_layer, layers = PL.layer_filter(cands, None, fn)
    assert len(in_layer) == 1
    assert layers == {("DeliverPlate",): PL.L_REDEEM, ("Chop",): PL.L_PREP}


def test_same_layer_candidates_all_pass():
    """同层全部保留，层内由打分分先后。"""
    cands = [_Cand(_KeyedTask("Chop", key=("Chop", i))) for i in range(3)]
    fn = _static_layer_fn({"Chop": PL.L_PREP})
    top, in_layer, _ = PL.layer_filter(cands, None, fn)
    assert top == PL.L_PREP
    assert len(in_layer) == 3


def test_in_layer_preserves_input_order():
    """顺序必须保留：下游 sorted 是稳定排序，同分时输入顺序决定胜者。
    若这里重排，planner 会在同分候选间横跳（Review Focus 4）。"""
    keys = [("Chop", i) for i in range(5)]
    cands = [_Cand(_KeyedTask("Chop", key=k)) for k in keys]
    fn = _static_layer_fn({"Chop": PL.L_PREP})
    _, in_layer, _ = PL.layer_filter(cands, None, fn)
    assert [c.task.key for c in in_layer] == keys


def test_fire_beats_everything():
    """L0 在场时其余全不参与——包括快糊的锅。"""
    cands = [_Cand(_KeyedTask("Extinguish")),
             _Cand(_KeyedTask("Discard")),
             _Cand(_KeyedTask("DeliverPlate"))]
    fn = _static_layer_fn({"Extinguish": PL.L_FIRE,
                           "Discard": PL.L_IRREVERSIBLE,
                           "DeliverPlate": PL.L_REDEEM})
    top, in_layer, _ = PL.layer_filter(cands, None, fn)
    assert top == PL.L_FIRE
    assert [c.task.TYPE for c in in_layer] == ["Extinguish"]


def test_layer_drops_when_high_layer_candidate_disappears():
    """spec §8.1 的核心性质：层是派生值。

    高层动作不可达（盘被 P1 拿走 / 锅被端走 / 料被取走）时，该候选从
    feasible 消失——于是它压根不进 layers，top_layer 自己掉到下一层。
    **不需要任何降层动作、不需要失效检测**（spec §8.4 记了被否掉的三个
    机制）。这里直接模拟「候选消失」这一件事。
    """
    fn = _static_layer_fn({"TakeCookedToPlate": PL.L_RESCUE,
                           "Chop": PL.L_PREP})
    rescue = _Cand(_KeyedTask("TakeCookedToPlate"))
    prep = _Cand(_KeyedTask("Chop"))

    # 这一拍：救料可行 → top_layer = L2
    top, in_layer, _ = PL.layer_filter([rescue, prep], None, fn)
    assert top == PL.L_RESCUE
    assert [c.task.TYPE for c in in_layer] == ["TakeCookedToPlate"]

    # 下一拍：P1 抢先盛了饭 → feasible 里没有它了 → top_layer 自动变 L6
    top2, in_layer2, layers2 = PL.layer_filter([prep], None, fn)
    assert top2 == PL.L_PREP
    assert [c.task.TYPE for c in in_layer2] == ["Chop"]
    assert ("TakeCookedToPlate",) not in layers2


def test_only_cleanup_candidates_yields_cleanup_layer():
    """开局/收盘：world.orders 为空 → 订单相关候选全无，只剩清理类。
    top_layer 应为 L7 而非崩溃（Review Focus 3）。"""
    cands = [_Cand(_KeyedTask("StashHeld")), _Cand(_KeyedTask("ReturnPot"))]
    fn = _static_layer_fn({"StashHeld": PL.L_CLEANUP,
                           "ReturnPot": PL.L_CLEANUP})
    top, in_layer, _ = PL.layer_filter(cands, None, fn)
    assert top == PL.L_CLEANUP
    assert len(in_layer) == 2


def test_layer_filter_defaults_to_layer_of_task():
    """`layer_fn=None` 要真的落到 layer_of_task 上。

    原先 8 个测试里只有空候选那条省略了 layer_fn，而它的候选是 []、循环体不
    执行——这条接线从未真正跑过（评审查出）。
    """
    cooker = {"n": "c1", "cooking_time": 12.0, "cooking_progress": 23.0}
    w = _W(cookers=[cooker])
    cands = [_Cand(_CookTask("RescueCooked", "c1")),
             _Cand(_KeyedTask("Chop"))]
    # 不传 layer_fn：快糊的锅应落 L1，切菜落 L6
    top, in_layer, layers = PL.layer_filter(cands, w)
    assert top == PL.L_IRREVERSIBLE
    assert [c.task.TYPE for c in in_layer] == ["RescueCooked"]
    assert layers[_KeyedTask("Chop").key] == PL.L_PREP


def test_duplicate_key_takes_the_higher_layer():
    """同 key 必同层是不变量；万一被违反，要保守地取更高的层。

    让后写的覆盖会让 top_layer 算错、且高层候选混进 in_layer（实测
    top=5 而 in_layer 含 L1 的候选）。取 min 后失败模式变成「保守地当高层
    处理」。
    """
    fn = _static_layer_fn({"A": PL.L_IRREVERSIBLE, "B": PL.L_CAPACITY})
    cands = [_Cand(_KeyedTask("A", key=("same",))),
             _Cand(_KeyedTask("B", key=("same",)))]
    top, _in_layer, layers = PL.layer_filter(cands, None, fn)
    assert top == PL.L_IRREVERSIBLE, "应取更高的层，不是后写的那个"
    assert layers[("same",)] == PL.L_IRREVERSIBLE


def test_filter_terms_keeps_only_whitelisted():
    """层内只算该层相关的项。这是「W_* 之间不再需要跨类可比」的实现——
    W_COOKER_IDLE 只跟 W_ETA 比（同在 L5），结构上碰不到备菜的项。"""
    terms = {"cooker_idle": 4.8, "eta": -1.5, "urgency": 3.0,
             "completion_ratio": 1.33}
    score, kept = PL.filter_terms(terms, PL.L_CAPACITY)
    assert set(kept) == {"cooker_idle", "eta"}
    assert score == pytest.approx(4.8 - 1.5)


# planner 当前产出的全部 term 键（与 ai/planner.py 的 terms[...] 对齐）。
_ALL_TERMS = (
    "urgency", "pressure", "eta", "p1_conflict", "async_resource",
    "relay_bonus", "completion_ratio", "ingredient_reuse", "combo_head",
    "combo_break", "freshness", "deadline_lookahead", "hands", "move_penalty",
    "deliver_bonus", "plate_pressure", "relay_1_3_bonus", "cooker_idle",
    "no_plate_placed",
)


def test_every_layer_has_a_whitelist():
    """八层都要登记白名单，否则 filter_terms 会抛 KeyError。

    **从模块反射层常量，不手抄名字**：原先遍历一个硬编码的 8 元组，评审往模块里
    加了个 L_HYPOTHETICAL_NEW = 8 而不改测试，39 个测试全绿——而「加了新层却忘了
    登记白名单」正是 filter_terms 那条 raise 想防的场景。
    """
    layers = [v for n, v in sorted(vars(PL).items())
              if n.startswith("L_") and isinstance(v, int)]
    assert len(layers) >= 8, "层常量少于 8 个，是不是被删了？"
    for layer in layers:
        assert layer in PL.TERM_WHITELIST, \
            "层 %r 没登记白名单——加新层时必须同时登记，否则 filter_terms 会炸" % (layer,)


def test_whitelists_only_reference_real_terms():
    """白名单里的键必须是 planner 真的会产出的——拼错一个键的症状是
    「这一项静默失效」，正是分层要消灭的那类 bug。"""
    for layer, allowed in PL.TERM_WHITELIST.items():
        unknown = set(allowed) - set(_ALL_TERMS)
        assert not unknown, "层 %s 的白名单含未知 term: %s" % (layer, unknown)


def test_deliver_bonus_is_in_no_whitelist():
    """W_DELIVER_BONUS 是恒等加常数的类别标记：L3 全部候选都是
    DeliverPlate，给每个人加同一个 3.0 不改变任何排序。它原本在调两件事
    （类别优先级 + 改选频率，见 4.0→3.0 那段注释），现在类别优先级归层序、
    改选频率归 COMMITMENT_MARGIN。

    常量本身保留（三个测试文件引用它，删会连带改测试，而「零测试改动」
    是分层正确性的核心证据）；这里只钉住它不再参与打分。
    """
    for allowed in PL.TERM_WHITELIST.values():
        assert "deliver_bonus" not in allowed


def test_rescue_layers_ignore_completion_ratio():
    """L1/L2 只看锅。盘满不满不影响该不该去盛这锅饭。"""
    terms = {"pressure": 1.8, "completion_ratio": 2.5, "urgency": 4.0}
    for layer in (PL.L_IRREVERSIBLE, PL.L_RESCUE):
        score, kept = PL.filter_terms(terms, layer)
        assert set(kept) == {"pressure"}
        assert score == pytest.approx(1.8)


def test_fire_layer_scores_zero():
    """L0 不打分（plan_firefighting 只发一个全局候选）。"""
    score, kept = PL.filter_terms({"pressure": 2.0, "eta": -1.0}, PL.L_FIRE)
    assert score == 0.0
    assert kept == {}


def test_filter_terms_sums_negative_contributions():
    """惩罚项存负数，求和即可。"""
    terms = {"eta": -1.5, "hands": -1.0, "move_penalty": -5.0,
             "urgency": 3.0, "completion_ratio": 1.33}
    score, kept = PL.filter_terms(terms, PL.L_PREP)
    assert score == pytest.approx(-1.5 - 1.0 - 5.0 + 3.0 + 1.33)
    assert "pressure" not in kept


def test_filter_terms_with_none_layer():
    assert PL.filter_terms({"eta": -1.0}, None) == (0.0, {})


def test_identical_scores_keep_input_order_after_sort():
    """分层收窄候选集后同分概率显著上升（L1 两口锅同进度、L5 两口空灶
    同缺口）。Python 的 sorted 是稳定排序，所以同分时输入顺序决定胜者
    ——planner 据此不会每拍横跳，承诺惯性也才接得住。

    这里直接验证「同分 + 稳定排序 = 顺序不变」这个下游依赖的性质。
    """
    scored = [("a", 4.8), ("b", 4.8), ("c", 4.8)]
    assert [k for k, _ in sorted(scored, key=lambda x: x[1], reverse=True)] \
        == ["a", "b", "c"]

    # 且 filter_terms 对相同输入给出相同输出（无随机性、无字典序依赖）
    terms = {"cooker_idle": 4.8, "eta": -1.2}
    first = PL.filter_terms(terms, PL.L_CAPACITY)
    for _ in range(5):
        assert PL.filter_terms(terms, PL.L_CAPACITY) == first


def test_unregistered_layer_raises():
    """未登记的层必须炸，不能退回扁平求和。

    静默不隔离比静默丢分危险：隔离是本模块存在的全部理由。
    """
    with pytest.raises(KeyError):
        PL.filter_terms({"urgency": 5.0, "eta": -1.0}, 999)


def test_unblock_layer_scores_plate_terms_only():
    """L4 解卡只看盘：plate_pressure（洗盘）与 no_plate_placed（摆盘）。

    评审把这层白名单换成语义错误但名字合法的 {hands, move_penalty}，39 个测试
    全绿——L4 此前只有 spec 的文字定义，没有行为断言。
    """
    terms = {"plate_pressure": 7.0, "no_plate_placed": 3.0,
             "urgency": 4.0, "eta": -1.5, "hands": -1.0}
    score, kept = PL.filter_terms(terms, PL.L_UNBLOCK)
    assert set(kept) == {"plate_pressure", "no_plate_placed"}
    assert score == pytest.approx(10.0)

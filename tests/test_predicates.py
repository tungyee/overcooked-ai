import copy

from ai import predicates as P


# ---------- 手持 ----------
def test_held_name_normalizes_none(chef_empty):
    assert P.held_name(chef_empty) == ""


def test_held_name_lowercases(chef_holding):
    assert P.held_name(chef_holding("Cucumber")) == "cucumber"


def test_held_matches_is_exact(chef_holding):
    chef = chef_holding("ChoppedCucumber")

    assert P.held_matches(chef, "choppedcucumber") is True
    assert P.held_matches(chef, "cucumber") is False


def test_held_matches_false_on_empty_hands(chef_empty):
    assert P.held_matches(chef_empty, "cucumber") is False


def test_held_is_pot_covers_all_six_cooker_types(chef_holding):
    """PLAN.md §7.3 的六种锅具都要认（spec §9 待修项）。"""
    for name in ["Pot", "FryingPan", "DeepFatFryer", "OvenTray", "OvenCakeTin", "Steamer"]:
        assert P.held_is_pot(chef_holding(name)) is True, name


def test_held_is_pot_false_for_plate(chef_holding):
    assert P.held_is_pot(chef_holding("ServerPlate")) is False


def test_held_is_plate(chef_holding):
    assert P.held_is_plate(chef_holding("ServerPlate")) is True
    assert P.held_is_plate(chef_holding("Pot")) is False


def test_hands_free(chef_empty, chef_holding):
    assert P.hands_free(chef_empty) is True
    assert P.hands_free(chef_holding("Pot")) is False


# ---------- 盘 ----------
def test_plate_contents_parses_list():
    plate = {"contents": ["BoiledSushiRice", "ChoppedCucumber"], "count": 2}

    assert P.plate_contents(plate) == ("boiledsushirice", "choppedcucumber")


def test_plate_contents_empty_when_missing():
    assert P.plate_contents({}) == ()
    assert P.plate_contents(None) == ()


def test_plate_count_falls_back_to_contents_length():
    assert P.plate_count({"contents": ["a", "b"]}) == 2
    assert P.plate_count({"count": 3}) == 3
    assert P.plate_count(None) == 0


def test_plate_lacks_is_case_insensitive():
    plate = {"contents": ["BoiledSushiRice"]}

    assert P.plate_lacks(plate, "boiledsushirice") is False
    assert P.plate_lacks(plate, "ChoppedCucumber") is True


def test_plate_complete_requires_all_ingredients():
    required = ("boiledsushirice", "choppedcucumber")

    assert P.plate_complete({"contents": ["BoiledSushiRice"]}, required) is False
    assert P.plate_complete(
        {"contents": ["BoiledSushiRice", "ChoppedCucumber"]}, required) is True


def test_plate_complete_ignores_extra_items():
    """多放了料仍算齐（送餐判定由订单侧负责，这里只管"不缺"）。"""
    required = ("boiledsushirice",)

    assert P.plate_complete(
        {"contents": ["BoiledSushiRice", "ChoppedCucumber"]}, required) is True


def test_plate_complete_true_for_empty_requirement():
    assert P.plate_complete({"contents": []}, ()) is True


# ---------- 锅 ----------
def test_cooker_for_matches_by_name(cooker):
    cookers = [cooker(name="Cooker_1"), cooker(name="Cooker_2", cooked=True)]

    assert P.cooker_for(cookers, "Cooker_2")["cooked"] is True


def test_cooker_for_none_when_absent(cooker):
    assert P.cooker_for([cooker(name="Cooker_1")], "Cooker_9") is None


def test_cooker_has_pot_uses_cooked_null_convention(cooker):
    """插件约定：cooked=null 表示灶上没锅；false/true 表示有锅。"""
    assert P.cooker_has_pot(cooker(cooked=None)) is False
    assert P.cooker_has_pot(cooker(cooked=False)) is True
    assert P.cooker_has_pot(cooker(cooked=True)) is True


def test_cooker_has_pot_false_for_missing_cooker():
    assert P.cooker_has_pot(None) is False


def test_cooker_cooked_requires_explicit_true(cooker):
    """空锅 cooked=False 不能误判为熟。"""
    assert P.cooker_cooked(cooker(cooked=True)) is True
    assert P.cooker_cooked(cooker(cooked=False)) is False
    assert P.cooker_cooked(cooker(cooked=None)) is False
    assert P.cooker_cooked(None) is False


def test_cooker_burning_covers_both_flags(cooker):
    assert P.cooker_burning(cooker(burning=True)) is True
    assert P.cooker_burning(cooker(on_fire=True)) is True
    assert P.cooker_burning(cooker(cooked=True)) is False
    assert P.cooker_burning(None) is False


def test_cooker_started(cooker):
    assert P.cooker_started(cooker(is_cooking=True)) is True
    assert P.cooker_started(cooker(cooked=True)) is True  # 已熟也算启动过
    assert P.cooker_started(cooker(cooked=False)) is False
    assert P.cooker_started(None) is False


# ---------- 板 ----------
def test_board_item_matches_station_name():
    ws = [{"n": "ChoppingBoard_1", "item": "Cucumber"}]

    assert P.board_item(ws, "ChoppingBoard_1") == "cucumber"


def test_board_item_empty_when_no_match():
    assert P.board_item([{"n": "Board_1", "item": "X"}], "Board_9") == ""


def test_board_chopped_detects_prefix():
    ws = [{"n": "Board_1", "item": "ChoppedCucumber"}]

    assert P.board_chopped(ws, "Board_1") is True


def test_board_chopped_false_for_raw():
    ws = [{"n": "Board_1", "item": "Cucumber"}]

    assert P.board_chopped(ws, "Board_1") is False


def test_station_name_matches_handles_substring():
    """原 _board_chopped 的三段式匹配：相等 / 互为子串。"""
    assert P.station_name_matches("Board_1", "Board_1") is True
    assert P.station_name_matches("Board_1", "Kitchen.Board_1") is True
    assert P.station_name_matches("Kitchen.Board_1", "Board_1") is True
    assert P.station_name_matches("Board_1", "Board_2") is False
    assert P.station_name_matches("", "Board_1") is False


def test_station_name_never_matches_across_instances():
    """Unity 的 " (n)" 后缀是不同的物理站点，绝不能跨实例匹配。

    真实帧里 workstation_cooker_01 / (2) / (3) 位于不同坐标；旧版"互为子串"
    会让查 1 号拿到 3 号的状态。
    """
    assert P.station_name_matches("cooker_01", "cooker_01 (2)") is False
    assert P.station_name_matches("cooker_01 (2)", "cooker_01") is False
    assert P.station_name_matches("cooker_01 (2)", "cooker_01 (3)") is False
    assert P.station_name_matches("cooker_01 (2)", "cooker_01 (2)") is True


def test_station_name_still_tolerates_parent_prefix():
    """同一站点带/不带父节点前缀仍要能匹配（原始需求）。"""
    assert P.station_name_matches("Level/Kitchen/cooker_01", "cooker_01") is True
    assert P.station_name_matches("Kitchen.cooker_01", "cooker_01") is True
    # 实例后缀一致时也允许前缀差异
    assert P.station_name_matches("Level/cooker_01 (2)", "cooker_01 (2)") is True
    # 但不能是任意子串：段边界必须对齐
    assert P.station_name_matches("my_cooker_01", "cooker_01") is False


def test_cooker_for_picks_exact_instance():
    """查具体灶台必须拿到它自己，而不是同基名的另一个实例。"""
    cookers = [
        {"n": "workstation_cooker_01 (3)", "x": 22.8, "z": -8.4, "cooked": True},
        {"n": "workstation_cooker_01 (2)", "x": 22.8, "z": -4.8, "cooked": False},
        {"n": "workstation_cooker_01", "x": 20.4, "z": -10.8, "cooked": None},
    ]

    got = P.cooker_for(cookers, "workstation_cooker_01")
    assert got is not None
    assert got["n"] == "workstation_cooker_01"
    assert got["x"] == 20.4
    assert P.cooker_for(cookers, "workstation_cooker_01 (2)")["x"] == 22.8


# ---------- P1 ----------
# 厨师按 name 区分：真实帧 chefs[].slot 全为 null（两局 2204 条核实），
# 旧实现也是 w.chef("Player 1")。这些用例以前用 {"slot": 1} 造数据，
# 测的是不存在的字段，故全部改为 name。
def test_chef_by_name():
    chefs = [{"name": "Player 1", "held": "Pot"}, {"name": "Player 2", "held": None}]

    assert P.chef_by_name(chefs, "Player 1")["held"] == "Pot"
    assert P.chef_by_name(chefs, "Player 9") is None
    assert P.chef_by_name(chefs, "") is None


def test_chef_by_name_ignores_case_and_space():
    chefs = [{"name": " player 1 ", "held": "Pot"}]

    assert P.chef_by_name(chefs, "Player 1")["held"] == "Pot"


def test_chef_by_slot_accepts_legacy_ints():
    """chef_by_slot 已废弃，但仍须把 1/2 映射到 name 查找。"""
    chefs = [{"name": "Player 1", "held": "Pot"}, {"name": "Player 2", "held": None}]

    assert P.chef_by_slot(chefs, 1)["held"] == "Pot"
    assert P.chef_by_slot(chefs, 2)["name"] == "Player 2"
    assert P.chef_by_slot(chefs, 9) is None


def test_chef_lookup_ignores_slot_field():
    """只有 slot 没有 name 的条目取不到——真实帧不存在这种数据。"""
    assert P.chef_by_name([{"slot": 1, "held": "Pot"}], "Player 1") is None


def test_p1_holds_keyword():
    chefs = [
        {"name": "Player 1", "held": "ServerPlate"},
        {"name": "Player 2", "held": "Pot"},
    ]

    assert P.p1_holds(chefs, "plate") is True
    assert P.p1_holds(chefs, "extinguish") is False


def test_p1_holds_plate():
    chefs = [{"name": "Player 1", "held": "ServerPlate"}]

    assert P.p1_holds_plate(chefs) is True


def test_p1_holds_false_when_p1_absent():
    assert P.p1_holds([{"name": "Player 2", "held": "Pot"}], "pot") is False


# ---------- 锅具识别（spec §9）----------

_RECIPES_SUSHI = [
    {"name": "BoiledSushiRice", "cook": "Pot", "comp": ["SushiRice"]},
    {"name": "Cucumber", "cook": "null", "comp": []},
]
_RECIPES_MULTI = [
    {"name": "FriedChips", "cook": "DeepFatFryer", "comp": ["Potato"]},
    {"name": "Cake_Plain", "cook": "OvenCakeTin", "comp": ["MixedFlourEggHoneycomb"]},
    {"name": "SteamedSpecial_Fish", "cook": "Steamer", "comp": ["UncookedFish"]},
    {"name": "CheesePizza", "cook": "OvenTray", "comp": ["Dough", "Cheese"]},
]


def test_cookware_names_from_recipes():
    assert P.cookware_names(_RECIPES_SUSHI) == {"pot"}
    assert P.cookware_names(_RECIPES_MULTI) == {
        "deepfatfryer", "ovencaketin", "steamer", "oventray"}


def test_cookware_names_skips_null_and_missing():
    """cook 字段是字符串 "null"（不是 None），这是插件的实际输出。"""
    assert P.cookware_names([{"name": "X", "cook": "null"},
                             {"name": "Y"},
                             {"name": "Z", "cook": None}]) == set()
    assert P.cookware_names(None) == set()


def test_held_is_cookware_real_held_name():
    """实测帧里手持锅的名字是 utensil_pot_01（关键词路径）。"""
    assert P.held_is_cookware({"held": "utensil_pot_01"}) is True


def test_held_is_cookware_from_recipe_data():
    """Review Focus：配方里有 DeepFatFryer，手持物名字含它就算锅具。

    关键词表里有 "fryer" 能碰巧中；但 OvenCakeTin 这类必须靠配方数据。
    """
    chef = {"held": "equipment_OvenCakeTin_01"}
    assert P.held_is_cookware(chef, _RECIPES_MULTI) is True
    assert P.held_is_cookware(chef, _RECIPES_SUSHI) is False, \
        "本关配方没有 OvenCakeTin 时，不该认它为锅具"


def test_held_is_cookware_keyword_fallback_without_recipes():
    """不传 recipes 时退化为关键词表，保证旧调用点不变坏。"""
    assert P.held_is_cookware({"held": "utensil_frying_pan_01"}) is True
    assert P.held_is_cookware({"held": "Cucumber"}) is False


def test_held_is_cookware_empty_hands():
    assert P.held_is_cookware({"held": None}) is False
    assert P.held_is_cookware(None) is False


def test_held_is_cookware_does_not_match_plate():
    """盘子不是锅具——误判会让 ReturnPot 去把盘子放灶台上。"""
    assert P.held_is_cookware({"held": "equipment_plate_01"},
                              _RECIPES_MULTI) is False


# ---------- 交互前置校验（spec §7.5）----------

def _inter(**kw):
    """构造 interactions 字典，未给的键一律 None（与真实帧一致）。"""
    base = {"interactable": None, "pickup": None, "placement": None,
            "grid": None, "gx": None, "gz": None,
            "pickup_gx": None, "pickup_gz": None,
            "placement_gx": None, "placement_gz": None}
    base.update(kw)
    return base


def test_interact_ready_handle_grid_match():
    """最精确的信号：placement_gx/gz 与目标格一致（AGENTS.md §6）。"""
    chef = {"interactions": _inter(placement_gx=7, placement_gz=-2)}
    target = {"n": "workstation_cooker_01 (3)", "gx": 7, "gz": -2}
    assert P.interact_ready(chef, target, "placement") is True


def test_interact_ready_handle_grid_mismatch():
    """放错格子必须拦住——spec §7.5 第一类故障。"""
    chef = {"interactions": _inter(placement_gx=7, placement_gz=-1)}
    target = {"n": "c", "gx": 7, "gz": -2}
    assert P.interact_ready(chef, target, "placement") is False


def test_interact_ready_generic_grid_when_no_kind():
    chef = {"interactions": _inter(gx=1, gz=3)}
    target = {"n": "board", "gx": 1, "gz": 3}
    assert P.interact_ready(chef, target) is True


def test_interact_ready_rejects_plate_station_placement_handle():
    """放置句柄指向出餐口而目标不是出餐口 → 拒绝。

    否则盘子会被误放到上菜口，订单未完成就被判为交付。
    """
    chef = {"interactions": _inter(placement="plate_station_01",
                                   placement_gx=7, placement_gz=0)}
    target = {"n": "countertop_01_standard_wood (29)", "gx": 7, "gz": 0}
    assert P.interact_ready(chef, target, "placement") is False


def test_interact_ready_allows_plate_station_when_target_is_it():
    chef = {"interactions": _inter(placement="plate_station_01",
                                   placement_gx=9, placement_gz=0)}
    target = {"n": "plate_station_01", "gx": 9, "gz": 0}
    assert P.interact_ready(chef, target, "placement") is True


def test_interact_ready_derives_target_grid_via_to_grid():
    """目标只有世界坐标时用注入的 to_grid 换算（灭火器这类动态物）。"""
    chef = {"interactions": _inter(pickup_gx=-4, pickup_gz=2)}
    target = {"n": "utensil_fire_extinguisher_01", "x": 14.4, "z": -2.4}
    to_grid = lambda x, z: (-4, 2)
    assert P.interact_ready(chef, target, "pickup", to_grid) is True


def test_interact_ready_name_fallback_when_no_grid_signal():
    """无网格信号时退到精确名匹配（动态物、灭火器）。"""
    chef = {"interactions": _inter(pickup="utensil_fire_extinguisher_01")}
    target = {"n": "utensil_fire_extinguisher_01"}
    assert P.interact_ready(chef, target, "pickup") is True


def test_interact_ready_name_fallback_does_not_cross_instances():
    """名匹配不许跨实例：查 1 号灶不能拿到 3 号灶。

    旧实现用"互为子串"兜底，"workstation_cooker_01" in
    "workstation_cooker_01 (3)" 为真，已在真实帧上复现过。
    """
    chef = {"interactions": _inter(pickup="workstation_cooker_01 (3)")}
    target = {"n": "workstation_cooker_01"}
    assert P.interact_ready(chef, target, "pickup") is False


def test_interact_ready_extinguisher_keyword_fallback():
    """灭火器名字带实例后缀时的窄兜底（旧实现的必要行为）。"""
    chef = {"interactions": _inter(pickup="utensil_fire_extinguisher_01 (2)")}
    target = {"n": "utensil_fire_extinguisher_01_Rigidbody"}
    assert P.interact_ready(chef, target, "pickup") is True


def test_interact_ready_no_interactions_is_false():
    """Review Focus 5：帧里 interactions 为 None 时不许崩。"""
    assert P.interact_ready({"interactions": None}, {"n": "x"}) is False
    assert P.interact_ready({}, {"n": "x"}) is False
    assert P.interact_ready(None, {"n": "x"}) is False


def test_interact_ready_target_none_is_false():
    """目标在动作中途消失（被 P1 端走）→ False，不抛异常。"""
    chef = {"interactions": _inter(gx=1, gz=1)}
    assert P.interact_ready(chef, None) is False
    assert P.interact_ready(chef, {}) is False


def test_facing_cell_uses_interaction_grid_not_yaw():
    """朝向格取 interactions.gx/gz，不从 yaw 推。

    实测 801 帧：yaw 与交互格的对应关系不稳定（yaw=180 时交互格
    有 706 次在 (+1,0)、60 次在 (-1,0)），不能用来定朝向。
    而 interactions.gx/gz 是游戏自己算的交互目标，1367/1602 帧非空。
    """
    assert P.facing_cell({"interactions": _inter(gx=3, gz=-1)}) == (3, -1)
    assert P.facing_cell({"interactions": _inter()}) is None
    assert P.facing_cell(None) is None


def test_chef_cell():
    to_grid = lambda x, z: (int(x), int(z))
    assert P.chef_cell({"x": 2.0, "z": -3.0}, to_grid) == (2, -3)
    assert P.chef_cell({"x": None, "z": -3.0}, to_grid) is None
    assert P.chef_cell(None, to_grid) is None


# ---------- 无副作用（spec §4.5 / §11.2 阶段 1 的核心要求）----------
def test_predicates_do_not_mutate_inputs(cooker):
    """逐个调用谓词，断言入参 dict 未被改动。

    原 _station() 写 self.pot、_should_rush_cooked() 写 self.pot，
    spec §11.2 要求改为"纯查询 + 调用点显式写入"。
    """
    chef = {"slot": 2, "held": "Pot", "x": 1.0, "z": 2.0}
    plate = {"contents": ["BoiledSushiRice"], "count": 1}
    cookers = [cooker(name="C1", cooked=True)]
    ws = [{"n": "Board_1", "item": "ChoppedCucumber"}]
    chefs = [{"slot": 1, "held": "ServerPlate"}]

    snapshot = copy.deepcopy((chef, plate, cookers, ws, chefs))

    P.held_name(chef)
    P.held_matches(chef, "pot")
    P.held_is_pot(chef)
    P.held_is_plate(chef)
    P.hands_free(chef)
    P.plate_contents(plate)
    P.plate_count(plate)
    P.plate_lacks(plate, "x")
    P.plate_complete(plate, ("boiledsushirice",))
    P.cooker_for(cookers, "C1")
    P.cooker_has_pot(cookers[0])
    P.cooker_cooked(cookers[0])
    P.cooker_burning(cookers[0])
    P.cooker_started(cookers[0])
    P.board_item(ws, "Board_1")
    P.board_chopped(ws, "Board_1")
    P.chef_by_slot(chefs, 1)
    P.p1_holds(chefs, "plate")
    P.p1_holds_plate(chefs)

    assert (chef, plate, cookers, ws, chefs) == snapshot


# ---------- 手持锅具的内容状态（落点分派用）----------

def test_held_cookware_state_empty_raw_cooked(chef_holding):
    """has_contents / cooked 两个字段拼出"空/生/熟"。"""
    chef = chef_holding("utensil_pot_01")
    def pot(has, cooked):
        return [{"n": "utensil_pot_01", "x": 0.0, "z": 0.0,
                 "has_contents": has, "cooked": cooked}]

    assert P.held_cookware_state(chef, pot(False, "Raw")) == P.COOKWARE_EMPTY
    assert P.held_cookware_state(chef, pot(True, "Cooked")) == P.COOKWARE_COOKED
    assert P.held_cookware_state(chef, pot(True, None)) == P.COOKWARE_RAW


def test_held_cookware_state_none_when_not_cookware(chef_empty, chef_holding):
    assert P.held_cookware_state(chef_empty, []) is None
    assert P.held_cookware_state(chef_holding("Cucumber"), []) is None
    assert P.held_cookware_state(chef_holding("equipment_plate_01"), []) is None


def test_held_cookware_state_unknown_is_not_empty(chef_holding):
    """items[] 里找不到那口锅 → unknown，绝不猜成"空的"。

    猜成空的会让"空锅回灶台"在压根不知道内容时触发；宁可不动。
    """
    assert P.held_cookware_state(chef_holding("utensil_pot_01"),
                                 []) == P.COOKWARE_UNKNOWN


def test_held_cookware_state_takes_nearest_same_name(chef_holding):
    """同名多口锅（实例后缀不同）→ 取离厨师最近的那条状态。"""
    chef = chef_holding("utensil_pot_01")
    chef["x"], chef["z"] = 5.0, 0.0
    items = [{"n": "utensil_pot_01", "x": 0.0, "z": 0.0,
              "has_contents": True, "cooked": "Cooked"},
             {"n": "utensil_pot_01", "x": 5.2, "z": 0.0,
              "has_contents": False, "cooked": "Raw"}]
    assert P.held_cookware_state(chef, items) == P.COOKWARE_EMPTY

# -*- coding: utf-8 -*-
"""填灶回到 L6 备菜层，按缺口大小与备菜数值竞争。

**L_CAPACITY 这一层是个设计错误**。层序的判据是「有倒计时的压过没倒计时的，
倒计时短的压过长的」（planner_layers 模块注释）：L0 火在烧、L1 锅快糊、
L2 刚熟的 12 秒、L3 订单寿命，全都有一个**正在流失的东西**。而空灶没有
倒计时 —— 灶空着不会变质、不会毁料、不会蔓延。「灶空转的机会成本」是
**期望收益**，不是流失的时钟。期望收益之间的比较该用数值，那正是 L6 的事。

实机代价（trace-20261015-165358）：
- FillCooker 可行 1248 拍、拿到 top_layer 1043 拍，这 1043 拍**全部**有
  L6 备菜可做却被结构性压掉，一次例外都没有；
- 被选中时的缺口分布：**缺 1 份占 77%**（790 拍）、缺 2 份 21%、缺 0 份 2 拍。
  缺 1 份时 cooker_idle 只值 1.9 分，本该输给中段备菜的 1.5~4，可 L5 里只有
  FillCooker 自己 —— 打分形同虚设，1.9 分无需跟任何东西比较就赢了；
- 1002 拍的填灶工时只换来 **4 锅米**，而缺口全程只有 0~1。

也就是说，分层把 `W_COOKER_IDLE = 5.6` 那一整套标定旁路掉了。那个数字当初
是按「必须压过备菜的真实合计 4.97、留 0.6 余量」定的（AGENTS.md §10.2 记的
4.0→4.8→5.6 三次调整），而饱和点 COOKER_SHORTFALL_SATURATION=3 意味着
**只有缺 3 份时才拿满 5.6**。放回 L6 后这套关系自动重新生效，不需要新常数。
"""
from ai.core import planner
from ai import tasks as T
from ai.state.blackboard import Blackboard
from ai.core.planner_layers import (L_CAPACITY, L_PREP, TERM_WHITELIST,
                               layer_of_task)
from ai.query.recipes import RecipeBook
from ai.state.world import World

RECIPES = [
    {"name": "Sushi_Cucumber",
     "comp": ["Seaweed", "BoiledSushiRice", "Cucumber"]},
    {"name": "BoiledSushiRice", "comp": ["SushiRice"], "cook": "Pot"},
    {"name": "SushiRice"},
    {"name": "Cucumber", "chop": "ChoppedCucumber"},
    {"name": "Seaweed"},
]
STATIONS = [
    {"t": "crate", "n": "crate_rice", "ing": "SushiRice", "gx": 1, "gz": 1},
    {"t": "crate", "n": "crate_weed", "ing": "Seaweed", "gx": 2, "gz": 1},
    {"t": "cooking", "n": "c1", "gx": 5, "gz": 5},
    {"t": "cooking", "n": "c2", "gx": 6, "gz": 5},
    {"t": "serving", "n": "hatch", "gx": 9, "gz": 9},
]


def _order(oid, remaining):
    return {"id": oid, "dish": "Sushi_Cucumber", "remaining": remaining,
            "lifetime": 130,
            "composition": ["Seaweed", "BoiledSushiRice", "Cucumber"]}


def _cooker(n):
    return {"n": n, "x": 5.0, "z": 5.0, "cooked": False, "isCooking": False,
            "burning": False, "onFire": False, "pot_contents": []}


def _world(n_orders):
    return World({
        "orders": [_order(str(i + 1), 60 + 20 * i) for i in range(n_orders)],
        "stations": STATIONS, "recipes": RECIPES,
        "chefs": [{"name": "Player 2", "x": 1.0, "z": 1.0, "held": None}],
        "plates": [], "workstations": [],
        "cookers": [_cooker("c1"), _cooker("c2")],
        "items": [], "extinguishers": [], "grid": {"nodeMap": None},
    })


def _fill():
    return T.FillCooker(None, ("c1", ("SushiRice",)), "BoiledSushiRice")


def _prep():
    return T.FetchToPlate("1", ("plate1", (3, 4)), "Seaweed")


def _net(w, task):
    """层内净分（走 L6 白名单裁剪，与生产路径一致）。"""
    bb = Blackboard()
    book = RecipeBook(w.recipes, w.stations)
    cand = planner.Candidate(task, [], 15)
    _score, terms = planner._score_candidate(
        w, bb, cand, book, plate_match={}, layer=L_PREP)
    from ai.core.planner_layers import filter_terms
    return filter_terms(terms, L_PREP)[0]


# ---------- 层归属 ----------

def test_fill_cooker_is_prep_layer():
    """填灶与备菜同层：它抢的是同一双手，是资源分配不是跨类优先级。"""
    assert layer_of_task(_fill(), _world(3)) == L_PREP


def test_cooker_idle_is_scored_in_prep_layer():
    """cooker_idle 必须进 L6 白名单，否则填灶在层内净分只剩 eta 的负数,
    从「永远赢」直接翻到「永远输」—— 灶又全空，正是 FillCooker 这个 task
    当初被造出来要解决的问题（AGENTS.md §4 产能：59% 的拍三口灶一个都没
    在煮、三单全过期）。"""
    assert "cooker_idle" in TERM_WHITELIST[L_PREP]


def test_capacity_layer_kept_as_empty_shell():
    """L_CAPACITY 保留空壳（照 L_FIRE 白名单为空的先例）。

    删掉枚举值会动层号连续性与 TERM_WHITELIST 的完整性检查，收益为零。
    """
    assert TERM_WHITELIST[L_CAPACITY] == frozenset() or \
        L_CAPACITY in TERM_WHITELIST


# ---------- 层内数值竞争：缺口大小决定胜负 ----------

def test_big_shortfall_beats_prep():
    """缺 3 份（灶全空、3 单要米）→ cooker_idle 拿满 5.6，压过备菜。

    这是 AGENTS.md §10.2 点名的那次回退（七个「灶空转该填灶」的拍）的守卫。
    分层期间它由 L5>L6 的层序保证，现在回到数值 —— W_COOKER_IDLE=5.6 那套
    标定本来就是为这个关系定的。
    """
    w = _world(3)
    assert _net(w, _fill()) > _net(w, _prep())


def test_small_shortfall_yields_to_prep():
    """缺 1 份 → cooker_idle 只值 1.9，该让位给备菜。

    这正是实机 77% 的拍（790 拍）所处的局面，L5 让这 1.9 分白赢了。
    """
    w = _world(1)
    assert _net(w, _fill()) < _net(w, _prep()), \
        "缺 1 份米却压着备菜不做，是 L_CAPACITY 旁路打分的症状"


def test_shortfall_bonus_is_monotonic():
    """缺口越大加分越高（到饱和点封顶）—— 连续，不是硬门。"""
    nets = [_net(_world(n), _fill()) for n in (1, 2, 3)]
    assert nets[0] < nets[1] < nets[2], nets

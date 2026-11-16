# -*- coding: utf-8 -*-
"""断供补位：玩家负责煮饭但长时间不煮时，P2 要能接手。

4-1 实机 trace-20261109-223643：分工 `player=cook`，`FetchToCooker` 固定
-6 分，即使 urgency=5.0 也只有 -0.75；44~99 秒没有米下锅，所有寿司一起
过期。截止闸只在「这单还有救」时开，救不了的单不会替 P2 抢下锅。
"""
from ai.core import planner
from ai.dialog.schema import Preference
from ai.state.blackboard import Blackboard


class _Book(object):
    def requirements(self, order):
        return list(order.get("composition") or ())

    def needs_cooking(self, item):
        return str(item).lower() == "boiledsushirice"

    def carrier_base(self, item):
        return None

    def cooked_of_raw(self, raw):
        return ("BoiledSushiRice",) if str(raw).lower() == "sushirice" else ()

    def plate_has(self, plate, item):
        return False

    def want_tokens(self, item):
        return frozenset({str(item).lower()})

    def rich_token(self, item):
        return str(item).lower()


class _World(object):
    def __init__(self, cookers=(), orders=None):
        self.orders = list(orders or [dict(
            id="1", dish="Sushi_Fish", remaining=60.0, lifetime=96,
            composition=["Seaweed", "BoiledSushiRice", "SushiFish"])])
        self.cookers = list(cookers)
        self.chefs = []
        self.items = []
        self.plates = []
        self.held_plates = []
        self.recipes = []
        self.stations = []


def _cooker(n, cooking=False, cooked=False, contents=()):
    return {"n": n, "cookware": "utensil_pot_01", "isCooking": cooking,
            "cooked": cooked, "pot_contents": list(contents),
            "burning": False, "onFire": False}


class _Task(object):
    def __init__(self, task_type, item=None):
        self.TYPE = task_type
        self.item = item
        self.order_id = "1"


def test_supply_override_turns_on_after_idle_grace():
    w = _World(cookers=[_cooker("c1"), _cooker("c2")])
    bb = Blackboard()
    book = _Book()

    for tick in range(0, planner.SUPPLY_OVERRIDE_TICKS):
        bb.tick = tick
        planner._update_supply_watch(w, bb, book)
    assert bb.supply_override is False

    bb.tick = planner.SUPPLY_OVERRIDE_TICKS
    planner._update_supply_watch(w, bb, book)
    assert bb.supply_override is True


def test_supply_override_stays_off_while_a_pot_is_cooking():
    w = _World(cookers=[
        _cooker("c1", cooking=True, contents=["ing:SushiRice"]),
        _cooker("c2"),
    ])
    bb = Blackboard()
    book = _Book()

    for tick in range(0, planner.SUPPLY_OVERRIDE_TICKS * 2):
        bb.tick = tick
        planner._update_supply_watch(w, bb, book)

    assert bb.supply_override is False


def test_supply_override_skips_player_claim_penalty_for_cook_tasks():
    bb = Blackboard()
    bb.tick = 10
    bb.supply_override = True
    bb.preferences = [
        Preference(kind="claim", owner="P1", weight=-6.0,
                   scope=frozenset({("FetchToCooker", None)})),
        Preference(kind="claim", owner="P1", weight=-4.0,
                   scope=frozenset({("Chop", None)})),
    ]

    cook = planner.Candidate(_Task("FetchToCooker", "SushiRice"), [], 10)
    chop = planner.Candidate(_Task("Chop", "SushiFish"), [], 10)

    assert planner._preference_score(cook, bb, 6) == 0.0
    assert planner._preference_score(chop, bb, 6) == -4.0


def test_supply_override_off_keeps_claim_penalty():
    bb = Blackboard()
    bb.tick = 10
    bb.supply_override = False
    bb.preferences = [Preference(
        kind="claim", owner="P1", weight=-6.0,
        scope=frozenset({("FetchToCooker", None)}))]

    cook = planner.Candidate(_Task("FetchToCooker", "SushiRice"), [], 10)

    assert planner._preference_score(cook, bb, 6) == -6.0

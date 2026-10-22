"""真实局回归：危急补位不能仍被分工负分压回填灶。"""
import gzip
import json
from pathlib import Path

from ai.agent import _TASK_META
from ai.core import planner
from ai.dialog.duties import Kitchen, Ledger
from ai.dialog.schema import Preference
from ai.query.recipe_graph import _compile_node
from ai.query.recipes import RecipeBook
from ai.state.blackboard import Blackboard
from ai.state.world import World


def _scene(name="claimed-prep.ndjson.gz"):
    path = Path(__file__).parent / "fixtures/rice_priority" / name
    with gzip.open(path, "rt") as stream:
        row = json.loads(stream.readline())
    world = World(row["state"])
    book = RecipeBook(world.recipes, world.stations)
    bb = Blackboard()
    bb.tick = row["t"]
    kitchen = Kitchen.from_orders(book, world.orders, _compile_node, _TASK_META,
                                  {s.get("t") for s in world.stations})
    ledger = Ledger()
    for duty in row["duties"]:
        ledger.apply(dict(op="add", mode=duty["mode"], target=duty["target"]),
                     kitchen, bb.tick)
    bb.preferences = ledger.preferences()
    return world, book, bb


def test_soft_duties_finish_near_complete_order_before_more_rice():
    """真实 t=856：盘里已有饭和海苔时，补黄瓜链应压过再填一口灶。"""
    world, book, bb = _scene("soft-fill-in.ndjson.gz")

    chosen, record = planner.plan(world, bb, None, book)

    assert chosen.TYPE == "FetchToBoard"
    assert chosen.item == "Cucumber"
    assert chosen.order_id == "2"
    row = next(r for r in record["candidates"] if r[0] == chosen)
    assert row[2]["completion_ratio"] > 1.0
    assert row[2]["pref"] == -1.0
    fills = [r for r in record["candidates"]
             if r[0].TYPE == "FillCooker" and r[3] and r[7]]
    assert fills and all(row[1] > fill[1] for fill in fills)


def test_urgent_claimed_prep_beats_more_rice_in_real_frame():
    world, book, bb = _scene()
    preferences = list(bb.preferences)
    chosen, record = planner.plan(world, bb, None, book)
    assert chosen.TYPE == "FetchToPlate"
    assert chosen.item == "Seaweed"
    assert chosen.order_id == "2"
    assert record["rescue_override"] == "2"
    row = next(r for r in record["candidates"] if r[0] == chosen)
    assert row[2]["pref"] == 0
    assert bb.preferences == preferences  # 临时接手不修改原分工


def test_rescue_hold_keeps_claim_penalty_disabled_during_cooldown():
    world, book, bb = _scene()
    from ai import tasks as T
    task = T.FetchToPlate("2", ("-13738",), "Seaweed")
    bb.dialog.rescue_task_key = task.key
    bb.dialog.rescue_hold_until = bb.tick + 200
    bb.rescue_cooldown_until = bb.tick + 100
    candidate = planner.Candidate(task, [], 10)
    _, terms = planner._score_candidate(world, bb, candidate, book,
                                        slack_map={"2": 20}, layer=6)
    assert terms["pref"] == 0
    bb.dialog.rescue_hold_until = bb.tick
    _, terms = planner._score_candidate(world, bb, candidate, book,
                                        slack_map={"2": 20}, layer=6)
    assert terms["pref"] == -1


def test_calm_claim_and_other_preferences_remain_effective():
    world, book, bb = _scene()
    from ai import tasks as T
    task = T.FetchToPlate("2", ("-13738",), "Seaweed")
    candidate = planner.Candidate(task, [], 10)
    bb.preferences.append(Preference(kind="role", weight=-2,
                                    scope=frozenset({("FetchToPlate", "seaweed")})))
    _, calm = planner._score_candidate(world, bb, candidate, book,
                                       slack_map={"2": 1000}, layer=6)
    _, urgent = planner._score_candidate(world, bb, candidate, book,
                                         slack_map={"2": 20}, layer=6)
    assert calm["pref"] == -3
    assert urgent["pref"] == -2

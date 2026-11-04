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



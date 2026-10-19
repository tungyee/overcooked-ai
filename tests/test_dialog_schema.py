"""对话层 schema：账本偏好与作用域匹配（spec §4.5）。"""
from ai.dialog.schema import (
    DialogState,
    Preference,
    preference_matches_task,
    scope_matches,
)


class _Task(object):
    def __init__(self, task_type, item=None):
        self.TYPE = task_type
        self.item = item
        self.key = (task_type, None, (), item)


def test_preference_defaults():
    p = Preference(kind="claim", scope=frozenset({("Chop", "cucumber")}),
                   weight=-8.0, expires_tick=600)
    assert p.kind == "claim" and p.owner == "P1"
    assert p.exclude == frozenset() and p.duty_id is None


def test_empty_scope_matches_no_task():
    pref = Preference(kind="claim", scope=frozenset(), weight=-8.0)
    assert preference_matches_task(pref, _Task("Chop", "Cucumber")) is False


def test_scope_matches_dag_node_identity_case_insensitive():
    scope = frozenset({("Chop", "cucumber"), ("WashPlate", None)})
    assert scope_matches(scope, _Task("Chop", "Cucumber"))
    assert not scope_matches(scope, _Task("Chop", "SushiFish"))
    # (类型, None) = 该类型任意 item
    assert scope_matches(scope, _Task("WashPlate"))
    assert scope_matches(scope, _Task("WashPlate", "anything"))
    assert not scope_matches(scope, _Task("FetchToBoard", "Cucumber"))


def test_exclude_carves_out_of_scope():
    pref = Preference(kind="claim", weight=-8.0,
                      scope=frozenset({("Chop", None)}),
                      exclude=frozenset({("Chop", "cucumber")}))
    assert not preference_matches_task(pref, _Task("Chop", "Cucumber"))
    assert preference_matches_task(pref, _Task("Chop", "SushiFish"))


def test_dialog_state_defaults():
    state = DialogState()
    assert state.history == []
    assert state.pending_orders == []
    assert state.last_transcript == ""
    assert state.pending_event is None
    assert state.rescue_task_key is None
    assert state.rescue_hold_until == 0


def test_rescue_and_relay_types_have_no_branch():
    from ai import tasks as T
    from ai.core import planner
    from ai.dialog.schema import NO_BRANCH_TYPES
    from ai.query.recipes import RecipeBook

    book = RecipeBook([{"name": "pot", "comp": []}], [])
    for task in (T.RecoverLoosePot(resources=("cooker_1",), item="pot"),
                 T.PlaceToRelay(item="pot")):
        assert task.TYPE in NO_BRANCH_TYPES
        assert planner.branch_of_task(task, book) is None
    # 对照：普通取料动作仍按生料归一。
    chop = T.Chop(resources=("board_1",), item="pot")
    assert planner.branch_of_task(chop, book) == "pot"

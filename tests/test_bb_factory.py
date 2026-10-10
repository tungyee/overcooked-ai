from ai.bb_factory import live_order_ids, make_bb, make_resource_view


def test_make_bb_returns_fresh_blackboard():
    bb = make_bb(tick=7)

    assert bb.tick == 7
    assert bb.claims == {}
    assert bb.intents.collab_mode == "relay"


def test_make_bb_honors_collab_mode():
    bb = make_bb(collab_mode="split")

    assert bb.intents.collab_mode == "split"


def test_make_bb_instances_are_independent():
    a = make_bb()
    b = make_bb()
    a.claim("x", "act", owner="P2", order_id="o1", tick=0)

    assert b.claims == {}


def test_resource_view_collects_station_names():
    state = {
        "stations": [{"n": "Cooker_1", "gx": 1, "gz": 2}, {"n": "Board_1"}],
        "cookers": [],
        "plates": [],
    }

    view = make_resource_view(state)

    assert "Cooker_1" in view.existing_ids
    assert "Board_1" in view.existing_ids


def test_resource_view_collects_plates_with_contents():
    state = {
        "stations": [],
        "cookers": [],
        "plates": [{"n": "ServerPlate_3", "contents": ["BoiledSushiRice"]}],
    }

    view = make_resource_view(state)

    assert "ServerPlate_3" in view.existing_ids
    assert view.contents["ServerPlate_3"] == ("boiledsushirice",)


def test_resource_view_includes_assembly_cells_by_coords():
    """拼装格以网格坐标为身份（spec §4.4）。"""
    state = {
        "stations": [{"n": "Counter_1", "t": "counter", "gx": 3, "gz": 0}],
        "cookers": [],
        "plates": [],
    }

    view = make_resource_view(state)

    assert (3, 0) in view.existing_ids


def test_resource_view_skips_entries_without_identity():
    state = {"stations": [{"t": "counter"}], "cookers": [], "plates": []}

    view = make_resource_view(state)

    assert view.existing_ids == set()


def test_live_order_ids_extracts_ids():
    state = {"orders": [{"id": "o1"}, {"id": "o2"}]}

    assert live_order_ids(state) == {"o1", "o2"}


def test_live_order_ids_falls_back_to_index():
    """插件可能不给订单 id，用下标兜底（与 Planner 侧约定一致）。"""
    state = {"orders": [{"recipe": "Sushi"}, {"recipe": "Rice"}]}

    assert live_order_ids(state) == {0, 1}


def test_live_order_ids_empty_when_no_orders():
    assert live_order_ids({}) == set()

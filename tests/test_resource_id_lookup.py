"""站点查找函数支持 (grid, name) 资源 id（spec §5.2，Task 11）。

验证 cooker_state / cooker_station / board_station / cleanup 内部灶查找
在收到 (grid, name) 元组时按两个分量匹配；收到裸字符串时退化为旧行为
（只比名字），保证 Task 12 之前的旧调用点不受影响。
"""
from ai.query import predicates as P
from ai.tasks.base import cooker_state, cooker_station
from ai.tasks.chop import board_station
from ai.tasks.cleanup import _cooker, _cooker_station


def _ctx(stations=None, cookers=None):
    class FakeWorld(object):
        pass
    w = FakeWorld()
    w.stations = stations or []
    w.cookers = cookers or []
    return {"w": w}


def test_cooker_state_matches_by_name_when_no_grid():
    """向后兼容：旧调用传裸字符串，忽略 grid 比对（过渡期行为）。"""
    ctx = _ctx(cookers=[{"n": "cooker_01", "cooked": False}])
    assert cooker_state(ctx, "cooker_01") == {"n": "cooker_01", "cooked": False}


def test_cooker_state_matches_grid_and_name_tuple():
    """新调用传 (grid, name)：两边都要匹配，解决同名撞车（§5.2）。"""
    ctx = _ctx(cookers=[
        {"n": "cooker_01", "grid": "GridManager", "cooked": False},
        {"n": "cooker_01", "grid": "Balloon Basket", "cooked": True},
    ])
    got = cooker_state(ctx, ("Balloon Basket", "cooker_01"))
    assert got["grid"] == "Balloon Basket"
    assert got["cooked"] is True


def test_cooker_station_matches_grid_and_name_tuple():
    ctx = _ctx(stations=[
        {"t": "cooking", "n": "cooker_01", "grid": "GridManager"},
        {"t": "cooking", "n": "cooker_01", "grid": "Balloon Basket"},
    ])
    got = cooker_station(ctx, ("Balloon Basket", "cooker_01"))
    assert got["grid"] == "Balloon Basket"


def test_board_station_distinguishes_same_name_different_grid():
    ctx = _ctx(stations=[
        {"t": "chopping", "n": "board_01", "grid": "GridManager"},
        {"t": "chopping", "n": "board_01", "grid": "Balloon Basket"},
    ])
    got = board_station(ctx, ("Balloon Basket", "board_01"))
    assert got["grid"] == "Balloon Basket"


def test_board_station_matches_by_name_when_no_grid():
    ctx = _ctx(stations=[{"t": "chopping", "n": "board_01", "grid": "GridManager"}])
    got = board_station(ctx, "board_01")
    assert got["n"] == "board_01"


def test_cleanup_cooker_lookup_distinguishes_same_name_different_grid():
    ctx = _ctx(cookers=[
        {"n": "cooker_01", "grid": "GridManager", "cooked": False},
        {"n": "cooker_01", "grid": "Balloon Basket", "cooked": True},
    ])
    got = _cooker(ctx, ("Balloon Basket", "cooker_01"))
    assert got["grid"] == "Balloon Basket"
    assert got["cooked"] is True


def test_cleanup_cooker_station_lookup_matches_by_name_when_no_grid():
    ctx = _ctx(stations=[{"t": "cooking", "n": "cooker_01", "grid": "GridManager"}])
    got = _cooker_station(ctx, "cooker_01")
    assert got["n"] == "cooker_01"


def test_cleanup_cooker_station_lookup_distinguishes_same_name_different_grid():
    ctx = _ctx(stations=[
        {"t": "cooking", "n": "cooker_01", "grid": "GridManager"},
        {"t": "cooking", "n": "cooker_01", "grid": "Balloon Basket"},
    ])
    got = _cooker_station(ctx, ("Balloon Basket", "cooker_01"))
    assert got["grid"] == "Balloon Basket"


# ---------- 两张表的 grid 必须一致（实机回归，trace-20261016-215154）----------
#
# 上面每个用例都给 cookers[] 与 stations[] 同样的 grid，所以「两张表不一致」
# 这个失效模式结构上测不出来。实机就栽在这儿：插件的 AppendCookers 读
# ServerCookingStation 上的 m_gridManager（恒 null，网格归属其实挂在同
# GameObject 的 StaticGridLocation 组件上），AppendStations 枚举的就是
# GridLocation 本体（报 "GridManager"）。
#
# 后果：候选 id 由 P.resource_id(cookers[i]) 构造 → ("", name)，
# cooker_state 查 cookers[] 命中（null 与 "" 都归一化成 ""），
# cooker_station 查 stations[] 落空（"GridManager" != ""）。FillCooker
# 于是取到米却解析不出灶的位置，放锅那步 target=None 直接 FAILURE，
# 下一棵树的 EnsureHandsFree 把米放下 —— 「拿起米又放下」，整局零烹饪。


def test_resource_id_from_cookers_resolves_in_both_tables():
    """同一口灶：从 cookers[] 构造的 id 必须在两张表里都查得到。

    这是协议契约而非实现细节——状态在 cookers[]、位置在 stations[]，
    任何按资源 id 工作的 task 都要两张表各取一半。
    """
    cooker = {"n": "workstation_cooker_01 (3)", "grid": "GridManager",
              "pot_contents": []}
    station = {"t": "cooking", "n": "workstation_cooker_01 (3)",
               "grid": "GridManager", "gx": 7, "gz": -2}
    ctx = _ctx(stations=[station], cookers=[cooker])

    rid = P.resource_id(cooker)
    assert cooker_state(ctx, rid) is cooker
    assert cooker_station(ctx, rid) is station, (
        "位置查不到：cookers[] 与 stations[] 的 grid 字段不一致")


def test_cooker_grid_mismatch_breaks_station_lookup():
    """反向钉住失效模式本身：grid 不一致时位置查找必然落空。

    这条不是在要求"保持这个 bug"，而是记录「两张表 grid 不一致」会
    精确地造成什么——将来有人再让插件某张表少报 grid 时，上面那条
    契约测试失败的原因在这里能直接读到。
    """
    cooker = {"n": "cooker_01", "grid": None}           # 插件旧行为
    station = {"t": "cooking", "n": "cooker_01", "grid": "GridManager"}
    ctx = _ctx(stations=[station], cookers=[cooker])

    rid = P.resource_id(cooker)
    assert rid == ("", "cooker_01")
    assert cooker_state(ctx, rid) is cooker       # 状态侧命中
    assert cooker_station(ctx, rid) is None       # 位置侧落空 → 树 FAILURE

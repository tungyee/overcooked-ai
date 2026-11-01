"""平台接缝门洞（ai/space/seams.py）：门洞开在两排格中心之间时也要能穿缝。

墙盒取自 trace-20261030-001857 t=1486（Balloon_2_3，x=13.2 固定地面 | 食材篮）：
固定墙开口 z∈[-13.8,-10.55]，篮子墙开口 z∈[-11.81,-8.96]，缝 1.26m 宽；
厨师半径 0.45 收窄后可站 z∈[-11.36,-11.0]，两排格中心 -12.0/-10.8 都在墙里，
旧判定整条接缝不通（P1 实际在 t=1486 走过去了）。
"""
from ai.query import predicates as P
from ai.space import navigation, reachability, seams
from ai.state.world import World

LEFT_WALLS = [[12.6, 13.2, -15.6, -13.8], [12.6, 13.2, -10.55, -8.95]]


def _frame(basket_dz=0.0, p2=(10.8, -10.8)):
    """basket_dz：篮子沿 z 平移（门洞跟着动）。"""
    def sh(b):
        return [b[0], b[1], b[2] + basket_dz, b[3] + basket_dz]
    basket_walls = [sh([13.2, 13.8, -14.01, -11.81]), sh([13.2, 13.8, -8.96, -7.36])]
    return {
        "level": "Balloon_2_3_2P",
        "grid": {"size": "(1.2, 1.0, 1.2)", "mapOffset": "0,0", "nodeMap": None},
        "grids": [{"id": "GridManager", "ox": 8.4, "oz": -12.0, "cell": 1.2,
                   "main": True, "moving": False}],
        "terrain": [
            {"layer": 9, "boxes": [[6.0, 13.2, -15.6, -8.4], sh([13.2, 19.2, -14.01, -6.81])]},
            {"layer": 10, "boxes": LEFT_WALLS + basket_walls},
        ],
        "chefs": [{"name": P.P2_NAME, "x": p2[0], "z": p2[1]},
                  {"name": P.P1_NAME, "x": 7.2, "z": -14.4}],
        # 校准主网格原点 (8.4,-12.0) 用的锚点台面（gx=-2, gz=-3）
        "stations": [{"n": "Counter", "t": "counter", "x": 6.0, "z": -15.6,
                      "gx": -2, "gz": -3, "grid": "GridManager"}],
    }


def test_door_between_cell_centers_is_a_gate():
    w = World(_frame())
    assert not w.walkable(4, 1) and not w.walkable(4, 0)      # 旧判定：两排都在墙里
    (seam,) = w.seam_gates.seams
    assert seam.axis == "x" and abs(seam.line - 13.2) < 1e-6
    (band,) = seam.bands()
    assert abs(band[0] + 11.36) < 1e-6 and abs(band[1] + 11.0) < 1e-6
    open_cells = sorted(c for c, (_, b) in w.seam_gates.gates.items() if b)
    assert open_cells == [(4, 1)]


def test_field_crosses_seam_only_through_gate():
    w = World(_frame())
    field = reachability.field_for(w)
    assert (6, 1) in field                                       # 篮子那边可达
    # 不叠门洞的旧判定过不去
    old = navigation.distance_field((2, 1), w.walkable, w.belt_dir,
                                    edge_blocked=w.edge_blocked)
    assert (6, 1) not in old


def test_door_closed_blocks_crossing():
    # 篮子上移 2.4m：篮子墙下段 [-11.61,-9.41] 正好盖住固定墙开口的可站段
    w = World(_frame(basket_dz=2.4))
    assert w.seam_gates is None or not any(b for _, b in w.seam_gates.gates.values())
    assert (6, 1) not in reachability.field_for(w)


def test_gate_cells_only_cross_never_slide_along_seam():
    w = World(_frame())
    walkable, edge = seams.nav_predicates(w)
    assert walkable(4, 1)
    assert edge((3, 1), (4, 1)) is False
    assert edge((4, 1), (4, 2)) is True       # 顺着墙带走
    assert edge((4, 0), (4, 1)) is True


def test_path_points_route_through_door_midline():
    w = World(_frame())
    field = reachability.field_for(w)
    path = navigation.trace_back(field, (6, 1), w.belt_dir, None,
                                 seams.nav_predicates(w)[1])
    assert (4, 1) in path
    points, gates = seams.path_points(w, path)
    (g,) = gates
    enter, exit_ = points[g["enter"]], points[g["exit"]]
    # 缝口两侧：墙带 13.2±0.6 外再让出半径 0.45；沿缝走门洞中线 -11.18
    assert abs(enter[0] - 12.15) < 1e-6 and abs(exit_[0] - 14.25) < 1e-6
    assert abs(enter[1] + 11.18) < 1e-6 and abs(exit_[1] + 11.18) < 1e-6
    assert g["axis"] == "x" and g["exit"] == g["enter"] + 1
    assert len(points) == len(path) + 1        # 一格门洞换成入点+出点


def test_crossing_right_to_left_flips_enter_side():
    w = World(_frame(p2=(16.8, -10.8)))
    field = reachability.field_for(w)
    path = navigation.trace_back(field, (1, 1), w.belt_dir, None,
                                 seams.nav_predicates(w)[1])
    points, gates = seams.path_points(w, path)
    (g,) = gates
    assert points[g["enter"]][0] > 13.2 > points[g["exit"]][0]


def test_no_seam_walls_no_gates():
    f = _frame()
    f["terrain"] = [f["terrain"][0]]           # 只剩地板，贴边无墙
    w = World(f)
    assert w.seam_gates is None
    points, gates = seams.path_points(w, [(2, 1), (3, 1)])
    assert gates == [] and points == [list(w.to_world(2, 1)), list(w.to_world(3, 1))]


def test_stairs_meeting_platform_is_not_a_gate():
    """Wizard_5_5（trace-20261101-141149 t=580）：左侧二楼平台右沿 x=10.2 有栏杆开口，
    楼梯（SlopedGround）外扩并入 Ground 后与平台贴边。旧逻辑当成门洞发给插件，
    插件在楼梯上按脚底高度测胶囊扎进二楼地板，判门恒关，P2 卡在楼梯顶。"""
    from ai.space import occupancy
    terrain = [
        {"layer": 9, "boxes": [[5.4, 10.2, -9.6, -1.2], [13.8, 23.4, -10.2, -0.6]]},
        {"layer": 10, "boxes": [[9.6, 10.2, -4.2, -1.2], [9.6, 10.2, -9.0, -6.6]]},
        {"layer": 28, "boxes": [[10.51, 11.14, -6.73, -4.21], [11.31, 11.94, -6.73, -4.21],
                                [12.11, 12.74, -6.73, -4.21], [12.91, 13.54, -6.73, -4.21]]},
    ]
    by_layer = occupancy.parse_terrain(terrain)
    assert seams.find_seams(by_layer) == []
    # 两块真地板之间有墙的接缝仍照常识别
    f = _frame()
    assert len(seams.find_seams(occupancy.parse_terrain(f["terrain"]))) == 1


# Wizard_5_5（trace-20261101-145921）：楼梯会沉到地下再升起，平面位置不变。
_LEFT_STAIRS = [(10.51, 11.14), (11.31, 11.94), (12.11, 12.74), (12.91, 13.54)]


def _stairs_terrain(dy):
    """左楼梯四级台阶整体下沉 dy（0 = 在位：底阶 0.01~0.39，顶阶 1.56~1.88）。"""
    steps = [[x0, x1, -6.73, -4.21, 1.56 - 0.5 * i - dy, 1.88 - 0.5 * i - dy]
             for i, (x0, x1) in enumerate(_LEFT_STAIRS)]
    steps[-1][4:] = [0.01 - dy, 0.39 - dy]
    return [
        {"layer": 9, "boxes": [[5.4, 10.2, -9.6, -1.2, 1.5, 2.0], [13.8, 23.4, -10.2, -0.6, -1.0, 0.0]]},
        {"layer": 28, "boxes": steps},
    ]


def test_sunk_stairs_not_merged_into_ground():
    from ai.space import occupancy
    n_ground = 2
    assert len(occupancy.parse_terrain(_stairs_terrain(0.0))[9]) == n_ground + 4
    for dy in (0.5, 3.0, 7.5):          # 下沉中 / 沉到底
        out = occupancy.parse_terrain(_stairs_terrain(dy))
        assert len(out[9]) == n_ground
        assert len(out[28]) == 4         # 原层号照留
    # 旧帧（无高度）照旧并入
    flat = [{"layer": 9, "boxes": [[0, 1, 0, 1]]}, {"layer": 28, "boxes": [[1, 2, 0, 1]]}]
    assert len(occupancy.parse_terrain(flat)[9]) == 2

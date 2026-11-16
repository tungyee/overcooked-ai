"""tools/walk_probe_report：插件物理掩码与 terrain 判定的离线对照。"""
import json

from tools import walk_probe_report as R

# 墙盒 z∈[0.2,1.0] 离 (0,0) 格中心 0.2m < CHEF_CLEARANCE：py 判不可走。
MAP = {
    "terrain": [
        {"layer": 9, "boxes": [[-5.0, 5.0, -5.0, 5.0]]},
        {"layer": 10, "boxes": [[-0.6, 0.6, 0.2, 1.0]]},
    ],
}


def _probe(center_row):
    # 1×1 格窗口：(0,0)，3×3 子点；中间一行由参数给出
    return {"ms": 0.5, "ox": 0.0, "oz": 0.0, "cell": 1.2, "sub": 3,
            "chefs": [{"name": "Player 1", "x": 0, "z": 0, "self": None,
                       "gx0": 0, "gz0": 0,
                       "rows": ["..." + center_row + "###"],
                       "who": ["---" + "---" + "000"]}],
            "blockers": [{"path": "Basket/Wall", "type": "BoxCollider", "layer": 10,
                          "layer_name": "Walls", "terrain": True, "rot": [0, 30, 0],
                          "aabb": [-0.6, 0.6, 0.2, 1.0, 0, 1]}]}


def test_classify_center_free_but_py_blocked_is_fn_center():
    probe = _probe("...")
    walk = R.py_walkable(MAP, probe)
    assert walk(0, 0) is False
    (gx, gz, mask, _), = list(R.cells(probe["chefs"][0], 3))
    assert R.classify(walk, gx, gz, mask, 3) == "FN_center"


def test_classify_only_offset_free_is_fn_offset():
    probe = _probe(".#.")
    walk = R.py_walkable(MAP, probe)
    (gx, gz, mask, _), = list(R.cells(probe["chefs"][0], 3))
    assert R.classify(walk, gx, gz, mask, 3) == "FN_offset"


def test_classify_py_walkable_center_blocked_is_fp():
    m = {"terrain": [MAP["terrain"][0]]}
    probe = _probe(".#.")
    walk = R.py_walkable(m, probe)
    (gx, gz, mask, _), = list(R.cells(probe["chefs"][0], 3))
    assert R.classify(walk, gx, gz, mask, 3) == "FP"


def test_summarize_dedupes_repeated_frames_and_flags_tilt(tmp_path, capsys):
    path = tmp_path / "trace.jsonl"
    rec = {"t": 1, "state": {"map": dict(MAP, walk_probe=_probe("..."))}}
    with open(str(path), "w") as f:
        for t in (1, 2, 3):
            rec["t"] = t
            f.write(json.dumps(rec) + "\n")
    R.summarize(str(path))
    out = capsys.readouterr().out
    assert "probe 帧数: 1" in out
    assert "'FN_center': 1" in out
    assert "TILTED" in out

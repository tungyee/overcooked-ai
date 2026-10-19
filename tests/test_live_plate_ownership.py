"""0.19.0 Windows 1-5 实机盘实例回归，无输入注入。"""
import gzip
import json
from pathlib import Path

from ai import tasks as T
from ai.query import predicates as P
from ai.state.world import World
from ai.tasks.cook import _holding_plate


def test_live_held_plate_survives_other_player_holding_same_model():
    fixture = Path(__file__).parent / 'fixtures' / 'ownership' / 'live019-held-plate.ndjson.gz'
    with gzip.open(fixture, 'rt', encoding='utf8') as stream:
        frame = json.loads(next(stream))
    w = World(frame)
    p = next(p for p in w.held_plates if p['held_by'] == 'Player 2')
    p1 = P.chef_by_name(w.chefs, P.P1_NAME)
    p2 = P.chef_by_name(w.chefs, P.P2_NAME)
    # 同一渲染模型名，两只不同的盘：复现 trace t=1154 的并发持盘。
    p1['held'] = p['name']
    ctx = {'w': w, 'chef': p2}
    guard = T.DeliverPlate('2', (p['n'],)).build().child
    assert guard.pred(ctx)
    assert _holding_plate(ctx, p['n'])
    p['held_by'] = 'Player 1'
    assert not guard.pred(ctx)
    assert not _holding_plate(ctx, p['n'])

"""平台接缝门洞的开关时间表：从逐帧观测学周期，预测「某时刻到缝口要等多久」。

篮子挂在 Animator 下按固定周期动（Balloon_2_3 实测一圈 ≈27s：x=13.2 那道门
每圈开 ≈9s、关 ≈18s），门洞开关因此也是周期的。这里不猜动画语义，只记每道
门（接缝）的开→关 / 关→开沿：
  - 周期 T：相邻两次「开」沿的间隔；最近两段一致（±8%）取均值，只有一段先暂用
    （与插件报的动画循环长度 grids[].anim.len/speed 明显不符时不用）；只见过
    一次开门沿时，有动画循环长度就先用它；
  - 开门时长 D：最近一次完整的「开→关」；
  - 相位：最近一次「开」沿 r0；第 k 圈开在 [r0+kT, r0+kT+D]。
观测与预测不符（新「开」沿偏离预测 > SLIP）就丢掉旧沿重学，不硬套。

时钟用插件的 level_time（Time.timeSinceLevelLoad，随暂停停、与动画同步），
旧插件没有时退回调用方给的单调时钟。不读 World、不写 Blackboard：由 agent
每拍 observe 一次，结果挂在 bb.seam_clock 上给寻路查。
"""
from typing import Dict, List, Optional, Tuple

PERIOD_TOL = 0.08     # 两段周期相对差 ≤ 8% 算一致
SLIP = 1.0            # 新开门沿偏离预测超过 1s → 时间表作废重学
OPEN_MARGIN = 0.5     # 预测开门窗口两头各让 0.5s（5Hz 采样 + 门洞收窄误差）
FLICKER = 1.0         # 开/关不足 1s 又翻回去：门洞擦边抖动，不算沿
MAX_RISES = 6
GAP_RESET = 2.0       # 两次观测间隔超过 2s（暂停 / 空转 / 掉帧）：中间的开关沿看不见，重新定初值
PERIOD_MIN, PERIOD_MAX = 5.0, 120.0   # 学出来的周期超出这个范围当脏数据，不预测

GateKey = Tuple[str, float]          # (axis, 接缝线坐标)，与 SeamGates 的 seam 对应


def seam_key(seam):
    # type: (object) -> GateKey
    return (seam.axis, round(seam.line, 1))


class _Track(object):
    def __init__(self):
        self.open = None        # type: Optional[bool]
        self.rises = []         # type: List[float]
        self.last_fall = None   # type: Optional[float]
        self.duration = None    # type: Optional[float]
        self.prev_duration = None  # type: Optional[float]
        self.dropped = None     # type: Optional[Tuple[float, float]]   # 抖动丢掉的 (开门沿, 丢弃时刻)
        self.armed = False      # 最近一次开门沿是亲眼看到的（关门沿才能据此算开门时长）
        self.period_hint = None  # type: Optional[float]   # 相位漂移重对相位时沿用的周期
        self.cells = {}         # type: Dict[Tuple[int, int], float]   # 开过的门洞格 → 最近中线
        self.anchors = {}       # type: Dict[Tuple[int, int], object]  # 开过的门洞格 → 开门时两侧地板下沿
        self.fresh = set()      # type: set   # 本轮开门已记过落点的格


class SeamClock(object):
    """每道门一条时间线。observe 每拍调一次；wait 查询是纯读。"""

    def __init__(self):
        self.tracks = {}         # type: Dict[GateKey, _Track]
        self.now = None          # type: Optional[float]
        self.anim_period = None  # type: Optional[float]

    # ---------- 观测 ----------
    def observe(self, now, seam_gates, anim_period=None):
        # type: (float, object, Optional[float]) -> None
        """记一帧门洞状态。now 不前进（同一帧重 tick）时不重复记沿。"""
        if self.now is not None and now <= self.now:
            return
        if self.now is not None and now - self.now > GAP_RESET:
            # 观测断档：断档里开关了几次不知道，不能把首尾两帧连成一条沿。
            # 只丢「当前开/关」初值，已学到的沿和周期保留（时间表仍按周期外推）。
            for tr in self.tracks.values():
                tr.open = None
                tr.dropped = None
                tr.armed = False
        self.now = now
        self.anim_period = anim_period
        if seam_gates is None:
            return
        opened = {}  # type: Dict[GateKey, Dict[Tuple[int, int], float]]
        anchors = {}  # type: Dict[GateKey, object]
        for seam in seam_gates.seams:
            opened.setdefault(seam_key(seam), {})
            anchors[seam_key(seam)] = getattr(seam, "anchors", None)
        for cell, (seam, band) in seam_gates.gates.items():
            if band is not None:
                opened[seam_key(seam)][cell] = (band[0] + band[1]) / 2.0
        for key, cells in opened.items():
            self._observe_one(key, now, bool(cells), cells, anchors.get(key))

    def _observe_one(self, key, now, is_open, cells, anchors=None):
        # type: (GateKey, float, bool, Dict[Tuple[int, int], float], object) -> None
        tr = self.tracks.setdefault(key, _Track())
        tr.cells.update(cells)
        # 每格记「这一轮开门时第一次见它开」的两侧地板位置：人一般在开门那一刻
        # 过缝，此后篮子还在动，取首帧最接近过缝时的落点。
        for cell in cells:
            if cell not in tr.fresh:
                tr.anchors[cell] = anchors
                tr.fresh.add(cell)
        if not is_open:
            tr.fresh.clear()
        if tr.open is None:
            tr.open = is_open      # 第一帧只定初值：开局就开着不算「开」沿
            return
        if is_open and not tr.open:
            if tr.dropped is not None and now - tr.dropped[1] < FLICKER:
                tr.rises.append(tr.dropped[0])   # 刚开一下就关、又马上开：还是那次开门
            elif (tr.last_fall is not None and now - tr.last_fall < FLICKER
                    and tr.rises and tr.rises[-1] < tr.last_fall):
                tr.last_fall = None              # 关了一下马上又开：门洞擦边抖动，并回去
                tr.duration = tr.prev_duration
            else:
                predicted = self._next_rise_after(tr, now - SLIP - 1e-6)
                if predicted is not None and abs(predicted - now) > SLIP:
                    # 观测与时间表不符。只是相位漂了（离上次开门仍接近整数圈，
                    # 例如掉帧 / 断档漏看了几帧）就保留周期与开门时长、按这次重新对相位；
                    # 否则整张表丢掉重学。不能一错就清空：重学要再等一整圈（≈27s）。
                    T = self._period(tr)
                    n = round((now - tr.rises[-1]) / T) if T else 0
                    if T and n >= 1 and abs(now - tr.rises[-1] - n * T) <= PERIOD_TOL * T * n:
                        tr.period_hint = T
                        tr.rises = []
                    else:
                        tr.rises, tr.duration, tr.period_hint = [], None, None
                tr.rises.append(now)
                del tr.rises[:-MAX_RISES]
            tr.armed = True
        elif not is_open and tr.open:
            if tr.rises and now - tr.rises[-1] < FLICKER:
                tr.dropped = (tr.rises.pop(), now)   # 开了一下马上又关：抖动，先不算开门
            else:
                tr.last_fall = now
                tr.prev_duration = tr.duration
                if tr.armed and tr.rises and tr.rises[-1] < now:
                    tr.duration = now - tr.rises[-1]
                tr.armed = False
        if is_open:
            tr.dropped = None        # 抖动丢掉的开门沿只给紧接着的这次开门用
        tr.open = is_open

    # ---------- 模型 ----------
    def period(self, key):
        # type: (GateKey) -> Optional[float]
        tr = self.tracks.get(key)
        return self._period(tr) if tr is not None else None

    def _period(self, tr):
        # type: (_Track) -> Optional[float]
        if len(tr.rises) < 2:
            if tr.rises and tr.duration and tr.period_hint:
                return tr.period_hint        # 相位漂移后重新对相位，周期沿用
            if tr.rises and tr.duration and self.anim_period:
                return self.anim_period      # 一个开门沿 + 动画循环长度
            return None
        gaps = [b - a for a, b in zip(tr.rises, tr.rises[1:])]
        last = gaps[-1]
        if len(gaps) >= 2 and abs(gaps[-2] - last) <= PERIOD_TOL * last:
            return (gaps[-2] + last) / 2.0
        if self.anim_period and abs(self.anim_period - last) > PERIOD_TOL * last:
            return None          # 动画循环长度与观测不符，宁可不预测
        # 只有一段间隔：先当周期用（一圈 27s，等两段要 80s+，一局才 180s）。
        # 下一次开门沿偏离预测 > SLIP 时 _observe_one 会丢掉旧沿重学。
        return last

    def known(self, key):
        # type: (GateKey) -> bool
        tr = self.tracks.get(key)
        if not (tr and tr.rises and tr.duration):
            return False
        T = self._period(tr)
        return bool(T and PERIOD_MIN <= T <= PERIOD_MAX and tr.duration < T)

    def _next_rise_after(self, tr, t):
        # type: (_Track, float) -> Optional[float]
        T = self._period(tr) if tr.rises else None
        if not T or not PERIOD_MIN <= T <= PERIOD_MAX:
            return None
        r0 = tr.rises[-1]
        k = max(0, int((t - r0) // T) + 1)
        return r0 + k * T

    def cell_mid(self, key, cell):
        # type: (GateKey, Tuple[int, int]) -> Optional[float]
        tr = self.tracks.get(key)
        return tr.cells.get(cell) if tr else None

    def open_cells(self, key):
        # type: (GateKey) -> Dict[Tuple[int, int], float]
        tr = self.tracks.get(key)
        return dict(tr.cells) if tr else {}

    def cell_anchors(self, key, cell):
        # type: (GateKey, Tuple[int, int]) -> object
        """这格上次开门时两侧地板的沿缝下沿 (低侧, 高侧)；没记过返回 None。"""
        tr = self.tracks.get(key)
        return tr.anchors.get(cell) if tr else None

    def wait(self, key, arrive, need=0.0, open_now=False):
        # type: (GateKey, float, float, bool) -> Optional[float]
        """在 arrive 时刻到缝口，要等几秒才能在门开着的时候过完（need 秒）。

        时间表未知：门此刻开着按 0（与没有时间表时一致），关着返回 None（不通）。
        """
        if not self.known(key):
            return 0.0 if open_now else None
        tr = self.tracks[key]
        T, D, r0 = self.period(key), tr.duration, tr.rises[-1]
        lo_pad, hi_pad = OPEN_MARGIN, OPEN_MARGIN + need
        if D - lo_pad - hi_pad <= 0:
            return None
        k = int((arrive - r0) // T)
        for kk in (k, k + 1, k + 2):
            start = r0 + kk * T + lo_pad
            end = r0 + kk * T + D - hi_pad
            if arrive <= end:
                return max(0.0, start - arrive)
        return None


def summary(clock):
    # type: (Optional[SeamClock]) -> Optional[Dict[str, Dict]]
    """trace 用的时间表快照：{"x=13.2": {known, T, D, open, next_open, next_close}}。

    next_open / next_close 是从 clock.now 起还有几秒（按时间表外推；未知为 None）。
    """
    if clock is None or clock.now is None:
        return None
    out = {}
    for key, tr in sorted(clock.tracks.items()):
        known = clock.known(key)
        row = {"known": known, "open": tr.open,
               "T": round(clock.period(key), 2) if clock.period(key) else None,
               "D": round(tr.duration, 2) if tr.duration else None,
               "next_open": None, "next_close": None}
        if known:
            T, D, r0 = clock.period(key), tr.duration, tr.rises[-1]
            k = (clock.now - r0) // T
            start = r0 + k * T
            if clock.now < start + D:
                row["next_close"] = round(start + D - clock.now, 2)
                row["next_open"] = round(start + T - clock.now, 2)
            else:
                row["next_open"] = round(start + T - clock.now, 2)
                row["next_close"] = round(start + T + D - clock.now, 2)
        out["%s=%s" % key] = row
    return out


def anim_period(grids):
    # type: (Optional[List[Dict]]) -> Optional[float]
    """会动网格的动画循环长度（秒）：都 loop 且长度一致才给。"""
    periods = []
    for g in grids or ():
        a = g.get("anim") if isinstance(g, dict) else None
        if not isinstance(a, dict) or not a.get("loop"):
            continue
        length, speed = a.get("len"), a.get("speed")
        try:
            if length and speed and float(speed) > 0:
                periods.append(float(length) / float(speed))
        except (TypeError, ValueError):
            continue
    if not periods:
        return None
    p0 = periods[0]
    return p0 if all(abs(p - p0) <= PERIOD_TOL * p0 for p in periods) else None

# -*- coding: utf-8 -*-
"""AI 主循环（新架构，Plan 3 Task 8）。

职责：13 步装配：收帧 → 对账 → 推断 → 仲裁 → 规划 → 执行 → 日志。
与 oc/ai/agent.py 的差异：旧实现 1180 行 God Object，新实现 ~180 行装配层。
Spec: §8.1 主循环
"""
import logging
import queue
import time
from typing import Any, Dict, List, Optional, Tuple

from ai import tasks as T
from ai.core import arbiter, executor, planner
from ai.dialog.duties import (Kitchen, Ledger, TaskMeta,
                              node_key, tool_schemas)
from ai.dialog.schema import DialogState, preference_matches_task
from ai.revenue import eta as ETA
from ai.space import navigation, reachability
from ai.state import p1model, reconcile
from ai.output import state_dump, trace
from ai.query import predicates as P
from ai.state.blackboard import Blackboard, Claim, Commitment
from ai.primitives.stuck import ABANDON_TTL_TICKS, note_motion_failure
from ai.core.recovery import FailureHistory, abandon_actions, waiting_reason
from ai.query import recipe_graph
from ai.query.recipes import RecipeBook
from ai.state.world import StateClient, World
from ai.exec.client import ExecClient

log = logging.getLogger(__name__)

# 主循环周期（秒）= 50 ms，即 20 Hz。全项目代码里的"拍"（tick）统一指
# **本循环拍**：所有按拍计的阈值（task 超时、按键冷却、卡住判定等）都按
# 20 Hz 理解，与插件 5 Hz 推帧（200 ms）不是一个量级——混用会让阈值差 4 倍。
TICK_INTERVAL = 0.05

# 帧最大年龄（秒）：超过视为断流，进入空闲守卫（halt 执行树、不做决策）。
# 原定义在 ai.primitives.motion，随键盘注入轨删除后挪到这里。
FRAME_MAX_AGE = 0.75

# L3 兜底：同一物理操作在 TTL 内失败满 N 次，对其认领动作短暂退避。
# 按操作独立累计，切换任务/订单不能清零；不同动作不会互相拉黑。
ABANDON_FAIL_STREAK = 3

# 对话上下文的「临期订单」阈值（秒）：remaining 低于此值的订单算快过期。
# 只给 LLM 提醒，不参与决策——决策口径仍走 planner 的 slack。
EXPIRING_ORDER_SECONDS = 20

# 最后 N 秒提醒：剩余时间首次降到该值以下时发一次 last_seconds 事件。
LAST_SECONDS_WARN_S = 10

def _timer_expired(timer):
    # type: (Any) -> bool
    """只认有效有限时长的权威计时器；未知/无时限关卡不按归零处理。"""
    if not isinstance(timer, dict):
        return False
    for side in ('server', 'client'):
        part = timer.get(side)
        if not isinstance(part, dict):
            continue
        try:
            limit, left = float(part['limit']), float(part['left'])
        except (KeyError, TypeError, ValueError):
            continue
        if limit > 0 and left <= 0:
            return True
        if limit > 0 and left > 0:
            return False
    return False


def _p1_record(p1_model):
    # type: (Any) -> Dict
    """把 P1 事实压成可序列化 dict，供 trace 的 p1 字段记录。"""
    if not p1_model:
        return {}
    return {
        "holding": getattr(p1_model, "holding", None),
    }


def _seconds(ticks):
    # type: (Any) -> Optional[float]
    """拍 → 秒（20 Hz），保留 1 位小数；None/非数值保持 None。

    对话上下文统一用秒，避免 LLM 把 ETA/slack 的拍数当成秒（差 20 倍）。
    """
    if ticks is None:
        return None
    try:
        return round(float(ticks) / planner.TICKS_PER_SECOND, 1)
    except (TypeError, ValueError):
        return None


def _claims_binding(bb):
    # type: (Any) -> Dict
    """认领绑定：{资源:动作} → 归属者 / 订单 / 已持有拍数，供 trace 排查。"""
    out = {}
    tick = getattr(bb, "tick", 0) or 0
    for (res_id, action), c in (getattr(bb, "claims", None) or {}).items():
        out["%s:%s" % (res_id, action)] = {
            "owner": getattr(c, "owner", None),
            "order": str(getattr(c, "order_id", None)),
            "age": max(0, tick - getattr(c, "claimed_tick", 0)),
        }
    return out


def _task_desc(task_type):
    # type: (str) -> str
    """task 类型 → 一句话说明（task 类的 DESC）；未注册返回空串。"""
    return getattr(T.TASK_TYPES.get(task_type), "DESC", "") or ""


def _build_task_meta():
    # type: () -> Dict[str, TaskMeta]
    """从 `T.TASK_TYPES` 汇总账本用的 task 元数据；救援类口径取 planner 的。"""
    out = {}
    for name, cls in T.TASK_TYPES.items():
        out[name] = TaskMeta(
            desc=getattr(cls, "DESC", "") or "",
            label=getattr(cls, "LABEL", "") or "",
            process=getattr(cls, "PROCESS", "") or "",
            shared=bool(getattr(cls, "SHARED", False)),
            station=getattr(cls, "STATION", None),
            rescue=planner._is_rescue_action(cls),
            allowed_modes=getattr(cls, "DUTY_MODES", None),
        )
    return out


_TASK_META = _build_task_meta()


def _task_text(task, with_order=True):
    # type: (Any, bool) -> Optional[str]
    """task → DAG 节点 key（`Chop:Cucumber` / `WashPlate`），给 LLM 看。"""
    if task is None:
        return None
    task_type = getattr(task, "TYPE", None)
    item = getattr(task, "item", None)
    if getattr(T.TASK_TYPES.get(task_type), "SHARED", False) or not item:
        text = str(task_type)
    else:
        text = node_key(task_type, item)
    order_id = getattr(task, "order_id", None)
    if not with_order or order_id is None:
        return text
    return text + "（订单 #%s）" % (order_id,)


def _token_text(token):
    # type: (str) -> str
    """盘内 token（plate_tokens 口径）→ 可读名：`cooked:sushirice:pot` → `SushiRice(煮熟)`。"""
    if token.startswith("cooked:"):
        parts = token.split(":")
        raw = parts[1] if len(parts) > 1 else "?"
        return "%s(熟)" % (raw,)
    return token


def _cooker_text(index, cooker):
    # type: (int, Dict) -> Dict
    """灶的一行描述：锅里有什么、煮到哪、是否快糊。灶按出现顺序编号。"""
    contents = [str(c) for c in cooker.get("pot_contents") or ()]
    if cooker.get("onFire"):
        state = "着火了"
    elif cooker.get("burning"):
        state = "快糊了"
    elif cooker.get("cooked"):
        state = "已熟"
    elif cooker.get("isCooking"):
        prog, total = cooker.get("cooking_progress"), cooker.get("cooking_time")
        try:
            state = "在煮，%d%%" % (int(100 * float(prog) / float(total)),)
        except (TypeError, ValueError, ZeroDivisionError):
            state = "在煮"
    elif contents:
        state = "有料未开火"
    else:
        state = "空"
    text = state
    if contents:
        text = "%s；锅里 %s" % (state, "、".join(contents))
    return {"name": "%d 号灶" % (index,), "text": text}


def _dirty_plate_count(world):
    # type: (World) -> int
    """待洗脏盘数：所有洗碗池待洗盘 + 场上脏盘摞。

    与 ai/tasks/plate.py 的 sink_plate_count / dirty_plate_exists 同一口径
    （washing 站点的 plate_count + items 里名字含 dirtyplate 的条数），只是
    把「单个池子的 plate_count」推广成「所有池子求和」。
    """
    count = 0
    for st in getattr(world, "stations", None) or ():
        if isinstance(st, dict) and st.get("t") == "washing":
            try:
                count += int(st.get("plate_count") or 0)
            except (TypeError, ValueError):
                pass
    for it in getattr(world, "items", None) or ():
        if isinstance(it, dict) and \
                "dirtyplate" in str(it.get("n") or "").lower():
            count += 1
    return count


def _board_item_text(item):
    # type: (Any) -> str
    """板上的物品名 → 可读文本：ChoppedX → X(已切)。"""
    s = str(item or "").strip()
    if not s:
        return ""
    if s.lower().startswith("chopped") and len(s) > len("chopped"):
        return "%s(已切)" % (s[len("chopped"):],)
    return s


def _board_texts(world):
    # type: (World) -> List[str]
    """切菜板内容列表（板名 + 板上物），空板不列；与 recipe_graph 同口径。"""
    out = []
    for name, item in recipe_graph._board_items(world).items():
        text = _board_item_text(item)
        if not text:
            continue
        out.append("%s：%s" % (name, text))
    return out


def _cooker_idle(cooker):
    # type: (Dict) -> bool
    """灶是否空：与 `_cooker_text` 的「空」同口径（无料、没火、没在煮）。"""
    contents = [c for c in (cooker.get("pot_contents") or ()) if c]
    return not contents and not (cooker.get("onFire") or cooker.get("burning")
                                 or cooker.get("cooked")
                                 or cooker.get("isCooking"))


def _idle_cooker_count(world):
    # type: (World) -> int
    return sum(1 for c in getattr(world, "cookers", None) or ()
               if isinstance(c, dict) and _cooker_idle(c))


def _score_field(world, field):
    # type: (World, str) -> Optional[int]
    """world.score / world.raw.score 里的整数结算字段；取不到返回 None。"""
    for src in (getattr(world, "score", None),
                (getattr(world, "raw", None) or {}).get("score")):
        if isinstance(src, dict) and src.get(field) is not None:
            try:
                return int(src[field])
            except (TypeError, ValueError):
                return None
    return None


def _combo(world):
    # type: (World) -> Optional[int]
    """当前连击数；取不到返回 None（与 planner._current_combo 同源，缺省不猜 0）。"""
    return _score_field(world, "combo")


def _score_total(world):
    # type: (World) -> Optional[int]
    """结算总分 = 送餐 base + 小费 tips - 漏单扣分；取不到 base 返回 None。"""
    base = _score_field(world, "base")
    if base is None:
        return None
    total = base + (_score_field(world, "tips") or 0)
    deductions = _score_field(world, "deductions")
    if deductions:
        total -= abs(deductions)
    return total


def _expiring_orders(orders):
    # type: (List[Dict]) -> List[Dict]
    """remaining 低于阈值的订单（给 LLM 的临期提醒，不参与决策）。"""
    out = []
    for order in orders or ():
        try:
            remaining = float(order.get("remaining"))
        except (TypeError, ValueError):
            continue
        if remaining < EXPIRING_ORDER_SECONDS:
            out.append({
                "id": order.get("id"),
                "dish": order.get("dish"),
                "remaining": order.get("remaining"),
            })
    return out


def _plates_text(world, book):
    # type: (World, Any) -> str
    """盘子概况：空盘数、装了料的盘各装了什么（token 归一后可读）、待洗脏盘。"""
    empty, filled = 0, []
    for plate in getattr(world, "plates", None) or ():
        try:
            tokens = sorted(book.plate_tokens(plate)) if book is not None \
                else [str(c).lower() for c in plate.get("contents") or ()]
        except Exception:                       # noqa: BLE001
            tokens = []
        if tokens:
            filled.append("[" + "、".join(_token_text(t) for t in tokens) + "]")
        else:
            empty += 1
    parts = ["空盘 %d 个" % (empty,)]
    if filled:
        parts.append("装了料的盘 " + " ".join(filled))
    parts.append("脏盘 %d 个" % (_dirty_plate_count(world),))
    return "；".join(parts)


def _duty_tools(kitchen, ledger):
    # type: (Kitchen, Ledger) -> List[dict]
    return tool_schemas(kitchen.targets(), ledger.ids())


def _option_text(task):
    # type: (Any) -> str
    """「接下来可做的」一项：节点 + 一句话说明。"""
    text = _task_text(task)
    desc = _task_desc(getattr(task, "TYPE", None))
    return "%s（%s）" % (text, desc) if desc else text


def _ranked_tasks(plan_record):
    # type: (Dict) -> List[Any]
    """decision_record → 可行候选按分数降序的 task 列表。"""
    rows = []
    for row in (plan_record or {}).get("candidates") or ():
        try:
            task, score, ok = row[0], row[1], row[3]
        except (TypeError, IndexError):
            continue
        if ok and score is not None:
            rows.append((score, task))
    rows.sort(key=lambda r: -float(r[0]))
    out, seen = [], set()
    for _score, task in rows:
        text = _task_text(task)
        if text in seen:
            continue
        seen.add(text)
        out.append(task)
    return out


def _stuck_info(bb, world, motion=None):
    # type: (Any, World, Optional[Dict]) -> Optional[Dict]
    """卡住事实（纯读）：arbiter 同格卡住优先，回退 motion 空转（spec §G.1）。

    只读 `bb.scratch` / `bb.commitment`，**不调用** arbiter._is_stuck——那会原地
    推进计数，而这里一拍内可能被对话上下文 + trace 各读一次，重复推进会污染
    arbiter 的卡住判据。返回 None 表示未卡住，否则是 trace 用的结构化字段。
    """
    from ai.state.blackboard import StuckScratch
    scratch = getattr(bb, "scratch", None)
    commitment = getattr(bb, "commitment", None)
    active = getattr(commitment, "active_task", None) if commitment else None
    retries = int(getattr(commitment, "retries", 0) or 0)
    task_text = _task_text(active)
    if isinstance(scratch, StuckScratch) and \
            scratch.same_count >= arbiter.STUCK_SAME_CELL_THRESHOLD:
        return {
            "task": task_text,
            "reason": "stuck detected",
            "pos": list(scratch.last_cell) if scratch.last_cell else None,
            "duration_ticks": int(scratch.same_count),
            "retries": retries,
        }
    motion = motion or {}
    if motion.get("stalled"):
        chef = P.chef_by_name(world.chefs, P.P2_NAME)
        cell = P.chef_cell(chef, world.to_grid) if chef is not None else None
        return {
            "task": task_text,
            "reason": "stalled",
            "pos": list(cell) if cell else None,
            "duration_ticks": int(round(
                (motion.get("stall_seconds") or 0.0) * planner.TICKS_PER_SECOND)),
            "retries": retries,
        }
    return None


def _stuck_text(info):
    # type: (Optional[Dict]) -> Optional[str]
    """卡住 → LLM 可读一句（位置 + 持续拍数）。"""
    if not info:
        return None
    pos = info.get("pos")
    loc = "(%s,%s)" % (pos[0], pos[1]) if pos and len(pos) == 2 else "未知位置"
    return "卡在 %s，%d 拍" % (loc, int(info.get("duration_ticks") or 0))


class Agent:
    """AI 主循环：13 步装配。"""

    def __init__(self, voice=None, dialog_worker=None, exec_client=None):
        # type: (Optional[Any], Optional[Any], Optional[Any]) -> None
        self.client = StateClient()
        self.bb = Blackboard()
        self.executor_inst = executor.Executor()
        self.book = None  # type: Optional[RecipeBook]
        # 唯一执行后端：插件 exec 通道（27412），叶子原语发命令而非按键。
        # exec_client 可注入（测试 / --dry-run 用 NullExec）；默认创建并连接真实客户端。
        self.exec_client = exec_client if exec_client is not None else ExecClient()
        self.exec_client.connect()
        self.tick = 0
        self.idle_logged = False  # 暂停/非关卡的心跳日志是否已写过
        self._failures = FailureHistory(ABANDON_FAIL_STREAK, ABANDON_TTL_TICKS)
        self._scene_key = None
        self._left_level = False
        self.require_live_protocol = False
        self._frame_seq = None      # 游戏收帧编号，与 Python tick 分开
        self._frame_received_at = None
        # 气球篮移动检测（跨帧）：prev 存上一帧篮子的 (minz, maxz)，
        # balloon_moving 是「最近一帧是否在动」的结论，同一帧内 4 拍复用。
        self._prev_balloon_box = None  # type: Optional[Tuple[float, float]]
        self._balloon_moving = False
        self._balloon_frame_seq = None

        # ---------- 对话层（spec §6.2–6.4）----------
        # dialog_worker 注入优先（测试）；否则有 voice 时自己建队列与 worker。
        self.dialog_worker = dialog_worker
        self.cmd_q = getattr(dialog_worker, "cmd_q", None)
        self.ack_q = getattr(dialog_worker, "ack_q", None)
        self.ctx_q = getattr(dialog_worker, "ctx_q", None)
        self.event_q = getattr(dialog_worker, "event_q", None)
        if dialog_worker is None and voice is not None:
            self.cmd_q = queue.Queue()
            self.ack_q = queue.Queue()
            self.ctx_q = queue.Queue(maxsize=1)
            self.event_q = queue.Queue(maxsize=1)
            # 延迟导入：离线运行不加载音频/网络客户端。
            from ai.dialog.worker import DialogWorker
            self.dialog_worker = DialogWorker(
                voice, self.cmd_q, self.ack_q, self.ctx_q, self.event_q)
        elif dialog_worker is not None:
            # 注入的 worker 若没暴露队列，补一套空队列，保证 service 不炸。
            self.cmd_q = self.cmd_q or queue.Queue()
            self.ack_q = self.ack_q or queue.Queue()
            self.ctx_q = self.ctx_q or queue.Queue(maxsize=1)
            self.event_q = self.event_q or queue.Queue(maxsize=1)

        self._dialog_scene_key = None  # 上次观察到的场景身份 (session_id, scene_epoch, level)；换关清账本
        self._seen_orders = {}         # 本关见过的菜 → 首张订单（新菜只说一次）
        self._seen_materials = set()   # 本关已分配过的物料（新单无新物料则不播）
        self._last_seconds_fired = False   # 最后 10 秒提醒只发一次
        self._round_summary_fired = False  # 结束总结只发一次
        self._round_started = False        # 计时器是否跑过（区分未开局/已结束）
        self._last_ranked = []         # 上一拍 planner 打分前几名（给 LLM 看）
        # 分工账本：`bb.preferences` 只由它编译（ai/dialog/duties.py）。
        self.ledger = Ledger()

    def run(self, max_seconds=None, stop_requested=None):
        """主循环入口（20 Hz）。插件 5 Hz 推帧，中间 4 拍复用最新帧重 tick。"""
        latest = None  # type: Optional[Any]
        end_at = time.monotonic() + max_seconds if max_seconds is not None else None
        try:
            if self.dialog_worker is not None:
                self.dialog_worker.start()
            while (end_at is None or time.monotonic() < end_at) and not (stop_requested and stop_requested()):
                started = time.monotonic()
                # exec 通道（27412）可能晚于快照端口就绪，或运行中短暂断开：
                # 未连接就重试，避免一次性 connect 失败后 AI 永久空转。
                if not self.exec_client.connected:
                    self.exec_client.connect()
                snapshot = self.client.read_latest()
                if snapshot is not None:
                    latest = World(snapshot)
                    self._frame_seq = (self._frame_seq or 0) + 1
                    self._frame_received_at = time.monotonic()
                if latest is None:
                    time.sleep(TICK_INTERVAL)
                    continue
                self._tick_once(latest)
                self.tick += 1
                # 处理耗时计入周期，不能变成“处理耗时 + 50ms”。短按松键
                # 另有 controller 定时器，即使某拍规划较慢也不会被拉长。
                time.sleep(max(0.0, TICK_INTERVAL - (time.monotonic() - started)))
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            # 断线、读帧错误与意外异常均要 halt 在途命令。
            self.executor_inst.halt_and_discard()
            if self.dialog_worker is not None:
                self.dialog_worker.stop()

    # ------------------------------------------------------------ 分工账本

    def _kitchen(self, world):
        # type: (World) -> Kitchen
        """本关配方 DAG。

        菜按**本关首次出现的顺序**编（`_seen_orders` 是有序 dict），再补上
        当前订单里还没记账的菜：订单轮换不改变 DAG 文本，头部 system 消息
        稳定，方舟上下文缓存才能命中。
        """
        book = self.book or RecipeBook(world.recipes, world.stations)
        orders = list(self._seen_orders.values())
        known = {o.get("dish") for o in orders}
        orders += [o for o in world.orders or () if o.get("dish") not in known]
        return Kitchen.from_orders(
            book, orders, recipe_graph._compile_node, _TASK_META,
            {st.get("t") for st in world.stations or () if st.get("t")})

    def _apply_duty_batch(self, world, batch):
        # type: (World, Any) -> List[Dict]
        """一轮 LLM 工具调用 → 逐条落账本，返回给 LLM 的结果。"""
        kitchen = self._kitchen(world)
        tick = self.bb.tick or 0
        results = []  # type: List[Dict]
        for name, args in getattr(batch, "calls", None) or ():
            if args is None:
                results.append({"ok": False, "reason": "参数不是合法 JSON"})
                continue
            if name == "update_duties":
                result = self.ledger.apply(args, kitchen, tick,
                                           source=getattr(batch, "source", ""))
            else:
                result = {"ok": False, "reason": "未知工具：%s" % (name,)}
            results.append(result)
        self._sync_duty_preferences()
        return results

    def _sync_duty_preferences(self):
        # type: () -> None
        """账本是唯一事实来源：重编 `bb.preferences`，并让新约定立刻作用到承诺。

        新的 stop 命中 P2 当前承诺 → 清承诺（救援动作豁免，与 planner
        `_clear_claimed_commitment` 同口径）。leave_to_p1 是软偏好，不中断
        已经在做的差事，后续重规划时自然倾向别的活。
        """
        prefs = self.ledger.preferences()
        self.bb.preferences[:] = prefs
        commitment = self.bb.commitment
        active = getattr(commitment, "active_task", None) if commitment else None
        # 救援动作不清承诺：与 planner `_clear_claimed_commitment` 同一口径。
        if active is None or planner._is_rescue_action(active):
            return
        if planner._rescue_hold_live(self.bb, active):
            return
        for pref in prefs:
            if pref.kind == "stop" and \
                    preference_matches_task(pref, active):
                commitment.active_task = None
                commitment.infeasible_streak = 0
                return

    def _dialog_matches(self, world, book):
        # type: (World, RecipeBook) -> Tuple[list, Dict, Dict]
        """与 planner.plan 同口径的盘/灶 1:1 匹配（spec §6.4）。"""
        live_orders = planner._dialog_live_orders(world, self.bb)
        plate_match = planner._match_plates_to_orders(
            world, self.bb, book, orders=live_orders)
        cooker_match = planner._match_cookers_to_orders(
            world, self.bb, book, plate_match, orders=live_orders)
        return live_orders, plate_match, cooker_match

    def _maybe_reset_dialog_level(self, world):
        # type: (World) -> None
        """换关卡时清空账本与对话历史（spec §4.7 / §6.3）。

        用场景身份 (session_id, scene_epoch, level) 判变，与 `_observe_scene`
        同口径：同一关重开（level 名不变、scene_epoch 变）也要清，否则上局
        的事实会漏进这一局的 LLM 上下文。暂停/菜单不清——玩家暂停一下回来，
        分工和聊天记录都还在。首次观察只记账，不重置——启动晚于玩家进入
        关卡时不该把已说的话抹掉。
        """
        level = getattr(world, "level", None)
        if level is None:
            return
        raw = getattr(world, "raw", {})
        key = (raw.get("session_id"), raw.get("scene_epoch"), level)
        if self._dialog_scene_key is None:
            self._dialog_scene_key = key
            self._log_dialog_level_start(level)
            return
        if key == self._dialog_scene_key:
            return
        self._dialog_scene_key = key
        self._log_dialog_level_start(level)
        self.ledger.clear()
        self.bb.preferences[:] = []
        self.bb.dialog = DialogState()
        self.bb.abandoned_orders = {}
        self.bb.rescue_cooldown_until = 0
        self._seen_orders = {}
        self._seen_materials = set()
        self._last_seconds_fired = False
        self._round_summary_fired = False
        self._round_started = False
        # 上一关遗留的批次和 ack 必须丢掉：它们代表旧 World 上的决定。
        self._drain_dialog_queues()
        reset = getattr(self.dialog_worker, "reset_state", None)
        if callable(reset):
            reset()

    def _log_dialog_level_start(self, level):
        # type: (Any) -> None
        """把关卡开始时间写进对话日志，供 since_level_ms 打点。"""
        log_event = getattr(self.dialog_worker, "log_event", None)
        if not callable(log_event):
            return
        try:
            log_event("level_start", level=level)
        except Exception:                       # noqa: BLE001
            # 打点失败不能影响主循环。
            log.debug("写 level_start 打点失败", exc_info=True)

    def _drain_dialog_queues(self):
        # type: () -> None
        """丢弃命令/ack 队列里的旧世代消息（主循环线程调用）。"""
        for q in (self.cmd_q, self.ack_q):
            if q is None:
                continue
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def _service_dialog(self, world):
        # type: (World) -> Dict
        """每拍对话服务：工具批次落地、到期、事件、上下文（spec §6.3）。

        必须在 `self.book` 重建、`bb.tick` 推进之后、arbiter/planner 之前
        调用：账本先编成偏好，本拍规划才能看见。返回本拍的 trace 摘要。
        """
        record = {"batches": [], "event": None, "duties": []}
        if self.dialog_worker is None:
            return record

        self._maybe_reset_dialog_level(world)
        events = []  # type: list
        tick = self.bb.tick or 0

        # ② drain 工具批次 → 账本落地 → 逐条结果回 worker
        while True:
            try:
                batch = self.cmd_q.get_nowait()
            except queue.Empty:
                break
            try:
                results = self._apply_duty_batch(world, batch)
            except Exception as exc:            # noqa: BLE001
                # 单个坏批次不能打断 20 Hz 主循环。
                log.exception("落地工具批次失败")
                results = [{"ok": False, "reason": "主循环出错：%s" % (exc,)}
                           for _ in getattr(batch, "calls", None) or ()]
            record["batches"].append({
                "id": getattr(batch, "batch_id", None),
                "calls": getattr(batch, "calls", None), "results": results})
            self._put_ack({
                "batch_id": getattr(batch, "batch_id", None),
                "results": results,
                "duties": self._duty_rows(world, None, tick),
            })

        # ③ 到期：stop 30 秒解除；放弃订单 60 秒恢复。
        if self.ledger.prune(tick):
            self._sync_duty_preferences()
        self.bb.abandoned_orders = {
            oid: expiry for oid, expiry in self.bb.abandoned_orders.items()
            if expiry > tick
        }

        # ④ 新菜谱事件：每种菜一局只说一次（开局第一批也算，spec §5.1）。
        new_ids = []
        book = self.book or RecipeBook(world.recipes, world.stations)
        for order in world.orders:
            order_id, dish = order.get("id"), order.get("dish")
            if order_id is None or not dish:
                continue
            if dish not in self._seen_orders:
                self._seen_orders[dish] = dict(order)
                # 只有带来「新物料」的菜才播：新单的物料若全被之前的单覆盖，
                # 分工没变，不再打扰（spec §5.1 的收窄）。
                ingredients = [str(i).lower()
                               for i in book.requirements(order)]
                new_mats = [i for i in ingredients
                            if i not in self._seen_materials]
                if new_mats:
                    self._seen_materials.update(new_mats)
                    new_ids.append(order_id)
        if new_ids:
            event = ("new_order", new_ids)
            self._put_event(event)
            events.append(event)

        # ④.5 临结束提醒：最后 10 秒发一次（结束总结在 _tick_once 的等待分支里，
        # 因为 round_finished 时本函数已不会被执行）。
        time_left = planner._time_left_ticks(world)
        if (not self._last_seconds_fired and time_left is not None
                and time_left <= LAST_SECONDS_WARN_S * 20):
            self._last_seconds_fired = True
            event = ("last_seconds", None)
            self._put_event(event)
            events.append(event)

        # ⑥ 2 Hz 上下文（20 Hz 主循环 / 10 拍）；有事件时立刻发一份，
        # 让 worker 处理事件时看到的是本拍状态。
        if self.tick % 10 == 0 or events or record["batches"]:
            publish = getattr(self.dialog_worker, "publish_context", None)
            if callable(publish):
                try:
                    publish(self._build_dialog_context(world))
                except Exception:               # noqa: BLE001
                    # 上下文是增强信息；构造失败不该打断主循环，但必须留痕。
                    log.exception("构造对话上下文失败")

        if events:
            record["event"] = events[-1] if len(events) == 1 else events
        record["duties"] = self.ledger.snapshot(tick)
        return record

    def _put_ack(self, ack):
        # type: (Any) -> None
        if self.ack_q is None:
            return
        try:
            self.ack_q.put_nowait(ack)
        except queue.Full:
            pass

    def _put_event(self, event):
        # type: (Any) -> None
        put = getattr(self.dialog_worker, "put_event", None)
        if callable(put):
            put(event)
            return
        if self.event_q is None:
            return
        try:
            self.event_q.put_nowait(event)
        except queue.Full:
            try:
                self.event_q.get_nowait()
                self.event_q.put_nowait(event)
            except queue.Empty:
                pass

    def _build_dialog_context(self, world):
        # type: (World) -> Dict
        """给 LLM 的只读快照：事实，不是建议（时间一律秒）。"""
        tick = self.bb.tick or 0
        book = self.book or RecipeBook(world.recipes, world.stations)
        kitchen = self._kitchen(world)
        active = (self.bb.commitment.active_task
                  if self.bb.commitment else None)
        p1_chef = P.chef_by_name(world.chefs, P.P1_NAME)

        live_orders = planner._dialog_live_orders(world, self.bb)
        plate_match, cooker_match = {}, {}
        try:
            live_orders, plate_match, cooker_match = self._dialog_matches(
                world, book)
        except Exception:                       # noqa: BLE001
            log.exception("对话上下文：盘/灶匹配失败")
        rows = {}
        try:
            rows = {row.order_id: row for row in ETA.order_queue(
                world, book, plate_match, cooker_match, orders=live_orders)}
        except Exception:                       # noqa: BLE001
            log.exception("对话上下文：ETA 计算失败")
        orders = [{
            "id": order.get("id"),
            "dish": order.get("dish"),
            "remaining": order.get("remaining"),
            "eta": _seconds(getattr(rows.get(order.get("id")), "eta", None)),
        } for order in live_orders]
        # P2 接下来可做的：planner 最近一拍的打分前几名（节点 + 说明）。
        options = [_option_text(task) for task in self._last_ranked[:6]]

        tools = _duty_tools(kitchen, self.ledger)
        return {
            "level": getattr(world, "level", None),
            "live": bool(getattr(world, "phase", None) == "level"
                         and not getattr(world, "paused", False)),
            "round_finished": _timer_expired(world.raw.get("timer")),
            "tick": tick,
            "time_left": _seconds(planner._time_left_ticks(world)),
            "score": _score_field(world, "base"),
            "total": _score_total(world),
            "orders": orders,
            "cookers": [_cooker_text(i, c) for i, c
                        in enumerate(world.cookers or (), 1)],
            "plates": _plates_text(world, book),
            "dirty_plates": _dirty_plate_count(world),
            "boards": _board_texts(world),
            "idle_cookers": _idle_cooker_count(world),
            "combo": _combo(world),
            "deliveries": _score_field(world, "deliveries"),
            "tips": _score_field(world, "tips"),
            "deductions": _score_field(world, "deductions"),
            "expiring_orders": _expiring_orders(live_orders),
            "p1": {"holding": P.held_name(p1_chef) or None},
            "p2": {
                "doing": _task_text(active) if active is not None else None,
                "stuck": _stuck_text(_stuck_info(self.bb, world)),
            },
            "p2_options": options,
            "duties": self._duty_rows(world, kitchen, tick),
            "dag": kitchen.render(),
            "dish_dags": {dish: kitchen.render([dish], shared=False)
                          for dish in kitchen.dags},
            "tools": tools,
        }

    def _duty_rows(self, world, kitchen, tick):
        # type: (World, Optional[Kitchen], int) -> List[Dict]
        """账本快照，供 LLM 看到当前分工。"""
        return self.ledger.snapshot(tick)

    def _tick_once(self, world):
        # type: (World) -> None
        """执行一拍的 13 步主循环。"""
        self._observe_scene(world)
        # 关卡/阶段切换要在生命周期守卫之前观察：暂停/菜单也是新世代，
        # 必须清掉上一局的对话状态与排队命令（spec §4.7 / §6.3）。
        if self.dialog_worker is not None:
            self._maybe_reset_dialog_level(world)
        # 生命周期守卫（spec §8.1）：暂停 / 非关卡 → 松键、不做任何决策。
        stale = (self._frame_received_at is not None and
                 time.monotonic() - self._frame_received_at > FRAME_MAX_AGE)
        missing_protocol = self.require_live_protocol and not (
            world.raw.get('grids') and (world.terrain.get(9) or world.raw.get('level_bounds')))
        missing_player = self.require_live_protocol and any(
            P.chef_by_name(world.chefs, name) is None for name in (P.P1_NAME, P.P2_NAME))
        round_finished = _timer_expired(world.raw.get('timer'))
        if stale or missing_protocol or missing_player or round_finished or world.paused or world.phase != "level":
            self.executor_inst.halt_and_discard()
            self.bb.commitment = Commitment()
            # 进入这个状态时记一条，之后不再逐拍记（一局里大部分时间可能
            # 都在暂停，逐拍记只会把日志刷成噪声）。留一条是为了区分
            # "trace 里什么都没有"到底是 agent 没起来还是 AI 没决策。
            if not self.idle_logged:
                self.idle_logged = True
                reason = ("stale_frame" if stale
                          else 'protocol_unavailable' if missing_protocol else 'missing_player' if missing_player
                          else 'round_finished' if round_finished
                          else "paused" if world.paused
                          else "phase=%s" % world.phase)
                trace.emit({"mode_reason": reason, "p1": {}, "candidates": []},
                           self.bb.mode, self.bb.commitment,
                           {"status": "skipped"}, [], self.tick)
            # 结束总结：round_finished 首次成立时发一次，并立刻发布上下文让
            # worker 读到最终战绩。其余等待原因（暂停/失焦等）按 2 Hz 发布
            # `live=False` 的上下文：worker 靠它停掉正在播的语音、清掉 check-in
            # 排期——否则上下文停在上一拍的 live=true，语音会继续念。
            publish_now = (round_finished and self._round_started
                           and not self._round_summary_fired)
            if publish_now:
                self._round_summary_fired = True
                self._put_event(("round_summary", None))
            if self.dialog_worker is not None and (publish_now or self.tick % 10 == 0):
                publish = getattr(self.dialog_worker, "publish_context", None)
                if callable(publish):
                    try:
                        publish(self._build_dialog_context(world))
                    except Exception:               # noqa: BLE001
                        log.exception("发布暂停/结束上下文失败")
            return
        self.idle_logged = False
        # 计时器在跑（left > 0）→ 本局确实开始过；用于区分「未开局」和
        # 「已结束」——两者插件都报 left=0，_timer_expired 都会返回 True。
        if planner._time_left_ticks(world) is not None:
            self._round_started = True

        # 黑板拍号与主循环对齐：planner 的可行性 ctx、认领过期、以及
        # abandoned 的 TTL 都读 bb.tick，必须在每拍推进（否则 TTL 恒为 0）。
        self.bb.tick = self.tick

        # 配方书每拍按当前帧重建。recipes.py 的注释写的是"一次构造、
        # **整帧**复用"，不是整局复用——只在第一帧构造一次会让早帧/残帧
        # 污染整局：若那帧 crate 的 chop 还没带上，needs_chopping 恒为
        # False，AI 会以为"鱼/黄瓜不用切"，拿生料直接去装盘（实机复现过）。
        self.book = RecipeBook(world.recipes, world.stations)

        # 对话服务：命令先落偏好/硬否决，本拍规划立刻可见（spec §6.3）。
        dialog_record = self._service_dialog(world)

        # ② skip_ticks 跳过
        if self.bb.scratch and getattr(self.bb.scratch, "skip_ticks", 0) > 0:
            self.bb.scratch.skip_ticks -= 1
            return

        # ③ 认领对账
        live_order_ids = {o.get("id") for o in world.orders if o.get("id")}
        view = self._make_resource_view(world)
        reconcile.reconcile_claims(view, self.bb, live_order_ids, self.tick)

        # ④ P1 推断（Plan 4 Task 2）
        p1model.update(world, self.bb)

        # ⑥ 模式仲裁
        new_mode, mode_reason = arbiter.select_mode(world, self.bb)

        # ⑦ 模式变化 → halt
        if new_mode != self.bb.mode:
            self.executor_inst.halt_and_discard()
            arbiter.on_mode_transition(self.bb.mode, new_mode, self.bb)
            self.bb.mode = new_mode

        # ⑨ 规划（多订单模式：不再选择单个订单）
        ctx = self._exec_context(world)
        planner_choice = None  # type: Optional[Any]
        if new_mode == "Normal":
            chosen_task, plan_record = planner.plan(world, self.bb, None, self.book)
            # planner 的**原始**选择要在 ⑨.5 覆盖之前抓一份给 trace：覆盖之后
            # 两者永远相等，那条可观测性就是假的。
            planner_choice = chosen_task
            # ⑨.5 正在跑的那棵树：如果它的后置条件**已经成立**，这一拍先让
            # 它把 SUCCESS 报完，别被 planner 抢走。
            #
            # 为什么需要：planner 每拍重选，而任务一旦自己做成了，就对自己
            # 不可行了（倒完米饭 plate_lacks 为假、下完锅 cooker_started 为
            # 真）→ 换任务 → executor 见身份变化就 halt 掉旧树 → ⑬ 的成功
            # 记账（释放认领）连带被吞掉。实机后果：倒完米饭那一拍被换成
            # ReturnPot，认领没释放 → 下一拍又去取熟料。
            #
            # 只在 Normal 分支里做：模式切换（火情等）已经 halt_and_discard
            # 过了，此时 active_task 是 None，不会把任务"复活"。
            active = self.executor_inst.active_task
            if active is not None and active != chosen_task \
                    and self._task_done(active, world, ctx):
                chosen_task = active
            claims_new = {}
            if chosen_task and hasattr(chosen_task, "claims"):
                # 认领绑定到任务的 order_id（而非全局 current_order_id）
                for res_id, action in chosen_task.claims():
                    # 记下 item：recipe_graph 的在途烹饪抑制按熟料区分作用域，
                    # 一单要两份熟料时第一份下锅不该压掉第二份（第 1 组 1.3）。
                    claims_new[(res_id, action)] = Claim(
                        "P2", chosen_task.order_id, self.tick,
                        item=getattr(chosen_task, "item", None))
        elif new_mode == "Firefighting":
            # Firefighting 模式：只生成 RescueCooked 候选，优先灭火。
            # 实机复现 trace-20261009-130459 拍 596-727: Firefighting 触发但
            # P2 完全 idle (candidates=0)，原因是旧分支直接跳过了 Planner 调用。
            chosen_task, plan_record = planner.plan_firefighting(world, self.bb, self.book)
            claims_new = {}
            if chosen_task and hasattr(chosen_task, "claims"):
                for res_id, action in chosen_task.claims():
                    # Firefighting task 不绑订单（灭火与订单无关）
                    claims_new[(res_id, action)] = Claim("P2", None, self.tick)
        else:
            # Arbiter 目前只产出 Normal / Firefighting，此分支为防御性兜底。
            chosen_task = None
            claims_new = {}
            plan_record = {"candidates": []}

        # 给 LLM 看的「接下来可做的」：本拍可行候选按分数排序。
        self._last_ranked = _ranked_tasks(plan_record)

        record = {
            "mode_reason": mode_reason,
            "p1": _p1_record(self.bb.p1_model),
            "candidates": plan_record.get("candidates", []),
            "dialog": dialog_record,
            "bindings": {
                "plate_order": plan_record.get("plate_match", {}),
                "cooker_order": plan_record.get("cooker_match", {}),
                "claims": _claims_binding(self.bb),
            },
            # 「这一拍选在哪层」是分层复盘的第一步（spec §6）。planner.plan()
            # 一直算好放在 decision_record 里，但这里原先没转抄，于是
            # trace.emit 读到的恒为 None —— 实机 trace-20261015-165358 的
            # 3637 行里 top_layer 全是 null，只能靠逐候选 layer 反推 min()。
            "top_layer": plan_record.get("top_layer"),
            # 对话分支与截止闸抢单（spec §6.4）：分支是认领复盘的第一步，
            # `rescue_override` 记录闸抢的是哪一单。火情路径没有这两项。
            "branches": plan_record.get("branches") or {},
            "rescue_override": plan_record.get("rescue_override"),
            # 「马上去」硬指令闸（Task 2）的覆盖标记：chosen 是闸选中的 task key。
            "imperative": plan_record.get("imperative"),
            "chosen": str(planner_choice) if planner_choice is not None else None,
        }

        # ⑩ 写回认领
        # 认领属于当前任务；被抢占的任务不能继续占住锅/板等待 TTL 到期。
        for key, claim in list(self.bb.claims.items()):
            if claim.owner == "P2" and key not in claims_new:
                self.bb.claims.pop(key, None)
        self.bb.claims.update(claims_new)

        # ⑪ 登记 commitment（存 Task 对象本身：承诺惯性按四元身份比较）
        if chosen_task:
            if not self.bb.commitment or self.bb.commitment.active_task != chosen_task:
                self.bb.commitment = Commitment(chosen_task, self.tick, 0, 0, 0)

        # ⑫ 执行
        if chosen_task:
            if self._frame_seq is not None:
                # 规划可能耗时；短按的时基必须接近真正发键的时刻。
                ctx["now"] = time.monotonic()
            status = self.executor_inst.tick(chosen_task, ctx)
        else:
            # 无任务可选时也要 halt 旧树松键：上一次 Approach 的移动键还按着，
            # 不松的话角色会一直朝一个方向顶墙（实机复现：RecoverLoosePot 因
            # 锅被 P1 端走变不可行后，角色卡在原地一直按 right）。
            self.executor_inst.halt_and_discard()
            status = "idle"
            record['waiting'] = waiting_reason(plan_record.get('candidates', []), world.orders)
            record['mode_reason'] = record['waiting']['reason']

        # ⑬ 处理结果
        success_task = None  # type: Optional[Any]
        if status == "success":
            success_task = chosen_task  # 保存成功的 task（trace 记录用）
            self._note_success(chosen_task)
            # 「马上去」落实**成功**才降级成 do：失败不降，now 的紧迫性保留到
            # 下次重试。执行期间由 planner 层去抖兜住（当前承诺命中 now 时
            # 闸不再抢选），这里只做终态消费。
            if chosen_task and self.ledger.consume_now(chosen_task):
                self.bb.preferences[:] = self.ledger.preferences()
            if chosen_task and hasattr(chosen_task, "claims"):
                for res_id, action in chosen_task.claims():
                    self.bb.claims.pop((res_id, action), None)
            self.bb.commitment = None
        elif status == "failure":
            if chosen_task is not None:
                for resource, action in chosen_task.claims():
                    self.bb.claims.pop((resource, action), None)
                self._note_motion_failure(chosen_task, ctx)
                self._note_failure(chosen_task)
            if self.bb.commitment:
                self.bb.commitment.interrupt_failure_count += 1
            self.bb.commitment = None

        # ⑭ 日志
        # 委托后端不再有「按住的键」，keys 恒为空；命令/结果由插件 step_done
        # 与 exec_state 记录，按键级观测随键盘注入轨一并移除。
        keys = []
        # 卡住事件：arbiter 同格卡住优先，回退本拍 motion 空转（spec §G.1）。
        # 在 ⑫ 执行之后算，motion 的 stalled/stall_seconds 这时才被原语填进 ctx。
        record["stuck"] = _stuck_info(self.bb, world, ctx.get("motion"))
        # commitment 在终态已清空，仍记录本拍 task 和移动/观测阶段。
        exec_state = trace.execution_record(
            status, success_task if status == "success" else chosen_task, ctx,
            self._frame_seq, self._frame_received_at)
        trace.emit(record, new_mode, self.bb.commitment, exec_state, keys,
                   self.tick, state_dump.snapshot(world))

    def _task_done(self, task, world, ctx):
        # type: (Any, World, Dict) -> bool
        """该 task 的后置条件是否已经成立（done() 是纯读谓词）。

        用来判断"这棵树还差最后一口气"：后置条件已成立说明它这一拍就能报
        SUCCESS，不该被 planner 抢走（见 ⑨.5 的说明）。必须**明确返回 True**
        才算数（不做真值判断：这既挡住了 Mock，也要求 done() 的返回是布尔
        声明而不是随手一个真值）；抛异常一律当成"没成立"。
        """
        done = getattr(task, "done", None)
        if not callable(done):
            return False
        try:
            return done(ctx) is True
        except Exception:                       # noqa: BLE001
            return False

    def _note_failure(self, task):
        # type: (Any) -> None
        if self._failures.failed(task, self.tick):
            abandon_actions(self.bb, task, self.tick + ABANDON_TTL_TICKS)

    def _note_success(self, task):
        # type: (Any) -> None
        if task is not None:
            self._failures.succeeded(task)

    def _observe_scene(self, world):
        # type: (World) -> None
        if world.phase != 'level':
            self._left_level = True
            return
        raw = getattr(world, 'raw', {})
        key = (raw.get('session_id'), raw.get('scene_epoch'), world.level)
        if self._scene_key is not None and (key != self._scene_key or self._left_level):
            self.executor_inst.halt_and_discard()
            self.bb = Blackboard()
            # 黑板整体换新：账本是 bb.preferences 的唯一来源，必须一起清，
            # 否则下一次同步会把上一局的约定编回新黑板。
            self.ledger.clear()
            self._failures = FailureHistory(ABANDON_FAIL_STREAK, ABANDON_TTL_TICKS)
            self.book = None
            self._prev_balloon_box = None
            self._balloon_moving = False
            self._balloon_frame_seq = None
            trace.reset_revenue_state()
        self._scene_key, self._left_level = key, False

    def _note_motion_failure(self, task, ctx):
        # type: (Any, Dict) -> None
        note_motion_failure(self.bb, task, self.tick,
                            ctx.get("motion_failure") or {},
                            world=ctx.get("w"), chef=ctx.get("chef"))

    def _update_balloon_motion(self, world):
        # type: (World) -> bool
        """跨帧检测气球篮是否在移动；同一帧内 4 拍复用同一结论。

        只在收到新帧时比较一次篮子地板 AABB 的 z 向位置，避免 20Hz 循环
        把同一个 World 反复比较（那样只会得到"没动"）。
        """
        box = world.balloon_ground_box()
        if box is None:
            self._prev_balloon_box = None
            self._balloon_moving = False
            return False
        if self._balloon_frame_seq == self._frame_seq:
            return self._balloon_moving
        self._balloon_frame_seq = self._frame_seq
        key = (round(box[2], 3), round(box[3], 3))
        prev = self._prev_balloon_box
        self._prev_balloon_box = key
        if prev is None:
            return False
        self._balloon_moving = (abs(key[0] - prev[0]) > 0.05
                                or abs(key[1] - prev[1]) > 0.05)
        return self._balloon_moving

    def _exec_context(self, world):
        # type: (World) -> dict
        """构造执行层上下文（primitives/base.py 的 ctx 契约）。

        keys: w / chef / bb / field / book / tick。
        field 是每拍从 P2 位置做一次的 flood-fill 距离场（spec §6.4），
        并叠加 P1 周边软代价（Plan 4 Task 2）。
        """
        chef = P.chef_by_name(world.chefs, P.P2_NAME)
        field = {}
        soft = {}
        blocked = set()
        # 篮子移动检测每拍更新一次黑板，供 planner 可行性过滤（方案 B）：
        # 篮子动时「目标在篮子上的 task」判不可行，而不是选出来再执行失败。
        self.bb.balloon_moving = self._update_balloon_motion(world)
        if chef is not None:
            cell = P.chef_cell(chef, world.to_grid)
            if cell is not None:
                scratch = self.bb.scratch
                bad = getattr(scratch, "bad_cells", None) or {}
                blocked = {c for c, expire in bad.items() if expire > self.tick}
                # 气球篮移动时避开篮子的格子（护栏，已按用户要求注释）：
                # 角色正站在篮子上则放行，否则跨网格踩空会掉下去
                # （trace-20261017-215633 t=1121 respawn 5s）。
                # if self.bb.balloon_moving:
                #     balloon_cells = world.balloon_cells()
                #     if cell not in balloon_cells:
                #         blocked |= balloon_cells
                soft = p1model.p1_soft_cost_map(world, self.bb.p1_model)
                field = reachability.field_for(world, self.bb, soft_cost=soft)
                if field is None:  # 缺地形的历史测试仍使用旧距离场。
                    field = navigation.distance_field(
                        cell, world.walkable, world.belt_dir, blocked, soft_cost=soft,
                        edge_blocked=world.edge_blocked)
        ctx = {
            "w": world,
            "chef": chef,
            "bb": self.bb,
            "field": field, "soft_cost": soft, "navigation_blocked": blocked,
            "book": self.book,
            "tick": self.tick,
        }
        if self._frame_seq is not None:
            ctx.update(frame_seq=self._frame_seq,
                       frame_received_at=self._frame_received_at,
                       now=time.monotonic())
        if self.exec_client is not None:
            ctx["exec"] = self.exec_client
        return ctx

    def _make_resource_view(self, world):
        # type: (World) -> reconcile.ResourceView
        """构造对账所需的资源视图。

        资源 id 必须与 task.claims() 的键一致，否则每个认领都会被对账
        误判为"资源消失"而释放：站点/锅/盘用实例名（scalar），拼装格用
        网格坐标 (gx,gz)（spec §4.4"坐标即身份"）。
        """
        from ai.state.reconcile import ResourceView
        existing_ids = set()
        contents = {}
        for st in world.stations:
            sid = st.get("n")
            if not sid:
                continue
            rid = P.resource_id(st)
            existing_ids.add(sid)      # 旧：裸名字（过渡期保留，§5.2）
            existing_ids.add(rid)      # 新：(grid, name) 元组
            if (st.get("t") == "counter"
                    and st.get("gx") is not None and st.get("gz") is not None):
                existing_ids.add((st.get("gx"), st.get("gz")))
            item = st.get("item")
            if item:
                contents[sid] = item
                contents[rid] = item
        for cooker in world.cookers:
            cid = cooker.get("n")
            if cid:
                rid = P.resource_id(cooker)
                existing_ids.add(cid)
                existing_ids.add(rid)
                item = cooker.get("item")
                if item:
                    contents[cid] = item
                    contents[rid] = item
        for plate in P.all_plates(world):
            pid = plate.get("n")
            if pid:
                existing_ids.add(pid)
        return ResourceView(existing_ids=existing_ids, contents=contents)

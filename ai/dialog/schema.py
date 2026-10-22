"""对话层 schema：账本偏好、作用域匹配与对话状态（spec §4.3–4.5）。

本模块**只允许导入标准库**：`ai/state/blackboard.py` 要引用这里的
`Preference` / `DialogState`，若这里反向导入 `ai.*` 会形成
`state ↔ dialog` 的循环导入。因此 task / world 相关的东西一律按鸭子
类型读取（`task.TYPE`、`task.item`），不 import `ai.tasks`。
"""
from dataclasses import dataclass, field
from typing import Any, FrozenSet, List, Optional, Tuple


# 救援类不参与认领/派活（spec §4.5 末段）：玩家说「我来煮饭」不等于
# 「饭要糊了也别管」。
RESCUE_TYPES = {"RescueCooked", "ReturnPot", "RecoverLoosePot"}

# 共享/整单/清理动作没有 branch（spec §4.5）。回收散锅/接力台只用于
# trace，不参与分支级认领，因此也归一为 None。
NO_BRANCH_TYPES = {
    "WashPlate", "PlaceCleanPlate", "DeliverPlate",
    "Discard", "ResetPlate", "StashHeld", "Extinguish",
    "PlaceToRelay", "RecoverLoosePot",
}


@dataclass
class Preference(object):
    """一条分工偏好：候选打分上的加法项，由账本编译（ai/dialog/duties.py）。

    - `kind`：direct（P2 优先做，正分）/ now（马上去，正分，权重 WEIGHT_NOW）
      / claim（P1 优先做，负分；P2 仍可按订单价值补位）
      / stop（硬压制，权重 0，`planner._is_stopped` 读）；
    - `scope`：`{(task 类型或 None, item 小写或 None)}`，见 `scope_matches`；
    - `exclude`：从 scope 里扣掉的部分（被更新的账本条目接管）；
    - `expires_tick`：0 = 长期；非零时 `tick >= expires_tick` 即失效。
    空 scope 不匹配任何候选（不会退化成「影响全部」）。
    """
    kind: str
    scope: FrozenSet[Tuple[str, Optional[str]]]
    exclude: FrozenSet[Tuple[str, Optional[str]]] = frozenset()
    owner: str = "P1"
    weight: float = 0.0
    expires_tick: int = 0
    # 编译出该偏好的账本条目 id（trace / rescue 事件里报给 LLM）。
    duty_id: Optional[str] = None


@dataclass
class DialogState(object):
    """对话层的跨回合状态；由主循环持有（spec §6.2）。"""
    last_speech_tick: int = 0
    last_order_tick: int = 0
    last_rescue_tick: int = 0
    # 新单合并窗口（spec §5.1）：1 秒内到达的订单攒成一批，由
    # `ai.dialog.policy.merge_new_orders` 维护；清批前一个都不丢。
    pending_orders: List = field(default_factory=list)
    # 上一条已触发的 speech（规范化后），供 4 秒内同句去重用。
    last_transcript: str = ""
    # 本关对话历史：OpenAI 多轮消息 dict（user / assistant / tool / 事件占位
    # system）。世界状态快照不进历史，每轮单独作为 system 消息发送。
    history: List[dict] = field(default_factory=list)
    pending_event: Optional[Any] = None
    # 截止闸抢回的 task（spec §4.6）：闸只在最后窗口开一瞬，抢回来之后必须
    # 让这个 task 活到做完，否则下一拍就会被同一条 claim 清掉、「抢回」只
    # 维持一拍。`rescue_hold_until` 是保护期失效拍号（拍）。
    rescue_task_key: Any = None
    rescue_hold_until: int = 0


@dataclass
class VoiceConfig(object):
    """语音链路的配置；密钥由 run_agent 从环境变量读入（spec §6.5）。"""
    asr_key: str
    asr_resource_id: str
    tts_key: str
    tts_voice: str
    tts_resource_id: str
    ark_key: str
    ark_base_url: str
    ark_model: str
    rate: float = 1.05
    volume: float = 0.7
    # VAD 开口阈值（底噪之上的 dB）。越小越敏感；默认 9 用于抑制游戏声。
    vad_threshold_db: float = 9.0
    # LLM 请求超时。方舟 plan 端点实测 2–3.2s，3s 太紧，默认放宽到 10s。
    llm_timeout: float = 10.0
    # PTT 键名（如 "f8"）；设置后按住录音、松开发送，不再用 VAD 自动切句。
    ptt_key: Optional[str] = None
    log_path: Optional[str] = None
    debug: bool = False


def preference_matches_task(pref, task):
    # type: (Preference, Any) -> bool
    """偏好是否命中候选：在 scope 里且不在 exclude 里。"""
    return (scope_matches(pref.scope, task)
            and not (pref.exclude and scope_matches(pref.exclude, task)))


def scope_matches(scope, task):
    # type: (Any, Any) -> bool
    """作用域是否覆盖该候选。

    作用域元素是 `(task 类型或 None, item 小写或 None)`：
    - `(None, item)`：物料——任意 task 类型、这个 item（账本按物料认领）；
    - `(类型, None)`：共享动作——这个类型、任意 item；
    - `(类型, item)`：精确到一个节点（测试/诊断用）。
    """
    task_type = getattr(task, "TYPE", None)
    if (task_type, None) in scope:
        return True
    item = getattr(task, "item", None)
    if item is None:
        return False
    item = str(item).lower()
    return (None, item) in scope or (task_type, item) in scope

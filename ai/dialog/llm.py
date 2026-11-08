"""火山方舟 OpenAI 兼容接口的 LLM 客户端与消息构建（function calling）。

主路径由 `worker._run_llm` 驱动多轮工具调用：`execute_actions` /
`cancel_action` / `speak` / `get_world_state` / `set_division` /
`read_cookbook`。工具结果由主循环权威校验后回填，模型据此继续下一轮或收尾。
`_run_llm_legacy` 保留给旧 trace/回放的两段式调用。

消息分工：
- 开头 system：人设、本关配方 DAG、攻略摘要（每关只变一次）；
- 历史：user（玩家原话）/ assistant（含 tool_calls）/ tool（落地结果）；
- 末尾 system：本轮世界状态 + 事件说明（不进历史）；
- user 只放玩家的原话，事件回合没有 user 消息。

`requests.post` 是模块级引用，测试直接 monkeypatch 它，不会发真实请求。
"""
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from ai.dialog import prompts


class LLMError(RuntimeError):
    """LLM 请求或流式响应失败；调用方据此静默。"""


@dataclass
class ToolCall(object):
    id: str
    name: str
    arguments: str

    def args(self):
        # type: () -> Any
        """解析参数 JSON；坏 JSON 返回 None（主循环按拒绝处理）。"""
        try:
            return json.loads(self.arguments or "{}")
        except ValueError:
            return None


@dataclass
class LLMReply(object):
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    # 从发请求到第一个 content/tool_calls delta 的耗时（毫秒）；用于分段打点。
    first_token_ms: Optional[float] = None


class LLMClient(object):
    """同步流式调用方舟 chat/completions；失败直接抛 LLMError，不重试。"""

    def __init__(self, api_key, base_url, model, timeout=10.0):
        # type: (str, str, str, float) -> None
        self.api_key = api_key
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.timeout = float(timeout)

    def warmup(self):
        # type: () -> None
        """后台发一个极小请求，吸收服务端冷启动（实测首 token 可到 7–8s）。

        不影响主链路；失败静默（真正请求时再报错）。
        """
        url = self.base_url + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "max_tokens": 1,
            "thinking": {"type": "disabled"},
        }
        headers = {
            "Authorization": "Bearer " + str(self.api_key),
            "Content-Type": "application/json",
        }
        timeout = max(5.0, float(self.timeout))

        def run():
            try:
                requests.post(url, headers=headers, json=payload,
                              timeout=timeout)
            except Exception:                   # noqa: BLE001
                pass

        threading.Thread(target=run, name="dialog-llm-warmup",
                         daemon=True).start()

    def chat(self, messages, tools=None, tool_choice="auto", on_text=None):
        # type: (List[dict], Optional[List[dict]], str, Any) -> LLMReply
        """一次流式请求。`tool_choice="none"` 用于两段式的第二段：工具结果已
        回填，只要最终那句话（tools 仍要带上，否则历史里的 tool_calls 无定义）。

        `on_text` 是可选回调：每收到一个 content delta 就调用一次，用于
        边生成边把文本交给 TTS；工具调用的 arguments 不经过它。"""
        url = self.base_url + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": 0.4,
            "max_tokens": 400,
            # 带「深度思考」的模型默认会先思考；实时对话必须关掉。
            "thinking": {"type": "disabled"},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        headers = {
            "Authorization": "Bearer " + str(self.api_key),
            "Content-Type": "application/json",
        }
        # 从发请求开始计时（含建连/首包等待），而不是从拿到响应对象开始。
        started = time.monotonic()
        try:
            resp = requests.post(url, headers=headers, json=payload,
                                 stream=True, timeout=self.timeout)
        except requests.RequestException as exc:
            raise LLMError("LLM 请求失败：%s" % (exc,)) from exc
        try:
            if resp.status_code != 200:
                body = getattr(resp, "text", "") or ""
                raise LLMError("LLM 请求失败 HTTP %s：%s"
                               % (resp.status_code, str(body)[:200]))
            try:
                return _read_sse(resp, started, on_text,
                                 timeout=self.timeout)
            except LLMError:
                raise
            except requests.RequestException as exc:
                raise LLMError("LLM 流读取失败：%s" % (exc,)) from exc
        finally:
            close = getattr(resp, "close", None)
            if close is not None:
                close()


def _read_sse(resp, started=None, on_text=None, timeout=None):
    # type: (Any, Optional[float], Any, Optional[float]) -> LLMReply
    """按 SSE 协议拼 content 与按 index 累积的 tool_calls，直到 [DONE]。"""
    if started is None:
        started = time.monotonic()
    deadline = None if timeout is None else started + float(timeout)
    first_token_ms = None
    chunks = []  # type: List[str]
    calls = {}   # type: Dict[int, Dict[str, str]]
    # 必须自己按 UTF-8 解码：部分兼容端点的 SSE 响应头没有 charset，
    # requests 会退回 Latin-1，中文会被 splitlines 在字符中间断开。
    for raw in resp.iter_lines(decode_unicode=False):
        if deadline is not None and time.monotonic() > deadline:
            raise LLMError("LLM 流超时（%.1fs）" % float(timeout))
        if not raw:
            continue
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break
        try:
            payload = json.loads(data)
        except ValueError as exc:
            raise LLMError("SSE 数据不是合法 JSON：%r" % (data[:200],)) from exc
        if not isinstance(payload, dict):
            continue
        error = payload.get("error")
        if error:
            raise LLMError("LLM 流返回错误：%s"
                           % json.dumps(error, ensure_ascii=False, default=str))
        choices = payload.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        has_token = bool(delta.get("content") or delta.get("tool_calls"))
        if has_token and first_token_ms is None:
            first_token_ms = (time.monotonic() - started) * 1000.0
        if delta.get("content"):
            chunks.append(delta["content"])
            if on_text is not None:
                on_text(delta["content"])
        for tc in delta.get("tool_calls") or ():
            slot = calls.setdefault(int(tc.get("index", 0) or 0),
                                    {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] += fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]
    tool_calls = []
    for index in sorted(calls):
        slot = calls[index]
        tool_calls.append(ToolCall(id=slot["id"] or "call_%d" % (index,),
                                   name=slot["name"],
                                   arguments=slot["arguments"]))
    return LLMReply(content="".join(chunks).strip(), tool_calls=tool_calls,
                    first_token_ms=first_token_ms)


# ---------------------------------------------------------------- prompt

def system_message(kitchen_dag, guide="", division=False, cookbook=False):
    # type: (str, str, bool, bool) -> dict
    """静态规则 + 本关 DAG + 攻略摘要（每关只变一次，方便上下文缓存）。"""
    return {"role": "system",
            "content": prompts.render_system(
                kitchen_dag, guide, division=division, cookbook=cookbook)}


def render_state(context):
    # type: (dict) -> str
    """主循环快照 → 给模型的世界状态文本。"""
    return prompts.render_state(context)


def state_message(event, context, new_dishes_dag="", event_detail="",
                  failed=False):
    # type: (str, dict, str, str, bool) -> dict
    """本轮末尾的 system 消息：世界状态 + 事件说明（不进历史）。"""
    parts = [prompts.render_state(context)]
    if event == "new_order" and new_dishes_dag:
        parts.append("【新菜】\n" + new_dishes_dag)
    if event in ("speech", "command_result", "last_seconds", "round_summary",
                 "checkin", "batch", "division", "division_review"):
        event_text = prompts.render_event(event, event_detail, failed=failed)
        if event_text.strip():
            parts.append(event_text)
    elif event_detail:
        parts.append(event_detail)
    return {"role": "system", "content": "\n\n".join(parts)}


def speech_message(transcript):
    # type: (str) -> dict
    """P1 的原话；换行压成空格。历史里存的也是这一条。"""
    spoken = str(transcript or "").replace("\r", " ").replace("\n", " ")
    return {"role": "user", "content": spoken}


def event_note(event):
    # type: (str) -> dict
    """事件回合写进历史的占位（让下一轮知道上一句是主动说的）。"""
    return {"role": "system", "content": "（%s 事件，你主动开口）" % (event,)}


def assistant_message(reply):
    # type: (LLMReply) -> dict
    msg = {"role": "assistant", "content": reply.content or ""}
    if reply.tool_calls:
        msg["tool_calls"] = [{
            "id": call.id, "type": "function",
            "function": {"name": call.name, "arguments": call.arguments},
        } for call in reply.tool_calls]
    return msg


def tool_message(call_id, result):
    # type: (str, Any) -> dict
    return {"role": "tool", "tool_call_id": call_id,
            "content": json.dumps(result, ensure_ascii=False, default=str)}

"""火山方舟 OpenAI 兼容接口的 LLM 客户端与消息构建（v2：function calling）。

一轮说话按标准两段式：
1. 第一段带 tools：模型决定是否调用 `update_duties`；
2. 有调用时，主循环权威校验并落地，结果以 tool 消息回填；
3. 第二段（`tool_choice="none"`）模型看到真实结果后再说最终那句话——
   被拒的调用会如实告诉 P1，而不是先答应再落空。
没有调用时第一段的 content 就是回复（闲聊/提问只请求一次）。

消息分工：
- 开头 system：人设、账本规则、本关配方 DAG（每关只变一次）；
- 历史：user（P1 原话）/ assistant（含 tool_calls）/ tool（落地结果）；
- 末尾 system：本轮世界状态 + 事件说明（不进历史）；
- user 只放 P1 的原话，事件回合没有 user 消息。

`requests.post` 是模块级引用，测试直接 monkeypatch 它，不会发真实请求。
"""
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from ai.dialog.duties import render_ledger


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
                return _read_sse(resp, started, on_text)
            except LLMError:
                raise
            except requests.RequestException as exc:
                raise LLMError("LLM 流读取失败：%s" % (exc,)) from exc
        finally:
            close = getattr(resp, "close", None)
            if close is not None:
                close()


def _read_sse(resp, started=None, on_text=None):
    # type: (Any, Optional[float], Any) -> LLMReply
    """按 SSE 协议拼 content 与按 index 累积的 tool_calls，直到 [DONE]。"""
    if started is None:
        started = time.monotonic()
    first_token_ms = None
    chunks = []  # type: List[str]
    calls = {}   # type: Dict[int, Dict[str, str]]
    # 必须自己按 UTF-8 解码：部分兼容端点的 SSE 响应头没有 charset，
    # requests 会退回 Latin-1，中文会被 splitlines 在字符中间断开。
    for raw in resp.iter_lines(decode_unicode=False):
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

_SYSTEM = """\
你是 Overcooked 2 里的 P2（AI 玩家），和真人 P1 一起做菜。你们通过语音交流：\
P1 说的话经语音识别转成文字给你（可能有错别字，按发音理解），你的回复会被念出来。

## 说话
- 只用中文口语，一两句、30 字以内；菜名和食材说中文，不要念英文名。
- 可以闲聊、开玩笑、回答问题；被吐槽时先认个错再说正事。
- 状态问题（「你在干嘛」「饭好了没」）只按最新的【世界状态】如实回答，不知道就说不知道，不要编。
- 你下一步具体做什么由系统决定；分工约定只是让系统偏向/避开某些活。不要承诺「马上」「这就去」。
- 调用过工具后，按工具返回的结果说话：成功就确认，被拒或带提醒就如实说明。
- 分工谈好了就用大白话确认（「那我来煮饭，你来切黄瓜」），不要念内部名词。

## 分工约定（update_duties）
P1 话里的「我」指 P1，「你」指你（P2）：「X 我来负责」= P1 做 = leave_to_p1，「X 你来」= do。
这些约定每条只描述你（P2）自己：
- do：你优先做（「你去X」「X 你来」）
- leave_to_p1：P1 在做，你避开（「我来X」「X 我来」「X 交给我」）
target 是一种**物料**（代表这种料的整条链：取料、切、下锅、盛盘、装盘都算）或一个**共享动作**。\
「煮饭」「切黄瓜」「鱼你包了」都是指那种物料整条链；一种物料只归一个人，不要拆开。
「我负责煮的」「切菜我来」这类按加工方式说的，就对目录里标了「要煮」「要切」的每种物料各调用一次。
调用工具时 target 用英文枚举值，只有开口说话时才用中文。
规则：
1. 玩家一句话里涉及几种物料/动作就调用几次，每处都要落。
2. 改口、对调时直接对同一 target 再 add（等于改写），不需要先删；不再需要的约定用 remove。
3. add 不要带 id（id 由系统分配）。
4. 闲聊、提问、抱怨不调用工具。
5. 分工要均衡：别把活全揽给自己，也别全推给 P1；P1 已经认领的（分工里 leave_to_p1 的）或话里说「我来」的，你不要抢。
6. P1 把不少活交给你时，你可以回话提议让 P1 分担一部分；P1 不帮、拒绝或不回应就别反复劝，自己接着做。

## 做不到的请求
- 目录里没有的物料或动作（这关没有的食材、「本关没有对应站点」「不开放认领」里列的），不调用工具，\
直接说做不了、为什么，可以顺带提一下本关有什么（「这关没有番茄，只有黄瓜和鱼」）。
- 跟游戏无关的请求（捶背、点外卖），轻松接一句，不调用工具。
"""

_EVENT_TEXT = {
    "new_order": ("事件：新菜出现了。先看这道新菜里哪些物料还没有归属，只对这些新物料调用 "
                  "update_duties（哪些你做、哪些请 P1 做）；已经约定好的物料（世界状态「分工」栏里已有的）"
                  "不要重复、不要改。再用一两句话告诉 P1 新分工，P1 之后可以改口。"),
}


def system_message(kitchen_dag):
    # type: (str) -> dict
    """静态规则 + 本关 DAG（每关只变一次，方便上下文缓存）。"""
    text = _SYSTEM
    if kitchen_dag:
        text += "\n## 本关目录\n" + kitchen_dag + "\n"
    return {"role": "system", "content": text}


def render_state(context):
    # type: (dict) -> str
    """主循环快照 → 给 LLM 的世界状态文本（中文分段，时间单位秒）。"""
    ctx = context or {}
    lines = ["【世界状态】"]
    if ctx.get("time_left") is not None:
        lines.append("本关剩余 %s 秒；得分 %s。" % (ctx.get("time_left"),
                                               ctx.get("score", "?")))
    orders = ctx.get("orders") or []
    if orders:
        lines.append("订单：")
        for o in orders:
            eta = o.get("eta")
            lines.append("- #%s %s 剩 %s 秒%s" % (
                o.get("id"), o.get("dish"), o.get("remaining"),
                "，预计 %s 秒能出" % (eta,) if eta is not None else ""))
    else:
        lines.append("订单：无")
    for cooker in ctx.get("cookers") or ():
        lines.append("%s：%s" % (cooker.get("name"), cooker.get("text")))
    plates = ctx.get("plates")
    if plates:
        lines.append("盘子：%s" % (plates,))
    p1 = ctx.get("p1") or {}
    lines.append("P1：手持 %s" % (p1.get("holding") or "空",))
    p2 = ctx.get("p2") or {}
    doing = p2.get("doing") or "没事做"
    lines.append("你（P2）：正在 %s" % (doing,))
    if p2.get("stuck"):
        lines.append("你卡住的原因：%s" % (p2.get("stuck"),))
    nxt = ctx.get("p2_options") or []
    if nxt:
        lines.append("你接下来可做的（按规划器打分）：" + "；".join(nxt))
    lines.append("分工：")
    lines.append(render_ledger(ctx.get("duties") or []))
    return "\n".join(lines)


def state_message(event, context, new_dishes_dag=""):
    # type: (str, dict, str) -> dict
    """本轮末尾的 system 消息：世界状态 + 事件说明（不进历史）。"""
    parts = [render_state(context)]
    if event == "new_order" and new_dishes_dag:
        parts.append("【新菜】\n" + new_dishes_dag)
    if event in _EVENT_TEXT:
        parts.append(_EVENT_TEXT[event])
    return {"role": "system", "content": "\n\n".join(parts)}


def speech_message(transcript):
    # type: (str) -> dict
    """P1 的原话；换行压成空格。历史里存的也是这一条。"""
    spoken = str(transcript or "").replace("\r", " ").replace("\n", " ")
    return {"role": "user", "content": spoken}


def event_note(event):
    # type: (str) -> dict
    """事件回合写进历史的占位（让下一轮知道上一句是主动说的）。"""
    return {"role": "system", "content": "（%s 事件，P2 主动开口）" % (event,)}


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

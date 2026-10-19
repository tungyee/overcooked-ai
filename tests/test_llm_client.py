"""LLM 客户端（流式 content + tool_calls）与消息构建。"""
import json

import pytest

from ai.dialog import llm
from ai.dialog.llm import (LLMClient, assistant_message, event_note,
                           render_state, speech_message, state_message,
                           system_message, tool_message)


class FakeSSE(object):
    """最小的流式响应替身；只实现客户端用到的 iter_lines。"""

    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code
        self.closed = False

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line

    def close(self):
        self.closed = True


def _chunk(content):
    payload = {"choices": [{"delta": {"content": content}}]}
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """任何测试都不许真的发请求；需要响应的用例在用例内再覆盖。"""

    def guard(*args, **kwargs):
        raise AssertionError("测试不应发起真实网络请求")

    monkeypatch.setattr(llm.requests, "post", guard)


def _tool_chunk(index, name=None, arguments=None, call_id=None):
    fn = {}
    if name:
        fn["name"] = name
    if arguments:
        fn["arguments"] = arguments
    tc = {"index": index, "function": fn}
    if call_id:
        tc["id"] = call_id
    payload = {"choices": [{"delta": {"tool_calls": [tc]}}]}
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def test_llm_chat_returns_content_and_sends_tools(monkeypatch):
    resp = FakeSSE([
        _chunk("行，"),
        _chunk("你切。"),
        "data: [DONE]\n\n",
        _chunk("不该出现"),
    ])
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return resp

    monkeypatch.setattr(llm.requests, "post", fake_post)
    client = LLMClient("key", "https://ark.cn-beijing.volces.com/api/v3",
                       "doubao-seed-2-1-pro-260915")
    tools = [{"type": "function", "function": {"name": "update_duties"}}]
    messages = [{"role": "user", "content": "我来切"}]
    reply = client.chat(messages, tools)

    assert reply.content == "行，你切。"
    assert reply.tool_calls == []
    assert resp.closed is True

    url, kwargs = calls[0]
    assert url == "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer key"
    assert kwargs["stream"] is True
    assert kwargs["timeout"] == 10.0
    body = kwargs["json"]
    assert body["model"] == "doubao-seed-2-1-pro-260915"
    assert body["messages"] == messages
    assert body["tools"] == tools
    assert body["tool_choice"] == "auto"
    # 带「深度思考」的模型默认会思考；语音对话必须显式关闭。
    assert body["thinking"] == {"type": "disabled"}


def test_llm_records_first_token_latency(monkeypatch):
    resp = FakeSSE([
        "data: " + json.dumps(
            {"choices": [{"delta": {"role": "assistant"}}]}) + "\n\n",
        _chunk("好"),
        _chunk("的"),
        "data: [DONE]\n\n",
    ])
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: resp)

    reply = LLMClient("key", "https://x.example/v3", "m").chat([])

    # role-only delta 不算首 token；第一次 content 才记时。
    assert reply.first_token_ms is not None
    assert reply.first_token_ms >= 0


def test_llm_on_text_receives_content_deltas(monkeypatch):
    resp = FakeSSE([_chunk("你好"), _chunk("世界"), "data: [DONE]\n\n"])
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: resp)
    seen = []

    reply = LLMClient("key", "https://x.example/v3", "m").chat(
        [], on_text=seen.append)

    assert reply.content == "你好世界"
    assert seen == ["你好", "世界"]


def test_llm_chat_without_tools_omits_tool_fields(monkeypatch):
    resp = FakeSSE(["data: [DONE]\n\n"])
    calls = []
    monkeypatch.setattr(llm.requests, "post",
                        lambda url, **kw: calls.append(kw) or resp)
    LLMClient("key", "https://x.example/v3", "m").chat([])
    assert "tools" not in calls[0]["json"]
    assert "tool_choice" not in calls[0]["json"]


def test_llm_chat_accumulates_streamed_tool_calls(monkeypatch):
    resp = FakeSSE([
        _chunk("好"),
        _tool_chunk(0, name="update_duties", arguments='{"op": "add", ',
                    call_id="call_a"),
        _tool_chunk(0, arguments='"mode": "do", "target": "WashPlate"}'),
        _tool_chunk(1, name="update_duties",
                    arguments='{"op": "remove", "id": "a1"}', call_id="call_b"),
        "data: [DONE]\n\n",
    ])
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: resp)
    reply = LLMClient("key", "https://x.example/v3", "m").chat([], [{}])

    assert reply.content == "好"
    assert [c.id for c in reply.tool_calls] == ["call_a", "call_b"]
    assert reply.tool_calls[0].args() == {
        "op": "add", "mode": "do", "target": "WashPlate"}
    assert reply.tool_calls[1].args() == {"op": "remove", "id": "a1"}


def test_tool_call_bad_json_args_is_none(monkeypatch):
    resp = FakeSSE([_tool_chunk(0, name="update_duties", arguments="{oops"),
                    "data: [DONE]\n\n"])
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: resp)
    reply = LLMClient("key", "https://x.example/v3", "m").chat([], [{}])
    assert reply.tool_calls[0].args() is None
    # 没有 id 的调用补一个稳定 id，tool 消息才能对上
    assert reply.tool_calls[0].id == "call_0"


def test_llm_query_decodes_sse_bytes_as_utf8(monkeypatch):
    """兼容端点可能不带 charset；必须按 UTF-8 自己解码，不能交给 requests。"""

    class ByteSSE(FakeSSE):
        def iter_lines(self, decode_unicode=False):
            assert decode_unicode is False
            payload = {"choices": [{"delta": {"content": "你先去洗盘子"}}]}
            yield ("data: " + json.dumps(
                payload, ensure_ascii=False) + "\n\n").encode("utf-8")
            yield b"data: [DONE]\n\n"

    resp = ByteSSE([])
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: resp)
    client = LLMClient("key", "https://ark.cn-beijing.volces.com/api/plan/v3",
                       "doubao-seed-2-1-lite-260915")

    assert client.chat([]).content == "你先去洗盘子"


def test_llm_query_raises_on_http_error(monkeypatch):
    resp = FakeSSE([], status_code=500)
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: resp)
    client = LLMClient("key", "https://ark.cn-beijing.volces.com/api/v3",
                       "doubao-seed-1.6-flash")
    with pytest.raises(llm.LLMError) as excinfo:
        client.chat([])
    assert "500" in str(excinfo.value)
    assert resp.closed is True


def test_llm_query_raises_on_stream_error_payload(monkeypatch):
    line = "data: " + json.dumps({"error": {"message": "quota"}}) + "\n\n"
    resp = FakeSSE([line])
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: resp)
    client = LLMClient("key", "https://ark.cn-beijing.volces.com/api/v3",
                       "doubao-seed-1.6-flash")
    with pytest.raises(llm.LLMError) as excinfo:
        client.chat([])
    assert "quota" in str(excinfo.value)


def test_llm_query_wraps_mid_stream_transport_error(monkeypatch):
    class ExplodingSSE(FakeSSE):
        def iter_lines(self, decode_unicode=False):
            yield _chunk("half")
            raise llm.requests.exceptions.ChunkedEncodingError("stream broke")

    resp = ExplodingSSE([])
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: resp)
    client = LLMClient("key", "https://ark.cn-beijing.volces.com/api/v3",
                       "doubao-seed-1.6-flash")
    with pytest.raises(llm.LLMError) as excinfo:
        client.chat([])
    assert "stream broke" in str(excinfo.value)
    assert resp.closed is True


def test_llm_query_normalizes_trailing_slash_in_base_url(monkeypatch):
    resp = FakeSSE(["data: [DONE]\n\n"])
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return resp

    monkeypatch.setattr(llm.requests, "post", fake_post)
    client = LLMClient("key", "https://ark.cn-beijing.volces.com/api/v3/",
                       "doubao-seed-1.6-flash")
    client.chat([])
    assert calls == [
        "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    ]


def test_system_message_carries_rules_and_dag():
    msg = system_message("本关菜：\n  Sushi_Cucumber = Cucumber")
    assert msg["role"] == "system"
    text = msg["content"]
    assert "你是 Overcooked 2 里的 P2" in text
    assert "leave_to_p1" in text and "do" in text
    assert "target" in text and "整条" in text
    assert "Sushi_Cucumber = Cucumber" in text
    # 回复用中文，不念英文节点名
    assert "中文" in text
    # 步骤语义来自目录渲染（task 的 DESC），prompt 里不手写类型清单、不写关卡物料
    assert "DAG 节点类型" not in text
    assert "BoiledSushiRice" not in text.split("本关目录")[0]


def test_render_state_is_readable_and_in_seconds():
    text = render_state({
        "time_left": 99.0, "score": 40,
        "orders": [{"id": "1", "dish": "Sushi_Fish", "remaining": 18.1,
                    "eta": 12.5}],
        "cookers": [{"name": "cooker (2)", "text": "在煮，40%；锅里 SushiRice"}],
        "plates": "空盘 2 个",
        "p1": {"holding": "cucumber"},
        "p2": {"doing": "Chop:SushiFish（订单 #1）", "held": None},
        "p2_options": ["FetchToBoard:SushiFish（订单 #1）"],
        "duties": [{"id": "a1", "mode": "leave_to_p1", "target": "Cucumber",
                    "source": "我来切黄瓜",
                    "impact": "挡住了你 2 个可做的活（Chop:Cucumber），已 12.0 秒；P1 手上：空手"}],
    })
    assert "剩余 99.0 秒" in text
    assert "#1 Sushi_Fish 剩 18.1 秒" in text
    assert "在煮，40%" in text
    assert "P1：手持 cucumber" in text
    assert "Chop:SushiFish" in text
    assert "[a1] 交给 P1 Cucumber  来源「我来切黄瓜」" in text
    assert "此刻：挡住了你 2 个可做的活" in text


def test_speech_message_is_plain_transcript_on_one_line():
    assert speech_message("我来\n切") == {"role": "user", "content": "我来 切"}


def test_state_message_is_system_with_world_state():
    msg = state_message("speech", {})
    assert msg["role"] == "system"
    assert msg["content"].startswith("【世界状态】")
    assert "事件" not in msg["content"]


def test_state_message_new_order_includes_new_dish_dag():
    msg = state_message("new_order", {}, "Pasta\n  └ Chop:Tomato")
    assert "【新菜】" in msg["content"]
    assert "Chop:Tomato" in msg["content"]
    assert "系统：" not in msg["content"]


def test_event_note_is_system():
    assert event_note("new_order")["role"] == "system"


def test_chat_passes_tool_choice_none(monkeypatch):
    resp = FakeSSE(["data: [DONE]\n\n"])
    calls = []
    monkeypatch.setattr(llm.requests, "post",
                        lambda url, **kw: calls.append(kw) or resp)
    LLMClient("key", "https://x.example/v3", "m").chat([], [{}], tool_choice="none")
    assert calls[0]["json"]["tool_choice"] == "none"


def test_assistant_and_tool_messages_round_trip():
    reply = llm.LLMReply("好", [llm.ToolCall("c1", "update_duties", '{"op":"add"}')])
    msg = assistant_message(reply)
    assert msg["tool_calls"][0] == {
        "id": "c1", "type": "function",
        "function": {"name": "update_duties", "arguments": '{"op":"add"}'}}
    tool = tool_message("c1", {"ok": True, "id": "a1"})
    assert tool["role"] == "tool" and tool["tool_call_id"] == "c1"
    assert json.loads(tool["content"]) == {"ok": True, "id": "a1"}

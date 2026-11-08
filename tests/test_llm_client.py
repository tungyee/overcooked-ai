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
    tools = [{"type": "function", "function": {"name": "assign_action"}}]
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
        _tool_chunk(0, name="assign_action", arguments='{"op": "add", ',
                    call_id="call_a"),
        _tool_chunk(0, arguments='"mode": "do", "target": "WashPlate"}'),
        _tool_chunk(1, name="assign_action",
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
    resp = FakeSSE([_tool_chunk(0, name="assign_action", arguments="{oops"),
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
    assert "AI 队友" in text
    assert "execute_actions" in text
    assert "execute_action(" not in text
    assert "cancel_action" in text and "speak" in text
    assert "get_world_state" in text
    assert "check_action" not in text
    assert "chop" in text and "cook" in text and "fetch" in text
    assert "Sushi_Cucumber = Cucumber" in text
    # 回复用中文，不念英文节点名
    assert "中文" in text
    # 模型可见文本里不能出现内部代号，否则会被念出来
    assert "P1" not in text and "P2" not in text


def test_system_prompt_allows_retry_after_state_change():
    """ok=false 后世界状态变了（例如现在有脏盘）要允许重试同一个动作。

    旧规则“不要重复同一个 target”会让模型在 wash 刚失败两次后，即使
    玩家说“切完洗碗”、场上已经有脏盘，也不敢再调 execute_actions。
    """
    text = system_message("本关菜：\n  Sushi_Cucumber = Cucumber")["content"]
    assert "不要重复同一个 target" not in text
    assert "可以重新放进 execute_actions" in text


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
        "duties": [{"id": "a1", "mode": "assign", "action": "下锅",
                    "direction": "P1", "source": "下锅我来",
                    "impact": "挡住了你 2 个可做的活，已 12.0 秒；P1 手上：空手"}],
    })
    assert "剩余 99.0 秒" in text
    # 订单明细 / 灶 / 盘都容易滞后，不再进快照；用 get_world_state 查。
    assert "订单：1 个" in text
    assert "Sushi_Fish" not in text
    assert "灶：" not in text and "砧板：" not in text and "盘子：" not in text
    assert "Chop:SushiFish" in text
    assert "get_world_state" in text


def test_speech_message_is_plain_transcript_on_one_line():
    assert speech_message("我来\n切") == {"role": "user", "content": "我来 切"}


def test_state_message_is_system_with_world_state():
    msg = state_message("speech", {})
    assert msg["role"] == "system"
    assert msg["content"].startswith("【世界状态】")
    assert "事件" not in msg["content"]


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
    reply = llm.LLMReply("好", [llm.ToolCall("c1", "assign_action", '{"op":"add"}')])
    msg = assistant_message(reply)
    assert msg["tool_calls"][0] == {
        "id": "c1", "type": "function",
        "function": {"name": "assign_action", "arguments": '{"op":"add"}'}}
    tool = tool_message("c1", {"ok": True, "id": "a1"})
    assert tool["role"] == "tool" and tool["tool_call_id"] == "c1"
    assert json.loads(tool["content"]) == {"ok": True, "id": "a1"}


def test_render_state_includes_reachable_division():
    """世界状态里要有「你能做的动作」，防止 LLM 抢够不着的活。"""
    text = render_state({
        "reachable_actions": {"p2_actions": ["切"],
                              "p1_actions": ["下锅", "装盘", "送餐"]},
    })
    assert "你能做的动作（单网格估计" in text and "：切" in text
    # 队友能做什么同样是快照，容易被滞后信息误导，不再进 state。
    assert "队友能做的动作" not in text


def test_render_state_prefers_reachable_actions_over_division():
    """够得着的动作不再受分工排除限制：送餐 / 洗盘也要列出来。"""
    text = render_state({
        "p2_reachable_actions": ["切", "装盘", "送餐", "洗盘"],
        "reachable_actions": {"p2_actions": ["装盘"], "p1_actions": ["切"]},
    })
    assert "你能做的动作（单网格估计" in text and "切、装盘、送餐、洗盘" in text


def test_render_state_does_not_fall_back_when_reachable_is_empty():
    """空列表是「一个都够不着」，不能回落到分工建议。"""
    text = render_state({
        "p2_reachable_actions": [],
        "division": {"p2_actions": ["装盘"], "p1_actions": ["切"]},
    })
    assert "你能做的动作" not in text


def test_render_state_omits_division_when_none():
    text = render_state({})
    assert "你能做的动作" not in text


def test_state_renders_success_last_result_without_reason():
    """成功结果没有 reason；模板不能因 StrictUndefined 抛异常。"""
    msg = state_message("speech", {
        "last_result": {"label": "切", "target": "BurritoMeat",
                        "status": "succeeded"},
    })
    assert "上一条命令结果" in msg["content"]


@pytest.mark.parametrize("event", ["last_seconds", "round_summary",
                                   "division", "division_review",
                                   "checkin", "batch"])
def test_proactive_event_prompts_require_speak(event):
    """新协议 content 不播：主动开口的事件必须明确要求调用 speak。"""
    msg = state_message(event, {})
    assert "调用 speak 工具" in msg["content"]
    assert "不调用工具" not in msg["content"]


def test_system_prompt_says_speak_is_a_tool_call():
    """模型会把 speak 当成文本标签；system 里要明确是工具调用。"""
    text = system_message("")["content"]
    assert "这是工具调用，不是文本格式" in text


def test_system_prompt_lists_division_tools_when_available():
    text = system_message("", guide="本关攻略", division=True,
                          cookbook=True)["content"]
    assert "set_division(assignments=" in text
    assert "read_cookbook(section)" in text
    assert "按攻略里「玩家 / 你」的对应关系" in text
    assert "送餐和洗盘不在分工里" in text


def test_system_prompt_omits_division_tools_when_absent():
    text = system_message("")["content"]
    assert "set_division" not in text
    assert "read_cookbook" not in text


def test_stream_total_timeout_raises(monkeypatch):
    """服务端持续发 keepalive/慢帧时，requests 的单次 read timeout 不会
    结束整个流；必须用总时长硬上限。"""
    import time

    class SlowSSE(FakeSSE):
        def iter_lines(self, decode_unicode=False):
            yield _chunk("a")
            time.sleep(0.2)
            yield _chunk("b")

    monkeypatch.setattr(llm.requests, "post",
                        lambda *a, **k: SlowSSE([]))
    client = LLMClient("key", "http://127.0.0.1:9", "model", timeout=0.05)
    with pytest.raises(llm.LLMError, match="流超时"):
        client.chat([{"role": "user", "content": "hi"}])


def test_state_warns_when_game_paused():
    msg = state_message("speech", {"live": False})
    assert "游戏当前已暂停" in msg["content"]


def test_state_does_not_warn_when_live():
    msg = state_message("speech", {"live": True})
    assert "游戏当前已暂停" not in msg["content"]

def test_state_does_not_render_plate_contents():
    """盘子明细容易滞后，不再进快照；需要时用 get_world_state。"""
    msg = state_message("speech", {
        "plates": "空盘 1 个；装了料的盘 1 个；脏盘 0 个",
    })
    assert "空盘" not in msg["content"]
    assert "装了料的盘" not in msg["content"]
    assert "pasta(熟)" not in msg["content"]

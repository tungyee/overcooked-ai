"""DialogWorker：回合流水线、工具批次/ack 与线程生命周期（spec §6.2–6.3）。

全部客户端均为 fake：不打开麦克风/扬声器，也不发网络请求。
"""
import dataclasses
import json
import logging
import queue
import threading
import time

import numpy as np
import pytest

from ai.dialog import worker as worker_mod
from ai.dialog.llm import LLMReply, ToolCall
from ai.dialog.schema import VoiceConfig
from ai.dialog.worker import DialogClients, DialogWorker, DutyBatch


def make_config():
    # type: () -> VoiceConfig
    return VoiceConfig(
        asr_key="test-asr",
        asr_resource_id="test-asr-rid",
        tts_key="test-tts",
        tts_voice="test-voice",
        tts_resource_id="test-tts-rid",
        ark_key="test-ark",
        ark_base_url="http://127.0.0.1:9",
        ark_model="test-model",
    )


# ---------------------------------------------------------------- fakes

def reply(content="", *calls):
    # type: (str, tuple) -> LLMReply
    """构造 LLMReply；calls 是 (工具名, 参数 dict)。"""
    return LLMReply(content=content, tool_calls=[
        ToolCall(id="call_%d" % i, name=name,
                 arguments=json.dumps(args, ensure_ascii=False))
        for i, (name, args) in enumerate(calls)])


def duty(**args):
    return ("update_duties", args)


class FakeLLM(object):
    """记录每次 chat 的 messages / tools / tool_choice；按顺序返回预设回复。

    两段式：有工具调用的回合会请求两次，第二次 tool_choice="none"。
    """

    def __init__(self, *replies, error=None):
        self.replies = list(replies) or [reply("好。")]
        self.error = error
        self.calls = []
        self.choices = []

    @property
    def prompts(self):
        # 每次调用的全部消息文本拼起来，方便 `in` 断言
        return ["\n".join(str(m.get("content")) for m in msgs)
                for msgs, _tools in self.calls]

    def chat(self, messages, tools=None, tool_choice="auto", on_text=None):
        self.calls.append((list(messages), tools))
        self.choices.append(tool_choice)
        if self.error is not None:
            raise self.error
        if len(self.replies) > 1:
            reply = self.replies.pop(0)
        else:
            reply = self.replies[0]
        if on_text is not None and reply.content:
            on_text(reply.content)
        return reply


class FakeTTS(object):
    def __init__(self):
        self.spoken = []
        self.gate = threading.Event()
        self.gate.set()
        self.error = None

    def synthesize(self, text):
        self.spoken.append(text)
        if self.error is not None:
            raise self.error
        gate = self.gate

        def gen():
            gate.wait(5.0)
            yield b"\x00\x00"

        return gen()

    def block(self):
        self.gate.clear()

    def release(self):
        self.gate.set()


class FakePlayer(object):
    def __init__(self):
        self._playing = False
        self._hold = False
        self.played = []
        self.stopped = False
        self.error = None
        self.first_output_at = None

    @property
    def is_playing(self):
        return self._playing

    def reset_output_mark(self):
        self.first_output_at = None

    def play(self, chunks):
        if self.error is not None:
            raise self.error
        self._playing = True
        self.played.extend(list(chunks))
        if self.played:
            self.first_output_at = time.monotonic()
        if not self._hold:
            self._playing = False

    def hold(self):
        self._hold = True

    def release(self):
        self._hold = False
        self._playing = False

    def stop(self):
        self.stopped = True
        self.release()


class FakeVAD(object):
    def __init__(self):
        self.paused = False
        self.events = []
        self.fed = 0

    def feed(self, frame):
        self.fed += 1
        return self.events.pop(0) if self.events else None


class FakePTT(object):
    def __init__(self):
        self.events = []
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def poll(self):
        events = list(self.events)
        self.events = []
        return events


class FakeCapture(object):
    def __init__(self):
        self._q = queue.Queue()
        self.flushed = 0
        self.closed = False

    def frames(self):
        def gen():
            while True:
                item = self._q.get()
                if item is None:
                    return
                yield item

        return gen()

    def push(self, frame):
        self._q.put(frame)

    def flush(self):
        self.flushed += 1

    def close(self):
        self.closed = True
        self._q.put(None)

    stop = close


class FakeASR(object):
    def __init__(self, texts=None):
        self.texts = list(texts or [])
        self.calls = []
        self.started = threading.Event()

    def stream(self, pcm_chunks):
        self.started.set()
        chunks = []
        if isinstance(pcm_chunks, queue.Queue):
            while True:
                item = pcm_chunks.get()
                if item is None:
                    break
                chunks.append(item)
        else:
            chunks = list(pcm_chunks)
        self.calls.append(b"".join(chunks))
        for text in self.texts:
            yield {"text": text, "definite": True}


class FakeBundle(object):
    def __init__(self):
        self.cmd_q = queue.Queue()
        self.ack_q = queue.Queue()
        self.ctx_q = queue.Queue(maxsize=1)
        self.event_q = queue.Queue(maxsize=1)
        self.capture = FakeCapture()
        self.vad = FakeVAD()
        self.asr = FakeASR()
        self.tts = FakeTTS()
        self.player = FakePlayer()


def make_worker(llm=None, asr=None, config=None, ptt=None):
    # type: (object, object, object, object) -> tuple
    bundle = FakeBundle()
    if asr is not None:
        bundle.asr = asr
    clients = DialogClients(
        capture=bundle.capture,
        vad=bundle.vad,
        asr=bundle.asr,
        tts=bundle.tts,
        player=bundle.player,
        llm=llm if llm is not None else FakeLLM(),
        ptt=ptt,
    )
    worker = DialogWorker(
        config if config is not None else make_config(),
        bundle.cmd_q, bundle.ack_q,
        bundle.ctx_q, bundle.event_q, clients=clients,
    )
    return worker, bundle


def run_async(fn, *args):
    thread = threading.Thread(target=fn, args=args, daemon=True)
    thread.start()
    return thread


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ------------------------------------------------------- speech pipeline

TOOLS = [{"type": "function", "function": {"name": "update_duties"}}]


def ack_for(bundle, results):
    batch = bundle.cmd_q.get(timeout=2)
    bundle.ack_q.put({"batch_id": batch.batch_id, "results": results})
    return batch


def test_speech_two_pass_speaks_final_reply_after_tool_results():
    llm = FakeLLM(
        reply("我先答应一下。",
              duty(op="add", mode="leave_to_p1", target="Cucumber")),
        reply("行，切菜你来。"))
    worker, bundle = make_worker(llm=llm)

    thread = run_async(worker._handle_transcript, "我来切",
                       {"tick": 100, "tools": TOOLS})
    batch = bundle.cmd_q.get(timeout=2)

    assert isinstance(batch, DutyBatch)
    assert batch.calls == [("update_duties", {
        "op": "add", "mode": "leave_to_p1", "target": "Cucumber"})]
    assert batch.source == "我来切"

    bundle.ack_q.put({"batch_id": batch.batch_id,
                      "results": [{"ok": True, "id": "a1"}]})
    thread.join(2)

    assert not thread.is_alive()
    # 第一段 text 现在也流式播，第二段紧随其后
    assert bundle.tts.spoken == ["我先答应一下。", "行，切菜你来。"]
    assert llm.choices == ["auto", "none"]
    assert llm.calls[0][1] == TOOLS and llm.calls[1][1] == TOOLS
    # 第二段看得到工具结果
    second = llm.calls[1][0]
    assert second[-1]["role"] == "tool"
    assert json.loads(second[-1]["content"]) == {"ok": True, "id": "a1"}
    assert bundle.capture.flushed == 1
    assert bundle.vad.paused is False


def test_rejection_reaches_second_pass_and_history():
    llm = FakeLLM(reply("", duty(op="add", mode="do", target="WashPlate")),
                  reply("这关没洗碗池，我洗不了。"))
    worker, bundle = make_worker(llm=llm)

    thread = run_async(worker._handle_transcript, "你去洗盘", {"tick": 100})
    ack_for(bundle, [{"ok": False, "reason": "本关没有洗碗池"}])
    thread.join(2)

    assert "本关没有洗碗池" in llm.calls[1][0][-1]["content"]
    roles = [m["role"] for m in worker.state.history]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert worker.state.history[0]["content"] == "你去洗盘"
    assert worker.state.history[1]["content"] == ""
    assert worker.state.history[1]["tool_calls"][0]["id"] == "call_0"
    assert worker.state.history[3] == {"role": "assistant",
                                       "content": "这关没洗碗池，我洗不了。"}
    assert bundle.tts.spoken == ["这关没洗碗池，我洗不了。"]


def test_world_state_is_system_message_not_in_history():
    llm = FakeLLM(reply("好呀。"), reply("我在切黄瓜。"))
    worker, _ = make_worker(llm=llm)

    worker._handle_transcript("你好", {"tick": 100})
    worker._handle_transcript("你在干嘛", {
        "tick": 200, "p2": {"doing": "Chop:Cucumber"}})

    messages = llm.calls[1][0]
    assert [m["role"] for m in messages] == [
        "system", "user", "assistant", "system", "user"]
    assert messages[1]["content"] == "你好"
    assert messages[2]["content"] == "好呀。"
    # 世界状态是本轮的 system 消息，紧挨在 P1 原话前
    assert "【世界状态】" in messages[3]["content"]
    assert "Chop:Cucumber" in messages[3]["content"]
    assert messages[4]["content"] == "你在干嘛"
    assert all("【世界状态】" not in str(m.get("content"))
               for m in worker.state.history)


def test_ack_timeout_reported_to_second_pass(monkeypatch):
    monkeypatch.setattr(worker_mod, "ACK_TIMEOUT_S", 0.05)
    llm = FakeLLM(reply("", duty(op="add", mode="do", target="Cucumber")),
                  reply("刚才没记上，再说一次？"))
    worker, bundle = make_worker(llm=llm)

    thread = run_async(worker._handle_transcript, "你去切", {"tick": 100})
    bundle.cmd_q.get(timeout=2)
    thread.join(2)

    assert not thread.is_alive()
    assert "超时" in llm.calls[1][0][-1]["content"]
    assert bundle.tts.spoken == ["刚才没记上，再说一次？"]


def test_second_pass_failure_falls_back_to_result_template():
    class FlakyLLM(FakeLLM):
        def chat(self, messages, tools=None, tool_choice="auto", on_text=None):
            if tool_choice == "none":
                raise RuntimeError("timeout")
            return FakeLLM.chat(self, messages, tools, tool_choice, on_text)

    llm = FlakyLLM(reply("", duty(op="add", mode="do", target="Cucumber")))
    worker, bundle = make_worker(llm=llm)

    thread = run_async(worker._handle_transcript, "你去切", {"tick": 100})
    ack_for(bundle, [{"ok": True, "id": "a1"}])
    thread.join(2)

    assert bundle.tts.spoken == ["好，记下了。"]


def test_chat_without_tools_is_single_request():
    llm = FakeLLM(reply("你好呀。"))
    worker, bundle = make_worker(llm=llm)

    worker._handle_transcript("你好", {"tick": 100, "tools": TOOLS})

    assert bundle.cmd_q.empty()
    assert len(llm.calls) == 1
    assert bundle.tts.spoken == ["你好呀。"]
    assert bundle.capture.flushed == 1


def test_empty_reply_stays_silent():
    worker, bundle = make_worker(llm=FakeLLM(reply("")))

    worker._handle_transcript("嗯嗯", {"tick": 100})

    assert bundle.tts.spoken == []
    # 没回应的话也进历史，下一轮能看到前文
    assert worker.state.history[0]["content"] == "嗯嗯"


def test_history_is_capped_and_never_starts_with_tool(monkeypatch):
    monkeypatch.setattr(worker_mod, "HISTORY_MAX", 5)
    llm = FakeLLM(reply("", duty(op="add", mode="do", target="Cucumber")))
    worker, bundle = make_worker(llm=llm)

    for index in range(3):
        thread = run_async(worker._handle_transcript, "第%d句" % index,
                           {"tick": 100 * (index + 1)})
        ack_for(bundle, [{"ok": True}])
        thread.join(2)

    assert len(worker.state.history) <= 5
    assert worker.state.history[0]["role"] not in ("tool", "assistant")


def test_empty_transcript_never_calls_llm():
    worker, bundle = make_worker(llm=FakeLLM(reply("好。")))

    worker._handle_transcript("   ", {"tick": 100})
    worker._handle_transcript("", {"tick": 100})

    assert worker.clients.llm.prompts == []
    assert bundle.tts.spoken == ["没听清，再说一遍", "没听清，再说一遍"]


def test_identical_speech_within_4s_is_deduped():
    worker, _ = make_worker(llm=FakeLLM(reply("好。")))

    worker._handle_transcript("我来切", {"tick": 100})
    worker._handle_transcript(" 我来切。", {"tick": 110})

    assert len(worker.clients.llm.prompts) == 1


def test_llm_error_speaks_missed_speech_fallback():
    worker, bundle = make_worker(llm=FakeLLM(error=RuntimeError("boom")))

    worker._handle_transcript("我来切", {"tick": 100})

    assert bundle.tts.spoken == ["没听清，再说一遍"]
    assert worker.state.history == []


def test_empty_asr_speaks_missed_speech_fallback():
    worker, bundle = make_worker()

    class _EmptyASRSession(object):
        def poll(self):
            return [("done", None)]

    utterance = worker_mod._Utterance(
        session=_EmptyASRSession(),
        timing=worker_mod.TurnTiming(source="vad", event="speech",
                                     started=time.monotonic()),
        texts=[],
    )

    assert worker._poll_utterance(utterance) is True
    assert bundle.tts.spoken == ["没听清，再说一遍"]


# -------------------------------------------------------------- speaking

def test_speak_pauses_vad_flushes_then_resumes():
    worker, bundle = make_worker(llm=FakeLLM(reply("行。")))
    bundle.player.hold()

    thread = run_async(worker._speak, "行。")
    assert wait_until(lambda: bundle.vad.paused and bundle.tts.spoken)
    assert bundle.vad.paused is True

    bundle.player.release()
    thread.join(2)

    assert not thread.is_alive()
    assert bundle.vad.paused is False
    assert bundle.capture.flushed == 1


def test_speak_empty_text_does_nothing():
    worker, bundle = make_worker()

    worker._speak("")

    assert bundle.vad.paused is False
    assert bundle.capture.flushed == 0
    assert bundle.tts.spoken == []


def test_speak_restores_vad_when_tts_raises():
    worker, bundle = make_worker()
    bundle.tts.error = RuntimeError("tts down")

    worker._speak("行。")

    assert bundle.vad.paused is False
    assert bundle.capture.flushed == 1


def test_speak_restores_vad_when_player_raises():
    worker, bundle = make_worker()
    bundle.player.error = RuntimeError("output down")

    worker._speak("行。")

    assert bundle.vad.paused is False
    assert bundle.capture.flushed == 1


def test_speak_is_noop_after_stop():
    worker, bundle = make_worker()
    worker.stop()

    worker._speak("行。")

    assert bundle.vad.paused is False
    assert bundle.tts.spoken == []


# --------------------------------------------------------------- events

def test_new_order_event_waits_for_batch_window():
    llm = FakeLLM(reply("新菜黄瓜寿司，我煮饭你切黄瓜？"))
    worker, bundle = make_worker(llm=llm)
    context = {"tick": 1000, "orders": [{"id": 1, "dish": "Sushi_Cucumber"}],
               "dish_dags": {"Sushi_Cucumber": "Sushi_Cucumber\n  └ Chop:Cucumber"},
               "tools": TOOLS}

    worker._handle_event(("new_order", [1]), context)
    assert llm.calls == []

    worker._handle_event(("new_order", []), dict(context, tick=1020))
    assert len(llm.calls) == 1
    messages, tools = llm.calls[0]
    # 新菜的 DAG 进本轮 system 消息；new_order 可直接调 update_duties
    state = messages[-2]
    assert state["role"] == "system"
    assert "Chop:Cucumber" in state["content"] and "新菜" in state["content"]
    # 事件回合没有 user 消息，只有一条事件占位
    assert messages[-1]["role"] == "system" and "new_order" in messages[-1]["content"]
    assert all(m["role"] != "user" for m in messages)
    # new_order 与 speech 一样 tool_choice=auto：可以直接落账本
    assert tools == TOOLS and llm.choices == ["auto"]
    assert bundle.tts.spoken == ["新菜黄瓜寿司，我煮饭你切黄瓜？"]


def test_new_order_can_write_duties_directly():
    llm = FakeLLM(
        reply("", duty(op="add", mode="do", target="BoiledSushiRice")),
        reply("新菜鱼寿司，米饭我煮，海苔和鱼你来切。"))
    worker, bundle = make_worker(llm=llm)
    context = {"tick": 1000, "orders": [{"id": 1, "dish": "Sushi_Fish"}],
               "dish_dags": {"Sushi_Fish": "Sushi_Fish\n  └ Chop:SushiFish"},
               "tools": TOOLS}

    worker._handle_event(("new_order", [1]), context)
    thread = run_async(worker._handle_event, ("new_order", []),
                       dict(context, tick=1020))

    batch = bundle.cmd_q.get(timeout=2)
    assert isinstance(batch, DutyBatch)
    assert batch.calls == [("update_duties", {
        "op": "add", "mode": "do", "target": "BoiledSushiRice"})]
    assert batch.origin == "new_order"
    assert bundle.tts.spoken == []

    bundle.ack_q.put({"batch_id": batch.batch_id,
                      "results": [{"ok": True, "id": "a1"}]})
    thread.join(2)

    assert not thread.is_alive()
    assert llm.choices == ["auto", "none"]
    assert bundle.tts.spoken == ["新菜鱼寿司，米饭我煮，海苔和鱼你来切。"]


def test_help_event_is_gone():
    worker, _ = make_worker(llm=FakeLLM(reply("好。")))

    worker._handle_event(("help", None), {"tick": 1000})

    assert worker.clients.llm.calls == []


def test_unknown_event_is_ignored():
    worker, _ = make_worker(llm=FakeLLM(reply("好。")))

    worker._handle_event(("bogus", None), {"tick": 1000})

    assert worker.clients.llm.calls == []


# ------------------------------------------------------- thread lifecycle

def test_start_stop_with_blocking_capture():
    worker, bundle = make_worker(llm=FakeLLM(reply("好。")))

    worker.start()
    assert worker._thread is not None
    assert worker._thread.daemon is True
    assert worker._thread.is_alive()

    worker.stop()

    assert not worker._thread.is_alive()
    assert bundle.capture.closed is True
    assert bundle.player.stopped is True


def test_stop_without_start_is_safe():
    worker, _ = make_worker()

    worker.stop()


def test_start_after_stop_raises():
    worker, _ = make_worker()
    worker.start()
    worker.stop()

    with pytest.raises(RuntimeError):
        worker.start()


def test_reset_state_clears_dialog_state():
    worker, _ = make_worker()
    worker.state.last_speech_tick = 9
    worker.state.pending_orders = [1, 2]
    worker.last_proactive_tick = 50

    worker.reset_state()

    assert worker.state.last_speech_tick == 0
    assert worker.state.pending_orders == []
    assert worker.last_proactive_tick == 0


def test_reset_state_clears_checkin_schedule():
    worker, _ = make_worker()
    worker._next_checkin_at = time.monotonic() + 100.0

    worker.reset_state()

    assert worker._next_checkin_at is None


def test_run_llm_schedules_next_checkin_after_normal_turn():
    worker, _ = make_worker(llm=FakeLLM(reply("你好呀。")))
    before = time.monotonic()

    worker._handle_transcript("你好", {"tick": 100, "tools": TOOLS})

    assert worker._next_checkin_at is not None
    assert worker._next_checkin_at - before >= worker_mod.CHECKIN_MIN_S


def test_checkin_llm_error_does_not_leave_schedule_in_past():
    worker, _ = make_worker(llm=FakeLLM(error=RuntimeError("llm down")))
    worker.publish_context({"level": "Sushi_1_4_2P",
                            "orders": [{"id": 1, "dish": "X"}],
                            "tick": 100,
                            "live": True})
    worker._next_checkin_at = time.monotonic() - 1.0

    worker._maybe_checkin(None, False, False)

    assert worker._next_checkin_at is None


def test_checkin_clears_schedule_when_not_live():
    worker, _ = make_worker(llm=FakeLLM())
    worker.publish_context({"level": "Sushi_1_4_2P",
                            "orders": [{"id": 1, "dish": "X"}],
                            "tick": 100,
                            "live": False})
    worker._next_checkin_at = time.monotonic() - 1.0

    worker._maybe_checkin(None, False, False)

    assert worker._next_checkin_at is None


def test_publish_context_keeps_only_latest():
    worker, bundle = make_worker()

    worker.publish_context({"tick": 1})
    worker.publish_context({"tick": 2})

    assert worker._latest_context()["tick"] == 2
    assert bundle.ctx_q.get_nowait()["tick"] == 2
    assert bundle.ctx_q.empty()


def test_put_event_drops_oldest_when_full():
    worker, bundle = make_worker()

    worker.put_event(("rescue", 2))
    worker.put_event(("rescue", 3))

    assert bundle.event_q.get_nowait() == ("rescue", 3)
    assert bundle.event_q.empty()


def test_thread_drains_event_queue():
    worker, bundle = make_worker(llm=FakeLLM(reply("我来帮你。")))
    worker.publish_context({"tick": 1000})
    worker.start()

    worker.put_event(("new_order", [1]))
    bundle.capture.push(np.zeros(160, dtype=np.float32))
    assert wait_until(lambda: worker.state.pending_orders == [1])

    worker.publish_context({"tick": 1021})
    bundle.capture.push(np.zeros(160, dtype=np.float32))

    assert wait_until(lambda: bundle.tts.spoken == ["我来帮你。"])
    worker.stop()


def test_run_flushes_single_new_order_batch_after_window():
    llm = FakeLLM(reply("新单来了。"))
    worker, bundle = make_worker(llm=llm)
    worker.publish_context({"tick": 100})
    worker.start()

    # 单张新单只投一次 [id]：批窗内不触发
    worker.put_event(("new_order", [1]))
    bundle.capture.push(np.zeros(160, dtype=np.float32))
    assert wait_until(lambda: worker.state.pending_orders == [1])
    assert llm.prompts == []

    # 窗口过后由 worker 的周期 flush 以空 id 推进；批次 [1] 触发一次
    worker.publish_context({"tick": 121})
    bundle.capture.push(np.zeros(160, dtype=np.float32))

    assert wait_until(lambda: len(llm.prompts) == 1)
    assert worker.state.pending_orders == []
    assert "新菜" in llm.prompts[0]
    assert bundle.tts.spoken == ["新单来了。"]
    worker.stop()


def test_thread_transcribes_utterance_as_int16_pcm():
    asr = FakeASR(["我来切"])
    worker, bundle = make_worker(
        llm=FakeLLM(reply("行，你切。")), asr=asr)
    bundle.vad.events = ["start", "end"]
    worker.publish_context({"tick": 1000})
    worker.start()

    loud = np.full(160, 0.5, dtype=np.float32)
    bundle.capture.push(loud)
    assert wait_until(lambda: bundle.vad.fed >= 1)
    bundle.capture.push(loud)

    assert wait_until(lambda: bundle.tts.spoken == ["行，你切。"])
    assert len(asr.calls) == 1
    # 两帧 × 160 个 float32 样本 → 320 个 int16 样本 = 640 字节
    assert len(asr.calls[0]) == 640
    assert np.frombuffer(asr.calls[0], dtype="<i2")[0] == 16383
    worker.stop()


def test_ptt_records_while_held_and_sends_on_release():
    asr = FakeASR(["你来煮饭"])
    ptt = FakePTT()
    config = dataclasses.replace(make_config(), ptt_key="f8")
    worker, bundle = make_worker(
        llm=FakeLLM(reply("好。")), asr=asr,
        config=config, ptt=ptt)
    worker.publish_context({"tick": 1000})
    worker.start()
    assert wait_until(lambda: ptt.started)

    loud = np.full(160, 0.5, dtype=np.float32)
    # 先喂两帧 pre-roll，确保按键按下前的音频也在缓存里。
    bundle.capture.push(loud)
    bundle.capture.push(loud)
    assert wait_until(lambda: bundle.capture._q.qsize() == 0)

    ptt.events = ["down"]
    for _ in range(5):
        bundle.capture.push(loud)
    assert wait_until(lambda: bundle.capture._q.qsize() == 0)

    ptt.events = ["up"]
    for _ in range(5):
        bundle.capture.push(loud)

    assert wait_until(lambda: len(asr.calls) == 1)
    # 5 帧按住 + 4 帧尾音 + pre-roll，PCM 必须明显长于单帧。
    assert len(asr.calls[0]) > 160 * 2 * 4
    # PTT 模式不再喂 VAD。
    assert bundle.vad.fed == 0
    worker.stop()
    assert ptt.stopped


def test_ptt_starts_asr_while_key_still_held():
    asr = FakeASR(["你来煮饭"])
    ptt = FakePTT()
    config = dataclasses.replace(make_config(), ptt_key="f8")
    worker, bundle = make_worker(
        llm=FakeLLM(reply("好。")), asr=asr,
        config=config, ptt=ptt)
    worker.publish_context({"tick": 1000})
    worker.start()
    assert wait_until(lambda: ptt.started)

    loud = np.full(160, 0.5, dtype=np.float32)
    bundle.capture.push(loud)
    bundle.capture.push(loud)
    assert wait_until(lambda: bundle.capture._q.qsize() == 0)

    # 按下后就应建立 ASR 会话，而不是等松开。
    ptt.events = ["down"]
    for _ in range(5):
        bundle.capture.push(loud)
    assert wait_until(lambda: asr.started.is_set())
    assert asr.calls == []  # 还没结束，音频仍在流式送入

    ptt.events = ["up"]
    for _ in range(5):
        bundle.capture.push(loud)

    assert wait_until(lambda: bundle.tts.spoken == ["好。"])
    assert len(asr.calls) == 1
    worker.stop()


def test_ptt_coalesces_asr_segments_into_one_turn():
    asr = FakeASR(["你。", "现在能。", "看到什么？"])
    ptt = FakePTT()
    llm = FakeLLM(reply("看得见。"))
    config = dataclasses.replace(make_config(), ptt_key="f8")
    worker, bundle = make_worker(
        llm=llm, asr=asr, config=config, ptt=ptt)
    worker.publish_context({"tick": 1000})
    worker.start()
    assert wait_until(lambda: ptt.started)

    loud = np.full(160, 0.5, dtype=np.float32)
    ptt.events = ["down"]
    for _ in range(5):
        bundle.capture.push(loud)
    assert wait_until(lambda: bundle.capture._q.qsize() == 0)
    ptt.events = ["up"]
    for _ in range(5):
        bundle.capture.push(loud)

    assert wait_until(lambda: bundle.tts.spoken == ["看得见。"])
    # 一次 PTT 录音只触发一轮 LLM，片段按顺序合并。
    assert len(llm.prompts) == 1
    assert "你。现在能。看到什么？" in llm.prompts[0]
    worker.stop()


def test_merge_asr_texts_dedupes_and_joins_overlap():
    assert worker_mod._merge_asr_texts(
        ["你。", "你。", "现在能。", "看到什么？"]) == "你。现在能。看到什么？"
    assert worker_mod._merge_asr_texts(["你好", "你好我", "我洗"]) == "你好我洗"
    assert worker_mod._merge_asr_texts([]) == ""


def test_default_clients_construct_without_opening_devices():
    from ai.dialog.asr import ASRClient
    from ai.dialog.audio import AudioCapture, AudioPlayer, VAD
    from ai.dialog.llm import LLMClient
    from ai.dialog.tts import TTSClient

    worker = DialogWorker(
        make_config(), queue.Queue(), queue.Queue(),
        queue.Queue(maxsize=1), queue.Queue(maxsize=1),
    )

    assert isinstance(worker.clients.capture, AudioCapture)
    assert isinstance(worker.clients.vad, VAD)
    assert isinstance(worker.clients.asr, ASRClient)
    assert isinstance(worker.clients.tts, TTSClient)
    assert isinstance(worker.clients.player, AudioPlayer)
    assert isinstance(worker.clients.llm, LLMClient)


def test_default_clients_wires_voice_rate_into_tts():
    config = make_config()
    config.rate = 1.3

    worker = DialogWorker(
        config, queue.Queue(), queue.Queue(),
        queue.Queue(maxsize=1), queue.Queue(maxsize=1),
    )

    assert worker.clients.tts.rate == pytest.approx(1.3)


# ---------------------------------------------------------------- logging

def test_log_path_writes_jsonl_for_transcript_and_turn(tmp_path):
    log_path = tmp_path / "nested" / "voice.jsonl"
    config = make_config()
    config.log_path = str(log_path)
    worker, _bundle = make_worker(llm=FakeLLM(reply("行，你切。")), config=config)

    worker.start()
    try:
        worker._handle_transcript("我来切", {"tick": 100})
    finally:
        worker.stop()

    raw = log_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines()]
    kinds = [record["kind"] for record in records]

    assert "start" in kinds
    assert "asr" in kinds
    assert "llm_reply" in kinds
    assert "tts_first_chunk" in kinds
    assert "turn_latency" in kinds
    assert "stop" in kinds
    assert any(record.get("text") == "我来切" for record in records)
    assert all("ts" in record for record in records)
    # 密钥不落日志
    assert "test-asr" not in raw
    assert "test-ark" not in raw


def test_debug_sets_ai_dialog_logger_debug_and_adds_handler_once():
    logger = logging.getLogger("ai.dialog")
    original_level = logger.level
    original_handlers = list(logger.handlers)
    workers = []
    try:
        config = make_config()
        config.debug = True
        first, _ = make_worker(config=config)
        workers.append(first)
        first.start()
        assert logger.level == logging.DEBUG
        marked = [
            handler for handler in logger.handlers
            if getattr(handler, "_oc2_voice_debug", False)
        ]
        assert len(marked) == 1

        second, _ = make_worker(config=config)
        workers.append(second)
        second.start()
        marked = [
            handler for handler in logger.handlers
            if getattr(handler, "_oc2_voice_debug", False)
        ]
        assert len(marked) == 1
    finally:
        for worker in workers:
            worker.stop()
        logger.setLevel(original_level)
        for handler in list(logger.handlers):
            if (getattr(handler, "_oc2_voice_debug", False)
                    and handler not in original_handlers):
                logger.removeHandler(handler)


def test_debug_logging_emits_pipeline_content(caplog):
    caplog.set_level(logging.DEBUG, logger="ai.dialog")
    llm = FakeLLM(reply("", duty(op="add", mode="leave_to_p1", target="Cucumber")),
                  reply("行，你切。"))
    worker, bundle = make_worker(llm=llm)

    thread = run_async(worker._handle_transcript, "我来切", {"tick": 100})
    ack_for(bundle, [{"ok": True, "id": "a1"}])
    thread.join(2)

    text = caplog.text
    assert "ASR" in text and "我来切" in text
    assert "LLM 回复" in text and "update_duties" in text
    assert "播报" in text and "行，你切。" in text



class TimedLLM(FakeLLM):
    """在 FakeLLM 上补一个首 token 耗时，模拟流式端点。"""

    def chat(self, messages, tools=None, tool_choice="auto", on_text=None):
        result = FakeLLM.chat(self, messages, tools, tool_choice, on_text)
        return dataclasses.replace(result, first_token_ms=12.5)


def _log_records(path):
    # type: (object) -> list
    return [json.loads(line) for line in path.read_text(
        encoding="utf-8").splitlines()]


def test_turn_latency_logs_asr_llm_tts_and_audio(tmp_path):
    log_path = tmp_path / "voice-latency.jsonl"
    config = make_config()
    config.log_path = str(log_path)
    worker, _bundle = make_worker(
        llm=TimedLLM(reply("行，你切。")),
        asr=FakeASR(["我来切"]), config=config)

    worker.start()
    try:
        # speech_end 故意提前 50 ms，验证 end_to_* 确实从“句尾”起算。
        worker._process_utterance(
            [np.zeros(160, dtype=np.float32)], source="vad",
            speech_end=time.monotonic() - 0.05, audio_ms=50)
    finally:
        worker.stop()

    records = _log_records(log_path)
    asr = next(r for r in records if r["kind"] == "asr")
    latency = next(r for r in records if r["kind"] == "turn_latency")

    assert asr["source"] == "vad"
    assert asr["audio_ms"] == 50
    assert asr["asr_ms"] is not None
    assert asr["end_to_asr_ms"] >= 45

    assert latency["event"] == "speech"
    assert latency["source"] == "vad"
    assert latency["audio_ms"] == 50
    assert latency["llm_first_token_ms"] is not None
    assert latency["llm_done_ms"] is not None
    assert latency["tts_first_chunk_ms"] is not None
    assert latency["first_audio_ms"] is not None
    assert latency["playback_done_ms"] >= latency["first_audio_ms"]
    assert latency["outcome"] == "ok"


def test_new_order_latency_includes_level_and_merge_window(tmp_path):
    log_path = tmp_path / "voice-new-order.jsonl"
    config = make_config()
    config.log_path = str(log_path)
    worker, _bundle = make_worker(
        llm=TimedLLM(reply("新单来了，我煮饭。")), config=config)
    context = {"tick": 1000, "orders": [{"id": 1, "dish": "Sushi_Cucumber"}],
               "tools": TOOLS}

    worker.start()
    try:
        worker.log_event("level_start", level="Sushi_1_4_2P")
        time.sleep(0.01)
        worker._handle_event(("new_order", [1]), context)
        worker._handle_event(("new_order", []), dict(context, tick=1020))
    finally:
        worker.stop()

    latency = next(r for r in _log_records(log_path)
                   if r["kind"] == "turn_latency")
    assert latency["event"] == "new_order"
    assert latency["source"] == "event"
    assert latency["since_level_ms"] >= 10
    # 事件回合没有 ASR/音频时长，但 LLM/TTS/出声都要有。
    assert latency["asr_ms"] is None
    assert latency["llm_first_token_ms"] is not None
    assert latency["tts_first_chunk_ms"] is not None
    assert latency["first_audio_ms"] is not None


def test_tool_ack_duration_is_logged(tmp_path):
    log_path = tmp_path / "voice-tools.jsonl"
    config = make_config()
    config.log_path = str(log_path)
    llm = TimedLLM(
        reply("", duty(op="add", mode="do", target="Cucumber")),
        reply("行，我来切。"))
    worker, bundle = make_worker(llm=llm, config=config)

    worker.start()
    try:
        thread = run_async(worker._handle_transcript, "你去切",
                           {"tick": 100, "tools": TOOLS})
        ack_for(bundle, [{"ok": True, "id": "a1"}])
        thread.join(2)
    finally:
        worker.stop()

    records = _log_records(log_path)
    ack = next(r for r in records if r["kind"] == "duty_ack")
    latency = next(r for r in records if r["kind"] == "turn_latency")
    assert ack["ack_ms"] is not None and ack["ack_ms"] >= 0
    assert latency["tool_ack_ms"] is not None
    # 两段式：第一段首 token + 工具落地 + 第二段，最终话必须已就绪。
    assert latency["llm_first_token_ms"] is not None
    assert latency["llm_done_ms"] is not None
    assert latency["outcome"] == "ok"


def test_speak_flushes_only_the_playback_window():
    class MarkedCapture(FakeCapture):
        def __init__(self):
            FakeCapture.__init__(self)
            self.marks = []
            self.flushed_since = []

        def mark(self):
            self.marks.append(7)
            return 7

        def flush_since(self, mark):
            self.flushed_since.append(mark)

    worker, _bundle = make_worker()
    capture = MarkedCapture()
    worker.clients.capture = capture

    worker._speak("行。")

    assert capture.marks == [7]
    assert capture.flushed_since == [7]
    assert capture.flushed == 0

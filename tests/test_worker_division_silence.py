# -*- coding: utf8 -*-
"""division_review 回合硬静音：不改分工就不出声。"""
from ai.dialog.duties import tool_schemas
from tests.test_dialog_worker import FakeLLM, make_worker, reply, run_async


def _run_review(changed):
    llm = FakeLLM(
        reply("", ("set_division", {"assignments": [
            {"action": "cook", "owner": "you"}]}),
            ("speak", {"text": "我改煎了"})),
        reply(""),
    )
    worker, bundle = make_worker(llm=llm)
    context = {
        "tick": 100,
        "dag": "本关菜：Burger",
        "guide": "",
        "tools": tool_schemas(["cook", "plate"], with_cookbook=False),
    }

    def ack():
        batch = bundle.cmd_q.get(timeout=2)
        bundle.ack_q.put({
            "batch_id": batch.batch_id,
            "results": [{
                "ok": True,
                "division_changed": changed,
                "changed": [{"action": "cook", "from": "player", "to": "you"}]
                if changed else [],
            }],
        })

    thread = run_async(ack)
    worker._run_llm(context, "division_review", "")
    thread.join(timeout=2)
    return worker, bundle


def test_review_speaks_when_division_changed():
    _worker, bundle = _run_review(True)
    assert bundle.tts.spoken
    assert "我改煎了" in "".join(bundle.tts.spoken)


def test_review_is_silent_when_division_unchanged():
    _worker, bundle = _run_review(False)
    assert bundle.tts.spoken == []
    assert bundle.player.played == []
